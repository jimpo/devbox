"""Cloud-config rendering: token substitution, common+project merge, and
injection of the modular provisioning payload. Provider-agnostic - both libvirt
(NoCloud seed) and AWS (EC2 user-data) render the same cloud-config, and the
same driver + mods run on first boot and via `devbox provision` (see provision.py).
"""
from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from .config import GlobalConfig, ProjectConfig
from .provision import PROVISION_LOG, REMOTE_PROVISION_ROOT, build_payload
from .util import die

COMMON_CLOUD_CONFIG_PATH = Path(__file__).parent / "common-cloud-config.yaml"

# Cloud-config keys whose values get concatenated (common-first) rather than
# whole-value-overridden when merging the common template with a project's
# fragment. Order matters: common's write_files/runcmd (the provisioning payload
# and the driver run) come before any project-specific step that might assume it.
LIST_MERGE_KEYS = {"packages", "write_files", "runcmd", "bootcmd", "users", "groups"}


def substitute_tokens(text: str, tokens: dict) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax and with valid YAML flow-mapping syntax). Only %DEVBOX_USER%
    and %DEVBOX_SSH_PUBKEY% are substituted into the YAML now - the mods get
    their context via the environment (see provision.py), not %TOKEN%s."""
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


def _provision_runcmd(user: str) -> list:
    """The first-boot runcmd that runs the provisioning driver. write_files
    stages the payload root-owned; here (as root) we hand the 0600 secret.env to
    the dev user, then run the driver as them, teeing the combined log that
    `devbox status` tails (per-mod logs land in ~/provision/)."""
    driver = f"{REMOTE_PROVISION_ROOT}/provision-driver.sh"
    secret = f"{REMOTE_PROVISION_ROOT}/secret.env"
    inner = f"bash {driver} 2>&1 | tee $HOME/{PROVISION_LOG}"
    script = (f"[ -e {secret} ] && chown {user}: {secret}; "
              f"runuser -l {user} -c {shlex.quote(inner)}")
    return ["bash", "-c", script]


def render_cloud_config(project: ProjectConfig, gc: GlobalConfig,
                        instance_name: str = None) -> str:
    if not gc.ssh_pubkey_path.is_file():
        die(f"SSH public key not found: {gc.ssh_pubkey_path}")
    if not COMMON_CLOUD_CONFIG_PATH.is_file():
        die(f"devbox internal error: common cloud-config template not found at "
            f"{COMMON_CLOUD_CONFIG_PATH} (broken install?)")

    tokens = {
        "DEVBOX_USER": project.user,
        "DEVBOX_SSH_PUBKEY": gc.ssh_pubkey_path.read_text().strip(),
    }
    common_text = substitute_tokens(COMMON_CLOUD_CONFIG_PATH.read_text(), tokens)
    common = _load_cloud_config_yaml(common_text, COMMON_CLOUD_CONFIG_PATH)

    # Inject the modular provisioning payload (driver + resolved mods + env +
    # manifest) into write_files, and a runcmd that runs the driver on first
    # boot. Appended after the template's own write_files so /etc/environment
    # stays first. `devbox provision` delivers the identical payload over SSH.
    write_files = common.setdefault("write_files", [])
    for pf in build_payload(project, gc):
        write_files.append({
            "path": f"{REMOTE_PROVISION_ROOT}/{pf.path}",
            "owner": "root:root",
            "permissions": f"{pf.mode:04o}",
            "content": pf.content,
        })
    common.setdefault("runcmd", []).append(_provision_runcmd(project.user))

    if project.cloud_config is not None:
        project_text = substitute_tokens(project.cloud_config.read_text(), tokens)
        project_data = _load_cloud_config_yaml(project_text, project.cloud_config)
    else:
        project_data = {}

    merged = merge_cloud_config(common, project_data)
    # The instance's hostname: the target name (= the project for the libvirt
    # default, or the explicit --name for a cloud instance), unless a fragment
    # overrides it.
    merged.setdefault("hostname", instance_name or project.project)

    body = yaml.dump(merged, Dumper=_CloudConfigDumper, sort_keys=False, default_flow_style=False)
    # yaml.safe_load drops comments, so the magic first-line marker cloud-init's
    # NoCloud datasource relies on to recognize this as cloud-config does not
    # survive a parse->dump round trip. Without it, the VM boots but is
    # silently never provisioned - no error anywhere.
    return "#cloud-config\n" + body
