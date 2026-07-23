"""Cloud-config rendering: token substitution and common+project merge.
Provider-agnostic - libvirt (NoCloud seed) and AWS (EC2 user-data) render the
same cloud-config. The cloud-config layer covers only genuine first-boot concerns
(the login user, baseline packages, BASH_ENV wiring); provisioning (the mods) is
driven entirely over SSH after boot (see cli.py / provision.py), not injected here.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .config import GlobalConfig, ProjectConfig
from .util import die

COMMON_CLOUD_CONFIG_PATH = Path(__file__).parent / "common-cloud-config.yaml"

# Cloud-config keys whose values get concatenated (common-first) rather than
# whole-value-overridden when merging the common template with a project's
# fragment.
LIST_MERGE_KEYS = {"packages", "write_files", "runcmd", "bootcmd", "users", "groups"}


def substitute_tokens(text: str, tokens: dict) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax and with valid YAML flow-mapping syntax). Only %DEVBOX_USER%
    and %DEVBOX_SSH_PUBKEY% are substituted into the YAML - the mods get their
    context via the environment (see provision.py), not %TOKEN%s."""
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
    # survive a parse->dump round trip. Without it, the VM boots but cloud-init
    # never runs (no user, no packages) - silently.
    return "#cloud-config\n" + body
