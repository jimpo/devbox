"""devbox: a global, multi-project dev-sandbox manager (libvirt + AWS).

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
from .provision import (BUILTIN_MODS_DIR, DEFAULT_BASE_MODS, DEFAULT_RUNAS,  # noqa: F401
                        DRIVER_PATH, GLOBAL_MODS_DIR, PROVISION_LOG,
                        RUNAS_TARGETS, ModSpec, PayloadFile,
                        build_payload, build_tarball, effective_mods,
                        load_mod_specs, parse_header, parse_packages,
                        parse_requires, parse_runas, render_env_file,
                        render_manifest, resolve_env, resolve_jj_identity,
                        resolve_mod, validate_mods)
from .config import (AwsConfig, DEFAULT_BASE_IMAGE_URL,  # noqa: F401
                     DEFAULT_CACHE_DIR, DEFAULT_DISK_GB, DEFAULT_LIBVIRT_URI,
                     DEFAULT_LIGHTSAIL_BLUEPRINT, DEFAULT_LIGHTSAIL_BUNDLE,
                     DEFAULT_MEMORY_MIB, DEFAULT_NETWORK, DEFAULT_PROVIDER,
                     DEFAULT_VCPUS, RESERVED_USERNAMES, USERNAME_RE,
                     Ec2Config, Ec2ProjectConfig,
                     GlobalConfig, LightsailProjectConfig, PREFIXED_PROVIDERS,
                     PROJECT_CONFIG_FILE, PROJECT_MARKER_DIR,
                     PROVIDERS, ProjectConfig, RepoConfig, SyncEntry, compose_instance_name,
                     find_project_dir, parse_instance_name, resolve_instance,
                     resolve_project)
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
