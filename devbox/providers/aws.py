"""The AWS/EC2 provider (ported from cfleet's src/aws/ec2.ts). No local DB:
state is discovered via describe-instances filtered by devbox tags. run-instances
covers spot + on-demand, AMI-filter lookup, EBS sizing, and subnet capacity
fallback. boto3 is imported lazily so libvirt-only users need not have it, and
tests inject a fake client.

Tags stamped on every instance:
  devbox:managed = "true"       (the filter for "mine")
  devbox:name    = <name>       (devbox's canonical name, e.g. aws-binius-arm;
                                 how commands address the instance)
  devbox:project = <project>
  devbox:created-at = <ISO-8601>
  Name           = devbox-<bare> (console display; the provider prefix is
                                  redundant AWS-side, so aws-binius-arm shows
                                  as devbox-binius-arm)
"""
from __future__ import annotations

from typing import Optional

from ..config import (AwsConfig, AwsProjectConfig, DEFAULT_AMI_NAME_FILTER,
                      GlobalConfig, parse_instance_name)
from ..util import die
from .base import Instance, InstanceSpec, Provider

MANAGED_TAG = "devbox:managed"
NAME_TAG = "devbox:name"
PROJECT_TAG = "devbox:project"
CREATED_AT_TAG = "devbox:created-at"

# Instance states that still "exist" (terminated instances linger in
# describe-instances briefly but are gone) - mirrors cfleet.
LIVE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]

# AWS error codes meaning a specific subnet/AZ can't currently satisfy the
# request, so it's worth retrying in another subnet. Only surface synchronously
# for on-demand launches (persistent spot requests wait rather than throw).
CAPACITY_ERROR_CODES = {"InsufficientInstanceCapacity", "Unsupported"}


def _make_ec2_client(aws: AwsConfig):
    try:
        import boto3
    except ImportError:
        die("boto3 is required for the AWS provider: pip install boto3 "
            "(or `uv tool install --with boto3 devbox`)")
    session = boto3.Session(profile_name=aws.profile, region_name=aws.region)
    return session.client("ec2")


def _error_code(err: Exception) -> Optional[str]:
    """boto3/botocore ClientError code, duck-typed (no botocore import needed)."""
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


class AwsProvider(Provider):
    name = "aws"

    def __init__(self, gc: GlobalConfig, client=None):
        if gc.aws is None:
            die("[aws] is not configured in the global config (needs region/profile)")
        self.gc = gc
        self.aws = gc.aws
        self._client = client  # injected in tests; lazily created otherwise

    @property
    def ec2(self):
        if self._client is None:
            self._client = _make_ec2_client(self.aws)
        return self._client

    # ---- discovery ----------------------------------------------------------
    def _instances_matching(self, filters: list) -> list:
        resp = self.ec2.describe_instances(Filters=filters)
        return [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]

    def _find(self, name: str) -> Optional[dict]:
        insts = self._instances_matching([
            {"Name": f"tag:{NAME_TAG}", "Values": [name]},
            {"Name": "instance-state-name", "Values": LIVE_STATES},
        ])
        return insts[0] if insts else None

    @staticmethod
    def _tag(instance: dict, key: str) -> Optional[str]:
        for t in instance.get("Tags", []):
            if t.get("Key") == key:
                return t.get("Value")
        return None

    def state(self, name: str) -> Optional[str]:
        inst = self._find(name)
        return inst["State"]["Name"] if inst else None

    def address(self, name: str) -> Optional[str]:
        inst = self._find(name)
        if not inst:
            return None
        return inst.get("PublicIpAddress") or inst.get("PrivateIpAddress")

    def list(self) -> list:
        instances = self._instances_matching([
            {"Name": f"tag:{MANAGED_TAG}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": LIVE_STATES},
        ])
        out = []
        for inst in instances:
            name = self._tag(inst, NAME_TAG)
            if not name:
                continue
            out.append(Instance(
                name=name,
                provider="aws",
                project=self._tag(inst, PROJECT_TAG),
                state=inst.get("State", {}).get("Name", "unknown"),
                resources=inst.get("InstanceType", "?"),
                created_at=self._tag(inst, CREATED_AT_TAG),
                cost_hint=True,
            ))
        out.sort(key=lambda i: i.name)
        return out

    # ---- lifecycle ----------------------------------------------------------
    def start(self, name: str) -> None:
        inst = self._require(name)
        self.ec2.start_instances(InstanceIds=[inst["InstanceId"]])

    def stop(self, name: str) -> None:
        inst = self._require(name)
        self.ec2.stop_instances(InstanceIds=[inst["InstanceId"]])

    def destroy(self, name: str) -> None:
        inst = self._find(name)
        if not inst:
            print(f"instance '{name}' not found (already terminated?)")
            return
        self.ec2.terminate_instances(InstanceIds=[inst["InstanceId"]])

    def _require(self, name: str) -> dict:
        inst = self._find(name)
        if not inst:
            die(f"instance '{name}' not found")
        return inst

    # ---- AMI lookup ---------------------------------------------------------
    def _resolve_ami(self, aws: AwsProjectConfig) -> str:
        if aws.ami:
            return aws.ami
        name_filter = aws.ami_name_filter or DEFAULT_AMI_NAME_FILTER[aws.architecture]
        resp = self.ec2.describe_images(
            Owners=[aws.ami_owner],
            Filters=[
                {"Name": "name", "Values": [name_filter]},
                {"Name": "architecture", "Values": [aws.architecture]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = resp.get("Images", [])
        if not images:
            die(f"no AMI found matching name={name_filter!r} arch={aws.architecture} "
                f"owner={aws.ami_owner}")
        images.sort(key=lambda i: i.get("CreationDate", ""), reverse=True)
        return images[0]["ImageId"]

    # ---- create -------------------------------------------------------------
    def create(self, spec: InstanceSpec) -> None:
        aws = spec.aws
        if aws is None:
            die("project has no [aws] section; add instance_type (and optionally "
                "architecture/ami/spot) under [aws] to launch on AWS")

        if self._find(spec.name) is not None:
            die(f"instance '{spec.name}' already exists; run `devbox destroy` first")

        ami = self._resolve_ami(aws)
        use_spot = aws.spot if aws.spot is not None else self.aws.spot

        # The console-display Name drops the redundant provider prefix (the
        # instance is already known to be on AWS): aws-binius-arm -> devbox-binius-arm.
        _, bare = parse_instance_name(spec.name)

        params = {
            "ImageId": ami,
            "InstanceType": aws.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            # boto3 base64-encodes UserData for run_instances.
            "UserData": spec.user_data,
            "BlockDeviceMappings": [{
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": spec.disk_gb, "VolumeType": "gp3", "DeleteOnTermination": True},
            }],
            "TagSpecifications": [{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": MANAGED_TAG, "Value": "true"},
                    {"Key": NAME_TAG, "Value": spec.name},
                    {"Key": PROJECT_TAG, "Value": spec.project},
                    {"Key": CREATED_AT_TAG, "Value": spec.created_at},
                    {"Key": "Name", "Value": f"devbox-{bare}"},
                ],
            }],
        }
        if self.aws.security_groups:
            params["SecurityGroupIds"] = self.aws.security_groups
        if use_spot:
            params["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": {
                    "SpotInstanceType": "persistent",
                    "InstanceInterruptionBehavior": aws.interruption_behavior,
                },
            }

        # Candidate subnets, tried in order; [] means "let AWS choose the AZ".
        # On insufficient capacity against an *explicit* subnet, fall back.
        candidates = self.aws.subnets or [None]
        market = "spot" if use_spot else "on-demand"
        print(f"==> Launching {market} {aws.instance_type} instance '{spec.name}' ...")
        last_error = None
        for i, subnet in enumerate(candidates):
            call = dict(params)
            if subnet is not None:
                call["SubnetId"] = subnet
            try:
                resp = self.ec2.run_instances(**call)
            except Exception as err:  # noqa: BLE001 - re-raised below unless capacity
                if subnet is None or _error_code(err) not in CAPACITY_ERROR_CODES:
                    raise
                last_error = err
                nxt = candidates[i + 1] if i + 1 < len(candidates) else None
                print(f"    no capacity in {subnet}, trying {nxt or 'next subnet'} ...")
                continue
            instance = resp["Instances"][0]
            print(f"    launched {instance['InstanceId']} ({instance['State']['Name']})")
            return

        detail = f": {last_error}" if last_error else ""
        die(f"no capacity for {aws.instance_type} in any configured subnet "
            f"({', '.join(str(s) for s in self.aws.subnets)}){detail}")
