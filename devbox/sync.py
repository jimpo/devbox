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
import subprocess
from pathlib import Path

from .config import ProjectConfig
from .util import die


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
    to the host's real home and the box's `/home/<user>`."""
    host_home = host_home if host_home is not None else str(Path.home())
    guest_home = guest_home if guest_home is not None else f"/home/{project.user}"
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
