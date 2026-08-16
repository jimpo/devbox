"""Command-line entry point and subcommand handlers. Everything here is
provider-agnostic: commands resolve config + target, then act through the
Provider seam.

    devbox create              # download image, define + first-boot the VM
    devbox up / stop           # start / gracefully shut down a defined VM
    devbox ip                  # print the VM's IPv4
    devbox ssh [args...]       # ssh into the VM as the unprivileged agent user
    devbox ssh --admin         # ssh in as the privileged user (the only sudo shell)
    devbox status              # cloud-init progress + tail provision.log
    devbox list                # list devbox-managed instances on this host
    devbox jj fetch            # jj git fetch the instance's work into the host repo
    devbox sync --envvars      # inject [env] creds into the box's shell env
    devbox sync --mods[=a,b]   # (re)run provisioning mods over SSH (reconverge)
    devbox sync --files        # rsync host-local files ([[sync]]) onto the box
    devbox host                # refresh the SSH alias used for jj fetch
    devbox destroy             # tear down, removing the overlay disk

An instance is no longer the same thing as a project: a project is a label,
and `--name` targets a specific instance (default: the project's libvirt one).
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .initscript import render_init_script
from .config import (DEFAULT_PROVIDER, GlobalConfig, KNOWN_HOSTS_PATH, PROVIDERS,
                     ProjectConfig, SSH_DIR, SSH_USER_CONFIG, compose_instance_name,
                     find_project_dir, parse_instance_name, resolve_instance,
                     resolve_project)
from .provision import PROVISION_LOG, build_payload, build_tarball, resolve_env
from .sync import run_envvars, run_sync
from .providers import (AwsAuthError, InstanceSpec, all_providers,
                        auth_error_message, get_provider, is_auth_error)
from .util import die


def _format_age(created_at: Optional[str]) -> str:
    """Coarse age since create (m/h/d). Note: this is time-since-`devbox create`,
    a stand-in for uptime - close for the common create-once-keep-running box,
    but it doesn't reset across stop/start (precise guest uptime needs the guest
    agent, out of scope). Shown only for running instances."""
    if not created_at:
        return "?"
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return "?"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _print_table(rows: list) -> None:
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def truncate_known_hosts() -> None:
    """A freshly created instance has a brand-new SSH host key; stale entries
    from a previous instance of the same name can only cause false mismatches."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS_PATH.write_text("")
    KNOWN_HOSTS_PATH.chmod(0o600)


def _resolve_target(args: argparse.Namespace, project: ProjectConfig, gc: GlobalConfig):
    """The (canonical name, provider) a per-instance command acts on. The
    provider is derived from the name (parse_instance_name), not project.provider
    (the bare-create default) - so `devbox ip --name ec2-binius-arm` queries EC2
    even from a libvirt-default project, routed offline from the name alone."""
    name = resolve_instance(args, project)
    provider_name, _ = parse_instance_name(name)
    return name, provider_name, get_provider(provider_name, gc)


def _check_name_unique(gc: GlobalConfig, name: str) -> None:
    """Instance names are globally unique across providers - enforced at create
    by checking the union `list`. A provider that can't be listed (e.g. EC2 creds
    missing) is warned about, not fatal, so a local create isn't blocked by it."""
    for provider in all_providers(gc):
        try:
            existing = provider.list()
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not check {provider.name} for name collisions: {e}")
            continue
        if any(i.name == name for i in existing):
            die(f"an instance named '{name}' already exists ({provider.name}); "
                f"pick another name or destroy it first")


# ---- subcommands --------------------------------------------------------------
def cmd_create(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    provider_name = args.provider or project.provider
    provider = get_provider(provider_name, gc)

    if provider_name == "libvirt":
        # libvirt's one instance per project is the bare-named default (= project);
        # arbitrary --name is reserved for cloud instances.
        if args.name:
            die("libvirt instances are the project's bare default (name = the project); "
                "--name is only for cloud instances")
        bare = project.project
        # A project literally named e.g. 'ec2-foo' would make the bare libvirt
        # name misparse as another provider. Reject it so the name stays routable.
        misparsed = parse_instance_name(bare)[0]
        if misparsed != "libvirt":
            die(f"project name '{bare}' collides with the '<provider>-' instance "
                f"naming scheme (it parses as provider '{misparsed}'); rename the "
                f"project via 'project = ...' in .devbox/config.toml")
    else:
        # Cloud instances always require an explicit --name.
        if not args.name:
            die(f"cloud instances always require --name "
                f"(e.g. `devbox create --provider {provider_name} --name foo-gpu`)")
        bare = args.name
        # The provider prefix is composed in automatically; a --name that already
        # carries one would double it (e.g. ec2-ec2-foo).
        if parse_instance_name(bare)[0] != "libvirt":
            die(f"--name '{bare}' must not start with a provider prefix "
                f"('{provider_name}-' is added automatically); "
                f"pass --name {parse_instance_name(bare)[1]}")

    # The canonical name (provider-prefixed for cloud, bare for libvirt) is the
    # instance's identity: stored on the instance and used by every later command.
    name = compose_instance_name(provider_name, bare)
    _check_name_unique(gc, name)

    # --spot/--on-demand are EC2-only knobs; Lightsail has no spot market.
    ec2_cfg = project.ec2
    if ec2_cfg is not None and (args.spot or args.on_demand):
        ec2_cfg = dataclasses.replace(ec2_cfg, spot=bool(args.spot))

    # Build the provisioning payload up front so a bad jj identity / missing mod
    # surfaces before we boot a box (build_payload resolves both).
    tarball = build_tarball(build_payload(project, gc))

    spec = InstanceSpec(
        name=name,
        admin_user=project.user,
        provider=provider_name,
        disk_gb=project.disk_gb,
        user_data=render_init_script(project, gc, instance_name=name),
        created_at=datetime.now(timezone.utc).isoformat(),
        vcpus=project.vcpus,
        memory_mib=project.memory_mib,
        ec2=ec2_cfg,
        lightsail=project.lightsail,
    )
    provider.create(spec)

    truncate_known_hosts()

    d = args.project_dir or "."
    target = f"devbox -C {d}" + (f" --name {name}" if provider_name != "libvirt" else "")

    # Provisioning is driven over SSH (single path shared with `devbox sync`):
    # wait for the box's base cloud-init (privileged user + packages) to be
    # ready, then run the same steps `devbox sync` does, over the PRIVILEGED
    # alias (mods need sudo; the agent account doesn't exist yet).
    #
    # Order: users -> env -> mods -> files. The agent account has to exist before
    # its [env] file can be written into its home; env precedes the mods because
    # a mod may need a cred (repo-bootstrap's private clone); files come last
    # because their destinations need the repo checkout. `create` blocks
    # through all of it.
    agent_alias, admin_alias = _wait_ready(provider, name, project)
    print("==> creating the box's users ...")
    _ensure_box_users(admin_alias, tarball)
    print("==> injecting [env] credentials ...")
    run_envvars(admin_alias, resolve_env(project, gc), project.agent_user)
    print("==> provisioning (running mods over SSH; several minutes) ...")
    _run_mods(admin_alias, tarball, [])
    if project.sync:
        print("==> syncing host-local files onto the box ...")
        run_sync(project, agent_alias)
    print(f"\n==> '{name}' is ready.\n\n  SSH:  {target} ssh\n")


def _poll(fn, timeout: int, interval: int = 5):
    """Poll `fn` until it returns a truthy value or `timeout` seconds elapse;
    returns the value (or None on timeout)."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _ssh_ok(alias: str) -> bool:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", alias, "true"],
                       capture_output=True)
    return r.returncode == 0


def _wait_ready(provider, name: str, project: ProjectConfig) -> tuple:
    """Block until a freshly-created box has an address, accepts SSH as the
    privileged user, and its base cloud-init (that user + packages the mods lean
    on) has finished; return the refreshed (agent, admin) SSH aliases. Times out
    pointing at `devbox status`.

    Readiness is checked over the PRIVILEGED alias: the agent account is created
    later, by the provisioning driver, so its alias cannot connect yet."""
    print("==> waiting for the instance to get an address ...")
    ip = _poll(lambda: provider.address(name), timeout=300)
    if not ip:
        die(f"timed out waiting for '{name}' to get an address; check `devbox status`")
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, project)

    print("==> waiting for SSH ...")
    if not _poll(lambda: _ssh_ok(admin_alias), timeout=300):
        die(f"timed out waiting for SSH to '{name}'; check `devbox status`")

    print("==> waiting for base cloud-init (user + packages) to finish ...")
    # `cloud-init status --wait` blocks until done: exit 0 = done, 2 = done with
    # recoverable warnings (degraded); anything else is a real failure.
    r = subprocess.run(["ssh", admin_alias, "cloud-init status --wait"])
    if r.returncode not in (0, 2):
        die(f"base cloud-init did not finish cleanly on '{name}' "
            f"(exit {r.returncode}); check `devbox status`")
    return agent_alias, admin_alias


def _run_driver(alias: str, tarball: bytes, driver_args: list, log: bool) -> int:
    """Deliver the provisioning payload over SSH and run the driver with
    `driver_args`: pipe the tarball into a per-run temp dir (extracted as the SSH
    user, then made world-readable so the driver can drop into the unprivileged
    agent to run a mod - the payload carries no secrets), optionally teeing to
    ~/provision.log (the log `devbox status` tails). Returns the driver's status.

    `alias` is always the PRIVILEGED user's: the driver needs sudo to create the
    agent account, install `# packages:` and drop privilege per mod."""
    selected = " ".join(shlex.quote(a) for a in driver_args)
    tee = f' 2>&1 | tee "$HOME/{PROVISION_LOG}"' if log else ""
    # `umask 022` before extracting: tar applies the extracting user's umask to
    # the modes it creates (including the `mods/` directory, which the archive
    # only implies), so a restrictive umask on the box would leave the payload
    # unreadable to the agent and fail every mod with a confusing EACCES.
    # `mktemp -d` ignores umask and is always 0700, hence the explicit chmod.
    remote = (
        'set -e; umask 022; d="$(mktemp -d)"; chmod 0755 "$d"; tar -xzf - -C "$d"; '
        f'bash "$d/provision-driver.sh" {selected}{tee}; '
        'rc=${PIPESTATUS[0]}; rm -rf "$d"; exit "$rc"'
    )
    return subprocess.run(
        ["ssh", alias, "bash", "-c", shlex.quote(remote)], input=tarball).returncode


def _ensure_box_users(alias: str, tarball: bytes) -> None:
    """Run only the driver's user preamble (create the unprivileged agent
    account, wire per-user shells). Split out of the full run because the
    agent's home has to exist before `run_envvars` can write the [env] file into
    it, and that happens before the mods."""
    rc = _run_driver(alias, tarball, ["--ensure-users"], log=False)
    if rc != 0:
        die(f"could not create the box's users (exit {rc}); see the output above")


def _run_mods(alias: str, tarball: bytes, mods: list) -> None:
    """Run the provisioning playbook over SSH (the driver re-runs its user
    preamble first). `mods` selects a subset; empty = the whole playbook."""
    rc = _run_driver(alias, tarball, mods, log=True)
    if rc != 0:
        die(f"provisioning failed (exit {rc}); see the output above "
            f"or ~/{PROVISION_LOG} on the box")


def cmd_up(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    state = provider.state(name)
    if state is None:
        die(f"instance '{name}' is not defined; run `devbox create` first")
    if state == "running":
        print(f"instance '{name}' is already running.")
        return
    print(f"==> starting '{name}' ...")
    provider.start(name)


def cmd_stop(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    state = provider.state(name)
    if state is None:
        die(f"instance '{name}' is not defined; run `devbox create` first")
    if state not in ("running",):
        print(f"instance '{name}' is already stopped ({state}).")
        return
    print(f"==> stopping '{name}' ...")
    provider.stop(name)


def _confirm_destroy(name: str, provider_name: str, assume_yes: bool,
                     stdin=None) -> bool:
    """Decide whether a destroy may proceed. `destroy` deletes the overlay disk
    irreversibly, so guard it: --yes (assume_yes) skips the prompt outright;
    otherwise a non-TTY stdin can't be prompted safely (would hang or silently
    proceed) so we die pointing at --yes; on a TTY we require the full word
    'yes'. Anything else -> False (no destroy). Factored out of cmd_destroy so
    the decision is unit-testable without real infra. `stdin` defaults to the
    live sys.stdin (resolved at call time so it's patchable)."""
    if assume_yes:
        return True
    if stdin is None:
        stdin = sys.stdin
    if not stdin.isatty():
        die(f"refusing to destroy '{name}' without a TTY; pass --yes to confirm")
        return False  # unreachable (die exits); kept for clarity
    print(f"About to destroy '{name}' ({provider_name}) and DELETE its disk. "
          f"This is irreversible.")
    return input("Type 'yes' to confirm: ").strip() == "yes"


def cmd_destroy(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, provider_name, provider = _resolve_target(args, project, gc)
    if not _confirm_destroy(name, provider_name, args.yes):
        print(f"Aborted; '{name}' left untouched.")
        return
    provider.destroy(name)
    tail = (f" (cached base image left in {gc.cache_dir})"
            if provider_name == "libvirt" else "")
    print(f"Destroyed '{name}'{tail}.")


def cmd_ip(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?). Try again shortly.")
    print(ip)


def cmd_ssh(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    # The unprivileged agent by default - that's where the repo checkout and the
    # tooling live. --admin is the privileged account, and the only shell on the
    # box with sudo; it exists for debugging the machine itself.
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, project)
    os.execvp("ssh", ["ssh", admin_alias if args.admin else agent_alias, *args.ssh_args])


def cmd_status(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    # Over the privileged alias: provision.log lives in that user's home, since
    # the driver runs there.
    _, admin_alias = _refresh_ssh_aliases(name, ip, project)
    subprocess.run([
        "ssh", admin_alias,
        f"cloud-init status --long; echo ---; tail -n 80 ~/{PROVISION_LOG} 2>/dev/null",
    ], check=False)


def cmd_list(args: argparse.Namespace, gc: GlobalConfig, project: Optional[ProjectConfig]) -> None:
    # Global view: every devbox-managed instance, unioned across providers.
    # EC2 is account-wide; libvirt is this-host-only.
    instances = [inst for provider in all_providers(gc) for inst in provider.list()]
    instances.sort(key=lambda i: i.name)
    if not instances:
        print("No devbox-managed instances.")
        return
    rows = [("NAME", "PROVIDER", "STATE", "RESOURCES", "UP")]
    for i in instances:
        up = "—"
        if i.state == "running":
            up = _format_age(i.created_at)
            if i.cost_hint:
                up += " ($)"  # this instance is costing money while running
        rows.append((i.name, i.provider, i.state, i.resources, up))
    _print_table(rows)


def _alias_names(name: str) -> tuple:
    """The two SSH aliases for an instance: (agent, admin).

    `devbox-<name>` stays pointed at the UNPRIVILEGED agent user - it is the
    alias muscle memory and every existing jj remote already names, and the
    agent's home is where the repo checkout lives. The privileged user gets the
    `-admin` suffix; it is what provisioning and `devbox ssh --admin` use."""
    return f"devbox-{name}", f"devbox-{name}-admin"


def _refresh_ssh_aliases(name: str, ip: str, project: ProjectConfig) -> tuple:
    """Write/refresh both SSH aliases for an instance (host, user, dedicated
    known-hosts) and return them as (agent_alias, admin_alias). Provider-agnostic
    - `ip` is whatever the provider resolved (libvirt DHCP lease or EC2 public
    IP)."""
    agent_alias, admin_alias = _alias_names(name)
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_PATH.is_file():
        KNOWN_HOSTS_PATH.write_text("")
        KNOWN_HOSTS_PATH.chmod(0o600)

    host_conf = SSH_DIR / f"{name}.conf"
    host_conf.write_text("".join(
        f"Host {alias}\n"
        f"    HostName {ip}\n"
        f"    User {user}\n"
        f"    UserKnownHostsFile {KNOWN_HOSTS_PATH}\n"
        f"    StrictHostKeyChecking accept-new\n"
        for alias, user in ((agent_alias, project.agent_user),
                            (admin_alias, project.user))
    ))

    include_line = f"Include {SSH_DIR}/*.conf"
    if SSH_USER_CONFIG.is_file():
        existing = SSH_USER_CONFIG.read_text()
        if include_line not in existing:
            with SSH_USER_CONFIG.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{include_line}\n")
    else:
        SSH_USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        SSH_USER_CONFIG.write_text(f"{include_line}\n")
        SSH_USER_CONFIG.chmod(0o600)

    return agent_alias, admin_alias


def cmd_host(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, project)
    # The agent alias is the one `jj fetch` uses and the one worth printing; the
    # admin alias is refreshed alongside it and named for completeness.
    print(agent_alias)
    print(f"(privileged: {admin_alias})")


def _ensure_jj_remote(repo_dir: Path, remote: str, url: str) -> None:
    """Idempotently point a jj git remote named `remote` at `url` in the host's
    local repo - add it if missing, re-point it if the URL drifted (a new IP =>
    a new alias target isn't at play here since we key on the stable alias, but
    the repo path can change if [repo] dir changes)."""
    listing = subprocess.run(["jj", "git", "remote", "list"], cwd=repo_dir,
                             capture_output=True, text=True)
    if listing.returncode != 0:
        die(f"`jj git remote list` failed in {repo_dir} - is this a jj repo? "
            f"Run `devbox jj fetch` from your project's jj checkout (or pass -C).")
    current = None
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == remote:
            current = parts[1]
            break
    if current is None:
        subprocess.run(["jj", "git", "remote", "add", remote, url], cwd=repo_dir, check=True)
    elif current != url:
        subprocess.run(["jj", "git", "remote", "set-url", remote, url], cwd=repo_dir, check=True)


def cmd_jj_fetch(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    # jj git fetch (box->host): refresh the SSH alias (address via the provider,
    # so this is identical for libvirt or AWS), ensure the local jj remote points
    # at the instance's colocated repo, and fetch. `[repo]` is project-level, so
    # any instance in the project is fetched the same way.
    if project.repo.mode == "none" or not project.repo.dir:
        die("this project has no [repo] configured (set [repo] mode/dir); nothing to fetch")
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")

    # The agent alias: the checkout is in the agent's home, and `jj git fetch`
    # runs git-upload-pack as the login user - there is no sudo-ing through that.
    # An existing remote from before the two-user split points at the privileged
    # user's home; _ensure_jj_remote re-points it, so old checkouts self-heal.
    alias, _ = _refresh_ssh_aliases(name, ip, project)
    remote_url = f"ssh://{alias}/home/{project.agent_user}/{project.repo.dir}"
    repo_dir = find_project_dir(args.project_dir or Path.cwd())
    _ensure_jj_remote(repo_dir, alias, remote_url)

    print(f"==> jj git fetch from '{alias}' ({remote_url}) ...")
    result = subprocess.run(["jj", "git", "fetch", "--remote", alias], cwd=repo_dir)
    if result.returncode != 0:
        die("jj git fetch failed")
    print(f"fetched. Check out an agent bookmark with e.g. `jj new <bookmark>@{alias}`.")


def _split_mods(value: str) -> list:
    """The `--mods` optional argument -> a mod list: "" (flag with no value) =>
    [] (the whole playbook); "a,b,c" => ["a", "b", "c"]."""
    return [m.strip() for m in value.split(",") if m.strip()]


def cmd_sync(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    # Reconverge a live box: inject [env] creds (--envvars), run provisioning
    # mods (--mods), and/or push host-local files (--files), over the
    # provider-resolved SSH alias. `devbox create` runs the same steps on a
    # fresh box (see cmd_create).
    do_envvars = args.envvars
    do_mods = args.mods is not None      # --mods present (with or without a value)
    do_files = args.files
    if not (do_envvars or do_mods or do_files):
        die("nothing to do: pass --envvars (inject [env] creds), --mods (run "
            "provisioning mods), and/or --files (push host-local [[sync]] files)")
    if do_files and not project.sync:
        die("this project has no [[sync]]/[[repo.sync]] entries; drop --files")

    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, project)

    # Order: env -> mods -> files. Mods may need a cred (e.g. repo-bootstrap's
    # private clone); file dsts (e.g. ~/<repo>/.claude/skills) need the repo clone.
    # env and mods go over the privileged alias (writing another user's home /
    # running the driver both need sudo); files go over the agent's.
    tarball = build_tarball(build_payload(project, gc)) if (do_envvars or do_mods) else None
    if do_envvars:
        # The agent's home has to exist before its [env] file can land in it -
        # true on a box that predates this account, not just a brand-new one.
        _ensure_box_users(admin_alias, tarball)
        print(f"==> injecting [env] credentials into '{name}' ...")
        run_envvars(admin_alias, resolve_env(project, gc), project.agent_user)
    if do_mods:
        mods = _split_mods(args.mods)
        scope = f" (mods: {', '.join(mods)})" if mods else ""
        print(f"==> provisioning '{name}' over SSH{scope} ...")
        _run_mods(admin_alias, tarball, mods)
    if do_files:
        print(f"==> syncing host-local files to '{name}' ...")
        run_sync(project, agent_alias)
    print("done.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Manage per-project dev-sandbox VMs (libvirt or AWS).")
    # Defined before add_subparsers so they can't be swallowed by ssh's
    # argparse.REMAINDER: `devbox --name foo ssh -- -v`.
    p.add_argument("-C", "--project-dir", type=Path, default=None,
                    help="project directory to resolve the devbox marker from (default: cwd)")
    p.add_argument("--name", default=None,
                    help="target a specific instance by name (default: the project's libvirt instance)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="download image, define + first-boot the VM")
    c.add_argument("--provider", choices=PROVIDERS, default=None,
                    help=f"provider to launch on (default: the project's, else {DEFAULT_PROVIDER})")
    market = c.add_mutually_exclusive_group()
    market.add_argument("--spot", action="store_true", help="EC2: force a spot instance")
    market.add_argument("--on-demand", action="store_true",
                        help="EC2: force an on-demand instance")
    c.set_defaults(func=cmd_create)
    sub.add_parser("up", help="start an already-defined, shut-off VM").set_defaults(func=cmd_up)
    sub.add_parser("stop", help="gracefully shut down a running VM").set_defaults(func=cmd_stop)
    d = sub.add_parser("destroy", help="tear down the VM and its overlay disk")
    d.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                   help="skip the confirmation prompt (for scripts)")
    d.set_defaults(func=cmd_destroy)
    sub.add_parser("ip", help="print the VM's IPv4").set_defaults(func=cmd_ip)
    s = sub.add_parser("ssh", help="ssh into the VM (as the unprivileged agent user)")
    s.add_argument("--admin", action="store_true",
                   help="log in as the privileged user instead (the box's only sudo shell)")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    s.set_defaults(func=cmd_ssh)
    sub.add_parser("status", help="show cloud-init progress + provision log").set_defaults(func=cmd_status)
    # Global view - doesn't resolve a project (works anywhere).
    sub.add_parser("list", help="list devbox-managed instances").set_defaults(func=cmd_list, needs_project=False)
    # `jj` groups jj-specific operations; `jj fetch` pulls the instance's
    # bookmarked work box->host (the global -C/--name still go before `jj`).
    jj = sub.add_parser("jj", help="jj operations against the instance")
    jj_sub = jj.add_subparsers(dest="jj_cmd", required=True)
    jj_sub.add_parser("fetch", help="jj git fetch the instance's bookmarked work into the host repo").set_defaults(func=cmd_jj_fetch)
    # `sync` reconverges a live box: [env] creds, provisioning mods, host-local files.
    sy = sub.add_parser("sync", help="reconverge a running box: --envvars / --mods / --files")
    sy.add_argument("--envvars", action="store_true",
                    help="inject the [env] creds/vars into the box's shell env")
    sy.add_argument("--mods", nargs="?", const="", default=None, metavar="a,b,c",
                    help="run provisioning mods over SSH (all if no list; else comma-separated)")
    sy.add_argument("--files", action="store_true",
                    help="push host-local files ([[sync]]/[[repo.sync]]) to the box via rsync")
    sy.set_defaults(func=cmd_sync)
    sub.add_parser("host", help="refresh the SSH alias used for jj-fetch, print the alias name").set_defaults(func=cmd_host)

    args = p.parse_args()
    global_config = GlobalConfig.load()
    # Most commands operate within a project; `list` is global and skips it.
    project = None
    if getattr(args, "needs_project", True):
        project = resolve_project(args.project_dir or Path.cwd(), global_config)
    _run(args, global_config, project)


def _run(args: argparse.Namespace, gc: GlobalConfig, project: Optional[ProjectConfig]) -> None:
    """Dispatch, turning AWS credential failures into one-line errors instead of
    a boto stack trace. Credentials that can be checked up front are handled at
    client construction (providers/awsauth.py), which offers to log in and then
    carries on; what reaches here is either that path giving up (AwsAuthError,
    message already written) or a token that only the server rejected - too late
    to resume, so we say what to run."""
    try:
        args.func(args, gc, project)
    except AwsAuthError as err:
        die(str(err))
    except Exception as err:  # noqa: BLE001 - re-raised unless it's an auth failure
        if gc.aws is not None and is_auth_error(err):
            die(auth_error_message(gc.aws, err))
        raise


if __name__ == "__main__":
    main()
