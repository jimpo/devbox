"""Provider registry + resolution. Above this seam everything is shared."""
from __future__ import annotations

from ..config import GlobalConfig
from ..util import die
from .base import Instance, InstanceSpec, Provider
from .ec2 import Ec2Provider
from .libvirt import LibvirtProvider
from .lightsail import LightsailProvider

PROVIDER_REGISTRY = {
    "libvirt": LibvirtProvider,
    "ec2": Ec2Provider,
    "aws": LightsailProvider,
}

# Providers backed by the shared `[aws]` account creds - none of them can be
# constructed without a configured region/profile.
AWS_PROVIDERS = ("ec2", "aws")


def get_provider(name: str, gc: GlobalConfig) -> Provider:
    """The Provider for a given name (a project's or the target instance's)."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        die(f"unknown provider '{name}'; choose one of {tuple(PROVIDER_REGISTRY)}")
    if name in AWS_PROVIDERS and gc.aws is None:
        die("[aws] is not configured in the global config (add region/profile "
            "to ~/.config/devbox/config.toml)")
    return cls(gc)


def all_providers(gc: GlobalConfig) -> list:
    """Every available provider, for the global `list` aggregation. The
    AWS-backed providers are included only when AWS creds are configured, so a
    libvirt-only host's `list` never reaches for AWS credentials."""
    providers = [LibvirtProvider(gc)]
    if gc.aws is not None:
        providers.append(Ec2Provider(gc))
        providers.append(LightsailProvider(gc))
    return providers


__all__ = [
    "Provider", "Instance", "InstanceSpec",
    "LibvirtProvider", "Ec2Provider", "LightsailProvider",
    "AWS_PROVIDERS", "PROVIDER_REGISTRY", "get_provider", "all_providers",
]
