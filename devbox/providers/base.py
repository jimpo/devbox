"""The provider seam. Everything a machine's *location* dictates - launch,
lifecycle, address discovery, enumeration - lives behind Provider. Everything
above it (config, cloud-config render, provisioning, ssh/exec, target/config
resolution, list aggregation) is shared.
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Optional

from ..config import Ec2ProjectConfig, LightsailProjectConfig


@dataclasses.dataclass
class InstanceSpec:
    """What `create` needs to launch one instance. Common fields plus the
    provider-specific bits each backend reads (libvirt: vcpus/memory_mib; ec2:
    the [ec2] launch config; aws/Lightsail: the [lightsail] launch config, which
    may be None since all of its fields have defaults)."""
    name: str
    project: str
    provider: str
    disk_gb: int
    user_data: str        # rendered cloud-config (first-boot provisioning)
    created_at: str       # ISO-8601, stamped onto the instance
    vcpus: Optional[int] = None
    memory_mib: Optional[int] = None
    ec2: Optional[Ec2ProjectConfig] = None
    lightsail: Optional[LightsailProjectConfig] = None


@dataclasses.dataclass
class Instance:
    """One row of `devbox list`. `resources` is provider-formatted (libvirt:
    vCPU/RAM; ec2: the instance type; aws: the Lightsail bundle id). `cost_hint`
    flags a paid provider so the listing can mark running instances that are
    costing money."""
    name: str
    provider: str
    project: Optional[str]
    state: str
    resources: str
    created_at: Optional[str]
    cost_hint: bool = False


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
