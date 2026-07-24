"""devbox: a global, multi-project dev-sandbox manager (libvirt + AWS).

The package is split by concern - `config`, `cloudconfig`, `cli`, and the
`providers` package (a `Provider` seam with `libvirt` and `aws` impls). This
module re-exports the public API as a flat `devbox.X` surface for convenience.
"""
from __future__ import annotations

# Stdlib re-exports: several call sites share these module singletons, so tests
# monkeypatch e.g. `devbox.shutil.which` / `devbox.subprocess.run` and the patch
# is seen everywhere the module is used.
import shutil  # noqa: F401
import subprocess  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path  # noqa: F401

# Submodules, imported so `devbox.config`, `devbox.cloudconfig`,
# `devbox.providers.libvirt`, ... are reachable as attributes (some tests
# monkeypatch module-local names there).
from . import cli, cloudconfig, config, provision, sync, util  # noqa: F401
from .providers import aws, base, libvirt  # noqa: F401

from .cli import main  # noqa: F401
from .cloudconfig import (COMMON_CLOUD_CONFIG_PATH, merge_cloud_config,  # noqa: F401
                          render_cloud_config, substitute_tokens)
from .provision import (BUILTIN_MODS_DIR, DEFAULT_BASE_MODS, DRIVER_PATH,  # noqa: F401
                        GLOBAL_MODS_DIR, PROVISION_LOG, PayloadFile,
                        build_payload, build_tarball, effective_mods,
                        render_env_file, resolve_env, resolve_jj_identity,
                        resolve_mod)
from .config import (AwsConfig, AwsProjectConfig, DEFAULT_BASE_IMAGE_URL,  # noqa: F401
                     DEFAULT_CACHE_DIR, DEFAULT_DISK_GB, DEFAULT_LIBVIRT_URI,
                     DEFAULT_MEMORY_MIB, DEFAULT_NETWORK, DEFAULT_PROVIDER,
                     DEFAULT_VCPUS, GlobalConfig, PREFIXED_PROVIDERS,
                     PROJECT_CLOUD_CONFIG_FILE, PROJECT_CONFIG_FILE, PROJECT_MARKER_DIR,
                     PROVIDERS, ProjectConfig, RepoConfig, SyncEntry, compose_instance_name,
                     find_project_dir, parse_instance_name, resolve_instance,
                     resolve_project)
from .sync import RsyncJob, plan_sync, run_envvars, run_sync  # noqa: F401
from .providers import (AwsProvider, Instance, InstanceSpec, LibvirtProvider,  # noqa: F401
                        Provider, all_providers, get_provider)
from .providers.libvirt import (CLOUDIMAGE_KEYRING_PATH, _format_resources,  # noqa: F401
                                build_metadata_xml, fetch_verified_checksum, read_domain_instance)
from .cli import _format_age  # noqa: F401
from .util import die, warn  # noqa: F401
