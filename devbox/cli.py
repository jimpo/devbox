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

**Workspace commands** act on this workspace's account on its box, and take none:

    devbox create              # ensure the box exists, then attach this workspace
    devbox ssh [args...]       # ssh in as this workspace's unprivileged agent user
    devbox sync --envvars      # inject [env] creds into that account's shell env
    devbox sync --mods         # (re)run provisioning mods over SSH (reconverge)
    devbox sync --files        # rsync host-local files ([[sync]]) onto the box
    devbox jj fetch [repo]     # jj git fetch the account's work into a host repo
    devbox host                # refresh the SSH alias used for jj fetch
    devbox detach              # give up this workspace's account on the box

A box is not a workspace: several workspaces can share one machine, each in its
own unprivileged account. Which box a workspace lives on comes from `box` in its
config (default: the workspace's own name), overridable with `--box`.

A workspace holds as many checkouts as it lists under `[[repos]]`, all in that
one account - which is what lets them share its git/jj credentials. It is marked
either by `devbox.toml` at a directory that holds repos, or by
`.devbox/config.toml` inside a single repo (see config.Marker).
"""
from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .initscript import render_init_script
from .config import (BoxSpec, DEFAULT_ADMIN_USER, DEFAULT_PROVIDER,
                     GlobalConfig, PROVIDERS,
                     WorkspaceConfig, SSH_DIR, SSH_USER_CONFIG, find_workspace,
                     find_workspace_opt, known_hosts_path, parse_instance_name,
                     resolve_box_spec, resolve_instance, resolve_workspace)
from .provision import (NIX_PAYLOAD_DIR, build_box_payload, build_payload,
                        build_tarball, nix_check_files, nix_module_names,
                        provision_log, resolve_env)
from .sync import run_envvars, run_sync
from .providers import (AWS_PROVIDERS, AwsAuthError, InstanceSpec, all_providers,
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
    one box: several workspaces now point at the same machine, and creating a
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


def _resolve_target(args: argparse.Namespace, workspace: WorkspaceConfig, gc: GlobalConfig):
    """The box a workspace command acts on: `--box`, else the workspace's `box` key,
    else its own name."""
    return _resolve_box(resolve_instance(args, workspace), gc)


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
    """Launch a box and run its own playbook. The box playbook needs no workspace:
    it is the apt package set the shared configs expect plus the nix store every
    attachment shares, on a machine whose privileged account is the box's
    debugging entry point."""
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
    _run_mods(admin_alias, tarball, log=provision_log())


def cmd_box_create(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    box = resolve_box_spec(args, gc, args.name)
    _, _, provider = _resolve_box(box.name, gc)
    if provider.state(box.name) is not None:
        print(f"box '{box.name}' already exists; leaving it alone.")
        return
    _boot_box(box, gc, provider)
    print(f"\n==> box '{box.name}' is ready.\n\n"
          f"  Attach a workspace:  box = \"{box.name}\" in its devbox.toml, "
          f"then `devbox create`\n"
          f"  Admin shell:       devbox box ssh {box.name}\n")


def _ensure_box(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig):
    """The box this workspace attaches to, booting it first if it doesn't exist.

    A workspace that never named a box owns one called after itself, and `devbox
    create` boots it on the global `[defaults]` - the zero-config path. A workspace
    that points at a *shared* box is a different matter: if that box is missing,
    guessing its shape would be worse than saying so, unless the user passed
    box-create flags on this very command line."""
    name, provider_name, provider = _resolve_target(args, workspace, gc)
    state = provider.state(name)
    if state is not None:
        return name, provider, state

    owns_box = name == workspace.workspace
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


def cmd_create(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    """Ensure this workspace's box exists, then attach the workspace to it.
    Idempotent: on a box that already has this workspace, it is a reconverge."""
    name, provider, state = _ensure_box(args, gc, workspace)
    if state != "running":
        print(f"==> starting '{name}' ...")
        provider.start(name)

    admin_user = _admin_user(provider, name)
    if admin_user == workspace.agent_user:
        die(f"this workspace's agent_user '{workspace.agent_user}' is also the "
            f"privileged account on box '{name}'; the whole point is that agents "
            f"run without sudo. Set `agent_user = \"...\"` in "
            f"{workspace.marker.config_path}.")

    # Built up front so a bad jj identity / missing mod surfaces before we touch
    # the box (build_payload resolves both).
    tarball = build_tarball(build_payload(workspace, gc, admin_user))
    agent_alias, admin_alias = _wait_ready(provider, name, admin_user, workspace)

    # Order: account -> env -> mods -> files. The agent account has to exist
    # before its [env] file can be written into its home; env precedes the mods
    # because a mod may need a cred (repo-bootstrap's private clone); files come
    # last because their destinations need the checkouts. `create` blocks
    # through all of it. Everything but the files goes over the PRIVILEGED alias:
    # creating an account and writing into another user's home both need sudo.
    print(f"==> attaching '{workspace.workspace}' to '{name}' as user "
          f"'{workspace.agent_user}' ...")
    _ensure_agent_account(admin_alias, tarball, adopt=args.adopt)
    print("==> injecting [env] credentials ...")
    run_envvars(admin_alias, resolve_env(workspace, gc), workspace.agent_user)
    print("==> provisioning (running mods over SSH; several minutes) ...")
    _run_mods(admin_alias, tarball, log=provision_log(workspace.agent_user))
    if workspace.sync:
        print("==> syncing host-local files onto the box ...")
        run_sync(workspace, agent_alias)

    d = args.workspace_dir or "."
    print(f"\n==> '{workspace.workspace}' is ready on '{name}'.\n\n"
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
                workspace: Optional[WorkspaceConfig] = None):
    """Block until a box has an address, accepts SSH as the privileged user, and
    its base cloud-init (that user + packages the mods lean on) has finished.
    Returns the admin alias, or the (agent, admin) pair when a workspace is given.
    Times out pointing at `devbox box status`.

    Readiness is checked over the PRIVILEGED alias: a workspace's agent account is
    created later, by the provisioning driver, so its alias cannot connect yet."""
    print("==> waiting for the box to get an address ...")
    ip = _poll(lambda: provider.address(name), timeout=300)
    if not ip:
        die(f"timed out waiting for '{name}' to get an address; "
            f"check `devbox box status {name}`")
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, admin_user, workspace)

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
    return (agent_alias, admin_alias) if workspace is not None else admin_alias


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
        die(f"could not set up the workspace's account (exit {rc}); "
            f"see the output above")


def _run_mods(alias: str, tarball: bytes, log: str) -> None:
    """Run a provisioning playbook over SSH (the driver re-runs its account
    preamble first). Always the whole playbook."""
    rc = _run_driver(alias, tarball, [], log=log)
    if rc != 0:
        die(f"provisioning failed (exit {rc}); see the output above "
            f"or ~/{log} on the box")


def cmd_box_up(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    state = provider.state(name)
    if state is None:
        die(f"box '{name}' is not defined; run `devbox box create` first")
    if state == "running":
        print(f"box '{name}' is already running.")
        return
    print(f"==> starting '{name}' ...")
    provider.start(name)


def cmd_box_stop(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    state = provider.state(name)
    if state is None:
        die(f"box '{name}' is not defined; run `devbox box create` first")
    if state not in ("running",):
        print(f"box '{name}' is already stopped ({state}).")
        return
    attached = _attached_workspaces(provider, name)
    if len(attached) > 1:
        print(f"note: {len(attached)} workspaces are attached ({', '.join(attached)}).")
    print(f"==> stopping '{name}' ...")
    provider.stop(name)


# Where a box records which workspace owns each unprivileged account (written by
# the provisioning driver). The legacy path is read-only, for a box last attached
# by a devbox that called a workspace a project.
MARKERS_DIR = "/etc/devbox/workspaces"
LEGACY_MARKERS_DIR = "/etc/devbox/projects"


def _attached_workspaces(provider, name: str) -> list:
    """The workspaces currently attached to a box, read from the box itself. The
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
         f"cat {MARKERS_DIR}/* {LEGACY_MARKERS_DIR}/* 2>/dev/null"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    # `project=` is the same field under the name it had before an attachment was
    # a workspace; a box migrates its markers the next time devbox attaches to it.
    names = set()
    for line in r.stdout.splitlines():
        field, _, value = line.partition("=")
        if field in ("workspace", "project") and value:
            names.add(value)
    return sorted(names)


def _confirm_destroy(name: str, provider_name: str, assume_yes: bool,
                     attached: list = (), stdin=None) -> bool:
    """Decide whether a destroy may proceed. `destroy` deletes the disk
    irreversibly - and with it every attached workspace's uncommitted work - so
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
        print(f"  {len(attached)} workspace(s) attached, with their checkouts: "
              f"{', '.join(attached)}")
    return input("Type 'yes' to confirm: ").strip() == "yes"


def cmd_box_destroy(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    name, provider_name, provider = _resolve_box(args.name, gc)
    if not _confirm_destroy(name, provider_name, args.yes,
                            _attached_workspaces(provider, name)):
        print(f"Aborted; '{name}' left untouched.")
        return
    provider.destroy(name)
    for conf in SSH_DIR.glob(f"{name}.*.conf"):
        conf.unlink()
    known_hosts_path(name).unlink(missing_ok=True)
    tail = (f" (cached base image left in {gc.cache_dir})"
            if provider_name == "libvirt" else "")
    print(f"Destroyed '{name}'{tail}.")


def cmd_destroy(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    """Destroy this workspace's box - but only when this workspace is the only thing
    on it. A shared box is somebody else's machine too, so the blast radius has
    to be spelled rather than inferred."""
    name, provider_name, provider = _resolve_target(args, workspace, gc)
    attached = _attached_workspaces(provider, name)
    others = [p for p in attached if p != workspace.workspace]
    if others:
        die(f"box '{name}' also hosts {', '.join(others)}. Destroying it would "
            f"take their checkouts with it.\n"
            f"  Give up just this workspace's account:  devbox detach\n"
            f"  Destroy the whole box anyway:         devbox box destroy {name}")
    args = argparse.Namespace(name=name, yes=args.yes)
    cmd_box_destroy(args, gc)


def cmd_ip(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?). Try again shortly.")
    print(ip)


def cmd_box_ssh(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    """The privileged shell - the box's only sudo account. For debugging the
    machine itself; a workspace's own shell is `devbox ssh`."""
    name, _, provider = _resolve_box(args.name, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    _, admin_alias = _refresh_ssh_aliases(name, ip, _admin_user(provider, name))
    os.execvp("ssh", ["ssh", admin_alias, *args.ssh_args])


def cmd_ssh(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    # This workspace's unprivileged account - that's where its checkouts and
    # tooling live. The box's privileged shell is `devbox box ssh <box>`.
    agent_alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), workspace)
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


def cmd_box_status(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    name, _, provider = _resolve_box(args.name, gc)
    _tail_provision_log(provider, name, provision_log())


def _report_failed_units(provider, name: str, workspace: WorkspaceConfig) -> None:
    """This attachment's failed systemd user units, over its own agent alias.

    A provisioning log only covers the run. The generation can leave long-lived
    services behind in the agent's own user manager - the `cloudflared` tunnel
    is one - and lingering keeps them running between logins, so a service that
    cannot start stays broken with nothing in any log to say so. `--failed`
    prints an empty listing when the account is healthy, so this costs a line
    when there is nothing to report."""
    ip = provider.address(name)
    if not ip:
        return
    agent_alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name),
                                          workspace)
    print("--- failed user services")
    subprocess.run([
        "ssh", agent_alias,
        # A box with no systemd has no user manager and so no unit to report.
        "command -v systemctl >/dev/null 2>&1 && "
        "systemctl --user --failed --no-pager || true",
    ], check=False)


def cmd_status(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    name, _, provider = _resolve_target(args, workspace, gc)
    _tail_provision_log(provider, name, provision_log(workspace.agent_user))
    _report_failed_units(provider, name, workspace)


def _auth_failure_message(gc: GlobalConfig, err: Exception) -> Optional[str]:
    """The user-facing line for a provider that can't be reached because its
    credentials are missing or expired, or None when `err` is any other failure
    (which stays fatal)."""
    if isinstance(err, AwsAuthError):
        return str(err)
    if gc.aws is not None and is_auth_error(err):
        return auth_error_message(gc.aws, err)
    return None


def _list_boxes(gc: GlobalConfig) -> tuple:
    """Every provider's boxes as `[(provider, instances)]`, plus the providers
    that could not be reached as `[(name, message)]`.

    A listing is a read-only global view, so credentials that have lapsed cost
    the user the rows they cover, not the whole command: the boxes devbox can
    still see are listed and the rest is reported as a warning. Both AWS-family
    providers share one set of creds, so the first one's failure stands in for
    the others - re-listing them would only re-ask for the same login."""
    listed, unreachable = [], []
    aws_failure = None
    for provider in all_providers(gc):
        if aws_failure is not None and provider.name in AWS_PROVIDERS:
            unreachable.append((provider.name, aws_failure))
            continue
        try:
            listed.append((provider, provider.list()))
        except Exception as err:  # noqa: BLE001 - re-raised unless it's an auth failure
            message = _auth_failure_message(gc, err)
            if message is None:
                raise
            if provider.name in AWS_PROVIDERS:
                aws_failure = message
            unreachable.append((provider.name, message))
    return listed, unreachable


def cmd_box_list(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    # Global view: every devbox-managed box, unioned across providers. EC2 is
    # account-wide; libvirt is this-host-only.
    listed, unreachable = _list_boxes(gc)
    for name, message in unreachable:
        print(f"warning: could not list {name} boxes: {message}")
    boxes = sorted((inst for _, insts in listed for inst in insts),
                   key=lambda i: i.name)
    if not boxes:
        print("No devbox-managed boxes.")
        return
    # Who is attached lives on each box, so it costs an SSH per row - opt-in.
    attached = {}
    if getattr(args, "attached", False):
        for provider, insts in listed:
            for box in insts:
                attached[box.name] = _attached_workspaces(provider, box.name)

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


def _alias_names(box: str, workspace: Optional[WorkspaceConfig] = None) -> tuple:
    """The SSH aliases for a box: (agent, admin). The agent alias is per
    attachment - `devbox-<box>-<workspace>` - because several workspaces now share
    one machine and each needs its own account.

    It collapses to plain `devbox-<box>` when the workspace owns the box (the name
    is the same either way), which is the one-workspace case: that keeps the alias
    every existing jj remote, doc and finger already names."""
    admin = f"devbox-{box}-admin"
    if workspace is None:
        return None, admin
    agent = (f"devbox-{box}" if workspace.workspace == box
             else f"devbox-{box}-{workspace.workspace}")
    return agent, admin


def _write_alias_conf(filename: str, alias: str, box: str, ip: str, user: str) -> None:
    """One alias, alone in its own file under SSH_DIR.

    One stanza per file is what makes attaching safe: a single shared file per
    box would have each workspace's refresh overwrite the last one's account, and
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
                         workspace: Optional[WorkspaceConfig] = None) -> tuple:
    """Write/refresh the SSH aliases for a box (host, user, dedicated
    known-hosts) and return them as (agent_alias, admin_alias); the agent one is
    None when no workspace was given. Provider-agnostic - `ip` is whatever the
    provider resolved (libvirt DHCP lease or EC2 public IP)."""
    agent_alias, admin_alias = _alias_names(box, workspace)
    _write_alias_conf(f"{box}.admin.conf", admin_alias, box, ip, admin_user)
    if workspace is not None:
        _write_alias_conf(f"{box}.{workspace.workspace}.conf", agent_alias, box, ip,
                          workspace.agent_user)

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


def cmd_host(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent_alias, admin_alias = _refresh_ssh_aliases(
        name, ip, _admin_user(provider, name), workspace)
    # The agent alias is the one `jj fetch` uses and the one worth printing; the
    # admin alias is refreshed alongside it and named for completeness.
    print(agent_alias)
    print(f"(privileged: {admin_alias})")


def cmd_herdr(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    """Launch a herdr session against this workspace's account on its box -
    `herdr --remote <alias>`, replacing `herdr --remote $(devbox host)`.

    The agent alias, for the same reason `devbox ssh` uses it: herdr drives a
    shell in the account the checkout and the agents live in, not the box's
    privileged one. Refreshing the alias first is what makes this work after the
    box's address has changed, which is the whole reason the `$(devbox host)`
    form existed."""
    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    agent_alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), workspace)
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
    the repo path can change if the entry's `dir` changes)."""
    listing = subprocess.run(["jj", "git", "remote", "list"], cwd=repo_dir,
                             capture_output=True, text=True)
    if listing.returncode != 0:
        die(f"`jj git remote list` failed in {repo_dir} - is this a jj repo? "
            f"Run `devbox jj fetch` from your workspace's jj checkout (or pass -C).")
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


def _select_repo(workspace: WorkspaceConfig, wanted: Optional[str], start: Path):
    """Which of the workspace's checkouts a repo-scoped command acts on.

    An explicit name wins. Otherwise the cwd decides, by walking up for the repo
    whose host path contains it - which is what makes `devbox jj fetch` work the
    way `jj` itself does, from anywhere inside a checkout. A workspace holding
    exactly one repo needs neither: there is nothing to disambiguate."""
    if not workspace.repos:
        die("this workspace has no [[repos]]; nothing to fetch")
    if wanted:
        repo = workspace.repo(wanted)
        if repo is None:
            die(f"this workspace has no repo named '{wanted}' "
                f"(it has: {', '.join(r.name for r in workspace.repos)})")
        return repo
    cur = start.resolve()
    while True:
        for repo in workspace.repos:
            if repo.host == cur:
                return repo
        if cur.parent == cur:
            break
        cur = cur.parent
    if len(workspace.repos) == 1:
        return workspace.repos[0]
    die(f"{start} is not inside any of this workspace's checkouts "
        f"({', '.join(r.name for r in workspace.repos)}); run this from one of "
        f"them, or name it: `devbox jj fetch <repo>`")


def cmd_jj_fetch(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    # jj git fetch (box->host): pick which checkout this is about, refresh the SSH
    # alias (address via the provider, so this is identical for libvirt or AWS),
    # ensure the local jj remote points at that repo on the box, and fetch.
    start = args.workspace_dir or Path.cwd()
    repo = _select_repo(workspace, args.repo, start)
    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")

    # The agent alias: the checkout is in this workspace's own account, and `jj git
    # fetch` runs git-upload-pack as the login user - there is no sudo-ing through
    # that. An existing remote pointing at an older home is re-pointed by
    # _ensure_jj_remote, so old checkouts self-heal.
    alias, _ = _refresh_ssh_aliases(name, ip, _admin_user(provider, name), workspace)
    remote_url = f"ssh://{alias}/home/{workspace.agent_user}/{repo.dir}"
    _ensure_jj_remote(repo.host, alias, remote_url)

    print(f"==> jj git fetch from '{alias}' ({remote_url}) ...")
    result = subprocess.run(["jj", "git", "fetch", "--remote", alias], cwd=repo.host)
    if result.returncode != 0:
        die("jj git fetch failed")
    print(f"fetched. Check out an agent bookmark with e.g. `jj new <bookmark>@{alias}`.")


def cmd_sync(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    # Reconverge a live box: inject [env] creds (--envvars), run provisioning
    # mods (--mods), and/or push host-local files (--files), over the
    # provider-resolved SSH alias. `devbox create` runs the same steps on a
    # fresh box (see cmd_create).
    do_envvars = args.envvars
    do_mods = args.mods
    do_files = args.files
    if not (do_envvars or do_mods or do_files):
        die("nothing to do: pass --envvars (inject [env] creds), --mods (run "
            "provisioning mods), and/or --files (push host-local [[sync]] files)")
    if do_files and not workspace.sync:
        die("this workspace has no [[sync]]/[[repos.sync]] entries; drop --files")

    name, _, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    admin_user = _admin_user(provider, name)
    agent_alias, admin_alias = _refresh_ssh_aliases(name, ip, admin_user, workspace)

    # Order: env -> mods -> files. Mods may need a cred (e.g. repo-bootstrap's
    # private clone); file dsts (e.g. ~/<repo>/.claude/skills) need the repo clone.
    # env and mods go over the privileged alias (writing another user's home /
    # running the driver both need sudo); files go over this workspace's own.
    tarball = (build_tarball(build_payload(workspace, gc, admin_user))
               if (do_envvars or do_mods) else None)
    if do_envvars:
        # The account's home has to exist before its [env] file can land in it -
        # true on a box this workspace has never attached to, not just a new one.
        _ensure_agent_account(admin_alias, tarball, adopt=args.adopt)
        print(f"==> injecting [env] credentials into '{name}' ...")
        run_envvars(admin_alias, resolve_env(workspace, gc), workspace.agent_user)
    if do_mods:
        print(f"==> provisioning '{name}' over SSH ...")
        _run_mods(admin_alias, tarball, log=provision_log(workspace.agent_user))
    if do_files:
        print(f"==> syncing host-local files to '{name}' ...")
        run_sync(workspace, agent_alias)
    print("done.")


def cmd_box_sync(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    """Reconverge the box's own playbook - the machine, not any attachment."""
    name, _, provider = _resolve_box(args.name, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    admin_user = _admin_user(provider, name)
    _, admin_alias = _refresh_ssh_aliases(name, ip, admin_user)
    print(f"==> provisioning box '{name}' over SSH ...")
    _run_mods(admin_alias, build_tarball(build_box_payload(admin_user, gc)),
              log=provision_log())
    print("done.")


# The nix state one account owns, relative to its home. Every one of these is,
# or holds, a GC root, so the account's whole store closure stays alive for as
# long as they do - and on the nix the box installs (2.35, multi-user) they all
# sit inside the home: a non-root user's profiles live under
# $XDG_STATE_HOME/nix/profiles (`home-manager`, `home-manager-<N>-link` and the
# `profile` that `~/.nix-profile` points at, one link per generation), and
# home-manager 26.05 keeps its "current generation" root in
# $XDG_STATE_HOME/home-manager/gcroots. /nix/var/nix/{profiles,gcroots}/per-user
# is the other place a profile or a root for a named account can sit, so detach
# clears that too rather than trusting the layout to stay put.
AGENT_NIX_STATE = (".local/state/nix/profiles", ".local/state/home-manager",
                   ".nix-profile")


def _detach_script(agent: str, purge: bool) -> str:
    """The root-owned teardown a detach runs on the box, as one shell command.

    The ownership record and the authorized key go first, so a run cut short
    leaves an account nothing can reach rather than a reachable account nothing
    claims.

    IT DROPS THE NIX ROOTS AND STOPS - no `nix-collect-garbage`. Collection is
    the box's policy and the box's policy (mods/nix.sh) is min-free/max-free:
    the daemon reclaims unreferenced paths mid-build, once the disk is actually
    running short. A sweep here would instead spend minutes of a teardown
    discarding byproducts every OTHER workspace on the box is about to reuse, at
    a moment chosen by whoever happened to detach. Dropping the roots is the
    part only detach can do; freeing the space is the daemon's, whenever it
    needs it. Each root is registered with the daemon as an indirect root under
    /nix/var/nix/gcroots/auto, and nix unlinks a stale one itself, so removing
    the paths below is the whole of the job.

    `rm -rf` on a path that is not there succeeds, which is also the whole of
    the "box that never installed nix" case.

    The three directories are read from the environment for the same reason the
    driver reads its two: it is what lets a test point this at a fake box."""
    a = shlex.quote(agent)
    home_state = " ".join(f'"$h"/{p}' for p in AGENT_NIX_STATE)
    parts = [
        "set -e",
        'm="${DEVBOX_WORKSPACES_DIR:-' + MARKERS_DIR + '}"',
        'lm="${DEVBOX_LEGACY_MARKERS_DIR:-' + LEGACY_MARKERS_DIR + '}"',
        'n="${NIX_STATE_DIR:-/nix/var/nix}"',
        f'sudo rm -f "$m"/{a} "$lm"/{a}',
        f'h="$(getent passwd {a} | cut -d: -f6)"',
        '[ -n "$h" ] || exit 0',
        'sudo truncate -s 0 "$h/.ssh/authorized_keys" 2>/dev/null || true',
        'sudo rm -f "$h/.config/devbox/env"',
        f'sudo rm -rf {home_state}',
        f'sudo rm -rf "$n"/profiles/per-user/{a} "$n"/gcroots/per-user/{a}',
        f'sudo usermod --lock --expiredate 1 {a}',
    ]
    if purge:
        # userdel refuses while a session is alive, hence the terminate first.
        parts += [
            f'sudo loginctl terminate-user {a} 2>/dev/null || true',
            f'sudo pkill -KILL -u {a} 2>/dev/null || true',
            "sleep 1",
            f'sudo userdel -r {a}',
        ]
    return "; ".join(parts)


def cmd_detach(args: argparse.Namespace, gc: GlobalConfig, workspace: WorkspaceConfig) -> None:
    """Give up this workspace's account on its box.

    Non-destructive by default: the ownership record, the authorized key, the
    credentials and the account's nix profile go, so nothing can reach the
    account any more and its store closure becomes collectable - but the files,
    and with them any uncommitted work, stay, so re-attaching is instant. The
    generation is not among them: it is derived, and `devbox create` switches
    the account back to one. `--purge` is the one that deletes."""
    name, provider_name, provider = _resolve_target(args, workspace, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    _, admin_alias = _refresh_ssh_aliases(name, ip, _admin_user(provider, name))

    if args.purge and not _confirm_destroy(
            f"{workspace.agent_user}@{name}", provider_name, args.yes):
        print("Aborted; nothing removed.")
        return
    remote = _detach_script(workspace.agent_user, args.purge)
    r = subprocess.run(["ssh", admin_alias, "bash", "-c", shlex.quote(remote)])
    if r.returncode != 0:
        die(f"detaching '{workspace.agent_user}' from '{name}' failed "
            f"(exit {r.returncode})")

    conf = SSH_DIR / f"{name}.{workspace.workspace}.conf"
    conf.unlink(missing_ok=True)
    kept = ("" if args.purge else
            "\nIts files stay; its nix generation is dropped and `devbox create`"
            "\nrebuilds one when it re-attaches.")
    print(f"Detached '{workspace.workspace}' from '{name}'.{kept}")


# The systems flake.nix builds `homeConfigurations.agent-<system>` for, keyed by
# what platform.machine() reports. A check evaluates the host's own: nix can
# evaluate either, but only one of them without a cross-compiling setup.
_NIX_SYSTEMS = {"x86_64": "x86_64-linux", "amd64": "x86_64-linux",
                "aarch64": "aarch64-linux", "arm64": "aarch64-linux"}


def _write_payload(files: list, out_dir: Path) -> None:
    """Lay a payload file list out as a real directory tree."""
    for pf in files:
        path = out_dir / pf.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pf.content)
        path.chmod(pf.mode)


def cmd_nix_check(args: argparse.Namespace, gc: GlobalConfig, workspace=None) -> None:
    """Assemble a payload flake on this host and evaluate it with nix.

    This is the ONLY way to check a module without a box, and it is why the
    check has to assemble: `devbox/nix/` is a skeleton, not a working flake -
    its options.nix imports the `workspace.nix` that only a rendered payload
    has - and a module's source may sit in any of the three mods directories.
    Run inside a workspace, that workspace's own modules are checked too; run
    anywhere else, the global and built-in ones are.

    Evaluating, not building, is the default: forcing the activation package's
    derivation catches every module error the nix module system can catch -
    including a malformed flake.nix, which is why there is nothing to run
    before it - and costs a fraction of realising a closure that holds an
    editor, a browser toolchain and Claude Code. `--build` does the rest."""
    if not shutil.which("nix"):
        die("nix is not on PATH, and `devbox nix check` runs it on this host. "
            "Install nix, or run the check on a box (`devbox ssh`), where the "
            "`nix` mod has installed it.")
    system = _NIX_SYSTEMS.get(platform.machine())
    if system is None:
        die(f"no home-manager configuration for this host's architecture "
            f"({platform.machine()}); the flake builds "
            f"{', '.join(sorted(set(_NIX_SYSTEMS.values())))}.")

    marker = find_workspace_opt(args.workspace_dir or Path.cwd())
    mods_dir = marker.mods_dir if marker is not None else None
    files = nix_check_files(mods_dir)
    modules = nix_module_names(mods_dir)

    with tempfile.TemporaryDirectory(prefix="devbox-nix-check.") as tmp:
        # Absolute, because the `path:` flakeref below is handed to nix as a
        # bare string. `modules/` is cleared first because it is the one
        # directory that accumulates - the flake readDirs it, so a module
        # deleted since the last run into a reused --out-dir would still be
        # discovered, with no flag for it in the fresh workspace.nix. Every
        # other file this writes is overwritten in place, so nothing wider is
        # removed: --out-dir is a directory the caller named.
        out_dir = Path(args.out_dir).resolve() if args.out_dir else Path(tmp)
        shutil.rmtree(out_dir / NIX_PAYLOAD_DIR / "modules", ignore_errors=True)
        _write_payload(files, out_dir)
        flake = f"path:{out_dir / NIX_PAYLOAD_DIR}"
        where = "this workspace, the global config and the built-ins" \
            if mods_dir is not None else "the global config and the built-ins"
        print(f"Checking {len(modules)} modules from {where}, all enabled:")
        print(f"  {', '.join(modules)}")
        if args.out_dir:
            print(f"Assembled flake: {out_dir / NIX_PAYLOAD_DIR}")

        # Forcing the activation package is what evaluates the modules, and a
        # flake too malformed to parse fails here too - `nix flake check` would
        # add nothing, since homeConfigurations is not an output it recognises.
        # `--raw` prints a store path this command has no use for, so only the
        # build step's output is shown.
        #
        # `--refresh` for the same reason `mods/home-manager.sh` passes it:
        # `configenv` is named by branch, and without it nix reuses its cached
        # resolution for up to `tarball-ttl` - so a check run right after a
        # push to that branch would evaluate the old tree and miss an option
        # the push just added.
        attr = f"{flake}#homeConfigurations.agent-{system}.activationPackage"
        steps = [(["eval", "--raw", "--refresh", f"{attr}.drvPath"],
                  f"evaluate agent-{system}", False)]
        if args.build:
            steps.append((["build", "--no-link", "--print-out-paths", "--refresh",
                           attr], f"build agent-{system}", True))
        for argv, label, show in steps:
            cmd = ["nix", "--extra-experimental-features", "nix-command flakes",
                   *argv]
            print(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
            r = subprocess.run(cmd, stdout=None if show else subprocess.DEVNULL)
            if r.returncode != 0:
                die(f"{label} failed (exit {r.returncode}).")
            print(f"ok: {label}", flush=True)
    print("\nnix check passed.")


def _add_box_create_flags(parser: argparse.ArgumentParser) -> None:
    """The shape of a box, as flags. There is no box config file: resources are
    fixed at create and readable from the provider afterwards, so a persisted
    spec could only ever disagree with the machine."""
    parser.add_argument("--provider", choices=PROVIDERS, default=None,
                        help=f"provider to launch on (default: {DEFAULT_PROVIDER})")
    parser.add_argument("--user", default=None,
                        help=f"the box's privileged account "
                             f"(default: {DEFAULT_ADMIN_USER})")
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
                    "workspaces attached to them.")
    # Defined before add_subparsers so they can't be swallowed by ssh's
    # argparse.REMAINDER: `devbox --box foo ssh -- -v`.
    p.add_argument("-C", "--workspace-dir", "--project-dir", type=Path, default=None,
                    help="workspace directory to resolve the devbox marker from (default: cwd)")
    p.add_argument("--box", "--name", dest="box", default=None,
                    help="target a specific box by name (default: the workspace's `box`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- box commands: the machine itself, addressed by name ----------------
    box = sub.add_parser("box", help="create and manage boxes (the VMs workspaces attach to)")
    box_sub = box.add_subparsers(dest="box_cmd", required=True)
    bc = box_sub.add_parser("create", help="download image, define + first-boot a box")
    bc.add_argument("name", help="the box's name (a provider prefix is added for cloud boxes)")
    _add_box_create_flags(bc)
    bc.set_defaults(func=cmd_box_create, needs_workspace=False)
    bl = box_sub.add_parser("list", help="list devbox-managed boxes")
    bl.add_argument("--attached", action="store_true",
                    help="also show which workspaces are attached (one SSH per box)")
    bl.set_defaults(func=cmd_box_list, needs_workspace=False)
    for verb, fn, helptext in (
            ("up", cmd_box_up, "start an already-defined, shut-off box"),
            ("stop", cmd_box_stop, "gracefully shut down a running box"),
            ("status", cmd_box_status, "show cloud-init progress + the box's provision log"),
    ):
        bp = box_sub.add_parser(verb, help=helptext)
        bp.add_argument("name", help="the box's canonical name")
        bp.set_defaults(func=fn, needs_workspace=False)
    bs = box_sub.add_parser("ssh", help="ssh into the box as the privileged user (its only sudo shell)")
    bs.add_argument("name", help="the box's canonical name")
    bs.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    bs.set_defaults(func=cmd_box_ssh, needs_workspace=False)
    bsy = box_sub.add_parser("sync", help="reconverge the box's own playbook")
    bsy.add_argument("name", help="the box's canonical name")
    bsy.add_argument("--mods", action="store_true",
                     help="run the box's mods over SSH")
    bsy.set_defaults(func=cmd_box_sync, needs_workspace=False)
    bd = box_sub.add_parser("destroy", help="tear down the box and its disk")
    bd.add_argument("name", help="the box's canonical name")
    bd.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                    help="skip the confirmation prompt (for scripts)")
    bd.set_defaults(func=cmd_box_destroy, needs_workspace=False)

    # ---- workspace commands: this workspace's account on its box ----------------
    c = sub.add_parser("create", help="ensure this workspace's box exists, then attach to it")
    c.add_argument("--adopt", action="store_true",
                   help="claim an existing account that devbox has no record of")
    _add_box_create_flags(c)
    c.set_defaults(func=cmd_create)
    d = sub.add_parser("destroy", help="tear down this workspace's box (only if nothing else is on it)")
    d.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                   help="skip the confirmation prompt (for scripts)")
    d.set_defaults(func=cmd_destroy)
    dt = sub.add_parser("detach", help="give up this workspace's account on its box")
    dt.add_argument("--purge", action="store_true",
                    help="also delete the account's home directory and its work")
    dt.add_argument("-y", "--yes", "--force", action="store_true", dest="yes",
                    help="skip the confirmation prompt (for scripts)")
    dt.set_defaults(func=cmd_detach)
    sub.add_parser("ip", help="print the box's IPv4").set_defaults(func=cmd_ip)
    s = sub.add_parser("ssh", help="ssh into the box as this workspace's agent user")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    s.set_defaults(func=cmd_ssh)
    sub.add_parser("status", help="show cloud-init progress + this workspace's provision log").set_defaults(func=cmd_status)
    # `jj` groups jj-specific operations; `jj fetch` pulls the account's
    # bookmarked work box->host (the global -C/--box still go before `jj`).
    jj = sub.add_parser("jj", help="jj operations against the box")
    jj_sub = jj.add_subparsers(dest="jj_cmd", required=True)
    jf = jj_sub.add_parser("fetch", help="jj git fetch the account's bookmarked work into the host repo")
    jf.add_argument("repo", nargs="?", default=None,
                    help="which of the workspace's [[repos]] to fetch "
                         "(default: the one the cwd is in)")
    jf.set_defaults(func=cmd_jj_fetch)
    # `sync` reconverges a live attachment: [env] creds, mods, host-local files.
    sy = sub.add_parser("sync", help="reconverge this workspace on its box: --envvars / --mods / --files")
    sy.add_argument("--envvars", action="store_true",
                    help="inject the [env] creds/vars into the account's shell env")
    sy.add_argument("--mods", action="store_true",
                    help="run the workspace's provisioning mods over SSH")
    sy.add_argument("--files", action="store_true",
                    help="push host-local files ([[sync]]/[[repos.sync]]) to the box via rsync")
    sy.add_argument("--adopt", action="store_true",
                    help="claim an existing account that devbox has no record of")
    sy.set_defaults(func=cmd_sync)
    # `nix check` runs on the HOST, not the box: it is the only way to evaluate
    # a module without attaching. It needs no workspace (the global + built-in
    # modules are checkable from anywhere) and picks one up if there is one.
    nx = sub.add_parser("nix", help="the home-manager half, checked on this host")
    nx_sub = nx.add_subparsers(dest="nix_cmd", required=True)
    nc = nx_sub.add_parser("check", help="assemble the payload flake and evaluate it with nix")
    nc.add_argument("--build", action="store_true",
                    help="also realise the activation package, not just evaluate it")
    nc.add_argument("--out-dir", default=None, metavar="DIR",
                    help="keep the assembled flake here instead of a temp dir")
    nc.set_defaults(func=cmd_nix_check, needs_workspace=False)
    sub.add_parser("host", help="refresh the SSH alias used for jj-fetch, print the alias name").set_defaults(func=cmd_host)
    hd = sub.add_parser("herdr", help="launch a herdr session on the box, as this workspace's agent user")
    hd.add_argument("herdr_args", nargs=argparse.REMAINDER, help="extra args passed to herdr (e.g. --session NAME)")
    hd.set_defaults(func=cmd_herdr)
    # `devbox list` stays as the familiar spelling of `devbox box list`.
    ls = sub.add_parser("list", help="list devbox-managed boxes")
    ls.add_argument("--attached", action="store_true",
                    help="also show which workspaces are attached (one SSH per box)")
    ls.set_defaults(func=cmd_box_list, needs_workspace=False)

    args = p.parse_args()
    global_config = GlobalConfig.load()
    # Most commands operate within a workspace; `list` is global and skips it.
    workspace = None
    if getattr(args, "needs_workspace", True):
        workspace = resolve_workspace(args.workspace_dir or Path.cwd(), global_config)
    _run(args, global_config, workspace)


def _run(args: argparse.Namespace, gc: GlobalConfig, workspace: Optional[WorkspaceConfig]) -> None:
    """Dispatch, turning AWS credential failures into one-line errors instead of
    a boto stack trace. Credentials that can be checked up front are handled at
    client construction (providers/awsauth.py), which offers to log in and then
    carries on; what reaches here is either that path giving up (AwsAuthError,
    message already written) or a token that only the server rejected - too late
    to resume, so we say what to run."""
    try:
        args.func(args, gc, workspace)
    except AwsAuthError as err:
        die(str(err))
    except Exception as err:  # noqa: BLE001 - re-raised unless it's an auth failure
        if gc.aws is not None and is_auth_error(err):
            die(auth_error_message(gc.aws, err))
        raise


if __name__ == "__main__":
    main()
