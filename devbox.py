"""devbox: a global, multi-project dev-sandbox VM manager (libvirt + cloud-init).

Each project supplies its own cloud-init config via a `.devbox.toml` marker
file (see README.md); all projects share one cached base VM image and one
global config at ~/.config/devbox/config.toml.

`create` uses virt-install --cloud-init to build/attach a NoCloud seed from
the project's cloud-init file, so cloud-init provisions the box on first
boot. `up`/`stop` only start/shut down an already-defined domain.

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
import urllib.request
from pathlib import Path
from typing import Optional

PROJECT_MARKER = ".devbox.toml"
PROJECT_MARKER_LOCAL = ".devbox.local.toml"

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
DEFAULT_BASE_IMAGE_SHA256 = "0c9fb915bab0b36b361d3bf8aeae2115dda19d81a306656964de048033481670"
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
    base_image_sha256: str
    vcpus: int
    memory_mib: int
    disk_gb: int
    ssh_pubkey_path: Path

    @classmethod
    def load(cls, path: Path = GLOBAL_CONFIG_PATH) -> "GlobalConfig":
        data: dict = {}
        if path.is_file():
            with path.open("rb") as f:
                data = tomllib.load(f)
        base_image = data.get("base_image", {})
        defaults = data.get("defaults", {})
        ssh = data.get("ssh", {})
        return cls(
            libvirt_uri=data.get("libvirt_uri", DEFAULT_LIBVIRT_URI),
            network=data.get("network", DEFAULT_NETWORK),
            cache_dir=Path(data.get("cache_dir", DEFAULT_CACHE_DIR)).expanduser(),
            base_image_url=base_image.get("url", DEFAULT_BASE_IMAGE_URL),
            base_image_sha256=base_image.get("sha256", DEFAULT_BASE_IMAGE_SHA256),
            vcpus=defaults.get("vcpus", DEFAULT_VCPUS),
            memory_mib=defaults.get("memory_mib", DEFAULT_MEMORY_MIB),
            disk_gb=defaults.get("disk_gb", DEFAULT_DISK_GB),
            ssh_pubkey_path=Path(ssh.get("pubkey_path", DEFAULT_SSH_PUBKEY_PATH)).expanduser(),
        )


# ---- project config / resolution --------------------------------------------
@dataclasses.dataclass
class ProjectConfig:
    name: str
    cloud_init: Optional[Path]  # absolute path, resolved relative to the marker file
    user: str
    vcpus: int
    memory_mib: int
    disk_gb: int


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def find_marker_dir(start: Path) -> Path:
    """Walk upward from `start` (like git looking for .git) for a devbox marker.

    A directory qualifies if it has either marker file; the first one found
    (nearest to `start`) wins.
    """
    cur = start.resolve()
    while True:
        if (cur / PROJECT_MARKER_LOCAL).is_file() or (cur / PROJECT_MARKER).is_file():
            return cur
        if cur.parent == cur:
            die(f"no {PROJECT_MARKER} found in {start} or any parent directory "
                f"(pass -C/--project-dir, or add a marker file to the project)")
        cur = cur.parent


def resolve_project(start: Path, global_config: GlobalConfig) -> ProjectConfig:
    marker_dir = find_marker_dir(start)
    base_path = marker_dir / PROJECT_MARKER
    local_path = marker_dir / PROJECT_MARKER_LOCAL

    data = _load_toml(base_path) if base_path.is_file() else {}
    if local_path.is_file():
        # .devbox.local.toml overrides whichever fields it sets (typically just `name`).
        data = {**data, **_load_toml(local_path)}

    if "name" not in data:
        die(f"{marker_dir}: marker is missing required field 'name'")

    cloud_init = data.get("cloud_init")
    return ProjectConfig(
        name=data["name"],
        cloud_init=(marker_dir / cloud_init) if cloud_init else None,
        user=data.get("user", os.environ.get("USER", "")),
        vcpus=data.get("vcpus", global_config.vcpus),
        memory_mib=data.get("memory_mib", global_config.memory_mib),
        disk_gb=data.get("disk_gb", global_config.disk_gb),
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


# ---- cloud-init templating ----------------------------------------------------
def render_cloud_init(cloud_init_path: Path, user: str, pubkey_path: Path) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax and with valid YAML flow-mapping syntax)."""
    if not cloud_init_path.is_file():
        die(f"cloud-init file not found: {cloud_init_path}")
    if not pubkey_path.is_file():
        die(f"SSH public key not found: {pubkey_path}")
    text = cloud_init_path.read_text()
    pubkey = pubkey_path.read_text().strip()
    return text.replace("%DEVBOX_USER%", user).replace("%DEVBOX_SSH_PUBKEY%", pubkey)


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
    if project.cloud_init is None:
        die("project marker is missing 'cloud_init'")

    if domain_state(gc.libvirt_uri, project.name) is not None:
        die(f"domain '{project.name}' is already defined; run `devbox destroy` first")

    net = virsh(gc.libvirt_uri, "net-info", gc.network, check=False, capture=True).stdout or ""
    if "Active:" not in net or "yes" not in net.lower().split("active:")[-1].splitlines()[0]:
        die(f"libvirt network '{gc.network}' is not active. Start it:\n"
            f"  sudo virsh -c {gc.libvirt_uri} net-start {gc.network} "
            f"&& sudo virsh -c {gc.libvirt_uri} net-autostart {gc.network}")

    # base cloud image (cached, checksum-verified, shared across all projects)
    gc.cache_dir.mkdir(parents=True, exist_ok=True)
    gc.cache_dir.chmod(0o755)
    img = gc.cache_dir / Path(gc.base_image_url).name
    if not img.is_file() or sha256_file(img) != gc.base_image_sha256:
        print(f"==> Downloading base image ({gc.base_image_url}) ...")
        tmp = img.with_name(img.name + ".tmp")
        with urllib.request.urlopen(gc.base_image_url) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        if sha256_file(tmp) != gc.base_image_sha256:
            tmp.unlink()
            die("downloaded image failed SHA256 verification")
        tmp.replace(img)
        # World-readable for qemu. Only meaningful right after download - on
        # later runs libvirt's dynamic ownership has chown'd it to
        # libvirt-qemu (and we'd no longer be allowed to chmod it), which is fine.
        img.chmod(0o644)

    rendered = render_cloud_init(project.cloud_init, project.user, gc.ssh_pubkey_path)
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
