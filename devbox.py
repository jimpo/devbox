"""devbox: a global, multi-project dev-sandbox VM manager (libvirt + cloud-init).

Each project supplies an optional cloud-config fragment via a `.devbox/`
marker directory (see README.md), merged onto devbox's own common
cloud-config template (dotfiles, jj, Claude Code, base packages); all
projects share one cached base VM image and one global config at
~/.config/devbox/config.toml.

`create` uses virt-install --cloud-init to build/attach a NoCloud seed from
the merged cloud-config, so cloud-init provisions the box on first boot.
`up`/`stop` only start/shut down an already-defined domain.

Requirements (Debian/Ubuntu/Pop!_OS):
    sudo apt install virtinst libvirt-daemon-system qemu-system-x86
Run as a user in the `libvirt` group (so qemu:///system works without sudo),
or invoke with sudo.

    devbox create              # download image (cached), define + boot the VM
    devbox up / stop           # start / gracefully shut down a defined VM
    devbox ip                  # print the VM's IPv4
    devbox ssh [args...]       # ssh into the VM
    devbox status              # cloud-init progress + tail provision.log
    devbox host                # refresh the SSH alias used for jj-sync
    devbox destroy             # tear down, removing the overlay disk
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import yaml

PROJECT_MARKER_DIR = ".devbox"
PROJECT_CONFIG_FILE = "config.toml"                # .devbox/config.toml
PROJECT_CLOUD_CONFIG_FILE = "cloud-config.yaml"    # .devbox/cloud-config.yaml (optional)
COMMON_CLOUD_CONFIG_PATH = Path(__file__).parent / "common-cloud-config.yaml"
REPO_MODES = ("none", "clone", "push")

GLOBAL_CONFIG_PATH = Path.home() / ".config" / "devbox" / "config.toml"
SSH_DIR = Path.home() / ".config" / "devbox" / "ssh"
KNOWN_HOSTS_PATH = SSH_DIR / "known_hosts"
SSH_USER_CONFIG = Path.home() / ".ssh" / "config"

# Canonical's cloud-image signing key, installed by the `ubuntu-cloudimage-keyring`
# apt package. Used to verify the SHA256SUMS file fetched alongside the base
# image, instead of pinning a checksum in devbox itself: the `release` build at
# a given URL is mutable (Canonical overwrites it in place on respins), so a
# hand-pinned hash inevitably goes stale.
CLOUDIMAGE_KEYRING_PATH = Path("/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg")

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


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---- global config ----------------------------------------------------------
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
    github_token: Optional[str] = None

    @classmethod
    def load(cls, path: Path = GLOBAL_CONFIG_PATH) -> "GlobalConfig":
        data: dict = {}
        if path.is_file():
            with path.open("rb") as f:
                data = tomllib.load(f)
        base_image = data.get("base_image", {})
        defaults = data.get("defaults", {})
        ssh = data.get("ssh", {})
        jj = data.get("jj", {})
        github = data.get("github", {})
        return cls(
            libvirt_uri=data.get("libvirt_uri", DEFAULT_LIBVIRT_URI),
            network=data.get("network", DEFAULT_NETWORK),
            cache_dir=Path(data.get("cache_dir", DEFAULT_CACHE_DIR)).expanduser(),
            base_image_url=base_image.get("url", DEFAULT_BASE_IMAGE_URL),
            vcpus=defaults.get("vcpus", DEFAULT_VCPUS),
            memory_mib=defaults.get("memory_mib", DEFAULT_MEMORY_MIB),
            disk_gb=defaults.get("disk_gb", DEFAULT_DISK_GB),
            ssh_pubkey_path=Path(ssh.get("pubkey_path", DEFAULT_SSH_PUBKEY_PATH)).expanduser(),
            # A fine-grained PAT the user scopes themselves. Left unset, the
            # sandbox just gets no GitHub HTTPS auth (fine for public repos).
            github_token=github.get("token"),
            # No built-in default, unlike every other field above - there's no
            # sensible guess for a person's identity. Validated lazily inside
            # render_cloud_config, not here, so subcommands that never render
            # cloud-config (ip/ssh/status/...) don't need [jj] set at all.
            jj_name=jj.get("name"),
            jj_email=jj.get("email"),
        )


# ---- project config / resolution --------------------------------------------
@dataclasses.dataclass
class RepoConfig:
    mode: str                    # "none" | "clone" | "push"
    dir: str                     # "" when mode == "none"
    url: Optional[str]           # set only when mode == "clone"


@dataclasses.dataclass
class ProjectConfig:
    name: str
    cloud_config: Optional[Path]  # .devbox/cloud-config.yaml, if present (now optional)
    user: str
    vcpus: int
    memory_mib: int
    disk_gb: int
    repo: RepoConfig


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


def resolve_project(start: Path, global_config: GlobalConfig) -> ProjectConfig:
    marker_dir = find_project_dir(start)
    devbox_dir = marker_dir / PROJECT_MARKER_DIR
    config_path = devbox_dir / PROJECT_CONFIG_FILE
    data = _load_toml(config_path)

    if "name" not in data:
        die(f"{config_path}: missing required field 'name'")

    cloud_config_path = devbox_dir / PROJECT_CLOUD_CONFIG_FILE
    return ProjectConfig(
        name=data["name"],
        cloud_config=cloud_config_path if cloud_config_path.is_file() else None,
        user=data.get("user", os.environ.get("USER", "")),
        vcpus=data.get("vcpus", global_config.vcpus),
        memory_mib=data.get("memory_mib", global_config.memory_mib),
        disk_gb=data.get("disk_gb", global_config.disk_gb),
        repo=_resolve_repo_config(data, config_path),
    )


# ---- libvirt helpers ---------------------------------------------------------
def virsh(uri: str, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["virsh", "-c", uri, *args], check=check, capture_output=capture, text=True)


def domain_state(uri: str, name: str) -> Optional[str]:
    """libvirt domain state (e.g. 'running', 'shut off'), or None if undefined."""
    r = virsh(uri, "domstate", name, check=False, capture=True)
    return r.stdout.strip() if r.returncode == 0 else None


def get_ip(uri: str, name: str) -> Optional[str]:
    """First IPv4 from libvirt's DHCP lease for the domain, or None."""
    out = virsh(uri, "domifaddr", name, "--source", "lease", check=False, capture=True).stdout or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "ipv4":
            return parts[3].split("/")[0]
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_verified_checksum(image_url: str) -> str:
    """Fetch SHA256SUMS + SHA256SUMS.gpg from alongside `image_url`, verify the
    signature against Canonical's cloud-image signing key, and return the
    checksum for the image's filename."""
    if not shutil.which("gpgv"):
        die("'gpgv' not found. Install: sudo apt install gnupg")
    if not CLOUDIMAGE_KEYRING_PATH.is_file():
        die(f"{CLOUDIMAGE_KEYRING_PATH} not found. Install: sudo apt install ubuntu-cloudimage-keyring")

    filename = Path(image_url).name
    sums_url = urllib.parse.urljoin(image_url, "SHA256SUMS")
    sig_url = urllib.parse.urljoin(image_url, "SHA256SUMS.gpg")
    with urllib.request.urlopen(sums_url) as resp:
        sums = resp.read()
    with urllib.request.urlopen(sig_url) as resp:
        sig = resp.read()

    with tempfile.TemporaryDirectory(prefix="devbox-checksums-") as tmpdir:
        sums_path = Path(tmpdir) / "SHA256SUMS"
        sig_path = Path(tmpdir) / "SHA256SUMS.gpg"
        sums_path.write_bytes(sums)
        sig_path.write_bytes(sig)
        r = subprocess.run(
            ["gpgv", "--keyring", str(CLOUDIMAGE_KEYRING_PATH), str(sig_path), str(sums_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            die(f"GPG verification of {sums_url} failed:\n{r.stderr}")

    for line in sums.decode().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0]
    die(f"{filename} not listed in {sums_url}")


# ---- cloud-config templating ---------------------------------------------------
# Cloud-config keys whose values get concatenated (common-first) rather than
# whole-value-overridden when merging the common template with a project's
# fragment. Order matters: common's write_files/runcmd install jj, dotfiles,
# and ~/.local/bin before any project-specific step that might assume they
# already exist.
LIST_MERGE_KEYS = {"packages", "write_files", "runcmd", "bootcmd", "users", "groups"}


def substitute_tokens(text: str, tokens: dict) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax and with valid YAML flow-mapping syntax)."""
    for key, value in tokens.items():
        text = text.replace(f"%{key}%", value)
    return text


def _load_cloud_config_yaml(text: str, source: Path) -> dict:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        die(f"{source}: expected a YAML mapping at the top level (cloud-config format)")
    return data


def merge_cloud_config(common: dict, project: dict) -> dict:
    """`common` provisions first; `project` layers on top. Keys in
    LIST_MERGE_KEYS are concatenated common-then-project; every other
    top-level key is a whole-value override (project replaces common if
    present) - no recursive/deep merge of nested dicts."""
    merged = dict(common)
    for key, project_value in project.items():
        if key in LIST_MERGE_KEYS:
            common_value = merged.get(key, [])
            if not isinstance(common_value, list) or not isinstance(project_value, list):
                die(f"cloud-config key '{key}' must be a YAML list in both the common "
                    f"template and the project's cloud-config.yaml")
            merged[key] = [*common_value, *project_value]
        else:
            merged[key] = project_value
    return merged


def _represent_multiline_str(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _CloudConfigDumper(yaml.SafeDumper):
    pass


_CloudConfigDumper.add_representer(str, _represent_multiline_str)


def _jj_config_get(key: str) -> Optional[str]:
    """Best-effort read of the host's own jj config (e.g. user.name/user.email)."""
    if not shutil.which("jj"):
        return None
    r = subprocess.run(["jj", "config", "get", key], capture_output=True, text=True)
    value = r.stdout.strip()
    return value if r.returncode == 0 and value else None


def resolve_jj_identity(gc: GlobalConfig) -> tuple:
    """[jj] name/email from global config, falling back to the host's own
    `jj config get user.name`/`user.email` if unset."""
    name = gc.jj_name or _jj_config_get("user.name")
    email = gc.jj_email or _jj_config_get("user.email")
    if name is None or email is None:
        die("could not determine jj identity: set [jj] name/email in "
            f"{GLOBAL_CONFIG_PATH}, or configure it on this host with "
            "`jj config set --user user.name/user.email` (devbox falls back to that "
            "if [jj] isn't set)")
    return name, email


def resolve_github_token(gc: GlobalConfig) -> Optional[str]:
    """[github] token from global config. Best-effort, not required: cloning
    a public repo or [repo] mode "push"/"none" never need GitHub HTTPS auth
    in the sandbox at all."""
    return gc.github_token


def render_cloud_config(project: ProjectConfig, gc: GlobalConfig) -> str:
    jj_name, jj_email = resolve_jj_identity(gc)
    github_token = resolve_github_token(gc)
    if github_token is None:
        print("note: no GitHub token found ([github].token in global config) - "
              "the sandbox will have no GitHub HTTPS auth. Fine for cloning public "
              "repos; set one up if the project needs a private repo or gh/git "
              "pushes to GitHub.")
    if not gc.ssh_pubkey_path.is_file():
        die(f"SSH public key not found: {gc.ssh_pubkey_path}")
    if not COMMON_CLOUD_CONFIG_PATH.is_file():
        die(f"devbox internal error: common cloud-config template not found at "
            f"{COMMON_CLOUD_CONFIG_PATH} (broken install?)")

    tokens = {
        "DEVBOX_USER": project.user,
        "DEVBOX_SSH_PUBKEY": gc.ssh_pubkey_path.read_text().strip(),
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_GITHUB_TOKEN": github_token or "",
        "DEVBOX_REPO_MODE": project.repo.mode,
        "DEVBOX_REPO_DIR": project.repo.dir,
        "DEVBOX_REPO_URL": project.repo.url or "",
    }

    common_text = substitute_tokens(COMMON_CLOUD_CONFIG_PATH.read_text(), tokens)
    common = _load_cloud_config_yaml(common_text, COMMON_CLOUD_CONFIG_PATH)

    if project.cloud_config is not None:
        project_text = substitute_tokens(project.cloud_config.read_text(), tokens)
        project_data = _load_cloud_config_yaml(project_text, project.cloud_config)
    else:
        project_data = {}

    merged = merge_cloud_config(common, project_data)
    merged.setdefault("hostname", project.name)

    body = yaml.dump(merged, Dumper=_CloudConfigDumper, sort_keys=False, default_flow_style=False)
    # yaml.safe_load drops comments, so the magic first-line marker cloud-init's
    # NoCloud datasource relies on to recognize this as cloud-config does not
    # survive a parse->dump round trip. Without it, the VM boots but is
    # silently never provisioned - no error anywhere.
    return "#cloud-config\n" + body


def truncate_known_hosts() -> None:
    """A freshly created domain has a brand-new SSH host key; stale entries
    from a previous domain of the same name can only cause false mismatches."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS_PATH.write_text("")
    KNOWN_HOSTS_PATH.chmod(0o600)


# ---- subcommands --------------------------------------------------------------
def cmd_create(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    for binary in ("virt-install", "virsh"):
        if not shutil.which(binary):
            die(f"'{binary}' not found. Install: sudo apt install virtinst libvirt-daemon-system qemu-system-x86")

    if domain_state(gc.libvirt_uri, project.name) is not None:
        die(f"domain '{project.name}' is already defined; run `devbox destroy` first")

    net = virsh(gc.libvirt_uri, "net-info", gc.network, check=False, capture=True).stdout or ""
    if "Active:" not in net or "yes" not in net.lower().split("active:")[-1].splitlines()[0]:
        die(f"libvirt network '{gc.network}' is not active. Start it:\n"
            f"  sudo virsh -c {gc.libvirt_uri} net-start {gc.network} "
            f"&& sudo virsh -c {gc.libvirt_uri} net-autostart {gc.network}")

    # base cloud image (cached, checksum-verified via GPG-signed SHA256SUMS,
    # shared across all projects)
    expected_sha256 = fetch_verified_checksum(gc.base_image_url)
    gc.cache_dir.mkdir(parents=True, exist_ok=True)
    gc.cache_dir.chmod(0o755)
    img = gc.cache_dir / Path(gc.base_image_url).name
    if not img.is_file() or sha256_file(img) != expected_sha256:
        print(f"==> Downloading base image ({gc.base_image_url}) ...")
        tmp = img.with_name(img.name + ".tmp")
        with urllib.request.urlopen(gc.base_image_url) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        if sha256_file(tmp) != expected_sha256:
            tmp.unlink()
            die("downloaded image failed SHA256 verification")
        tmp.replace(img)
        # World-readable for qemu. Only meaningful right after download - on
        # later runs libvirt's dynamic ownership has chown'd it to
        # libvirt-qemu (and we'd no longer be allowed to chmod it), which is fine.
        img.chmod(0o644)

    rendered = render_cloud_config(project, gc)
    fd, tmp_path = tempfile.mkstemp(prefix="devbox-cloud-init-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rendered)

        # virt-install makes a qcow2 overlay (in the default pool, via libvirtd)
        # backed by the cached image, builds the NoCloud seed, and boots it.
        print(f"==> Creating domain '{project.name}' ...")
        subprocess.run([
            "virt-install",
            "--connect", gc.libvirt_uri,
            "--name", project.name,
            "--memory", str(project.memory_mib),
            "--vcpus", str(project.vcpus),
            "--cpu", "host-passthrough",
            "--os-variant", "detect=on,name=ubuntu24.04",
            "--disk", f"size={project.disk_gb},backing_store={img},format=qcow2",
            "--cloud-init", f"user-data={tmp_path}",
            "--network", f"network={gc.network}",
            "--graphics", "none",
            "--import",
            "--noautoconsole",
        ], check=True)
    finally:
        os.unlink(tmp_path)

    truncate_known_hosts()

    print(f"""
==> '{project.name}' is booting. cloud-init provisioning runs on first boot (several minutes).

  IP:        devbox -C {args.project_dir or '.'} ip
  SSH:       devbox -C {args.project_dir or '.'} ssh
  Progress:  devbox -C {args.project_dir or '.'} status
  Console:   virsh -c {gc.libvirt_uri} console {project.name}   # ctrl-] to exit
""")


def cmd_up(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    state = domain_state(gc.libvirt_uri, project.name)
    if state is None:
        die(f"domain '{project.name}' is not defined; run `devbox create` first")
    if state == "running":
        print(f"domain '{project.name}' is already running.")
        return
    print(f"==> starting '{project.name}' ...")
    virsh(gc.libvirt_uri, "start", project.name)


def cmd_stop(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    state = domain_state(gc.libvirt_uri, project.name)
    if state is None:
        die(f"domain '{project.name}' is not defined; run `devbox create` first")
    if state != "running":
        print(f"domain '{project.name}' is already stopped ({state}).")
        return
    print(f"==> stopping '{project.name}' ...")
    virsh(gc.libvirt_uri, "shutdown", project.name)


def cmd_destroy(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    virsh(gc.libvirt_uri, "destroy", project.name, check=False)
    # --nvram is rejected by older libvirt without nvram; retry without it.
    if virsh(gc.libvirt_uri, "undefine", project.name, "--remove-all-storage", "--nvram",
             check=False, capture=True).returncode != 0:
        virsh(gc.libvirt_uri, "undefine", project.name, "--remove-all-storage", check=False)
    print(f"Destroyed '{project.name}' (cached base image left in {gc.cache_dir}).")


def cmd_ip(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    ip = get_ip(gc.libvirt_uri, project.name)
    if not ip:
        die(f"no IPv4 lease for '{project.name}' yet (still booting?). Try again shortly.")
    print(ip)


def cmd_ssh(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    ip = get_ip(gc.libvirt_uri, project.name)
    if not ip:
        die(f"no IPv4 lease for '{project.name}' yet (still booting?).")
    os.execvp("ssh", ["ssh", f"{project.user}@{ip}", *args.ssh_args])


def cmd_status(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    ip = get_ip(gc.libvirt_uri, project.name)
    if not ip:
        die(f"no IPv4 lease for '{project.name}' yet (still booting?).")
    subprocess.run([
        "ssh", f"{project.user}@{ip}",
        "cloud-init status --long; echo ---; tail -n 80 ~/provision.log 2>/dev/null",
    ], check=False)


def cmd_host(args: argparse.Namespace, gc: GlobalConfig, project: ProjectConfig) -> None:
    ip = get_ip(gc.libvirt_uri, project.name)
    if not ip:
        die(f"no IPv4 lease for '{project.name}' yet (still booting?).")

    alias = f"devbox-{project.name}"
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_PATH.is_file():
        KNOWN_HOSTS_PATH.write_text("")
        KNOWN_HOSTS_PATH.chmod(0o600)

    host_conf = SSH_DIR / f"{project.name}.conf"
    host_conf.write_text(
        f"Host {alias}\n"
        f"    HostName {ip}\n"
        f"    User {project.user}\n"
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

    print(alias)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Manage per-project dev-sandbox VMs (libvirt + cloud-init).")
    # Defined before add_subparsers so -C can't be swallowed by ssh's argparse.REMAINDER.
    p.add_argument("-C", "--project-dir", type=Path, default=None,
                    help="project directory to resolve the devbox marker from (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("create", help="download image, define + first-boot the VM").set_defaults(func=cmd_create)
    sub.add_parser("up", help="start an already-defined, shut-off VM").set_defaults(func=cmd_up)
    sub.add_parser("stop", help="gracefully shut down a running VM").set_defaults(func=cmd_stop)
    sub.add_parser("destroy", help="tear down the VM and its overlay disk").set_defaults(func=cmd_destroy)
    sub.add_parser("ip", help="print the VM's IPv4").set_defaults(func=cmd_ip)
    s = sub.add_parser("ssh", help="ssh into the VM")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER, help="extra args passed to ssh")
    s.set_defaults(func=cmd_ssh)
    sub.add_parser("status", help="show cloud-init progress + provision log").set_defaults(func=cmd_status)
    sub.add_parser("host", help="refresh the SSH alias used for jj-sync, print the alias name").set_defaults(func=cmd_host)

    args = p.parse_args()
    global_config = GlobalConfig.load()
    project = resolve_project(args.project_dir or Path.cwd(), global_config)
    args.func(args, global_config, project)


if __name__ == "__main__":
    main()
