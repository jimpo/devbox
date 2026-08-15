"""Host->box file sync: push host-local, non-VCS-tracked files (agent skills,
git-excluded project files) to a devbox, re-runnably, over rsync.

A mod runs *on the box* and can't read host-local files, so host-only files
fundamentally need a host-side push - distinct from mods (which handle repo- or
GitHub-sourced files). Config is `[[sync]]` (host = $HOME/~/absolute-anchored)
and `[[repo.sync]]` (repo-relative: host = the marker dir, guest = ~/<repo.dir>);
see config.SyncEntry.

Delete semantics - **mirror per synced unit**: an entry with `include` expands
to one rsync per named child (`src/<name> -> dst/<name>`), each mirrored
(`--delete`) within its own subtree; without `include`, one mirror of
`src -> dst`. This keeps `include = ["jujutsu"]` from `--delete`-ing
box-installed skills outside the include list. Host wins (mirror deletes
host-removed files within a synced subtree).
"""
from __future__ import annotations

import dataclasses
import posixpath
import shlex
import subprocess
from pathlib import Path

from .config import ProjectConfig
from .provision import env_for_owner, render_env_file
from .util import die

# The box-side, devbox-managed 0600 env file, one per account, in that account's
# home. Holds the account's slice of the effective [env] map as `export KEY=val`
# lines; sourced from ~/.bash_env (the BASH_ENV target, what non-interactive bash
# reads) and ~/.zshenv (interactive zsh) so the creds reach both an agent's Bash
# tool and a human at a shell.
#
# Each entry lands in exactly one of the two files, per its `owner` (see
# config.EnvVar): the agent's by default, the privileged user's for a cred that
# only a `# runas: admin|root` mod needs. Homes are 0750 and the files 0600, so
# the agent cannot read the admin's; the privileged side can read both.
GUEST_ENV_REL = ".config/devbox/env"
_ENV_SOURCE_LINE = '[ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"'


def run_envvars(alias: str, env: dict, agent_user: str) -> None:
    """Write the box's devbox-managed [env] files (0600) - the agent's and the
    privileged user's, each from its owner's slice of `env` - and ensure each
    ~/.zshenv sources its own (~/.bash_env is wired by the provisioning driver).
    Both are rewritten in full every run, so a key dropped from config, or moved
    to the other owner, is dropped from the account that had it. Content is piped
    over stdin (never echoed to a log, never in argv).

    `alias` is the PRIVILEGED user's alias: it owns one of the files, and reaches
    the other's home with sudo. The agent's home must already exist - `devbox
    create` and `devbox sync` run the driver's `--ensure-users` preamble first."""
    user = shlex.quote(agent_user)
    home = shlex.quote(f"/home/{agent_user}")
    line = shlex.quote(_ENV_SOURCE_LINE)
    _write_env_file(alias, env_for_owner(env, "agent"), agent_user, (
        f'sudo install -d -o {user} -g {user} -m 0700 {home}/.config/devbox; '
        # /dev/stdin is the piped content: the creds never touch a command line.
        f'sudo install -o {user} -g {user} -m 0600 /dev/stdin {home}/.config/devbox/env; '
        f'sudo touch {home}/.zshenv; sudo chown {user}:{user} {home}/.zshenv; '
        f'sudo grep -qxF {line} {home}/.zshenv || '
        f'printf "%s\\n" {line} | sudo tee -a {home}/.zshenv >/dev/null'
    ))
    # The privileged user is the SSH user here, writing its own home: no sudo.
    _write_env_file(alias, env_for_owner(env, "admin"), "the privileged user", (
        'install -d -m 0700 "$HOME/.config/devbox"; '
        'install -m 0600 /dev/stdin "$HOME/.config/devbox/env"; '
        'touch "$HOME/.zshenv"; '
        f'grep -qxF {line} "$HOME/.zshenv" || '
        f'printf "%s\\n" {line} >> "$HOME/.zshenv"'
    ))


def _write_env_file(alias: str, env: dict, owner: str, remote: str) -> None:
    """Render one account's slice and pipe it to `remote` - a bash snippet that
    reads the file from /dev/stdin - over SSH."""
    result = subprocess.run(
        ["ssh", alias, "bash", "-c", shlex.quote(f"set -e; {remote}")],
        input=render_env_file(env).encode())
    if result.returncode != 0:
        die(f"writing the [env] file of {owner} failed (exit {result.returncode})")


@dataclasses.dataclass
class RsyncJob:
    """One resolved mirror unit: `host_path` -> `guest_path`. `mirror` (a
    directory) syncs with `--delete`; a file copies without it."""
    host_path: str
    guest_path: str
    mirror: bool


def _expand(path: str, home: str) -> str:
    """Resolve a leading `~`/`$HOME` against `home` (per side - host home when
    reading, guest home when writing); absolute paths pass through. Only
    `~`/`$HOME` are supported (no arbitrary env vars - guest-side expansion
    would require running a shell on the box)."""
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + path[1:]
    if path == "$HOME":
        return home
    if path.startswith("$HOME/"):
        return home + path[len("$HOME"):]
    return path


def _job(host_src: str, guest_dst: str) -> RsyncJob:
    src = Path(host_src)
    if not src.exists():
        die(f"devbox sync: src not found on the host: {host_src}")
    return RsyncJob(host_src, guest_dst, mirror=src.is_dir())


def plan_sync(project: ProjectConfig, host_home: str = None, guest_home: str = None) -> list:
    """Resolve the project's sync entries into concrete rsync jobs (one per
    mirror unit). `host_home`/`guest_home` are injectable for tests; they default
    to the host's real home and the box's `/home/<agent_user>` - synced files are
    agent skills, project config and the like, which belong to the account that
    does the work, not the privileged one."""
    host_home = host_home if host_home is not None else str(Path.home())
    guest_home = guest_home if guest_home is not None else f"/home/{project.agent_user}"
    jobs = []
    for entry in project.sync:
        if entry.repo_relative:
            host_src = posixpath.join(str(project.marker_dir), entry.src)
            guest_dst = posixpath.join(guest_home, project.repo.dir, entry.dst)
        else:
            host_src = _expand(entry.src, host_home)
            guest_dst = _expand(entry.dst, guest_home)
        if entry.include:
            # Each included child is its own mirror unit (see module docstring).
            for name in entry.include:
                jobs.append(_job(posixpath.join(host_src, name),
                                 posixpath.join(guest_dst, name)))
        else:
            jobs.append(_job(host_src, guest_dst))
    return jobs


def _rsync_argv(job: RsyncJob, alias: str) -> list:
    # Trailing slash on a directory source mirrors its *contents* into the dest
    # dir; --mkpath creates missing dest path components (no separate ssh mkdir).
    slash = "/" if job.mirror else ""
    src = job.host_path + slash
    dst = f"{alias}:{job.guest_path}{slash}"
    argv = ["rsync", "-az", "--mkpath"]
    if job.mirror:
        argv.append("--delete")
    argv += [src, dst]
    return argv


def run_sync(project: ProjectConfig, alias: str,
             host_home: str = None, guest_home: str = None) -> None:
    """rsync each mirror unit to the box through the `devbox-<name>` SSH alias
    (which carries the user, host, and known-hosts). Idempotent + incremental."""
    jobs = plan_sync(project, host_home=host_home, guest_home=guest_home)
    for job in jobs:
        arrow = "mirror" if job.mirror else "copy"
        print(f"==> sync ({arrow}) {job.host_path} -> {job.guest_path}")
        result = subprocess.run(_rsync_argv(job, alias))
        if result.returncode != 0:
            die(f"rsync failed for {job.host_path} (exit {result.returncode})")
