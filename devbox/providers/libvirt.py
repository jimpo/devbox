"""The libvirt provider: local VMs via virsh/virt-install, cloud-init NoCloud
seed, DHCP-lease IPs, and a <metadata> stamp so `list` recognizes its own
domains. Also owns the shared base cloud-image cache/verification.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import GlobalConfig
from ..util import die
from .base import Instance, InstanceSpec, Provider

# libvirt domain <metadata> stamp: how `devbox list` tells its own domains apart
# from every other domain on the host (the equivalent of cfleet's EC2 tags).
# Written at create, read back at list. The namespace URI is an opaque, stable
# identifier - it need not resolve to anything.
DEVBOX_METADATA_NS = "https://github.com/jimpo/devbox"
DEVBOX_METADATA_KEY = "devbox"


# ---- libvirt low-level helpers ----------------------------------------------
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


# ---- <metadata> stamp & instance discovery ----------------------------------
def build_metadata_xml(admin_user: str, provider: str, created_at: str) -> str:
    """The <devbox> element stamped into a domain's <metadata> at create.

    `<user>` is the box's privileged account: devbox needs it to build the box's
    SSH alias before it can log in and ask, so the instance has to carry it."""
    dev = ET.Element("devbox", {"xmlns": DEVBOX_METADATA_NS})
    ET.SubElement(dev, "user").text = admin_user
    ET.SubElement(dev, "provider").text = provider
    ET.SubElement(dev, "createdAt").text = created_at
    ET.SubElement(dev, "managedBy").text = "devbox"
    return ET.tostring(dev, encoding="unicode")


def stamp_domain(uri: str, name: str, admin_user: str, provider: str, created_at: str) -> None:
    """Mark a freshly-created domain as devbox-managed. Written to both the live
    and persistent config: `list` reads a running domain's *live* XML, so a
    config-only stamp would leave a just-created (running) box invisible until it
    was restarted."""
    xml = build_metadata_xml(admin_user, provider, created_at)
    r = virsh(uri, "metadata", name, "--live", "--config",
              "--uri", DEVBOX_METADATA_NS, "--key", DEVBOX_METADATA_KEY, "--set", xml,
              check=False, capture=True)
    if r.returncode != 0:
        # e.g. domain not live yet - at least persist to the config so the stamp
        # isn't lost (it'll apply to the live XML on next start).
        virsh(uri, "metadata", name, "--config",
              "--uri", DEVBOX_METADATA_NS, "--key", DEVBOX_METADATA_KEY, "--set", xml,
              check=False)


def read_domain_instance(uri: str, name: str) -> Optional[dict]:
    """Parse a domain's XML for the devbox stamp + its resources. Returns None
    for domains that aren't devbox-managed (no stamp) or can't be read - that's
    how `list` filters the host's unrelated domains out."""
    r = virsh(uri, "dumpxml", name, check=False, capture=True)
    if r.returncode != 0:
        return None
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    meta = root.find("metadata")
    dev = meta.find(f"{{{DEVBOX_METADATA_NS}}}devbox") if meta is not None else None
    if dev is None:
        return None  # not a devbox-managed domain

    def stamp(tag: str) -> Optional[str]:
        el = dev.find(f"{{{DEVBOX_METADATA_NS}}}{tag}")
        return el.text if el is not None else None

    def intval(tag: str) -> Optional[int]:
        el = root.find(tag)
        return int(el.text) if el is not None and el.text and el.text.isdigit() else None

    return {
        "name": name,
        # Absent on a box created before the stamp existed; the caller falls back
        # to the host's $USER, which is what such a box was created with.
        "admin_user": stamp("user"),
        "provider": stamp("provider") or "libvirt",
        "created_at": stamp("createdAt"),
        "vcpus": intval("vcpu"),
        "memory_kib": intval("memory"),  # <memory> is the allocated max, in KiB
    }


def _format_resources(vcpus: Optional[int], memory_kib: Optional[int]) -> str:
    if vcpus is None or memory_kib is None:
        return "?"
    return f"{vcpus} vCPU / {memory_kib / (1024 * 1024):g} GiB"


# ---- base image (shared, cached, GPG-verified) ------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_filename(image_filename: str) -> str:
    """The clearsigned CHECKSUM file Fedora publishes beside a release image:
    Fedora-Cloud-Base-<Variant>-<ver>-<build>.<arch>.qcow2 ->
    Fedora-Cloud-<ver>-<build>-<arch>-CHECKSUM."""
    m = re.fullmatch(r"Fedora-Cloud-Base-.+-(\d+)-([\d.]+)\.(\w+)\.qcow2", image_filename)
    if not m:
        die(f"cannot derive the CHECKSUM file name from '{image_filename}' (expected "
            f"Fedora-Cloud-Base-<Variant>-<ver>-<build>.<arch>.qcow2)")
    return f"Fedora-Cloud-{m[1]}-{m[2]}-{m[3]}-CHECKSUM"


def fetch_verified_checksum(image_url: str, gpg_key: Path) -> str:
    """Fetch the clearsigned CHECKSUM file from alongside `image_url`, verify it
    against the Fedora release key `gpg_key` (ASCII-armored, as
    fedora-gpg-keys ships it), and return the SHA256 for the image's filename.
    Release images are immutable (the build number is in the name), so a cached
    image never goes stale under a respin."""
    for binary in ("gpg", "gpgv"):
        if not shutil.which(binary):
            die(f"'{binary}' not found. Install: sudo dnf install gnupg2")
    if not gpg_key.is_file():
        die(f"{gpg_key} not found. Install: sudo dnf install fedora-gpg-keys, "
            f"or set [base_image] gpg_key")

    filename = Path(image_url).name
    sums_url = urllib.parse.urljoin(image_url, checksum_filename(filename))
    with urllib.request.urlopen(sums_url) as resp:
        sums = resp.read()

    with tempfile.TemporaryDirectory(prefix="devbox-checksums-") as tmpdir:
        sums_path = Path(tmpdir) / "CHECKSUM"
        keyring = Path(tmpdir) / "keyring.gpg"
        sums_path.write_bytes(sums)
        # gpgv reads a binary keyring, not the armored key file.
        r = subprocess.run(["gpg", "--dearmor", "--output", str(keyring), str(gpg_key)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            # --output -: the signed text only. Anything outside the clearsigned
            # block is unsigned, so parse gpgv's output, never the raw download.
            r = subprocess.run(["gpgv", "--keyring", str(keyring), "--output", "-", str(sums_path)],
                               capture_output=True, text=True)
        if r.returncode != 0:
            die(f"GPG verification of {sums_url} failed:\n{r.stderr}")

    for line in r.stdout.splitlines():
        m = re.fullmatch(r"SHA256 \((.+)\) = ([0-9a-f]{64})", line.strip())
        if m and m[1] == filename:
            return m[2]
    die(f"{filename} not listed in {sums_url}")


class LibvirtProvider(Provider):
    name = "libvirt"

    def __init__(self, gc: GlobalConfig):
        self.gc = gc
        self.uri = gc.libvirt_uri

    def state(self, name: str) -> Optional[str]:
        return domain_state(self.uri, name)

    def address(self, name: str) -> Optional[str]:
        return get_ip(self.uri, name)

    def start(self, name: str) -> None:
        virsh(self.uri, "start", name)

    def stop(self, name: str) -> None:
        virsh(self.uri, "shutdown", name)

    def destroy(self, name: str) -> None:
        virsh(self.uri, "destroy", name, check=False)
        # --nvram is rejected by older libvirt without nvram; retry without it.
        if virsh(self.uri, "undefine", name, "--remove-all-storage", "--nvram",
                 check=False, capture=True).returncode != 0:
            virsh(self.uri, "undefine", name, "--remove-all-storage", check=False)

    def list(self) -> list:
        r = virsh(self.uri, "list", "--all", "--name", check=False, capture=True)
        out = []
        for name in (r.stdout or "").split():
            info = read_domain_instance(self.uri, name)
            if info is None:
                continue
            out.append(Instance(
                name=info["name"],
                provider="libvirt",
                state=domain_state(self.uri, name) or "unknown",
                resources=_format_resources(info["vcpus"], info["memory_kib"]),
                created_at=info["created_at"],
                admin_user=info["admin_user"],
            ))
        out.sort(key=lambda i: i.name)
        return out

    def create(self, spec: InstanceSpec) -> None:
        for binary in ("virt-install", "virsh"):
            if not shutil.which(binary):
                die(f"'{binary}' not found. Install: sudo dnf install virt-install libvirt-daemon-kvm qemu-kvm gnupg2")

        # Global-uniqueness of the instance name. The union of providers is just
        # libvirt today, so an existing domain of this name (devbox-managed or
        # not) is the whole check; the AWS provider will extend it.
        if domain_state(self.uri, spec.name) is not None:
            die(f"domain '{spec.name}' is already defined; run `devbox destroy` first")

        net = virsh(self.uri, "net-info", self.gc.network, check=False, capture=True).stdout or ""
        if "Active:" not in net or "yes" not in net.lower().split("active:")[-1].splitlines()[0]:
            die(f"libvirt network '{self.gc.network}' is not active. Start it:\n"
                f"  sudo virsh -c {self.uri} net-start {self.gc.network} "
                f"&& sudo virsh -c {self.uri} net-autostart {self.gc.network}")

        img = self._ensure_base_image()
        fd, tmp_path = tempfile.mkstemp(prefix="devbox-cloud-init-", suffix=".sh")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(spec.user_data)

            # virt-install makes a qcow2 overlay (in the default pool, via
            # libvirtd) backed by the cached image, builds the NoCloud seed,
            # and boots it.
            print(f"==> Creating domain '{spec.name}' ...")
            subprocess.run([
                "virt-install",
                "--connect", self.uri,
                "--name", spec.name,
                "--memory", str(spec.memory_mib),
                "--vcpus", str(spec.vcpus),
                "--cpu", "host-passthrough",
                "--os-variant", "detect=on,name=fedora-unknown",
                "--disk", f"size={spec.disk_gb},backing_store={img},format=qcow2",
                "--cloud-init", f"user-data={tmp_path}",
                "--network", f"network={self.gc.network}",
                "--graphics", "none",
                "--import",
                "--noautoconsole",
            ], check=True)
        finally:
            os.unlink(tmp_path)

        # Stamp the domain so `devbox list` recognizes it (and, from day one,
        # records provider + created-at for a future auto-stop reaper).
        stamp_domain(self.uri, spec.name, spec.admin_user, spec.provider, spec.created_at)

    def _ensure_base_image(self) -> Path:
        """The shared, cached, GPG-verified base cloud image (one per host,
        reused across workspaces)."""
        expected_sha256 = fetch_verified_checksum(self.gc.base_image_url, self.gc.base_image_gpg_key)
        self.gc.cache_dir.mkdir(parents=True, exist_ok=True)
        self.gc.cache_dir.chmod(0o755)
        img = self.gc.cache_dir / Path(self.gc.base_image_url).name
        if not img.is_file() or sha256_file(img) != expected_sha256:
            print(f"==> Downloading base image ({self.gc.base_image_url}) ...")
            tmp = img.with_name(img.name + ".tmp")
            with urllib.request.urlopen(self.gc.base_image_url) as resp, tmp.open("wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            if sha256_file(tmp) != expected_sha256:
                tmp.unlink()
                die("downloaded image failed SHA256 verification")
            tmp.replace(img)
            # World-readable for qemu. Only meaningful right after download - on
            # later runs libvirt's dynamic ownership has chown'd it to
            # libvirt-qemu (and we'd no longer be allowed to chmod it), which is fine.
            img.chmod(0o644)
        return img
