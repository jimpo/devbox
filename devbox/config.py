"""Global + per-project configuration, marker resolution, and instance targeting.

Provider-agnostic. `GlobalConfig` is `~/.config/devbox/config.toml`;
`ProjectConfig` is a project's `.devbox/config.toml` ("defaults for machines
launched from here", not "my one VM"). `resolve_instance` picks which instance
a command targets.
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path
from typing import Optional

from .util import die, warn

PROJECT_MARKER_DIR = ".devbox"
PROJECT_CONFIG_FILE = "config.toml"                # .devbox/config.toml
PROJECT_CLOUD_CONFIG_FILE = "cloud-config.yaml"    # .devbox/cloud-config.yaml (optional)
REPO_MODES = ("none", "clone", "push")

# Providers a project can launch instances on. Validated against `provider` in a
# project's config and the `--provider` flag.
DEFAULT_PROVIDER = "libvirt"
PROVIDERS = ("libvirt", "ec2")

# Providers whose instances carry a "<provider>-" prefix in their canonical
# name. libvirt (the default) stays bare - local is the common case, and
# "bare = the project's libvirt default" is worth keeping. The prefix lets every
# per-instance command derive the provider from the name alone (offline string
# routing), instead of an API call per `ssh`/`ip`/... to find the owner.
PREFIXED_PROVIDERS = tuple(p for p in PROVIDERS if p != DEFAULT_PROVIDER)

GLOBAL_CONFIG_PATH = Path.home() / ".config" / "devbox" / "config.toml"
SSH_DIR = Path.home() / ".config" / "devbox" / "ssh"
KNOWN_HOSTS_PATH = SSH_DIR / "known_hosts"
SSH_USER_CONFIG = Path.home() / ".ssh" / "config"

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
    # [env]: credentials/vars injected into every box's shell env (see sync.py).
    # A project's [env] overrides per-key.
    env: dict = dataclasses.field(default_factory=dict)
    aws: Optional[AwsConfig] = None      # shared AWS account creds
    ec2: Optional[Ec2Config] = None      # EC2-specific globals

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
            # sensible guess for a person's identity. Validated lazily inside
            # render_cloud_config, not here, so subcommands that never render
            # cloud-config (ip/ssh/status/...) don't need [jj] set at all.
            jj_name=jj.get("name"),
            jj_email=jj.get("email"),
            aws=_load_aws_config(data.get("aws"), path),
            ec2=_load_ec2_config(data.get("ec2"), path),
        )


def _resolve_env(data: dict, config_path: Path) -> dict:
    """Parse `[env]` - a flat table of KEY = "value" credentials/vars injected
    into a box's shell environment (see sync.py). All values must be strings."""
    env = data.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        die(f"{config_path}: [env] must be a table of string values "
            f"(e.g. GITHUB_TOKEN = \"github_pat_...\")")
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
class Ec2ProjectConfig:
    """Per-project `[ec2]` launch defaults (cfleet's "instance template")."""
    instance_type: str
    architecture: str = "x86_64"           # "x86_64" | "arm64"
    ami: Optional[str] = None              # explicit AMI id; else looked up
    ami_name_filter: Optional[str] = None  # name glob for the AMI lookup
    ami_owner: str = DEFAULT_AMI_OWNER
    spot: Optional[bool] = None            # overrides global [ec2].spot
    interruption_behavior: str = "stop"    # spot only: stop | terminate | hibernate


@dataclasses.dataclass
class ProjectConfig:
    project: str                  # label/tag on instances (default: marker dir basename)
    provider: str                 # default provider for a bare `create` ("libvirt")
    cloud_config: Optional[Path]  # .devbox/cloud-config.yaml, if present (optional)
    user: str
    vcpus: int
    memory_mib: int
    disk_gb: int
    repo: RepoConfig
    marker_dir: Path              # the project's marker directory (holds .devbox/)
    # Ordered provisioning mods appended after the built-in base playbook (see
    # provision.py). Resolved by name project > global > built-in.
    mods: list = dataclasses.field(default_factory=list)
    # Host->box file-sync rules from [[sync]] / [[repo.sync]] (see sync.py).
    sync: list = dataclasses.field(default_factory=list)
    # Project-level [env]: shell-env creds/vars injected into the box, overriding
    # the global [env] per-key (see sync.py). .devbox/config.toml is git-excluded,
    # so hardcoding scoped creds here is acceptable.
    env: dict = dataclasses.field(default_factory=dict)
    ec2: Optional[Ec2ProjectConfig] = None


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


def _resolve_ec2_project_config(data: dict, config_path: Path) -> Optional[Ec2ProjectConfig]:
    ec2_data = data.get("ec2")
    if ec2_data is None:
        return None
    instance_type = ec2_data.get("instance_type")
    if not instance_type:
        die(f"{config_path}: [ec2] requires 'instance_type'")
    arch = ec2_data.get("architecture", "x86_64")
    if arch not in ("x86_64", "arm64"):
        die(f"{config_path}: [ec2] architecture must be 'x86_64' or 'arm64' (got {arch!r})")
    return Ec2ProjectConfig(
        instance_type=instance_type,
        architecture=arch,
        ami=ec2_data.get("ami"),
        ami_name_filter=ec2_data.get("ami_name_filter"),
        ami_owner=ec2_data.get("ami_owner", DEFAULT_AMI_OWNER),
        spot=ec2_data.get("spot"),
        interruption_behavior=ec2_data.get("interruption_behavior", "stop"),
    )


def resolve_project(start: Path, global_config: GlobalConfig) -> ProjectConfig:
    marker_dir = find_project_dir(start)
    devbox_dir = marker_dir / PROJECT_MARKER_DIR
    config_path = devbox_dir / PROJECT_CONFIG_FILE
    data = _load_toml(config_path)

    # `name` used to be "the VM"; an instance is no longer a project (a project
    # can now have several). Fail loudly rather than silently ignoring a stale
    # field so the mismatch is obvious.
    if "name" in data:
        die(f"{config_path}: 'name' is no longer a project field - an instance is "
            f"not a project. Remove it; the project defaults to the directory name "
            f"('{marker_dir.name}', override with 'project = ...'). Address a specific "
            f"instance with --name.")

    _warn_unknown_keys(
        data,
        known_top={"project", "provider", "user", "vcpus", "memory_mib", "disk_gb", "mods"},
        known_sections={
            "repo": {"mode", "dir", "url", "sync"},
            "sync": _ANY_KEYS,  # array-of-tables, validated by _parse_sync_entry
            "env": _ANY_KEYS,   # arbitrary user-defined credentials/vars
            "ec2": {"instance_type", "architecture", "ami", "ami_name_filter",
                    "ami_owner", "spot", "interruption_behavior"},
        },
        config_path=config_path,
    )

    provider = data.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        die(f"{config_path}: provider must be one of {PROVIDERS} (got {provider!r})")

    cloud_config_path = devbox_dir / PROJECT_CLOUD_CONFIG_FILE
    repo = _resolve_repo_config(data, config_path)
    return ProjectConfig(
        project=data.get("project", marker_dir.name),
        provider=provider,
        cloud_config=cloud_config_path if cloud_config_path.is_file() else None,
        user=data.get("user", os.environ.get("USER", "")),
        vcpus=data.get("vcpus", global_config.vcpus),
        memory_mib=data.get("memory_mib", global_config.memory_mib),
        disk_gb=data.get("disk_gb", global_config.disk_gb),
        repo=repo,
        marker_dir=marker_dir,
        mods=_resolve_mods(data, config_path),
        sync=_resolve_sync(data, config_path, repo),
        env=_resolve_env(data, config_path),
        ec2=_resolve_ec2_project_config(data, config_path),
    )


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
    """Which instance a command targets, as its canonical name. Explicit --name
    wins (the full canonical name, e.g. `ec2-binius-arm`); otherwise the
    project's bare-named libvirt default (= the project name). Cloud instances
    are always named, so they only ever match via --name."""
    return args.name or project.project
