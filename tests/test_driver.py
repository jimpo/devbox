"""The provisioning driver (`provision-driver.sh`), exercised against stubbed
privileged commands.

The driver is bash that runs on a booted box as the privileged user, so its real
effects (useradd, apt, dropping to another account) cannot run in a test. What
CAN be tested is all the logic around them: manifest parsing, the `# runas:`
dispatch, `# packages:` ordering, selective runs, failure propagation, and the
isolation assertions. This module puts fake `sudo`/`useradd`/`id`/`getent`/... on
PATH ahead of the real ones and asserts on what the driver tried to do.

The fake `sudo` records every invocation and then just executes the command, so
the driver's control flow is real; only the privilege is simulated. Notably it
exports FAKE_RUNAS, which lets a fake mod record which account the driver
intended to run it as - the single most important thing to get right here.

This is deliberately NOT a claim that the driver is verified: nothing here boots
a box, so `useradd` flags, sudoers behavior and apt are still hand-verification
territory (see AGENTS.md).
"""
import os
import subprocess

import pytest

import devbox

pytestmark = pytest.mark.skipif(
    not os.path.exists("/usr/bin/install"), reason="needs coreutils install(1)")

ADMIN = "alice"
AGENT = "agentbot"

# Each fake logs to $FAKE_LOG/<tool>.log and otherwise does the least it can.
_FAKES = {
    # `sudo -l -U <user>` answers "does this user have sudo privileges?" - exit 0
    # means yes, which the driver treats as fatal. Everything else is executed
    # for real, with HOME/FAKE_RUNAS set as `sudo -u <user> -H` would.
    "sudo": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/sudo.log"
if [ "$1" = "-l" ]; then exit "${FAKE_AGENT_HAS_SUDO:-1}"; fi
if [ "$1" = "-u" ]; then
  user="$2"; shift 2
  if [ "${1:-}" = "-H" ]; then shift; export HOME="$FAKE_HOMES/$user"; fi
  export FAKE_RUNAS="$user"
  exec "$@"
fi
exec "$@"
""",
    "useradd": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/useradd.log"
user="${@: -1}"
mkdir -p "$FAKE_HOMES/$user"
""",
    "passwd": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/passwd.log"
""",
    "chown": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/chown.log"
""",
    "apt-get": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/apt-get.log"
""",
    "getent": r"""#!/usr/bin/env bash
printf '%s:x:1000:1000::%s/%s:/bin/bash\n' "$2" "$FAKE_HOMES" "$2"
""",
    "id": r"""#!/usr/bin/env bash
case "$1" in
  -u)  [ -d "$FAKE_HOMES/$2" ] && exit 0 || exit 1 ;;
  -gn) printf '%s\n' "$2" ;;
  -nG) printf '%s\n' "${FAKE_GROUPS:-$2}" ;;
esac
""",
    # Real install, minus the -o/-g the test user cannot honor.
    "install": r"""#!/usr/bin/env bash
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
""",
}

# A mod body that records which account the driver ran it as.
_MOD = """#!/usr/bin/env bash
set -euo pipefail
printf '%s:%s\\n' "{name}" "${{FAKE_RUNAS:-none}}" >> "$FAKE_LOG/mods.log"
echo "{name} ran as ${{FAKE_RUNAS:-none}}"
{extra}
"""


class _Box:
    """One fake box: the payload dir, the fake $HOME, and the run() entry point."""

    def __init__(self, root, mods, repo_mode="none", repo_dir=""):
        self.root = root
        self.log_dir = root / "log"
        self.homes = root / "homes"
        self.payload = root / "payload"
        self.bin = root / "bin"
        for d in (self.log_dir, self.homes, self.payload, self.bin,
                  self.payload / "mods"):
            d.mkdir(parents=True, exist_ok=True)
        # The privileged user already exists (the init script made it at boot);
        # the agent does not - the driver's preamble is what creates it.
        (self.homes / ADMIN).mkdir(exist_ok=True)

        for name, body in _FAKES.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

        (self.payload / "provision-driver.sh").write_text(devbox.DRIVER_PATH.read_text())
        # Real renderers, so this also pins the Python->bash format agreement.
        (self.payload / "manifest").write_text(devbox.render_manifest(mods))
        (self.payload / "context.env").write_text(devbox.render_env_file({
            "DEVBOX_ADMIN_USER": ADMIN,
            "DEVBOX_AGENT_USER": AGENT,
            "DEVBOX_SSH_PUBKEY": "ssh-ed25519 AAAAtest user@host",
            "DEVBOX_REPO_MODE": repo_mode,
            "DEVBOX_REPO_DIR": repo_dir,
        }))
        for spec in mods:
            path = self.payload / "mods" / f"{spec.name}.sh"
            path.write_text(_MOD.format(name=spec.name,
                                        extra=getattr(spec, "_extra", "")))
            path.chmod(0o755)

    def run(self, *args, **env_overrides):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.homes / ADMIN),
            "FAKE_HOMES": str(self.homes),
            "FAKE_LOG": str(self.log_dir),
            **env_overrides,
        }
        return subprocess.run(
            ["bash", str(self.payload / "provision-driver.sh"), *args],
            capture_output=True, text=True, env=env)

    def log(self, name):
        path = self.log_dir / f"{name}.log"
        return path.read_text() if path.exists() else ""

    def ran(self):
        """[(mod, account)] in the order the driver ran them."""
        return [tuple(line.split(":")) for line in self.log("mods").splitlines()]


def _spec(name, runas=("agent",), packages=(), requires=(), extra=""):
    spec = devbox.ModSpec(name=name, path=devbox.Path(f"/x/{name}.sh"),
                          requires=list(requires), runas=list(runas),
                          packages=list(packages))
    spec._extra = extra
    return spec


@pytest.fixture
def box(tmp_path):
    def _make(mods, **kw):
        return _Box(tmp_path, mods, **kw)
    return _make


# ---- the user preamble ------------------------------------------------------
def test_ensure_users_creates_the_agent_and_runs_no_mods(box):
    b = box([_spec("jj")])
    r = b.run("--ensure-users")
    assert r.returncode == 0, r.stderr
    assert (b.homes / AGENT).is_dir()
    assert b.ran() == []          # --ensure-users is preamble only
    # No supplementary groups at all, and above all not sudo.
    assert "--groups" not in b.log("useradd")
    assert "HOME_MODE=0750" in b.log("useradd")


def test_ensure_users_is_idempotent(box):
    b = box([_spec("jj")])
    assert b.run("--ensure-users").returncode == 0
    first = b.log("useradd")
    assert b.run("--ensure-users").returncode == 0
    assert b.log("useradd") == first        # no second useradd


def test_preamble_authorizes_the_host_key_for_the_agent_once(box):
    b = box([_spec("jj")])
    b.run("--ensure-users")
    b.run("--ensure-users")
    keys = (b.homes / AGENT / ".ssh" / "authorized_keys").read_text()
    assert keys.count("ssh-ed25519 AAAAtest user@host") == 1


def test_preamble_writes_bash_env_for_both_accounts(box):
    # BASH_ENV=/etc/devbox/bash_env resolves to whichever user's file applies, so
    # both accounts need one for provisioned tools to reach non-interactive bash.
    b = box([_spec("jj")])
    b.run("--ensure-users")
    for user in (ADMIN, AGENT):
        content = (b.homes / user / ".bash_env").read_text()
        assert ".local/bin" in content
        assert ".config/devbox/env" in content


def test_preamble_refuses_when_the_agent_has_sudo(box):
    # The isolation is asserted, not assumed: partial sudo is not a lesser
    # version of this, it is equivalent to root.
    b = box([_spec("jj")])
    r = b.run(FAKE_AGENT_HAS_SUDO="0")
    assert r.returncode != 0
    assert "sudo" in r.stderr and AGENT in r.stderr
    assert b.ran() == []


def test_preamble_refuses_when_the_agent_is_in_a_root_equivalent_group(box):
    b = box([_spec("jj")])
    r = b.run(FAKE_GROUPS=f"{AGENT} sudo")
    assert r.returncode != 0
    assert "root-equivalent" in r.stderr


def test_preamble_warns_about_a_stale_checkout_in_the_admin_home(box):
    # A repo in the privileged user's home means the box predates the split.
    b = box([_spec("jj")], repo_mode="clone", repo_dir="binius64")
    (b.homes / ADMIN / "binius64").mkdir()
    r = b.run("--ensure-users")
    assert r.returncode == 0
    assert "the two-user split" in r.stderr
    assert "binius64" in r.stderr


# ---- mod dispatch -----------------------------------------------------------
def test_mods_run_in_manifest_order_as_the_agent_by_default(box):
    b = box([_spec("jj"), _spec("github-auth"), _spec("repo-bootstrap")])
    r = b.run()
    assert r.returncode == 0, r.stderr
    assert b.ran() == [("jj", AGENT), ("github-auth", AGENT), ("repo-bootstrap", AGENT)]


def test_runas_root_and_admin_dispatch_to_the_right_accounts(box):
    b = box([_spec("dotfiles-packages", runas=["root"]),
             _spec("claude-code", runas=["agent"]),
             _spec("something-admin", runas=["admin"])])
    assert b.run().returncode == 0
    assert b.ran() == [("dotfiles-packages", "root"),
                       ("claude-code", AGENT),
                       ("something-admin", ADMIN)]


def test_a_multi_target_mod_runs_once_per_account_with_separate_logs(box):
    b = box([_spec("dotfiles", runas=["admin", "agent"])])
    assert b.run().returncode == 0
    assert b.ran() == [("dotfiles", ADMIN), ("dotfiles", AGENT)]
    provision = b.homes / ADMIN / "provision"
    assert (provision / f"dotfiles.{ADMIN}.log").exists()
    assert (provision / f"dotfiles.{AGENT}.log").exists()


def test_a_single_target_mod_keeps_the_familiar_log_path(box):
    b = box([_spec("jj")])
    assert b.run().returncode == 0
    assert (b.homes / ADMIN / "provision" / "jj.log").exists()


def test_the_mod_body_runs_with_the_target_users_home(box):
    b = box([_spec("jj", extra='echo "home=$HOME" >> "$FAKE_LOG/homes.log"')])
    b.run()
    assert f"home={b.homes / AGENT}" in b.log("homes")


def test_context_env_reaches_the_mod_across_the_privilege_drop(box):
    # sudo resets the environment, so the context is re-sourced inside the target
    # shell. If that broke, every mod would see an empty DEVBOX_*.
    b = box([_spec("jj", extra='echo "jjname=$DEVBOX_AGENT_USER" >> "$FAKE_LOG/ctx.log"')])
    b.run()
    assert f"jjname={AGENT}" in b.log("ctx")


# ---- packages ---------------------------------------------------------------
def test_packages_are_installed_before_the_mod_body(box):
    b = box([_spec("claude-code", packages=["jq", "unzip"])])
    r = b.run()
    assert r.returncode == 0, r.stderr
    apt = b.log("apt-get")
    assert "install -y jq unzip" in apt
    assert "update" in apt                       # index refreshed first
    assert r.stdout.index("installing packages") < r.stdout.index("claude-code ran as")


def test_apt_update_runs_once_across_several_mods(box):
    b = box([_spec("a", packages=["jq"]), _spec("b", packages=["ripgrep"])])
    assert b.run().returncode == 0
    assert len([l for l in b.log("apt-get").splitlines() if "update" in l]) == 1


def test_no_apt_call_when_nothing_declares_packages(box):
    b = box([_spec("jj"), _spec("vim")])
    assert b.run().returncode == 0
    assert b.log("apt-get") == ""


# ---- selection and failure --------------------------------------------------
def test_selective_run_runs_only_the_named_mods(box):
    b = box([_spec("jj"), _spec("dotfiles"), _spec("vim")])
    assert b.run("vim").returncode == 0
    assert b.ran() == [("vim", AGENT)]


def test_selective_run_accepts_the_first_mod_in_the_playbook(box):
    # Index 0 is a legitimate manifest position; a truthiness test on the lookup
    # would reject it as "not in the playbook".
    b = box([_spec("jj"), _spec("vim")])
    r = b.run("jj")
    assert r.returncode == 0, r.stderr
    assert b.ran() == [("jj", AGENT)]


def test_selective_run_rejects_an_unknown_mod(box):
    b = box([_spec("jj")])
    r = b.run("nope")
    assert r.returncode == 2
    assert "unknown mod" in r.stderr
    assert b.ran() == []


def test_a_failing_mod_stops_the_run(box):
    b = box([_spec("jj"), _spec("boom", extra="exit 1"), _spec("vim")])
    r = b.run()
    assert r.returncode == 1
    assert "boom" in r.stderr
    assert b.ran() == [("jj", AGENT), ("boom", AGENT)]   # vim never runs


def test_a_missing_mod_script_is_reported(box):
    b = box([_spec("jj")])
    (b.payload / "mods" / "jj.sh").unlink()
    r = b.run()
    assert r.returncode == 127
    assert "mod script missing" in r.stderr


def test_the_full_run_ensures_users_first(box):
    # A plain run must create the agent too - not just `--ensure-users` - or the
    # first `sudo -u agent` would fail on a box that never had one.
    b = box([_spec("jj")])
    assert b.run().returncode == 0
    assert (b.homes / AGENT).is_dir()
