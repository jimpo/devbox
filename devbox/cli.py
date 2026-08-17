"""Command-line entry point and subcommand handlers. Everything here is
provider-agnostic: commands resolve config + target, then act through the
Provider seam.

**Box commands** act on a machine, and take its name:

    devbox box create NAME     # download image, define + first-boot a box
    devbox box list            # every devbox-managed box (--attached: who's on them)
    devbox box up / stop       # start / gracefully shut down a box
    devbox box ssh NAME        # ssh in as the privileged user (the only sudo shell)
    devbox box status NAME     # cloud-init progress + the box's provision log
    devbox box sync --mods     # reconverge the box playbook
    devbox box destroy NAME    # tear down, removing the disk

**Project commands** act on this project's account on its box, and take none:

    devbox create              # ensure the box exists, then attach this project
    devbox ssh [args...]       # ssh in as this project's unprivileged agent user
    devbox sync --envvars      # inject [env] creds into that account's shell env
    devbox sync --mods[=a,b]   # (re)run provisioning mods over SSH (reconverge)
    devbox sync --files        # rsync host-local files ([[sync]]) onto the box
    devbox jj fetch            # jj git fetch the account's work into the host repo
    devbox host                # refresh the SSH alias used for jj fetch
    devbox detach              # give up this project's account on the box

A box is not a project: several projects can share one machine, each in its own
unprivileged account. Which box a project lives on comes from `box` in its
config (default: the project's own name), overridable with `--box`.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .initscript import render_init_script
from .config import (BoxSpec, DEFAULT_PROVIDER, GlobalConfig, PROVIDERS,
                     ProjectConfig, SSH_DIR, SSH_USER_CONFIG, find_project_dir,
                     known_hosts_path, parse_instance_name, resolve_box_spec,
                     resolve_instance, resolve_project)
from .provision import (build_box_payload, build_payload, build_tarball,
                        provision_log, resolve_env)
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


def truncate_known_hosts(box: str) -> None:
    """A freshly created box has a brand-new SSH host key; stale entries from a
    previous box of the same name can only cause false mismatches. Scoped to the
    one box: several projects now point at the same machine, and creating a
    *different* box must not invalidate this one's key for all of them."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    path = known_hosts_path(box)
    path.write_text("")
    path.chmod(0o600)


def _resolve_box(name: str, gc: GlobalConfig):
    """The (canonical name, provider name, provider) a box command acts on. The
    provider is derived from the name (parse_instance_name) - pure string
    routing, so `devbox box ip ec2-dev` reaches EC2 with no config lookup and no
    API call to find the owner."""
    provider_name, _ = parse_instance_name(name)
    return name, provider_name, get_provider(provider_name, gc)


def _resolve_target(args: argparse.Namespace, project: ProjectConfig, gc: GlobalConfig):
    """The box a project command acts on: `--box`, else the project's `box` key,
    else its own name."""
    return _resolve_box(resolve_instance(args, project), gc)


def _admin_user(provider, name: str) -> str:
    """The box's privileged account, read back from the instance itself. A box
    created before devbox stamped it reports nothing, and was created with the
    host's own $USER - which is the fallback."""
    inst = provider.get(name)
    stamped = inst.admin_user if inst is not None else None
    return stamped or os.environ.get("USER", "")


def _check_name_unique(gc: GlobalConfig, name: str) -> None:
    """Box names are globally unique across providers - enforced at create by
    checking the union `list`. A provider that can't be listed (e.g. EC2 creds
    missing) is warned about, not fatal, so a local create isn't blocked by it."""
    for provider in all_providers(gc):
        try:
            existing = provider.list()
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not check {provider.name} for name collisions: {e}")
            continue
        if any(i.name == name for i in existing):
            die(f"a box named '{name}' already exists ({provider.name}); "
                f"pick another name or destroy it first")


# ---- box lifecycle --------------------------------------------------------------
def _boot_box(box: BoxSpec, gc: GlobalConfig, provider) -> None:
    """Launch a box and run its own playbook. The box playbook needs no project:
    it is the apt half of the dotfiles setup plus a usable shell for the
    privileged account, which is the box's debugging entry point."""
    _check_name_unique(gc, box.name)
    # Built before the launch so a broken mod surfaces before we boot a machine.
    tarball = build_tarball(build_box_payload(box.admin_user, gc))

    provider.create(InstanceSpec(
        name=box.name,
        admin_user=box.admin_user,
        provider=box.provider,
        disk_gb=box.disk_gb,
        user_data=render_init_script(box, gc),
        created_at=datetime.now(timezone.utc).isoformat(),
        vcpus=box.vcpus,
        memory_mib=box.memory_mib,
        ec2=box.ec2,
        lightsail=box.lightsail,
    ))
    truncate_known_hosts(box.name)

    admin_alias = _wait_ready(provider, box.name, box.admin_user)
    print("==> provisioning the box (running its mods over SSH) ...")
    _run_mods(admin_alias, tarball, [], log=provision_log())


def cmd_box_create(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    box = resolve_box_spec(args, gc, args.name)
    _, _, provider = _resolve_box(box.name, gc)
    if provider.state(box.name) is not None:
        print(f"box '{box.name}' already exists; leaving it alone.")
        return
    _boot_box(box, gc, provider)
    print(f"\n==> box '{box.name}' is ready.\n\n"
          f"  Attach a project:  box = \"{box.name}\" in its .devbox/config.toml, "
          f"then `devbox create`\n"
          f"  Admin shell:       devbox box ssh {box.name}\n")


def _ensure_box(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig):
    """The box this project attaches to, booting it first if it doesn't exist.

    A project that never named a box owns one called after itself, and `devbox
    create` boots it on the global `[defaults]` - the zero-config path. A project
    that points at a *shared* box is a different matter: if that box is missing,
    guessing its shape would be worse than saying so, unless the user passed
    box-create flags on this very command line."""
    name, provider_name, provider = _resolve_target(args, project, gc)
    state = provider.state(name)
    if state is not None:
        return name, provider, state

    owns_box = name == project.project
    if not owns_box and not _box_flags_given(args):
        die(f"box '{name}' does not exist. Create it first:\n"
            f"  devbox box create {parse_instance_name(name)[1]}"
            f"{'' if provider_name == DEFAULT_PROVIDER else f' --provider {provider_name}'}")
    box = resolve_box_spec(args, gc, parse_instance_name(name)[1])
    _boot_box(box, gc, provider)
    return name, provider, provider.state(name)


def _box_flags_given(args: argparse.Namespace) -> bool:
    """Whether this command line asked for a box of a particular shape - the
    signal that creating one here is intended rather than a fallback."""
    return any(getattr(args, f, None) for f in
               ("provider", "vcpus", "memory", "disk", "instance_type", "arch",
                "ami", "spot", "bundle", "blueprint", "zone", "user"))


def cmd_create(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    """Ensure this project's box exists, then attach the project to it.
    Idempotent: on a box that already has this project, it is a reconverge."""
    name, provider, state = _ensure_box(args, gc, project)
    if state != "running":
        print(f"==> starting '{name}' ...")
        provider.start(name)

    admin_user = _admin_user(provider, name)
    if admin_user == project.agent_user:
        die(f"this project's agent_user '{project.agent_user}' is also the "
            f"privileged account on box '{name}'; the whole point is that agents "
            f"run without sudo. Set `agent_user = \"...\"` in .devbox/config.toml.")

    # Built up front so a bad jj identity / missing mod surfaces before we touch
    # the box (build_payload resolves both).
    tarball = build_tarball(build_payload(project, gc, admin_user))
    agent_alias, admin_alias = _wait_ready(provider, name, admin_user, project)

    # Order: account -> env -> mods -> files. The agent account has to exist
    # before its [env] file can be written into its home; env precedes the mods
    # because a mod may need a cred (repo-bootstrap's private clone); files come
    # last because their destinations need the repo checkout. `create` blocks
    # through all of it. Everything but the files goes over the PRIVILEGED alias:
    # creating an account and writing into another user's home both need sudo.
    print(f"==> attaching '{project.project}' to '{name}' as user "
          f"'{project.agent_user}' ...")
    _ensure_agent_account(admin_alias, tarball, adopt=args.adopt)
    print("==> injecting [env] credentials ...")
    run_envvars(admin_alias, resolve_env(project, gc), project.agent_user)
    print("==> provisioning (running mods over SSH; several minutes) ...")
    _run_mods(admin_alias, tarball, [], log=provision_log(project.agent_user))
    if project.sync:
        print("==> syncing host-local files onto the box ...")
        run_sync(project, agent_alias)

    d = args.project_dir or "."
    print(f"\n==> '{project.project}' is ready on '{name}'.\n\n"
          f"  SSH:  devbox -C {d} ssh\n")


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


def _wait_ready(provider, name: str, admin_user: str,
                project: Optional[ProjectConfig] = None):
    """Block until a box has an address, accepts SSH as the privileged user, and
    its base cloud-init (that user + packages the mods lean on) has finished.
    Returns the admin alias, or the (agent, admin) pair when a project is given.
    Times out pointing at `devbox box status`.

    Readiness is checked over the PRIVILEGED alias: a project's agent account is
    created later, by the provisioning driver, so its alias cannot connect yet."""
    print("==> waiting for the box to get an address ...")
    ip = _poll(lambda: provider.address(name), timeout=300)
    if not ip:
        die(f"timed out waiting for '{name}' to get an address; "
            f"check `devbox box status {name}`")
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, admin_user, project)

    print("==> waiting for SSH ...")
    if not _poll(lambda: _ssh_ok(admin_alias), timeout=300):
        die(f"timed out waiting for SSH to '{name}'; check `devbox box status {name}`")

    print("==> waiting for base cloud-init (user + packages) to finish ...")
    # `cloud-init status --wait` blocks until done: exit 0 = done, 2 = done with
    # recoverable warnings (degraded); anything else is a real failure.
    r = subprocess.run(["ssh", admin_alias, "cloud-init status --wait"])
    if r.returncode not in (0, 2):
        die(f"base cloud-init did not finish cleanly on '{name}' "
            f"(exit {r.returncode}); check `devbox box status {name}`")
    return (agent_alias, admin_alias) if project is not None else admin_alias


def _run_driver(alias: str, tarball: bytes, driver_args: list, log: str = "",
                env: dict = None) -> int:
    """Deliver the provisioning payload over SSH and run the driver with
    `driver_args`: pipe the tarball into a per-run temp dir (extracted as the SSH
    user, then made world-readable so the driver can drop into an unprivileged
    agent to run a mod - the payload carries no secrets), optionally teeing to
    `~/<log>` (what `devbox status` tails). Returns the driver's status.

    `alias` is always the PRIVILEGED user's: the driver needs sudo to create an
    agent account, install each mod's packages and drop privilege per mod."""
    selected = " ".join(shlex.quote(a) for a in driver_args)
    tee = f' 2>&1 | tee "$HOME/{log}"' if log else ""
    # Flags for the driver that aren't per-mod data go through the environment
    # rather than argv, so the driver's own argv stays "which mods to run".
    exports = "".join(f"export {k}={shlex.quote(v)}; " for k, v in (env or {}).items())
    # `umask 022` before extracting: tar applies the extracting user's umask to
    # the modes it creates (including the `mods/` directory, which the archive
    # only implies), so a restrictive umask on the box would leave the payload
    # unreadable to the agent and fail every mod with a confusing EACCES.
    # `mktemp -d` ignores umask and is always 0700, hence the explicit chmod.
    remote = (
        'set -e; umask 022; d="$(mktemp -d)"; chmod 0755 "$d"; tar -xzf - -C "$d"; '
        f'{exports}bash "$d/provision-driver.sh" {selected}{tee}; '
        'rc=${PIPESTATUS[0]}; rm -rf "$d"; exit "$rc"'
    )
    return subprocess.run(
        ["ssh", alias, "bash", "-c", shlex.quote(remote)], input=tarball).returncode


def _ensure_agent_account(alias: str, tarball: bytes, adopt: bool = False) -> None:
    """Run only the driver's agent-account preamble. Split out of the full run
    because the account's home has to exist before `run_envvars` can write the
    [env] file into it, and that happens before the mods.

    `adopt` claims an account that exists with no ownership record - a box built
    before devbox tracked attachments, or an account made by hand. The driver
    refuses without it rather than silently moving into somebody's home."""
    rc = _run_driver(alias, tarball, ["--ensure-agent"],
                     env={"DEVBOX_ATTACH_ADOPT": "1"} if adopt else None)
    if rc != 0:
        die(f"could not set up the project's account (exit {rc}); "
            f"see the output above")


def _run_mods(alias: str, tarball: bytes, mods: list, log: str) -> None:
    """Run a provisioning playbook over SSH (the driver re-runs its account
    preamble first). `mods` selects a subset; empty = the whole playbook."""
    rc = _run_driver(alias, tarball, mods, log=log)
    if rc != 0:
        die(f"provisioning failed (exit {rc}); see the output above "
            f"or ~/{log} on the box")


def cmd_box_up(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    state = provider.state(name)
    if state is None:
        die(f"box '{name}' is not defined; run `devbox box create` first")
    if state == "running":
        print(f"box '{name}' is already running.")
        return
    print(f"==> starting '{name}' ...")
    provider.start(name)


def cmd_box_stop(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    state = provider.state(name)
    if state is None:
        die(f"box '{name}' is not defined; run `devbox box create` first")
    if state not in ("running",):
        print(f"box '{name}' is already stopped ({state}).")
        return
    attached = _attached_projects(provider, name)
    if len(attached) > 1:
        print(f"note: {len(attached)} projects are attached ({', '.join(attached)}).")
    print(f"==> stopping '{name}' ...")
    provider.stop(name)


def _attached_projects(provider, name: str) -> list:
    """The projects currently attached to a box, read from the box itself. The
    markers are the only record - there is no host-side registry to go stale -
    so this needs the box up; a box that can't be reached reports nothing rather
    than failing the command it was called from."""
    ip = provider.address(name)
    if not ip:
        return []
    admin_alias = _alias_names(name)[1]
    _write_alias_conf(f"{name}.admin.conf", admin_alias, name, ip,
                      _admin_user(provider, name))
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", admin_alias,
         "cat /etc/devbox/projects/* 2>/dev/null"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return sorted(line.split("=", 1)[1] for line in r.stdout.splitlines()
                  if line.startswith("project=") and line.split("=", 1)[1])


def _confirm_destroy(name: str, provider_name: str, assume_yes: bool,
                     attached: list = (), stdin=None) -> bool:
    """Decide whether a destroy may proceed. `destroy` deletes the disk
    irreversibly - and with it every attached project's uncommitted work - so
    guard it: --yes (assume_yes) skips the prompt outright; otherwise a non-TTY
    stdin can't be prompted safely (would hang or silently proceed) so we die
    pointing at --yes; on a TTY we require the full word 'yes'. Anything else ->
    False (no destroy). Factored out of cmd_box_destroy so the decision is
    unit-testable without real infra. `stdin` defaults to the live sys.stdin
    (resolved at call time so it's patchable)."""
    if assume_yes:
        return True
    if stdin is None:
        stdin = sys.stdin
    if not stdin.isatty():
        die(f"refusing to destroy '{name}' without a TTY; pass --yes to confirm")
        return False  # unreachable (die exits); kept for clarity
    print(f"About to destroy '{name}' ({provider_name}) and DELETE its disk. "
          f"This is irreversible.")
    if attached:
        print(f"  {len(attached)} project(s) attached, with their checkouts: "
              f"{', '.join(attached)}")
    return input("Type 'yes' to confirm: ").strip() == "yes"


def cmd_box_destroy(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    name, provider_name, provider = _resolve_box(args.name, gc)
    if not _confirm_destroy(name, provider_name, args.yes,
                            _attached_projects(provider, name)):
        print(f"Aborted; '{name}' left untouched.")
        return
    provider.destroy(name)
    for conf in SSH_DIR.glob(f"{name}.*.conf"):
        conf.unlink()
    known_hosts_path(name).unlink(missing_ok=True)
    tail = (f" (cached base image left in {gc.cache_dir})"
            if provider_name == "libvirt" else "")
    print(f"Destroyed '{name}'{tail}.")


def cmd_destroy(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    """Destroy this project's box - but only when this project is the only thing
    on it. A shared box is somebody else's machine too, so the blast radius has
    to be spelled rather than inferred."""
    name, provider_name, provider = _resolve_target(args, project, gc)
    attached = _attached_projects(provider, name)
    others = [p for p in attached if p != project.project]
    if others:
        die(f"box '{name}' also hosts {', '.join(others)}. Destroying it would "
            f"take their checkouts with it.\n"
            f"  Give up just this project's account:  devbox detach\n"
            f"  Destroy the whole box anyway:         devbox box destroy {name}")
    args = argparse.Namespace(name=name, yes=args.yes)
    cmd_box_destroy(args, gc)


def cmd_ip(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?). Try again shortly.")
    print(ip)


def cmd_box_ssh(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    """The privileged shell - the box's only sudo account. For debugging the
    machine itself; a project's own shell is `devbox ssh`."""
    name, _, provider = _resolve_box(args.name, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    _, admin_alias = _refresh_ssh_aliases(name, ip, _admin_user(provider, name))
    os.execvp("ssh", ["ssh", admin_alias, *args.ssh_args])


def cmd_ssh(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    # This project's unprivileged account - that's where its repo checkout and
    # tooling live. The box's privileged shell is `devbox box ssh <box>`.
    agent_alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), project)
    os.execvp("ssh", ["ssh", agent_alias, *args.ssh_args])


def _tail_provision_log(provider, name: str, log: str) -> None:
    """cloud-init progress + the tail of one provisioning log, over the
    privileged alias (the driver runs there, so the logs are in its home)."""
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    _, admin_alias = _refresh_ssh_aliases(name, ip, _admin_user(provider, name))
    subprocess.run([
        "ssh", admin_alias,
        f"cloud-init status --long; echo ---; tail -n 80 ~/{log} 2>/dev/null",
    ], check=False)


def cmd_box_status(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    _tail_provision_log(provider, name, provision_log())


def cmd_status(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    _tail_provision_log(provider, name, provision_log(project.agent_user))


def cmd_box_list(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    # Global view: every devbox-managed box, unioned across providers. EC2 is
    # account-wide; libvirt is this-host-only.
    boxes = [inst for provider in all_providers(gc) for inst in provider.list()]
    boxes.sort(key=lambda i: i.name)
    if not boxes:
        print("No devbox-managed boxes.")
        return
    # Who is attached lives on each box, so it costs an SSH per row - opt-in.
    attached = {}
    if getattr(args, "attached", False):
        for provider in all_providers(gc):
            for box in provider.list():
                attached[box.name] = _attached_projects(provider, box.name)

    header = ["NAME", "PROVIDER", "STATE", "RESOURCES", "UP"]
    if attached:
        header.insert(2, "PROJECTS")
    rows = [tuple(header)]
    for i in boxes:
        up = "—"
        if i.state == "running":
            up = _format_age(i.created_at)
            if i.cost_hint:
                up += " ($)"  # this box is costing money while running
        row = [i.name, i.provider, i.state, i.resources, up]
        if attached:
            row.insert(2, ", ".join(attached.get(i.name) or []) or "—")
        rows.append(tuple(row))
    _print_table(rows)


def _alias_names(box: str, project: Optional[ProjectConfig] = None) -> tuple:
    """The SSH aliases for a box: (agent, admin). The agent alias is per
    attachment - `devbox-<box>-<project>` - because several projects now share
    one machine and each needs its own account.

    It collapses to plain `devbox-<box>` when the project owns the box (the name
    is the same either way), which is the one-project case: that keeps the alias
    every existing jj remote, doc and finger already names."""
    admin = f"devbox-{box}-admin"
    if project is None:
        return None, admin
    agent = (f"devbox-{box}" if project.project == box
             else f"devbox-{box}-{project.project}")
    return agent, admin


def _write_alias_conf(filename: str, alias: str, box: str, ip: str, user: str) -> None:
    """One alias, alone in its own file under SSH_DIR.

    One stanza per file is what makes attaching safe: a single shared file per
    box would have each project's refresh overwrite the last one's account, and
    `Include .../*.conf` is first-match-wins, so a stale duplicate stanza would
    quietly shadow the current address."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    known_hosts = known_hosts_path(box)
    if not known_hosts.is_file():
        known_hosts.write_text("")
        known_hosts.chmod(0o600)
    (SSH_DIR / filename).write_text(
        f"Host {alias}\n"
        f"    HostName {ip}\n"
        f"    User {user}\n"
        f"    UserKnownHostsFile {known_hosts}\n"
        f"    StrictHostKeyChecking accept-new\n"
    )


def _refresh_ssh_aliases(box: str, ip: str, admin_user: str,
                         project: Optional[ProjectConfig] = None) -> tuple:
    """Write/refresh the SSH aliases for a box (host, user, dedicated
    known-hosts) and return them as (agent_alias, admin_alias); the agent one is
    None when no project was given. Provider-agnostic - `ip` is whatever the
    provider resolved (libvirt DHCP lease or EC2 public IP)."""
    agent_alias, admin_alias = _alias_names(box, project)
    _write_alias_conf(f"{box}.admin.conf", admin_alias, box, ip, admin_user)
    if project is not None:
        _write_alias_conf(f"{box}.{project.project}.conf", agent_alias, box, ip,
                          project.agent_user)

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
    agent_alias, admin_alias = _refresh_ssh_aliases(
        name, ip, _admin_user(provider, name), project)
    # The agent alias is the one `jj fetch` uses and the one worth printing; the
    # admin alias is refreshed alongside it and named for completeness.
    print(agent_alias)
    print(f"(privileged: {admin_alias})")


def cmd_herdr(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    """Launch a herdr session against this project's account on its box -
    `herdr --remote <alias>`, replacing `herdr --remote $(devbox host)`.

    The agent alias, for the same reason `devbox ssh` uses it: herdr drives a
    shell in the account the checkout and the agents live in, not the box's
    privileged one. Refreshing the alias first is what makes this work after the
    box's address has changed, which is the whole reason the `$(devbox host)`
    form existed."""
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent_alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), project)
    if not shutil.which("herdr"):
        die("herdr is not installed on this host (it drives the session from "
            "here, over SSH; the box needs nothing). Install it, or use "
            "`devbox ssh`.")
    # exec, not run: herdr takes over the terminal, exactly as `devbox ssh` does.
    os.execvp("herdr", ["herdr", "--remote", agent_alias, *args.herdr_args])


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
    # at this project's colocated repo on its box, and fetch.
    if project.repo.mode == "none" or not project.repo.dir:
        die("this project has no [repo] configured (set [repo] mode/dir); nothing to fetch")
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")

    # The agent alias: the checkout is in this project's own account, and `jj git
    # fetch` runs git-upload-pack as the login user - there is no sudo-ing through
    # that. An existing remote pointing at an older home is re-pointed by
    # _ensure_jj_remote, so old checkouts self-heal.
    alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), project)
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
    admin_user = _admin_user(provider, name)
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, admin_user, project)

    # Order: env -> mods -> files. Mods may need a cred (e.g. repo-bootstrap's
    # private clone); file dsts (e.g. ~/<repo>/.claude/skills) need the repo clone.
    # env and mods go over the privileged alias (writing another user's home /
    # running the driver both need sudo); files go over this project's own.
    tarball = (build_tarball(build_payload(project, gc, admin_user))
               if (do_envvars or do_mods) else None)
    if do_envvars:
        # The account's home has to exist before its [env] file can land in it -
        # true on a box this project has never attached to, not just a new one.
        _ensure_agent_account(admin_alias, tarball, adopt=args.adopt)
        print(f"==> injecting [env] credentials into '{name}' ...")
        run_envvars(admin_alias, resolve_env(project, gc), project.agent_user)
    if do_mods:
        mods = _split_mods(args.mods)
        subset = f" (mods: {', '.join(mods)})" if mods else ""
        print(f"==> provisioning '{name}' over SSH{subset} ...")
        _run_mods(admin_alias, tarball, mods, log=provision_log(project.agent_user))
    if do_files:
        print(f"==> syncing host-local files to '{name}' ...")
        run_sync(project, agent_alias)
    print("done.")


def cmd_box_sync(args: argparse.Namespace, gc: GlobalConfig, project=None) -> None:
    """Reconverge the box's own playbook - the machine, not any attachment."""
    name, _, provider = _resolve_box(args.name, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    admin_user = _admin_user(provider, name)
    _, admin_alias = _refresh_ssh_aliases(name, ip, admin_user)
    mods = _split_mods(args.mods or "")
    subset = f" (mods: {', '.join(mods)})" if mods else ""
    print(f"==> provisioning box '{name}' over SSH{subset} ...")
    _run_mods(admin_alias, build_tarball(build_box_payload(admin_user, gc)), mods,
              log=provision_log())
    print("done.")


def cmd_detach(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    """Give up this project's account on its box.

    Non-destructive by default: the ownership record, the authorized key and the
    credentials go, so nothing can reach the account any more - but the home, and
    with it any uncommitted work, stays, so re-attaching is instant. `--purge` is
    the one that deletes; `userdel` refuses while a session is alive, hence the
    terminate first."""
    name, provider_name, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent = shlex.quote(project.agent_user)
    _, admin_alias = _refresh_ssh_aliases(name, ip, _admin_user(provider, name))

    if args.purge and not _confirm_destroy(
            f"{project.agent_user}@{name}", provider_name, args.yes):
        print("Aborted; nothing removed.")
        return
    remote = (
        f'set -e; sudo rm -f /etc/devbox/projects/{agent}; '
        f'h="$(getent passwd {agent} | cut -d: -f6)"; '
        f'[ -n "$h" ] || exit 0; '
        f'sudo truncate -s 0 "$h/.ssh/authorized_keys" 2>/dev/null || true; '
        f'sudo rm -f "$h/.config/devbox/env"; '
        f'sudo usermod --lock --expiredate 1 {agent}'
    )
    if args.purge:
        remote += (
            f'; sudo loginctl terminate-user {agent} 2>/dev/null || true; '
            f'sudo pkill -KILL -u {agent} 2>/dev/null || true; sleep 1; '
            f'sudo userdel -r {agent}'
        )
    r = subprocess.run(["ssh", admin_alias, "bash", "-c", shlex.quote(remote)])
    if r.returncode != 0:
        die(f"detaching '{project.agent_user}' from '{name}' failed "
            f"(exit {r.returncode})")

    conf = SSH_DIR / f"{name}.{project.project}.conf"
    conf.unlink(missing_ok=True)
    kept = ("" if args.purge else
            f"\nIts home is untouched; `devbox create` re-attaches it.")
    print(f"Detached '{project.project}' from '{name}'.{kept}")


def _add_box_create_flags(parser: argparse.ArgumentParser) -> None:
    """The shape of a box, as flags. There is no box config file: resources are
    fixed at create and readable from the provider afterwards, so a persisted
    spec could only ever disagree with the machine."""
    parser.add_argument("--provider", choices=PROVIDERS, default=None,
                        help=f"provider to launch on (default: {DEFAULT_PROVIDER})")
    parser.add_argument("--user", default=None,
                        help="the box's privileged account (default: $USER)")
    parser.add_argument("--vcpus", type=int, default=None, help="libvirt: vCPUs")
    parser.add_argument("--memory", type=int, default=None, metavar="MIB",
                        help="libvirt: memory in MiB")
    parser.add_argument("--disk", type=int, default=None, metavar="GB",
                        help="disk size in GB (ignored on Lightsail, whose bundle fixes it)")
    parser.add_argument("--instance-type", default=None, help="EC2: instance type")
    parser.add_argument("--arch", choices=("x86_64", "arm64"), default=None,
                        help="EC2: architecture (default: x86_64)")
    parser.add_argument("--ami", default=None, help="EC2: pin an explicit AMI id")
    parser.add_argument("--bundle", default=None, help="Lightsail: bundle id")
    parser.add_argument("--blueprint", default=None, help="Lightsail: OS blueprint id")
    parser.add_argument("--zone", default=None, help="Lightsail: availability zone")
    market = parser.add_mutually_exclusive_group()
    market.add_argument("--spot", action="store_true", help="EC2: force a spot instance")
    market.add_argument("--on-demand", action="store_true",
                        help="EC2: force an on-demand instance")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Manage shared dev-sandbox VMs (libvirt or AWS) and the "
                    "projects attached to them.")
    # Defined before add_subparsers so they can't be swallowed by ssh's
    # argparse.REMAINDER: `devbox --box foo ssh -- -v`.
    p.add_argument("-C", "--project-dir", type=Path, default=None,
                    help="project directory to resolve the devbox marker from (default: cwd)")
    p.add_argument("--box", "--name", dest="box", default=None,
                    help="target a specific box by name (default: the project's `box`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- box commands: the machine itself, addressed by name ----------------
    box = sub.add_parser("box", help="create and manage boxes (the VMs projects attach to)")
    box_sub = box.add_subparsers(dest="box_cmd", required=True)
    bc = box_sub.add_parser("create", help="download image, define + first-boot a box")
    bc.add_argument("name", help="the box's name (a provider prefix is added for cloud boxes)")
    _add_box_create_flags(bc)
    bc.set_defaults(func=cmd_box_create, needs_project=False)
    bl = box_sub.add_parser("list", help="list devbox-managed boxes")
    bl.add_argument("--attached", action="store_true",
                    help="also show which projects are attached (one SSH per box)")
    bl.set_defaults(func=cmd_box_list, needs_project=False)
    for verb, fn, helptext in (
            ("up", cmd_box_up, "start an already-defined, shut-off box"),
            ("stop", cmd_box_stop, "gracefully shut down a running box"),
            ("status", cmd_box_status, "show cloud-init progress + the box's provision log"),
    ):
        bp = box_sub.add_parser(verb, help=helptext)
        bp.add_argument("name", help="the box's canonical name")
        bp.set_defaults(func=fn, needs_project=False)
    bs = box_sub.add_parser("ssh", help="ssh into the box as the privileged user (its only sudo shell)")
    bs.add_argument("name", help="the box's canonical name")
    bs.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    bs.set_defaults(func=cmd_box_ssh, needs_project=False)
    bsy = box_sub.add_parser("sync", help="reconverge the box's own playbook")
    bsy.add_argument("name", help="the box's canonical name")
    bsy.add_argument("--mods", nargs="?", const="", default=None, metavar="a,b,c",
                     help="run box mods over SSH (all if no list; else comma-separated)")
    bsy.set_defaults(func=cmd_box_sync, needs_project=False)
    bd = box_sub.add_parser("destroy", help="tear down the box and its disk")
    bd.add_argument("name", help="the box's canonical name")
    bd.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                    help="skip the confirmation prompt (for scripts)")
    bd.set_defaults(func=cmd_box_destroy, needs_project=False)

    # ---- project commands: this project's account on its box ----------------
    c = sub.add_parser("create", help="ensure this project's box exists, then attach to it")
    c.add_argument("--adopt", action="store_true",
                   help="claim an existing account that devbox has no record of")
    _add_box_create_flags(c)
    c.set_defaults(func=cmd_create)
    d = sub.add_parser("destroy", help="tear down this project's box (only if nothing else is on it)")
    d.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                   help="skip the confirmation prompt (for scripts)")
    d.set_defaults(func=cmd_destroy)
    dt = sub.add_parser("detach", help="give up this project's account on its box")
    dt.add_argument("--purge", action="store_true",
                    help="also delete the account's home directory and its work")
    dt.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                    help="skip the confirmation prompt (for scripts)")
    dt.set_defaults(func=cmd_detach)
    sub.add_parser("ip", help="print the box's IPv4").set_defaults(func=cmd_ip)
    s = sub.add_parser("ssh", help="ssh into the box as this project's agent user")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    s.set_defaults(func=cmd_ssh)
    sub.add_parser("status", help="show cloud-init progress + this project's provision log").set_defaults(func=cmd_status)
    # `jj` groups jj-specific operations; `jj fetch` pulls the account's
    # bookmarked work box->host (the global -C/--box still go before `jj`).
    jj = sub.add_parser("jj", help="jj operations against the box")
    jj_sub = jj.add_subparsers(dest="jj_cmd", required=True)
    jj_sub.add_parser("fetch", help="jj git fetch the account's bookmarked work into the host repo").set_defaults(func=cmd_jj_fetch)
    # `sync` reconverges a live attachment: [env] creds, mods, host-local files.
    sy = sub.add_parser("sync", help="reconverge this project on its box: --envvars / --mods / --files")
    sy.add_argument("--envvars", action="store_true",
                    help="inject the [env] creds/vars into the account's shell env")
    sy.add_argument("--mods", nargs="?", const="", default=None, metavar="a,b,c",
                    help="run provisioning mods over SSH (all if no list; else comma-separated)")
    sy.add_argument("--files", action="store_true",
                    help="push host-local files ([[sync]]/[[repo.sync]]) to the box via rsync")
    sy.add_argument("--adopt", action="store_true",
                    help="claim an existing account that devbox has no record of")
    sy.set_defaults(func=cmd_sync)
    sub.add_parser("host", help="refresh the SSH alias used for jj-fetch, print the alias name").set_defaults(func=cmd_host)
    hd = sub.add_parser("herdr", help="launch a herdr session on the box, as this project's agent user")
    hd.add_argument("herdr_args", nargs=argparse.REMAINDER, help="extra args passed to herdr (e.g. --session NAME)")
    hd.set_defaults(func=cmd_herdr)
    # `devbox list` stays as the familiar spelling of `devbox box list`.
    ls = sub.add_parser("list", help="list devbox-managed boxes")
    ls.add_argument("--attached", action="store_true",
                    help="also show which projects are attached (one SSH per box)")
    ls.set_defaults(func=cmd_box_list, needs_project=False)

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
