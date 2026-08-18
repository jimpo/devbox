"""Global + per-project configuration, marker resolution, and box targeting.

Provider-agnostic. `GlobalConfig` is `~/.config/devbox/config.toml`;
`ProjectConfig` is a project's `.devbox/config.toml`.

**A box is not a project.** A box is a VM - provider, resources, image, and one
privileged account - and several projects can share one. A project is content:
its unprivileged account, its checkout, its creds, its mods. So a project's
config carries no hardware at all: a box's shape is given as CLI flags to
`devbox box create` (`BoxSpec`), and the box itself is the record of what it is
(provider tags / libvirt <metadata>), since hardware can't change after create
and the live values are readable from the provider anyway.

A project says which box it lives on with `box = "<name>"`, defaulting to its
own name - which is why the one-box-per-project case still needs no config at
all. `resolve_instance` picks which box a command targets.
"""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path
from typing import Optional

from .util import die, warn

PROJECT_MARKER_DIR = ".devbox"
PROJECT_CONFIG_FILE = "config.toml"                # .devbox/config.toml
REPO_MODES = ("none", "clone", "push")

# Providers a project can launch instances on. Validated against `provider` in a
# project's config and the `--provider` flag.
DEFAULT_PROVIDER = "libvirt"
PROVIDERS = ("libvirt", "ec2", "aws")

# Providers whose instances carry a "<provider>-" prefix in their canonical
# name. libvirt (the default) stays bare - local is the common case, and
# "bare = the project's libvirt default" is worth keeping. The prefix lets every
# per-instance command derive the provider from the name alone (offline string
# routing), instead of an API call per `ssh`/`ip`/... to find the owner.
PREFIXED_PROVIDERS = tuple(p for p in PROVIDERS if p != DEFAULT_PROVIDER)

# A box has a privileged account (`admin_user`, the SSH target devbox
# provisions through, sudo-enabled; a box property, set at `devbox box create`)
# and one unprivileged `agent_user` per attached project, which that project's
# AI agents and repo checkout live under. `agent_user` defaults to the project
# name, which is a directory basename by default - so it is validated here
# rather than trusted, since plenty of legal project names ('MyProject',
# 'foo.bar', '2fa-lib') are not legal Linux usernames. Failing at config-resolve
# time costs a second; failing in `useradd` costs a boot.
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Usernames the agent account must never take: standard Debian/Ubuntu system
# accounts, plus the default login user of the cloud images the providers boot
# (`ubuntu` on Ubuntu cloud images and Lightsail blueprints, `ec2-user` on
# Amazon Linux). Taking one of these would either collide with an existing
# sudo-capable account or fight the distro over a system uid.
RESERVED_USERNAMES = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd-network", "systemd-resolve", "messagebus", "sshd",
    "ubuntu", "admin", "ec2-user",
})

# The accounts an `[env]` entry can belong to. There is no "root" owner: a
# `runas = ["root"]` mod reads the privileged account's file (see
# provision-driver.sh), so root is covered by "admin".
ENV_OWNERS = ("agent", "admin")
DEFAULT_ENV_OWNER = "agent"

GLOBAL_CONFIG_PATH = Path.home() / ".config" / "devbox" / "config.toml"
SSH_DIR = Path.home() / ".config" / "devbox" / "ssh"
SSH_USER_CONFIG = Path.home() / ".ssh" / "config"


def known_hosts_path(box: str) -> Path:
    """The known-hosts file for one box. Per box, not shared: several projects
    now point at the same machine, and creating a *different* box must not
    invalidate this one's pinned key for all of them."""
    return SSH_DIR / f"known_hosts.{box}"


# Built-in fallbacks for any key not set in the (optional) global config, so
# `devbox` works with zero global setup. Override via ~/.config/devbox/config.toml.
DEFAULT_LIBVIRT_URI = "qemu:///system"
DEFAULT_NETWORK = "default"
DEFAULT_CACHE_DIR = "/var/tmp/devbox"
DEFAULT_BASE_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/releases/26.04/release/"
    "ubuntu-26.04-server-cloudimg-amd64.img"
)
DEFAULT_VCPUS = 6
DEFAULT_MEMORY_MIB = 12288
DEFAULT_DISK_GB = 60
DEFAULT_SSH_PUBKEY_PATH = "~/.ssh/id_ed25519.pub"

# EC2 AMI defaults (Canonical's Ubuntu cloud images). A project's [ec2] section
# can override the name glob / owner, or pin an explicit `ami`.
DEFAULT_AMI_OWNER = "099720109477"  # Canonical
DEFAULT_AMI_NAME_FILTER = {
    "x86_64": "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-*",
    "arm64": "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-arm64-server-*",
}

# Lightsail (the `aws` provider) defaults. Lightsail sells fixed bundles rather
# than an instance type + EBS size, so the bundle picks CPU/RAM/disk together:
# the default is the 16 GB / 4 vCPU / 320 GB tier (~$84/mo). Lightsail's Linux
# lineup is x86-only - there are no ARM/Graviton bundles - so there is no
# architecture knob; a project's [lightsail] section can pin a different
# `bundle` (or a different `blueprint`).
DEFAULT_LIGHTSAIL_BUNDLE = "xlarge_3_0"        # 16 GB / 4 vCPU / 320 GB
# The newest Ubuntu LTS blueprint Lightsail offers - older than the 26.04
# userland the libvirt/ec2 providers boot, which pull Ubuntu cloud images.
DEFAULT_LIGHTSAIL_BLUEPRINT = "ubuntu_24_04"


# Sentinel for a known section whose inner keys are user-defined (`[env]`) or
# validated by their own parser (`[[sync]]`): mark the section known, but skip
# checking the keys inside it.
_ANY_KEYS = None


def _warn_unknown_keys(data: dict, known_top, known_sections: dict, config_path: Path) -> None:
    """Warn (non-fatally) on config keys devbox doesn't recognize, so a typo or
    misplaced setting doesn't silently no-op. Forward-compat by design: an older
    devbox reading a newer config should keep working, not die.

    `known_top` is the set of accepted top-level scalar keys; `known_sections`
    maps a known section name to its set of accepted keys (or `_ANY_KEYS` to skip
    inner validation). An unknown section name is itself an unknown top-level key.
    """
    allowed_top = set(known_top) | set(known_sections)
    for key, value in data.items():
        if key not in allowed_top:
            warn(f"{config_path}: unknown key '{key}' (ignored)")
            continue
        allowed = known_sections.get(key, _ANY_KEYS)
        if allowed is _ANY_KEYS or not isinstance(value, dict):
            continue  # scalar top-level key, or a section we don't key-check
        for subkey in value:
            if subkey not in allowed:
                warn(f"{config_path}: unknown key '{key}.{subkey}' (ignored)")


# ---- global config ----------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class EnvVar:
    """One `[env]` entry: the value, and the account whose shell env it lands in.

    `owner` picks exactly one account - `owner = "admin"` puts a credential in
    the privileged user's environment and keeps it out of the agent's, which is
    what makes it usable by a privileged mod (`runas = ["root"]`/`["admin"]`) without
    handing it to the agents. The plain `KEY = "value"` form means the agent."""
    value: str
    owner: str = DEFAULT_ENV_OWNER


@dataclasses.dataclass
class AwsConfig:
    """Global `[aws]` section - the shared AWS account creds, used by every
    AWS-family provider (ec2 today). One configured target (multi-region /
    multi-profile deferred). Absent section => no AWS provider available."""
    region: str
    profile: Optional[str] = None


@dataclasses.dataclass
class Ec2Config:
    """Global `[ec2]` section - EC2-specific launch defaults (the account creds
    live in `[aws]`). Absent section => empty lists / spot off."""
    security_groups: list = dataclasses.field(default_factory=list)
    # Candidate subnets, tried in order; each pins the AZ. Empty => AWS chooses.
    subnets: list = dataclasses.field(default_factory=list)
    spot: bool = False  # default market when a project doesn't say otherwise


@dataclasses.dataclass
class GlobalConfig:
    libvirt_uri: str
    network: str
    cache_dir: Path
    base_image_url: str
    vcpus: int
    memory_mib: int
    disk_gb: int
    ssh_pubkey_path: Path
    jj_name: Optional[str]
    jj_email: Optional[str]
    # [env]: {KEY: EnvVar} credentials/vars injected into the shell env of one of
    # a box's two accounts (see sync.py). A project's [env] overrides per-key.
    env: dict = dataclasses.field(default_factory=dict)
    aws: Optional[AwsConfig] = None      # shared AWS account creds
    ec2: Optional[Ec2Config] = None      # EC2-specific globals
    # [[sync]]: default host->box file-sync rules (see sync.py), applied ahead of
    # every project's own [[sync]] - the $HOME/~/absolute-anchored shape only,
    # since there is no repo to anchor [[repo.sync]] against at this scope.
    sync: list = dataclasses.field(default_factory=list)

    @classmethod
    def load(cls, path: Path = GLOBAL_CONFIG_PATH) -> "GlobalConfig":
        data: dict = {}
        if path.is_file():
            with path.open("rb") as f:
                data = tomllib.load(f)
        _warn_unknown_keys(
            data,
            known_top={"libvirt_uri", "network", "cache_dir"},
            known_sections={
                "base_image": {"url"},
                "defaults": {"vcpus", "memory_mib", "disk_gb"},
                "ssh": {"pubkey_path"},
                "jj": {"name", "email"},
                "env": _ANY_KEYS,  # arbitrary user-defined credentials/vars
                "aws": {"region", "profile"},
                "ec2": {"default_security_groups", "default_subnets", "spot"},
                "sync": _ANY_KEYS,  # array-of-tables, validated by _parse_sync_entry
            },
            config_path=path,
        )
        base_image = data.get("base_image", {})
        defaults = data.get("defaults", {})
        ssh = data.get("ssh", {})
        jj = data.get("jj", {})
        return cls(
            libvirt_uri=data.get("libvirt_uri", DEFAULT_LIBVIRT_URI),
            network=data.get("network", DEFAULT_NETWORK),
            cache_dir=Path(data.get("cache_dir", DEFAULT_CACHE_DIR)).expanduser(),
            base_image_url=base_image.get("url", DEFAULT_BASE_IMAGE_URL),
            vcpus=defaults.get("vcpus", DEFAULT_VCPUS),
            memory_mib=defaults.get("memory_mib", DEFAULT_MEMORY_MIB),
            disk_gb=defaults.get("disk_gb", DEFAULT_DISK_GB),
            ssh_pubkey_path=Path(ssh.get("pubkey_path", DEFAULT_SSH_PUBKEY_PATH)).expanduser(),
            env=_resolve_env(data, path),
            # No built-in default, unlike every other field above - there's no
            # sensible guess for a person's identity. Validated lazily when the
            # provisioning payload is built (provision.resolve_jj_identity), not
            # here, so subcommands that never provision (ip/ssh/status/...)
            # don't need [jj] set at all.
            jj_name=jj.get("name"),
            jj_email=jj.get("email"),
            aws=_load_aws_config(data.get("aws"), path),
            ec2=_load_ec2_config(data.get("ec2"), path),
            sync=_resolve_global_sync(data, path),
        )

    def read_ssh_pubkey(self) -> str:
        """The host's SSH public key, as a single line. Installed twice on a box:
        into the privileged user's authorized_keys at first boot (initscript.py)
        and into the agent user's by the provisioning driver (provision.py puts
        it in the payload's context). Read through one place so both paths fail
        the same way when it's missing."""
        if not self.ssh_pubkey_path.is_file():
            die(f"SSH public key not found: {self.ssh_pubkey_path}")
        return self.ssh_pubkey_path.read_text().strip()


def _resolve_env(data: dict, config_path: Path) -> dict:
    """Parse `[env]` - credentials/vars injected into a box's shell environment
    (see sync.py) - into `{KEY: EnvVar}`. An entry is either a bare string (the
    agent's environment) or a table `{ value = "...", owner = "admin" }`. A
    project's `[env]` overrides the global one per key, entry for whole entry:
    re-declaring a key replaces its owner along with its value."""
    raw = data.get("env", {})
    if not isinstance(raw, dict):
        die(f"{config_path}: [env] must be a table "
            f"(e.g. GITHUB_TOKEN = \"github_pat_...\")")
    env = {}
    for key, spec in raw.items():
        if isinstance(spec, str):
            env[key] = EnvVar(spec)
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("value"), str):
            die(f"{config_path}: [env] '{key}' must be a string, or a table with a "
                f"string `value` (e.g. {key} = {{ value = \"...\", owner = \"admin\" }})")
        unknown = sorted(set(spec) - {"value", "owner"})
        if unknown:
            die(f"{config_path}: [env] '{key}' has unknown key(s) {', '.join(unknown)} - "
                f"an entry takes `value` and `owner` "
                f"(one of: {', '.join(ENV_OWNERS)})")
        owner = spec.get("owner", DEFAULT_ENV_OWNER)
        if owner not in ENV_OWNERS:
            die(f"{config_path}: [env] '{key}' has an unknown owner '{owner}' "
                f"(one of: {', '.join(ENV_OWNERS)}). A `runas = [\"root\"]` mod reads "
                f"the admin's environment.")
        env[key] = EnvVar(spec["value"], owner)
    return env


def _load_aws_config(aws_data: Optional[dict], path: Path) -> Optional[AwsConfig]:
    if aws_data is None:
        return None
    region = aws_data.get("region")
    if not region:
        die(f"{path}: [aws] requires 'region'")
    return AwsConfig(region=region, profile=aws_data.get("profile"))


def _load_ec2_config(ec2_data: Optional[dict], path: Path) -> Optional[Ec2Config]:
    if ec2_data is None:
        return None
    return Ec2Config(
        security_groups=ec2_data.get("default_security_groups", []),
        subnets=ec2_data.get("default_subnets", []),
        spot=ec2_data.get("spot", False),
    )


def _resolve_global_sync(data: dict, path: Path) -> list:
    """Parse the global `[[sync]]` - the same $HOME/~/absolute-anchored shape a
    project's own `[[sync]]` uses (see config.SyncEntry), applied ahead of every
    project's own entries. There is no global `[[repo.sync]]`: that shape anchors
    at a project's own checkout, which doesn't exist at this scope."""
    return [_parse_sync_entry(raw, path, repo_relative=False) for raw in data.get("sync", [])]


# ---- project config / resolution --------------------------------------------
@dataclasses.dataclass
class RepoConfig:
    mode: str                    # "none" | "clone" | "push"
    dir: str                     # "" when mode == "none"
    url: Optional[str]           # set only when mode == "clone"


@dataclasses.dataclass
class SyncEntry:
    """One host->box file-sync rule (see sync.py). `src`/`dst` are `$HOME`/`~`/
    absolute-anchored for `[[sync]]`, or repo-relative for `[[repo.sync]]`
    (host = the marker dir, guest = ~/<repo.dir>). `include` narrows to named
    children of `src` (each mirrored in its own subtree); empty = the whole
    `src`. `dst` defaults to `src`."""
    src: str
    dst: str
    include: list                # child names; empty = whole src
    repo_relative: bool          # True for [[repo.sync]], False for [[sync]]


@dataclasses.dataclass
class Ec2BoxConfig:
    """EC2 launch settings for one box, from `devbox box create --instance-type
    ...`. Not persisted: hardware is fixed at create, and the live values are
    readable from EC2 afterwards."""
    instance_type: str
    architecture: str = "x86_64"           # "x86_64" | "arm64"
    ami: Optional[str] = None              # explicit AMI id; else looked up
    ami_name_filter: Optional[str] = None  # name glob for the AMI lookup
    ami_owner: str = DEFAULT_AMI_OWNER
    spot: Optional[bool] = None            # overrides global [ec2].spot
    interruption_behavior: str = "stop"    # spot only: stop | terminate | hibernate


@dataclasses.dataclass
class LightsailBoxConfig:
    """Lightsail launch settings for one box (the `aws` provider). Every field is
    optional: `devbox box create foo --provider aws` with no other flags launches
    on pure defaults.

    There is no disk knob: a Lightsail bundle fixes CPU/RAM/disk together, so
    `--disk` is ignored on this provider. There is no architecture knob either:
    Lightsail's Linux bundles are x86-only.
    """
    # Explicit Lightsail bundle id; when unset, DEFAULT_LIGHTSAIL_BUNDLE
    # (xlarge_3_0 = 16 GB / 4 vCPU / 320 GB).
    bundle: Optional[str] = None
    blueprint: str = DEFAULT_LIGHTSAIL_BLUEPRINT   # OS image id
    # When unset, the provider defaults to "<region>a" (the region is only known
    # there, from the shared [aws] creds).
    availability_zone: Optional[str] = None


@dataclasses.dataclass
class BoxSpec:
    """One box's shape, assembled from `devbox box create` flags and the global
    `[defaults]`. Deliberately NOT persisted host-side: resources are immutable
    after create, and what a box *is* can be read back from the provider. The
    one fact that can't be (the privileged account, needed to build the SSH alias
    before we can log in and ask) is stamped onto the instance instead."""
    name: str                     # canonical instance name (bare, or <provider>-<n>)
    provider: str
    admin_user: str               # the box's PRIVILEGED account (sudo)
    vcpus: int
    memory_mib: int
    disk_gb: int
    ec2: Optional[Ec2BoxConfig] = None
    lightsail: Optional[LightsailBoxConfig] = None


@dataclasses.dataclass
class ProjectConfig:
    """A project: content, not hardware. Everything here describes what gets set
    up inside one unprivileged account on a box - never the box itself."""
    project: str                  # the project's name (default: marker dir basename)
    # The box this project attaches to, as a canonical instance name. Defaults to
    # the project's own name, which is what keeps the one-box-per-project case
    # working with no configuration at all.
    box: str
    # The unprivileged account this project's agents, checkout and creds live
    # under, created by the provisioning driver at attach. One per project; the
    # box's privileged account is a box property, not a project one.
    agent_user: str
    repo: RepoConfig
    marker_dir: Path              # the project's marker directory (holds .devbox/)
    # Ordered provisioning mods appended after the built-in base playbook (see
    # provision.py). Resolved by name project > global > built-in.
    mods: list = dataclasses.field(default_factory=list)
    # Host->box file-sync rules: the global [[sync]] defaults (see
    # GlobalConfig.sync), followed by this project's own [[sync]] / [[repo.sync]]
    # (see sync.py).
    sync: list = dataclasses.field(default_factory=list)
    # Project-level [env]: {KEY: EnvVar} shell-env creds/vars injected into the
    # box, overriding the global [env] per-key (see sync.py). .devbox/config.toml
    # is git-excluded, so hardcoding scoped creds here is acceptable.
    env: dict = dataclasses.field(default_factory=dict)


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def find_project_dir(start: Path) -> Path:
    """Walk upward from `start` (like git looking for .git) for a devbox marker
    (a directory containing `.devbox/config.toml`)."""
    cur = start.resolve()
    while True:
        if (cur / PROJECT_MARKER_DIR / PROJECT_CONFIG_FILE).is_file():
            return cur
        if cur.parent == cur:
            die(f"no {PROJECT_MARKER_DIR}/{PROJECT_CONFIG_FILE} found in {start} or any "
                f"parent directory (pass -C/--project-dir, or add a marker file to the project)")
        cur = cur.parent


def _resolve_repo_config(data: dict, config_path: Path) -> RepoConfig:
    repo_data = data.get("repo", {})
    mode = repo_data.get("mode", "none")
    if mode not in REPO_MODES:
        die(f"{config_path}: [repo] mode must be one of {REPO_MODES} (got {mode!r})")
    repo_dir = repo_data.get("dir", "")
    if mode != "none" and not repo_dir:
        die(f"{config_path}: [repo] dir is required when mode = \"{mode}\"")
    url = repo_data.get("url")
    if mode == "clone" and not url:
        die(f"{config_path}: [repo] url is required when mode = \"clone\"")
    if mode != "clone" and url:
        die(f"{config_path}: [repo] url is only used when mode = \"clone\" (mode is \"{mode}\")")
    return RepoConfig(mode=mode, dir=repo_dir, url=url)


def _path_anchored(path: str) -> bool:
    """A path anchored at $HOME/~/absolute (vs. a bare relative path)."""
    return (path == "~" or path.startswith("~/")
            or path == "$HOME" or path.startswith("$HOME/")
            or path.startswith("/"))


def _parse_sync_entry(raw: dict, config_path: Path, repo_relative: bool) -> SyncEntry:
    table = "[[repo.sync]]" if repo_relative else "[[sync]]"
    if not isinstance(raw, dict):
        die(f"{config_path}: each {table} entry must be a table with a `src` key")
    src = raw.get("src")
    if not isinstance(src, str) or not src:
        die(f"{config_path}: {table} requires a non-empty string `src`")
    dst = raw.get("dst", src)
    if not isinstance(dst, str) or not dst:
        die(f"{config_path}: {table} `dst` must be a non-empty string (defaults to `src`)")
    include = raw.get("include", [])
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        die(f"{config_path}: {table} `include` must be a list of child names (strings)")
    # [[sync]] paths are $HOME/~/absolute-anchored (resolved per side); [[repo.sync]]
    # paths are repo-relative. Reject the wrong shape so anchoring is unambiguous.
    for label, value in (("src", src), ("dst", dst)):
        if repo_relative and _path_anchored(value):
            die(f"{config_path}: [[repo.sync]] {label} must be repo-relative, not "
                f"$HOME/~/absolute (got {value!r}); use [[sync]] for anchored paths")
        if not repo_relative and not _path_anchored(value):
            die(f"{config_path}: [[sync]] {label} must be anchored at $HOME, ~, or / "
                f"(got {value!r}); use [[repo.sync]] for repo-relative paths")
    return SyncEntry(src=src, dst=dst, include=include, repo_relative=repo_relative)


def _resolve_sync(data: dict, config_path: Path, repo: RepoConfig) -> list:
    entries = [_parse_sync_entry(raw, config_path, repo_relative=False)
               for raw in data.get("sync", [])]
    repo_syncs = data.get("repo", {}).get("sync", [])
    if repo_syncs and (repo.mode == "none" or not repo.dir):
        die(f"{config_path}: [[repo.sync]] needs a [repo] with mode != \"none\" "
            f"(paths anchor at the repo checkout ~/<repo.dir>)")
    entries += [_parse_sync_entry(raw, config_path, repo_relative=True) for raw in repo_syncs]
    return entries


# Keys that used to describe a project's one VM and are now box-level, given as
# flags to `devbox box create`. Kept as hard errors rather than ignored warnings:
# silently dropping `vcpus = 16` would boot the wrong-sized box.
_BOX_LEVEL_KEYS = {
    "provider": "--provider",
    "user": "--user",
    "vcpus": "--vcpus",
    "memory_mib": "--memory",
    "disk_gb": "--disk",
    "ec2": "--instance-type/--arch/--ami/--spot",
    "lightsail": "--bundle/--blueprint/--zone",
}


def resolve_project(start: Path, global_config: GlobalConfig) -> ProjectConfig:
    marker_dir = find_project_dir(start)
    devbox_dir = marker_dir / PROJECT_MARKER_DIR
    config_path = devbox_dir / PROJECT_CONFIG_FILE
    data = _load_toml(config_path)

    # `name` used to be "the VM"; a box is not a project (a box now hosts as many
    # projects as attach to it). Fail loudly rather than silently ignoring a
    # stale field so the mismatch is obvious.
    if "name" in data:
        die(f"{config_path}: 'name' is no longer a project field - a box is not a "
            f"project. Remove it; the project defaults to the directory name "
            f"('{marker_dir.name}', override with 'project = ...'). Say which box "
            f"this project lives on with 'box = ...'.")

    stale = sorted(k for k in _BOX_LEVEL_KEYS if k in data)
    if stale:
        flags = ", ".join(f"'{k}' ({_BOX_LEVEL_KEYS[k]})" for k in stale)
        die(f"{config_path}: {flags} describe a BOX, not a project - a box is "
            f"created independently and can host several projects. Remove "
            f"{'them' if len(stale) > 1 else 'it'} and pass the flag to "
            f"`devbox box create` instead; point this project at the result with "
            f"'box = \"<name>\"'.")

    _warn_unknown_keys(
        data,
        known_top={"project", "box", "agent_user", "mods"},
        known_sections={
            "repo": {"mode", "dir", "url", "sync"},
            "sync": _ANY_KEYS,  # array-of-tables, validated by _parse_sync_entry
            "env": _ANY_KEYS,   # arbitrary user-defined credentials/vars
        },
        config_path=config_path,
    )

    repo = _resolve_repo_config(data, config_path)
    project_name = data.get("project", marker_dir.name)
    return ProjectConfig(
        project=project_name,
        box=_resolve_box_name(data, project_name, config_path),
        agent_user=_resolve_agent_user(data, project_name, config_path),
        repo=repo,
        marker_dir=marker_dir,
        mods=_resolve_mods(data, config_path),
        sync=[*global_config.sync, *_resolve_sync(data, config_path, repo)],
        env=_resolve_env(data, config_path),
    )


def _resolve_box_name(data: dict, project_name: str, config_path: Path) -> str:
    """The canonical name of the box this project attaches to. Defaults to the
    project's own name, so a project that never mentions a box gets one of its
    own - today's single-box behavior, unchanged."""
    box = data.get("box", project_name)
    if not isinstance(box, str) or not box:
        die(f"{config_path}: box must be a non-empty instance name "
            f"(e.g. box = \"dev\" or box = \"ec2-dev\")")
    origin = ("box" if "box" in data else
              f"the project name {project_name!r}, which box defaults to")
    _validate_instance_name(box, f"{config_path}: {origin}")
    return box


def _validate_instance_name(name: str, origin: str) -> None:
    """A box's name has to survive the round trip through
    compose/parse_instance_name, which is how every command routes to a provider
    offline. A name like 'ec2-foo' on libvirt would misparse as another
    provider's, and a name that already carries its own provider prefix would
    have it doubled."""
    head, sep, _ = name.partition("-")
    if sep and head in PREFIXED_PROVIDERS:
        return  # already canonical for that provider
    if name in PREFIXED_PROVIDERS or any(c.isspace() for c in name) or "/" in name:
        die(f"{origin}: {name!r} is not a usable box name")


def _resolve_agent_user(data: dict, project_name: str, config_path: Path) -> str:
    """The unprivileged account this project occupies on its box. Defaults to the
    project name, and is validated here so an illegal name fails in a second
    rather than at `useradd` time, a boot later. It cannot be checked against the
    box's privileged account until attach time - that account is a property of
    the box, not of this file."""
    agent_user = data.get("agent_user", project_name)

    if not USERNAME_RE.match(agent_user):
        # The overwhelmingly common cause is the default: a project name (i.e. a
        # directory basename) that is not a legal username. Say so.
        origin = ("agent_user" if "agent_user" in data else
                  f"the project name {project_name!r}, which agent_user defaults to")
        die(f"{config_path}: agent_user {agent_user!r} is not a valid Linux username "
            f"(lowercase letters/digits/'-'/'_', not starting with a digit, "
            f"32 chars max) - it comes from {origin}; set `agent_user = \"...\"`")
    if agent_user in RESERVED_USERNAMES:
        die(f"{config_path}: agent_user {agent_user!r} is a reserved system or "
            f"cloud-image account; set `agent_user = \"...\"` to something else")
    return agent_user


def _resolve_mods(data: dict, config_path: Path) -> list:
    """Parse `mods` - an ordered list of mod names appended to the built-in base
    playbook. (The escape hatch for dropping/reordering the base - a
    `mods = { base = false, steps = [...] }` table - is a later addition.)"""
    mods = data.get("mods", [])
    if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
        die(f"{config_path}: `mods` must be a list of mod names (strings), "
            f"e.g. mods = [\"rust\", \"wasm-tools\"]")
    return mods


def parse_instance_name(name: str) -> tuple:
    """Derive (provider, rest) from a canonical instance name. Non-libvirt
    instances are named "<provider>-<rest>"; a bare name is a libvirt instance
    (the project's default). Split on the first "-", treating the left segment as
    the provider only when it's a known prefixed-provider keyword. This is pure
    string routing - no API call or credential load - so per-instance commands
    (ssh/ip/status/...) pick the right backend offline. Inverse of
    compose_instance_name."""
    head, sep, rest = name.partition("-")
    if sep and head in PREFIXED_PROVIDERS:
        return head, rest
    return DEFAULT_PROVIDER, name


def compose_instance_name(provider: str, name: str) -> str:
    """The canonical instance name to store at create: bare for libvirt, else
    "<provider>-<name>". Inverse of parse_instance_name."""
    return name if provider == DEFAULT_PROVIDER else f"{provider}-{name}"


def resolve_instance(args, project: ProjectConfig) -> str:
    """Which box a command targets, as its canonical name. Explicit --box wins
    (the full canonical name, e.g. `ec2-dev`), then the project's `box` key,
    then - for a project that has never mentioned a box - its own name."""
    return getattr(args, "box", None) or project.box


def resolve_box_spec(args, gc: GlobalConfig, bare_name: str) -> BoxSpec:
    """The box `devbox box create` is about to launch, from its flags and the
    global `[defaults]`. `bare_name` is the name without a provider prefix; the
    canonical name is composed here.

    This is the only place a box's shape is assembled, and it lives nowhere
    afterwards: hardware is immutable once the instance exists, so re-deriving it
    from a config file would only ever risk disagreeing with the machine."""
    provider = args.provider or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        die(f"provider must be one of {PROVIDERS} (got {provider!r})")
    _validate_instance_name(bare_name, "box name")
    if parse_instance_name(bare_name)[0] != DEFAULT_PROVIDER:
        die(f"box name '{bare_name}' must not start with a provider prefix "
            f"('{provider}-' is added automatically); "
            f"pass the name as '{parse_instance_name(bare_name)[1]}'")

    admin_user = args.user or os.environ.get("USER", "")
    if not USERNAME_RE.match(admin_user):
        die(f"the box's privileged user {admin_user!r} is not a valid Linux "
            f"username (lowercase letters/digits/'-'/'_', not starting with a "
            f"digit, 32 chars max); pass --user")

    return BoxSpec(
        name=compose_instance_name(provider, bare_name),
        provider=provider,
        admin_user=admin_user,
        vcpus=args.vcpus or gc.vcpus,
        memory_mib=args.memory or gc.memory_mib,
        disk_gb=args.disk or gc.disk_gb,
        ec2=_ec2_box_config(args, provider),
        lightsail=_lightsail_box_config(args, provider),
    )


def _ec2_box_config(args, provider: str) -> Optional[Ec2BoxConfig]:
    if provider != "ec2":
        return None
    if not args.instance_type:
        die("EC2 boxes need an instance type: pass --instance-type "
            "(e.g. `devbox box create dev --provider ec2 --instance-type m7g.xlarge`)")
    arch = args.arch or "x86_64"
    if arch not in ("x86_64", "arm64"):
        die(f"--arch must be 'x86_64' or 'arm64' (got {arch!r})")
    return Ec2BoxConfig(
        instance_type=args.instance_type,
        architecture=arch,
        ami=args.ami,
        ami_owner=DEFAULT_AMI_OWNER,
        # --spot/--on-demand are mutually exclusive at the parser; neither given
        # leaves it None, which defers to the global [ec2].spot default.
        spot=True if args.spot else (False if args.on_demand else None),
    )


def _lightsail_box_config(args, provider: str) -> Optional[LightsailBoxConfig]:
    if provider != "aws":
        return None
    return LightsailBoxConfig(
        bundle=args.bundle,
        blueprint=args.blueprint or DEFAULT_LIGHTSAIL_BLUEPRINT,
        availability_zone=args.zone,
    )
