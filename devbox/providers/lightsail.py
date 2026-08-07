"""The `aws` provider, backed by AWS Lightsail. Like EC2 there is no local DB:
state is discovered from the API by devbox tags. Unlike EC2, Lightsail's
`get_instances` takes no filter argument, so discovery pages the full instance
list and matches tags client-side. boto3 is imported lazily so libvirt-only
users need not have it, and tests inject a fake client.

Lightsail sells fixed *bundles* (CPU + RAM + SSD together) rather than an
instance type plus an EBS volume, so `spec.disk_gb` is ignored here - the bundle
picks the disk. The bundles are x86-only (Lightsail has no ARM/Graviton Linux
tier), so unlike [ec2] there is no architecture knob. Networking is the
instance's dynamic public IPv4 (no static IP is allocated or released),
re-resolved on each command. Port 22 is open by default on a Lightsail instance,
so there are no firewall calls at create.

Config: the shared global `[aws]` section (account creds) plus a project's
optional `[lightsail]` section (bundle / blueprint / availability_zone). There
is no global `[lightsail]` section.

Tags stamped on every instance (same keys as EC2, but Lightsail spells a tag
{"key": ..., "value": ...} rather than EC2's {"Key": ..., "Value": ...}):
  devbox:managed = "true"       (the filter for "mine")
  devbox:name    = <name>       (devbox's canonical name, e.g. aws-binius;
                                 how commands address the instance)
  devbox:project = <project>
  devbox:created-at = <ISO-8601>

The Lightsail-side `instanceName` is the BARE name (aws-binius is created as
`binius`): the provider prefix is redundant AWS-side. The canonical name
lives in the devbox:name tag, which is what every lookup matches on.
"""
from __future__ import annotations

from typing import Optional

from ..config import (AwsConfig, DEFAULT_LIGHTSAIL_BUNDLE, GlobalConfig,
                      LightsailProjectConfig, parse_instance_name)
from ..util import die
from .awsauth import make_client
from .base import Instance, InstanceSpec, Provider

MANAGED_TAG = "devbox:managed"
NAME_TAG = "devbox:name"
PROJECT_TAG = "devbox:project"
CREATED_AT_TAG = "devbox:created-at"


def _make_lightsail_client(aws: AwsConfig):
    # Credentials are resolved (and a login offered if they've expired) inside
    # make_client, before the first API call - see providers/awsauth.py.
    return make_client(aws, "lightsail", "AWS (Lightsail) provider")


class LightsailProvider(Provider):
    name = "aws"

    def __init__(self, gc: GlobalConfig, client=None):
        if gc.aws is None:
            die("[aws] is not configured in the global config (needs region/profile)")
        self.gc = gc
        self.aws = gc.aws                      # shared account creds (region/profile)
        self._client = client  # injected in tests; lazily created otherwise

    @property
    def ls(self):
        if self._client is None:
            self._client = _make_lightsail_client(self.aws)
        return self._client

    # ---- discovery ----------------------------------------------------------
    def _all_instances(self) -> list:
        """Every Lightsail instance in the region, following pagination.
        Lightsail has no server-side tag filter, so callers filter client-side."""
        instances = []
        token = None
        while True:
            resp = self.ls.get_instances(**({"pageToken": token} if token else {}))
            instances.extend(resp.get("instances", []))
            token = resp.get("nextPageToken")
            if not token:
                return instances

    def _find(self, name: str) -> Optional[dict]:
        """The instance whose devbox:name tag is the canonical `name`, or None."""
        for inst in self._all_instances():
            if self._tag(inst, NAME_TAG) == name:
                return inst
        return None

    @staticmethod
    def _tag(instance: dict, key: str) -> Optional[str]:
        for t in instance.get("tags", []):
            if t.get("key") == key:
                return t.get("value")
        return None

    def state(self, name: str) -> Optional[str]:
        inst = self._find(name)
        return inst.get("state", {}).get("name") if inst else None

    def address(self, name: str) -> Optional[str]:
        # Dynamic public IPv4: no static IP is attached, so the address can change
        # across a stop/start. Resolved fresh per command rather than cached.
        inst = self._find(name)
        if not inst:
            return None
        return inst.get("publicIpAddress") or None

    def list(self) -> list:
        out = []
        for inst in self._all_instances():
            if self._tag(inst, MANAGED_TAG) != "true":
                continue
            name = self._tag(inst, NAME_TAG)
            if not name:
                continue
            out.append(Instance(
                name=name,
                provider="aws",
                project=self._tag(inst, PROJECT_TAG),
                state=inst.get("state", {}).get("name", "unknown"),
                resources=inst.get("bundleId", "?"),
                created_at=self._tag(inst, CREATED_AT_TAG),
                cost_hint=True,
            ))
        out.sort(key=lambda i: i.name)
        return out

    # ---- lifecycle ----------------------------------------------------------
    def start(self, name: str) -> None:
        inst = self._require(name)
        self.ls.start_instance(instanceName=inst["name"])

    def stop(self, name: str) -> None:
        inst = self._require(name)
        self.ls.stop_instance(instanceName=inst["name"])

    def destroy(self, name: str) -> None:
        inst = self._find(name)
        if not inst:
            print(f"instance '{name}' not found (already deleted?)")
            return
        self.ls.delete_instance(instanceName=inst["name"])

    def _require(self, name: str) -> dict:
        inst = self._find(name)
        if not inst:
            die(f"instance '{name}' not found")
        return inst

    # ---- create -------------------------------------------------------------
    def create(self, spec: InstanceSpec) -> None:
        # Unlike [ec2] (which needs an instance_type), [lightsail] is entirely
        # optional: every field has a default, so an absent section is fine.
        cfg = spec.lightsail or LightsailProjectConfig()

        # The AWS-side name drops the redundant provider prefix (the instance is
        # already known to be on Lightsail): aws-binius -> binius.
        _, bare = parse_instance_name(spec.name)

        # One listing answers both collision questions: is this devbox instance
        # already up, and is the (unprefixed) Lightsail name taken by something
        # else? Lightsail names are unique per account/region, so an untagged
        # instance named `bare` would otherwise fail deep inside boto3.
        existing = self._all_instances()
        if any(self._tag(i, NAME_TAG) == spec.name for i in existing):
            die(f"instance '{spec.name}' already exists; run `devbox destroy` first")
        if any(i.get("name") == bare for i in existing):
            die(f"a Lightsail instance named '{bare}' already exists in "
                f"{self.aws.region} but is not managed by devbox (devbox drops the "
                f"'aws-' prefix when naming instances AWS-side). Pick another "
                f"--name, or delete that instance first.")

        bundle = cfg.bundle or DEFAULT_LIGHTSAIL_BUNDLE
        # The region only carries a letter suffix at the AZ level; default to the
        # region's first AZ rather than making every project spell one out.
        az = cfg.availability_zone or (self.aws.region + "a")

        print(f"==> Launching Lightsail {bundle} instance '{spec.name}' ...")
        self.ls.create_instances(
            instanceNames=[bare],
            availabilityZone=az,
            blueprintId=cfg.blueprint,
            bundleId=bundle,
            # Lightsail takes userData as PLAIN TEXT (EC2's run_instances is the
            # one that base64-encodes). No keyPairName: the SSH user and pubkey
            # come from the init-script user-data, exactly as on EC2.
            userData=spec.user_data,
            ipAddressType="ipv4",
            tags=[
                {"key": MANAGED_TAG, "value": "true"},
                {"key": NAME_TAG, "value": spec.name},
                {"key": PROJECT_TAG, "value": spec.project},
                {"key": CREATED_AT_TAG, "value": spec.created_at},
            ],
        )
        print(f"    launched {bare} ({cfg.blueprint} in {az})")
