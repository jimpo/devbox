"""devbox: a global, multi-workspace dev-sandbox manager (libvirt + AWS).

The package is split by concern - `config`, `initscript`, `cli`, and the
`providers` package (a `Provider` seam with `libvirt`, `ec2`, and `aws`
/Lightsail impls). This module re-exports the public API as a flat `devbox.X`
surface for convenience.
"""
from __future__ import annotations

# Stdlib re-exports: several call sites share these module singletons, so tests
# monkeypatch e.g. `devbox.shutil.which` / `devbox.subprocess.run` and the patch
# is seen everywhere the module is used.
import shutil  # noqa: F401
import subprocess  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path  # noqa: F401

# Submodules, imported so `devbox.config`, `devbox.initscript`,
# `devbox.providers.libvirt`, ... are reachable as attributes (some tests
# monkeypatch module-local names there).
from . import cli, config, initscript, provision, sync, util  # noqa: F401
from .providers import awsauth, base, ec2, libvirt, lightsail  # noqa: F401

from .cli import main  # noqa: F401
from .initscript import (INIT_SCRIPT_PATH, render_init_script,  # noqa: F401
                         substitute_tokens)
from .provision import (BOX_BASE_MODS, BUILTIN_MODS_DIR, DEFAULT_RUNAS,  # noqa: F401
                        DRIVER_PATH, GLOBAL_MODS_DIR, MOD_METADATA_KEYS,
                        MOD_METADATA_TYPE, NIX_DIR, NIX_MODULES_DIR,
                        NIX_PAYLOAD_DIR, NIX_WORKSPACE_FILE, RUNAS_TARGETS,
                        SCOPE_BOX, SCOPE_WORKSPACE, WORKSPACE_BASE_MODS,
                        ModSpec, Playbook, PayloadFile, box_playbook,
                        build_box_payload, build_payload, build_tarball,
                        env_for_owner, find_mod_script, load_mod_specs,
                        nix_attr_name, nix_module_names, nix_payload_files,
                        nix_string, parse_mod_metadata, provision_log,
                        render_env_file, render_manifest, render_repos,
                        render_workspace_nix, resolve_env, resolve_jj_identity,
                        resolve_mod, resolve_playbook, scoped_runas,
                        validate_playbooks, workspace_playbook)
from .config import (ANCHOR_HOME, ANCHOR_REPO, ANCHOR_WORKSPACE,  # noqa: F401
                     AwsConfig, BoxSpec, DEFAULT_BASE_IMAGE_URL,
                     DEFAULT_CACHE_DIR, DEFAULT_DISK_GB, DEFAULT_LIBVIRT_URI,
                     DEFAULT_LIGHTSAIL_BLUEPRINT, DEFAULT_LIGHTSAIL_BUNDLE,
                     DEFAULT_MEMORY_MIB, DEFAULT_NETWORK, DEFAULT_PROVIDER,
                     DEFAULT_VCPUS, ENV_OWNERS, MODS_SUBDIR, REPO_MARKER_CONFIG,
                     REPO_MARKER_DIR, REPO_MODES, RESERVED_USERNAMES,
                     USERNAME_RE, WORKSPACE_MARKER_FILE,
                     Ec2BoxConfig, Ec2Config, EnvVar, GlobalConfig,
                     LightsailBoxConfig, Marker, PREFIXED_PROVIDERS, PROVIDERS,
                     RepoSpec, SyncEntry, WorkspaceConfig,
                     compose_instance_name, find_workspace, known_hosts_path,
                     parse_instance_name, resolve_box_spec, resolve_instance,
                     resolve_workspace)
from .sync import RsyncJob, plan_sync, run_envvars, run_sync  # noqa: F401
from .providers import (AwsAuthError, Ec2Provider, Instance, InstanceSpec,  # noqa: F401
                        LibvirtProvider, LightsailProvider, Provider,
                        all_providers, auth_error_message, get_provider,
                        is_auth_error)
from .providers.awsauth import (credential_problem, ensure_credentials,  # noqa: F401
                                login_command, make_client, prompt_login)
from .providers.libvirt import (CLOUDIMAGE_KEYRING_PATH, _format_resources,  # noqa: F401
                                build_metadata_xml, fetch_verified_checksum, read_domain_instance)
from .cli import _format_age  # noqa: F401
from .util import die, warn  # noqa: F401
