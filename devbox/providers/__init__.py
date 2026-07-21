"""Provider registry + resolution. Above this seam everything is shared."""
from __future__ import annotations

from ..config import GlobalConfig
from ..util import die
from .aws import AwsProvider
from .base import Instance, InstanceSpec, Provider
from .libvirt import LibvirtProvider

PROVIDER_REGISTRY = {"libvirt": LibvirtProvider, "aws": AwsProvider}


def get_provider(name: str, gc: GlobalConfig) -> Provider:
    """The Provider for a given name (a project's or the target instance's)."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        die(f"unknown provider '{name}'; choose one of {tuple(PROVIDER_REGISTRY)}")
    if name == "aws" and gc.aws is None:
        die("[aws] is not configured in the global config (add region/profile "
            "to ~/.config/devbox/config.toml)")
    return cls(gc)


def all_providers(gc: GlobalConfig) -> list:
    """Every available provider, for the global `list` aggregation. AWS is
    included only when configured, so a libvirt-only host's `list` never reaches
    for AWS credentials."""
    providers = [LibvirtProvider(gc)]
    if gc.aws is not None:
        providers.append(AwsProvider(gc))
    return providers


__all__ = [
    "Provider", "Instance", "InstanceSpec",
    "LibvirtProvider", "AwsProvider",
    "PROVIDER_REGISTRY", "get_provider", "all_providers",
]
