"""Cloud-config rendering: token substitution, common+project merge, and the
re-runnable provisioning script. Provider-agnostic - both libvirt (NoCloud seed)
and AWS (EC2 user-data) render the same cloud-config and run the same script.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from .config import GlobalConfig, GLOBAL_CONFIG_PATH, ProjectConfig
from .util import die

COMMON_CLOUD_CONFIG_PATH = Path(__file__).parent / "common-cloud-config.yaml"

# The standalone, re-runnable provisioning body (jj, dotfiles, Claude Code,
# repo bootstrap). Rendered (token-substituted) and injected into the first-boot
# cloud-config seed's write_files, and re-run over SSH by `devbox provision`.
PROVISION_SCRIPT_PATH = Path(__file__).parent / "provision-common.sh"
# Where cloud-init's first-boot copy of the script lands (root-owned, runnable
# before the dev user's home exists); must match common-cloud-config.yaml's
# runcmd. `devbox provision` instead drops it under the dev user's $HOME.
CLOUD_INIT_PROVISION_PATH = "/usr/local/bin/provision-common.sh"
# Provisioning output log, under the dev user's $HOME on the box - written by
# both the first-boot runcmd and `devbox provision`, and tailed by `devbox status`.
PROVISION_LOG = "provision.log"

# Cloud-config keys whose values get concatenated (common-first) rather than
# whole-value-overridden when merging the common template with a project's
# fragment. Order matters: common's write_files/runcmd install jj, dotfiles,
# and ~/.local/bin before any project-specific step that might assume they
# already exist.
LIST_MERGE_KEYS = {"packages", "write_files", "runcmd", "bootcmd", "users", "groups"}


def substitute_tokens(text: str, tokens: dict) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax and with valid YAML flow-mapping syntax)."""
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


def _jj_config_get(key: str) -> Optional[str]:
    """Best-effort read of the host's own jj config (e.g. user.name/user.email)."""
    if not shutil.which("jj"):
        return None
    r = subprocess.run(["jj", "config", "get", key], capture_output=True, text=True)
    value = r.stdout.strip()
    return value if r.returncode == 0 and value else None


def resolve_jj_identity(gc: GlobalConfig) -> tuple:
    """[jj] name/email from global config, falling back to the host's own
    `jj config get user.name`/`user.email` if unset."""
    name = gc.jj_name or _jj_config_get("user.name")
    email = gc.jj_email or _jj_config_get("user.email")
    if name is None or email is None:
        die("could not determine jj identity: set [jj] name/email in "
            f"{GLOBAL_CONFIG_PATH}, or configure it on this host with "
            "`jj config set --user user.name/user.email` (devbox falls back to that "
            "if [jj] isn't set)")
    return name, email


def resolve_github_token(gc: GlobalConfig) -> Optional[str]:
    """[github] token from global config. Best-effort, not required: cloning
    a public repo or [repo] mode "push"/"none" never need GitHub HTTPS auth
    in the sandbox at all."""
    return gc.github_token


def _provision_tokens(project: ProjectConfig, gc: GlobalConfig) -> dict:
    """The %DEVBOX_*% tokens substituted into the provisioning script. A superset
    (adding DEVBOX_SSH_PUBKEY) is used for the whole cloud-config. Shared by the
    first-boot render and `devbox provision` so both fill the same values."""
    jj_name, jj_email = resolve_jj_identity(gc)
    github_token = resolve_github_token(gc)
    if github_token is None:
        print("note: no GitHub token found ([github].token in global config) - "
              "the sandbox will have no GitHub HTTPS auth. Fine for cloning public "
              "repos; set one up if the project needs a private repo or gh/git "
              "pushes to GitHub.")
    return {
        "DEVBOX_USER": project.user,
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_GITHUB_TOKEN": github_token or "",
        "DEVBOX_REPO_MODE": project.repo.mode,
        "DEVBOX_REPO_DIR": project.repo.dir,
        "DEVBOX_REPO_URL": project.repo.url or "",
    }


def render_provision_script(project: ProjectConfig, gc: GlobalConfig) -> str:
    """The standalone provisioning script with its %DEVBOX_*% tokens filled in.
    Injected into the first-boot cloud-config seed (render_cloud_config) and
    re-run over SSH by `devbox provision` - one script, two delivery paths."""
    if not PROVISION_SCRIPT_PATH.is_file():
        die(f"devbox internal error: provisioning script not found at "
            f"{PROVISION_SCRIPT_PATH} (broken install?)")
    return substitute_tokens(PROVISION_SCRIPT_PATH.read_text(), _provision_tokens(project, gc))


def render_cloud_config(project: ProjectConfig, gc: GlobalConfig,
                        instance_name: Optional[str] = None) -> str:
    if not gc.ssh_pubkey_path.is_file():
        die(f"SSH public key not found: {gc.ssh_pubkey_path}")
    if not COMMON_CLOUD_CONFIG_PATH.is_file():
        die(f"devbox internal error: common cloud-config template not found at "
            f"{COMMON_CLOUD_CONFIG_PATH} (broken install?)")
    if not PROVISION_SCRIPT_PATH.is_file():
        die(f"devbox internal error: provisioning script not found at "
            f"{PROVISION_SCRIPT_PATH} (broken install?)")

    tokens = _provision_tokens(project, gc)
    tokens["DEVBOX_SSH_PUBKEY"] = gc.ssh_pubkey_path.read_text().strip()

    common_text = substitute_tokens(COMMON_CLOUD_CONFIG_PATH.read_text(), tokens)
    common = _load_cloud_config_yaml(common_text, COMMON_CLOUD_CONFIG_PATH)

    # Single source of truth: inject the standalone provisioning script into the
    # seed's write_files (the template's runcmd runs it on first boot; `devbox
    # provision` re-runs the very same script over SSH). Appended after the
    # template's own write_files so the /etc/environment entry stays first.
    script_text = substitute_tokens(PROVISION_SCRIPT_PATH.read_text(), tokens)
    common.setdefault("write_files", []).append({
        "path": CLOUD_INIT_PROVISION_PATH,
        "owner": "root:root",
        "permissions": "0755",
        "content": script_text,
    })

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
