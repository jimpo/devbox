"""First-boot init-script rendering: token substitution into `init-script.sh`.

Provider-agnostic - libvirt (NoCloud seed), ec2 (UserData) and aws/Lightsail
(userData) all hand the very same rendered bash script to the instance as
user-data. A shell script is the only format all three accept: cloud-init runs a
`#!` user-data script on libvirt/ec2, and Lightsail executes user-data as a
script natively (it does not understand `#cloud-config`) - hence there is no
cloud-config layer.

The init script covers only genuine first-boot concerns (the login user + its
SSH key, baseline packages, BASH_ENV wiring, hostname); provisioning (the mods)
is driven entirely over SSH after boot (see cli.py / provision.py), not injected
here, and so is a workspace's own setup (a workspace mod, not a first-boot hook).
"""
from __future__ import annotations

from pathlib import Path

from .config import BoxSpec, GlobalConfig
from .util import die

INIT_SCRIPT_PATH = Path(__file__).parent / "init-script.sh"


def substitute_tokens(text: str, tokens: dict) -> str:
    """Literal token substitution (not {{...}}, which collides with cloud-init's
    own jinja syntax). Only %DEVBOX_USER%, %DEVBOX_SSH_PUBKEY% and
    %DEVBOX_HOSTNAME% appear in the init script - the mods get their context via
    the environment (see provision.py), not %TOKEN%s."""
    for key, value in tokens.items():
        text = text.replace(f"%{key}%", value)
    return text


def render_init_script(box: BoxSpec, gc: GlobalConfig) -> str:
    """The rendered first-boot user-data script for a box. Takes the box, not a
    workspace: first boot sets up the machine and its privileged account, and knows
    nothing about which workspaces will later attach to it."""
    pubkey = gc.read_ssh_pubkey()
    if not INIT_SCRIPT_PATH.is_file():
        die(f"devbox internal error: init script not found at {INIT_SCRIPT_PATH} "
            f"(broken install?)")

    tokens = {
        # The PRIVILEGED user only - a workspace's unprivileged agent account is
        # created by the provisioning driver at attach, not at first boot (see
        # init-script.sh), so that attaching a workspace reconverges an existing
        # box instead of needing a new one.
        "DEVBOX_USER": box.admin_user,
        "DEVBOX_SSH_PUBKEY": pubkey,
        "DEVBOX_HOSTNAME": box.name,
    }
    # The tokens land inside single-quoted shell assignments, so a value
    # carrying a quote or a newline would break out of (or corrupt) the script.
    # Refuse rather than ship a mangled first boot.
    for key, value in tokens.items():
        if "'" in value or "\n" in value:
            die(f"invalid value for %{key}%: must not contain a single quote or "
                f"a newline (got {value!r})")

    return substitute_tokens(INIT_SCRIPT_PATH.read_text(), tokens)
