"""The kubevirt provider: a box is a KubeVirt VirtualMachine whose root disk is a
CDI import of `image_url` (downloaded per box, not verified). Runs `kubectl` (and, for the SSH
ProxyCommand, `virtctl`) as subprocesses, like libvirt runs `virsh`.

Kubernetes objects use the BARE name (kube-dev -> VM `dev`, Secret
`dev-userdata`); the `devbox/name` label holds the canonical name and every
lookup goes through it. The cluster admin owns the boundary (namespace,
NetworkPolicy, RBAC, quota); devbox only checks that it is there.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..config import GlobalConfig, KubevirtConfig, parse_instance_name
from ..util import die
from .base import Instance, InstanceSpec, Provider

MANAGED_LABEL = "devbox/managed"
NAME_LABEL = "devbox/name"
ADMIN_USER_ANNOTATION = "devbox/admin-user"
CREATED_AT_ANNOTATION = "devbox/created-at"
# The admin's NetworkPolicy carries this label; create refuses without one.
EGRESS_LABEL = "devbox/egress=enforced"


class KubectlError(Exception):
    """A checked kubectl call failed; the message is kubectl's own stderr."""


def kubectl(cfg: KubevirtConfig, *args: str, input: Optional[str] = None,
            check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    base = ["kubectl"]
    if cfg.kubeconfig:
        base += ["--kubeconfig", str(Path(cfg.kubeconfig).expanduser())]
    if cfg.context:
        base += ["--context", cfg.context]
    base += ["-n", cfg.namespace]
    # A checked call always captures stderr, so a failure can say what kubectl said.
    r = subprocess.run([*base, *args], input=input, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture or check else None)
    if check and r.returncode != 0:
        raise KubectlError(f"kubectl: {(r.stderr or '').strip()}")
    if not capture and r.stderr:
        sys.stderr.write(r.stderr)  # warnings from a call that succeeded
    return r


def build_secret(bare: str, user_data: str) -> dict:
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": f"{bare}-userdata", "labels": {MANAGED_LABEL: "true"}},
        "stringData": {"userdata": user_data},
    }


def build_vm(cfg: KubevirtConfig, spec: InstanceSpec) -> dict:
    """No resources.limits and no explicit requests: KubeVirt derives the
    virt-launcher pod's requests from cpu.cores and memory.guest, and the
    admin's ResourceQuota caps requests only."""
    _, bare = parse_instance_name(spec.name)
    storage = {"resources": {"requests": {"storage": f"{spec.disk_gb}Gi"}}}
    if cfg.storage_class:
        storage["storageClassName"] = cfg.storage_class
    return {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {
            "name": bare,
            "labels": {MANAGED_LABEL: "true", NAME_LABEL: spec.name},
            "annotations": {ADMIN_USER_ANNOTATION: spec.admin_user,
                            CREATED_AT_ANNOTATION: spec.created_at},
        },
        "spec": {
            "runStrategy": "Always",
            "dataVolumeTemplates": [{
                "metadata": {"name": f"{bare}-root"},
                "spec": {"storage": storage,
                         "source": {"http": {"url": cfg.image_url}}},
            }],
            "template": {"spec": {
                "domain": {
                    "cpu": {"cores": spec.vcpus},
                    "memory": {"guest": f"{spec.memory_mib}Mi"},
                    "devices": {
                        "disks": [{"name": "root", "disk": {"bus": "virtio"}},
                                  {"name": "cloudinit", "disk": {"bus": "virtio"}}],
                        "interfaces": [{"name": "default", "masquerade": {}}],
                    },
                },
                "networks": [{"name": "default", "pod": {}}],
                "dnsPolicy": "None",
                "dnsConfig": {"nameservers": list(cfg.nameservers)},
                "volumes": [
                    {"name": "root", "dataVolume": {"name": f"{bare}-root"}},
                    {"name": "cloudinit", "cloudInitNoCloud": {
                        "secretRef": {"name": f"{bare}-userdata"}}},
                ],
            }},
        },
    }


def _resources(vm: dict) -> str:
    dom = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {})
    cores = dom.get("cpu", {}).get("cores")
    guest = dom.get("memory", {}).get("guest", "")
    if cores is None or not guest.endswith("Mi") or not guest[:-2].isdigit():
        return "?"
    return f"{cores} vCPU / {int(guest[:-2]) / 1024:g} GiB"


class KubevirtProvider(Provider):
    name = "kubevirt"

    def __init__(self, gc: GlobalConfig):
        if gc.kubevirt is None:
            die("[kubevirt] is not configured in the global config")
        self.gc = gc
        self.cfg = gc.kubevirt

    def _vm(self, name: str) -> Optional[dict]:
        """The VM whose devbox/name label is the canonical `name`, or None."""
        out = kubectl(self.cfg, "get", "vm", "-l", f"{NAME_LABEL}={name}",
                      "-o", "json", capture=True).stdout
        items = json.loads(out).get("items", [])
        return items[0] if items else None

    def _require(self, name: str) -> str:
        vm = self._vm(name)
        if vm is None:
            die(f"box '{name}' not found")
        return vm["metadata"]["name"]

    def state(self, name: str) -> Optional[str]:
        vm = self._vm(name)
        if vm is None:
            return None
        return (vm.get("status", {}).get("printableStatus") or "unknown").lower()

    def address(self, name: str) -> Optional[str]:
        vm = self._vm(name)
        if vm is None:
            return None
        r = kubectl(self.cfg, "get", "vmi", vm["metadata"]["name"], "-o", "json",
                    check=False, capture=True)
        if r.returncode != 0:
            return None
        ifaces = json.loads(r.stdout).get("status", {}).get("interfaces") or []
        return (ifaces[0].get("ipAddress") if ifaces else None) or None

    def ssh_proxy(self, name: str) -> str:
        _, bare = parse_instance_name(name)
        cmd = ["virtctl"]
        # ssh token-expands ProxyCommand, so a literal % must be doubled.
        if self.cfg.kubeconfig:
            cmd += ["--kubeconfig", str(Path(self.cfg.kubeconfig).expanduser()).replace("%", "%%")]
        if self.cfg.context:
            cmd += ["--context", self.cfg.context.replace("%", "%%")]
        cmd += ["port-forward", "--stdio=true", f"vm/{bare}/{self.cfg.namespace}", "22"]
        return " ".join(shlex.quote(c) for c in cmd)

    def _set_run_strategy(self, name: str, strategy: str) -> None:
        kubectl(self.cfg, "patch", "vm", self._require(name), "--type", "merge",
                "-p", json.dumps({"spec": {"runStrategy": strategy}}))

    def start(self, name: str) -> None:
        self._set_run_strategy(name, "Always")

    def stop(self, name: str) -> None:
        self._set_run_strategy(name, "Halted")

    def destroy(self, name: str) -> None:
        vm = self._vm(name)
        if vm is None:
            print(f"instance '{name}' not found (already deleted?)")
            return
        bare = vm["metadata"]["name"]
        # Foreground: wait for the DataVolume/PVC to go too, so an immediate
        # re-create doesn't race the garbage collector. The Secret is owned by the
        # VM as well, but deleting it explicitly makes that certain.
        kubectl(self.cfg, "delete", "vm", bare, "--cascade=foreground", "--wait")
        kubectl(self.cfg, "delete", "secret", f"{bare}-userdata",
                "--ignore-not-found", "--wait")

    def list(self) -> list:
        out = kubectl(self.cfg, "get", "vm", "-l", f"{MANAGED_LABEL}=true",
                      "-o", "json", capture=True).stdout
        rows = []
        for vm in json.loads(out).get("items", []):
            meta = vm["metadata"]
            name = meta.get("labels", {}).get(NAME_LABEL)
            if not name:
                continue
            rows.append(Instance(
                name=name,
                provider="kubevirt",
                state=(vm.get("status", {}).get("printableStatus") or "unknown").lower(),
                resources=_resources(vm),
                created_at=meta.get("annotations", {}).get(CREATED_AT_ANNOTATION),
                admin_user=meta.get("annotations", {}).get(ADMIN_USER_ANNOTATION),
            ))
        rows.sort(key=lambda i: i.name)
        return rows

    def create(self, spec: InstanceSpec) -> None:
        cfg = self.cfg
        _, bare = parse_instance_name(spec.name)

        # Fail closed: devbox can't write the boundary, so it checks it exists.
        policies = kubectl(cfg, "get", "networkpolicy", "-l", EGRESS_LABEL,
                           "-o", "name", check=False, capture=True)
        if policies.returncode != 0 or not policies.stdout.strip():
            die(f"no NetworkPolicy labelled {EGRESS_LABEL} in namespace "
                f"'{cfg.namespace}'; the cluster admin must apply the egress "
                f"policy before devbox will create boxes there")

        # Kubernetes names are unique per namespace: an unmanaged VM called
        # `bare` would otherwise be overwritten by the apply below.
        r = kubectl(cfg, "get", "vm", bare, "-o", "json", check=False, capture=True)
        if r.returncode == 0:
            labels = json.loads(r.stdout)["metadata"].get("labels", {})
            if labels.get(NAME_LABEL) == spec.name:
                die(f"instance '{spec.name}' already exists; run `devbox destroy` first")
            die(f"a VirtualMachine named '{bare}' already exists in namespace "
                f"'{cfg.namespace}' but is not managed by devbox (devbox drops the "
                f"'kube-' prefix when naming VMs). Pick another name, or delete it first.")

        print(f"==> Creating VirtualMachine '{bare}' ...")
        # The Secret goes first: runStrategy Always starts the VM immediately.
        kubectl(cfg, "apply", "-f", "-", input=json.dumps(build_secret(bare, spec.user_data)))
        kubectl(cfg, "apply", "-f", "-", input=json.dumps(build_vm(cfg, spec)))
        uid = kubectl(cfg, "get", "vm", bare, "-o", "jsonpath={.metadata.uid}",
                      capture=True).stdout.strip()
        # Owned by the VM, the Secret is garbage-collected with it.
        kubectl(cfg, "patch", "secret", f"{bare}-userdata", "--type", "merge", "-p",
                json.dumps({"metadata": {"ownerReferences": [{
                    "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                    "name": bare, "uid": uid}]}}))
