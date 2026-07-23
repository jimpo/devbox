"""Command-line entry point and subcommand handlers. Everything here is
provider-agnostic: commands resolve config + target, then act through the
Provider seam.

    devbox create              # download image, define + first-boot the VM
    devbox up / stop           # start / gracefully shut down a defined VM
    devbox ip                  # print the VM's IPv4
    devbox ssh [args...]       # ssh into the VM
    devbox status              # cloud-init progress + tail provision.log
    devbox provision [mods...] # re-run provisioning mods over SSH (reconverge)
    devbox list                # list devbox-managed instances on this host
    devbox jj fetch            # jj git fetch the instance's work into the host repo
    devbox sync                # rsync host-local files ([[sync]]) onto the box
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cloudconfig import render_cloud_config
from .config import (DEFAULT_PROVIDER, GlobalConfig, KNOWN_HOSTS_PATH, PROVIDERS,
                     ProjectConfig, SSH_DIR, SSH_USER_CONFIG, compose_instance_name,
                     find_project_dir, parse_instance_name, resolve_instance,
                     resolve_project)
from .provision import PROVISION_LOG, build_payload, build_tarball
from .sync import run_sync
from .providers import InstanceSpec, all_providers, get_provider
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
    (the bare-create default) - so `devbox ip --name aws-binius-arm` queries AWS
    even from a libvirt-default project, routed offline from the name alone."""
    name = resolve_instance(args, project)
    provider_name, _ = parse_instance_name(name)
    return name, provider_name, get_provider(provider_name, gc)


def _check_name_unique(gc: GlobalConfig, name: str) -> None:
    """Instance names are globally unique across providers - enforced at create
    by checking the union `list`. A provider that can't be listed (e.g. AWS creds
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
        # A project literally named e.g. 'aws-foo' would make the bare libvirt
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
        # carries one would double it (e.g. aws-aws-foo).
        if parse_instance_name(bare)[0] != "libvirt":
            die(f"--name '{bare}' must not start with a provider prefix "
                f"('{provider_name}-' is added automatically); "
                f"pass --name {parse_instance_name(bare)[1]}")

    # The canonical name (provider-prefixed for cloud, bare for libvirt) is the
    # instance's identity: stored on the instance and used by every later command.
    name = compose_instance_name(provider_name, bare)
    _check_name_unique(gc, name)

    aws_cfg = project.aws
    if aws_cfg is not None and (args.spot or args.on_demand):
        aws_cfg = dataclasses.replace(aws_cfg, spot=bool(args.spot))

    spec = InstanceSpec(
        name=name,
        project=project.project,
        provider=provider_name,
        disk_gb=project.disk_gb,
        user_data=render_cloud_config(project, gc, instance_name=name),
        created_at=datetime.now(timezone.utc).isoformat(),
        vcpus=project.vcpus,
        memory_mib=project.memory_mib,
        aws=aws_cfg,
    )
    provider.create(spec)

    truncate_known_hosts()

    d = args.project_dir or "."
    target = f"devbox -C {d}" + (f" --name {name}" if provider_name != "libvirt" else "")

    # With [[sync]] entries, wait for the box to finish provisioning (the sync
    # dsts, e.g. ~/<repo>/.claude/skills, need the repo clone in place) and then
    # push host-local files. Without any, create stays fire-and-forget.
    if project.sync:
        alias = _wait_until_provisioned(provider, name, project.user, gc)
        print("==> syncing host-local files onto the box ...")
        run_sync(project, alias)
        print(f"\n==> '{name}' is ready and synced.\n\n  SSH:  {target} ssh\n")
        return

    console = (f"\n  Console:   virsh -c {gc.libvirt_uri} console {name}   # ctrl-] to exit"
               if provider_name == "libvirt" else "")
    print(f"""
==> '{name}' is booting. cloud-init provisioning runs on first boot (several minutes).

  IP:        {target} ip
  SSH:       {target} ssh
  Progress:  {target} status{console}
""")


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


def _wait_until_provisioned(provider, name: str, user: str, gc: GlobalConfig) -> str:
    """Block until a freshly-created box is reachable and cloud-init has finished
    provisioning, then return its refreshed `devbox-<name>` SSH alias. Used by
    `create` before an auto-sync (the sync dsts depend on provisioning). Times
    out with a clear message pointing at `devbox status`."""
    print("==> waiting for the instance to get an address ...")
    ip = _poll(lambda: provider.address(name), timeout=300)
    if not ip:
        die(f"timed out waiting for '{name}' to get an address; check `devbox status`")
    alias = _refresh_ssh_alias(name, ip, user)

    print("==> waiting for SSH ...")
    if not _poll(lambda: _ssh_ok(alias), timeout=300):
        die(f"timed out waiting for SSH to '{name}'; check `devbox status`")

    print("==> waiting for cloud-init provisioning to finish (several minutes) ...")
    # `cloud-init status --wait` blocks until done: exit 0 = done, 2 = done with
    # recoverable warnings (degraded); anything else is a real failure.
    r = subprocess.run(["ssh", alias, "cloud-init status --wait"])
    if r.returncode not in (0, 2):
        die(f"cloud-init provisioning did not finish cleanly on '{name}' "
            f"(exit {r.returncode}); check `devbox status`")
    return alias


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


def cmd_destroy(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, provider_name, provider = _resolve_target(args, project, gc)
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
    os.execvp("ssh", ["ssh", f"{project.user}@{ip}", *args.ssh_args])


def cmd_status(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    subprocess.run([
        "ssh", f"{project.user}@{ip}",
        "cloud-init status --long; echo ---; tail -n 80 ~/provision.log 2>/dev/null",
    ], check=False)


def cmd_provision(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    state = provider.state(name)
    if state is None:
        die(f"instance '{name}' is not defined; run `devbox create` first")
    if state != "running":
        die(f"instance '{name}' is not running ({state}); run `devbox up` first")
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")

    tarball = build_tarball(build_payload(project, gc))
    scope = f" (mods: {', '.join(args.mods)})" if args.mods else ""
    print(f"==> re-running provisioning on '{name}' ({ip}){scope} ...")
    # Pipe the payload tarball over stdin into a per-run temp dir (extracted as
    # the SSH user, so the 0600 secret.env is user-owned - no chown needed), then
    # run the driver, tee'ing to ~/provision.log (the log `devbox status` tails).
    # Positional mod names select a subset; none => the whole playbook.
    selected = " ".join(shlex.quote(m) for m in args.mods)
    remote = (
        'set -e; d="$(mktemp -d)"; tar -xzf - -C "$d"; '
        f'bash "$d/provision-driver.sh" {selected} 2>&1 | tee "$HOME/{PROVISION_LOG}"; '
        'rc=${PIPESTATUS[0]}; rm -rf "$d"; exit "$rc"'
    )
    result = subprocess.run(
        ["ssh", f"{project.user}@{ip}", "bash", "-c", shlex.quote(remote)],
        input=tarball,
    )
    if result.returncode != 0:
        die(f"provisioning failed (exit {result.returncode}); see the output above "
            f"or ~/{PROVISION_LOG} on the box")


def cmd_list(args: argparse.Namespace, gc: GlobalConfig, project: Optional[ProjectConfig]) -> None:
    # Global view: every devbox-managed instance, unioned across providers.
    # AWS is account-wide; libvirt is this-host-only.
    instances = [inst for provider in all_providers(gc) for inst in provider.list()]
    instances.sort(key=lambda i: i.name)
    if not instances:
        print("No devbox-managed instances.")
        return
    rows = [("NAME", "PROVIDER", "PROJECT", "STATE", "RESOURCES", "UP")]
    for i in instances:
        up = "—"
        if i.state == "running":
            up = _format_age(i.created_at)
            if i.cost_hint:
                up += " ($)"  # this instance is costing money while running
        rows.append((i.name, i.provider, i.project or "—", i.state, i.resources, up))
    _print_table(rows)


def _refresh_ssh_alias(name: str, ip: str, user: str) -> str:
    """Write/refresh the `devbox-<name>` SSH alias (host, user, dedicated
    known-hosts) that `devbox jj fetch` addresses the instance through, and return it.
    Provider-agnostic - `ip` is whatever the provider resolved (libvirt DHCP
    lease or EC2 public IP)."""
    alias = f"devbox-{name}"
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_PATH.is_file():
        KNOWN_HOSTS_PATH.write_text("")
        KNOWN_HOSTS_PATH.chmod(0o600)

    host_conf = SSH_DIR / f"{name}.conf"
    host_conf.write_text(
        f"Host {alias}\n"
        f"    HostName {ip}\n"
        f"    User {user}\n"
        f"    UserKnownHostsFile {KNOWN_HOSTS_PATH}\n"
        f"    StrictHostKeyChecking accept-new\n"
    )

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

    return alias


def cmd_host(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    print(_refresh_ssh_alias(name, ip, project.user))


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

    alias = _refresh_ssh_alias(name, ip, project.user)
    remote_url = f"ssh://{alias}/home/{project.user}/{project.repo.dir}"
    repo_dir = find_project_dir(args.project_dir or Path.cwd())
    _ensure_jj_remote(repo_dir, alias, remote_url)

    print(f"==> jj git fetch from '{alias}' ({remote_url}) ...")
    result = subprocess.run(["jj", "git", "fetch", "--remote", alias], cwd=repo_dir)
    if result.returncode != 0:
        die("jj git fetch failed")
    print(f"fetched. Check out an agent bookmark with e.g. `jj new <bookmark>@{alias}`.")


def cmd_sync(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    # Host->box file push (non-VCS files, e.g. host-local agent skills). rsync
    # over the provider-resolved SSH alias; re-runnable and incremental. Auto-run
    # as the last step of `devbox create` too (see cmd_create).
    if not project.sync:
        die("this project has no [[sync]]/[[repo.sync]] entries; nothing to sync")
    name, _, provider = _resolve_target(args, project, gc)
    ip = provider.address(name)
    if not ip:
        die(f"no address for '{name}' yet (still booting?).")
    alias = _refresh_ssh_alias(name, ip, project.user)
    run_sync(project, alias)
    print("sync complete.")


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
    market.add_argument("--spot", action="store_true", help="AWS: force a spot instance")
    market.add_argument("--on-demand", action="store_true", help="AWS: force an on-demand instance")
    c.set_defaults(func=cmd_create)
    sub.add_parser("up", help="start an already-defined, shut-off VM").set_defaults(func=cmd_up)
    sub.add_parser("stop", help="gracefully shut down a running VM").set_defaults(func=cmd_stop)
    sub.add_parser("destroy", help="tear down the VM and its overlay disk").set_defaults(func=cmd_destroy)
    sub.add_parser("ip", help="print the VM's IPv4").set_defaults(func=cmd_ip)
    s = sub.add_parser("ssh", help="ssh into the VM")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    s.set_defaults(func=cmd_ssh)
    sub.add_parser("status", help="show cloud-init progress + provision log").set_defaults(func=cmd_status)
    pr = sub.add_parser("provision", help="re-run provisioning mods over SSH (reconverge a running VM)")
    pr.add_argument("mods", nargs="*", help="run only these mods (default: the whole playbook)")
    pr.set_defaults(func=cmd_provision)
    # Global view - doesn't resolve a project (works anywhere).
    sub.add_parser("list", help="list devbox-managed instances").set_defaults(func=cmd_list, needs_project=False)
    # `jj` groups jj-specific operations; `jj fetch` pulls the instance's
    # bookmarked work box->host (the global -C/--name still go before `jj`).
    jj = sub.add_parser("jj", help="jj operations against the instance")
    jj_sub = jj.add_subparsers(dest="jj_cmd", required=True)
    jj_sub.add_parser("fetch", help="jj git fetch the instance's bookmarked work into the host repo").set_defaults(func=cmd_jj_fetch)
    sub.add_parser("sync", help="push host-local files ([[sync]]/[[repo.sync]]) to the box via rsync").set_defaults(func=cmd_sync)
    sub.add_parser("host", help="refresh the SSH alias used for jj-fetch, print the alias name").set_defaults(func=cmd_host)

    args = p.parse_args()
    global_config = GlobalConfig.load()
    # Most commands operate within a project; `list` is global and skips it.
    project = None
    if getattr(args, "needs_project", True):
        project = resolve_project(args.project_dir or Path.cwd(), global_config)
    args.func(args, global_config, project)


if __name__ == "__main__":
    main()
