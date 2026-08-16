"""The provider seam. Everything a machine's *location* dictates - launch,
lifecycle, address discovery, enumeration - lives behind Provider. Everything
above it (config, init-script render, provisioning, ssh/exec, target/config
resolution, list aggregation) is shared.
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Optional

from ..config import Ec2BoxConfig, LightsailBoxConfig


@dataclasses.dataclass
class InstanceSpec:
    """What `create` needs to launch one box. Common fields plus the
    provider-specific bits each backend reads (libvirt: vcpus/memory_mib; ec2:
    the [ec2] launch config; aws/Lightsail: the [lightsail] launch config, which
    may be None since all of its fields have defaults).

    `admin_user` is stamped onto the instance because the host needs it to build
    the box's SSH alias *before* it can log in to ask - see `Instance`."""
    name: str
    admin_user: str
    provider: str
    disk_gb: int
    user_data: str        # rendered init script (first-boot base setup)
    created_at: str       # ISO-8601, stamped onto the instance
    vcpus: Optional[int] = None
    memory_mib: Optional[int] = None
    ec2: Optional[Ec2BoxConfig] = None
    lightsail: Optional[LightsailBoxConfig] = None


@dataclasses.dataclass
class Instance:
    """One row of `devbox box list`. `resources` is provider-formatted (libvirt:
    vCPU/RAM; ec2: the instance type; aws: the Lightsail bundle id). `cost_hint`
    flags a paid provider so the listing can mark running boxes that are costing
    money.

    `admin_user` is the box's privileged account. It is recorded on the instance
    rather than in host-side config because devbox needs it to build the box's
    SSH alias before it can connect - and because it must be the same answer from
    any workstation. A box created before the stamp existed reports None; callers
    fall back to the host's own $USER, which is what it was created with.

    There is deliberately no `project` field: a box hosts as many projects as
    attach to it, and the answer lives on the box (`/etc/devbox/projects/`), not
    in a launch-time tag. `devbox box list --attached` reads it from there."""
    name: str
    provider: str
    state: str
    resources: str
    created_at: Optional[str]
    cost_hint: bool = False
    admin_user: Optional[str] = None


class Provider(abc.ABC):
    """Only create/lifecycle/address/enumeration are per-backend; everything
    else is shared above it."""
    name: str

    @abc.abstractmethod
    def create(self, spec: InstanceSpec) -> None: ...

    @abc.abstractmethod
    def start(self, name: str) -> None: ...

    @abc.abstractmethod
    def stop(self, name: str) -> None: ...

    @abc.abstractmethod
    def destroy(self, name: str) -> None: ...

    @abc.abstractmethod
    def state(self, name: str) -> Optional[str]:
        """Backend state ('running', 'shut off'/'stopped', ...), or None if the
        instance doesn't exist."""

    @abc.abstractmethod
    def address(self, name: str) -> Optional[str]:
        """SSH host/IP, or None if not available yet."""

    @abc.abstractmethod
    def list(self) -> list:
        """Instances this provider manages, as Instance rows."""

    def get(self, name: str) -> Optional[Instance]:
        """One instance by canonical name, or None. Filters `list()`; a backend
        with a cheaper single-instance lookup can override."""
        return next((i for i in self.list() if i.name == name), None)
