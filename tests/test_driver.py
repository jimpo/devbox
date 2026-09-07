"""The provisioning driver (`provision-driver.sh`), exercised against stubbed
privileged commands.

The driver is bash that runs on a booted box as the privileged user, so its real
effects (useradd, apt, dropping to another account) cannot run in a test. What
CAN be tested is all the logic around them: manifest parsing, the runas
dispatch, package-install ordering, selective runs, failure propagation, and the
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
WORKSPACE = "binius"
HOST = "laptop"

# Each fake logs to $FAKE_LOG/<tool>.log and otherwise does the least it can.
_FAKES = {
    # `sudo -l -U <user>` answers "does this user have sudo privileges?" in its
    # listing, and exits 0 either way - as sudo-rs does. A listing of granted
    # commands is what the driver treats as fatal. Everything else is executed
    # for real, with HOME/FAKE_RUNAS set as `sudo -u <user> -H` would.
    "sudo": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/sudo.log"
if [ "$1" = "-l" ] || [ "$2" = "-l" ]; then
  user="${@: -1}"
  if [ "${FAKE_AGENT_HAS_SUDO:-1}" = "0" ]; then
    printf 'User %s may run the following commands on box:\n    (ALL) ALL\n' "$user"
  else
    printf 'User %s is not allowed to run sudo on box.\n' "$user"
  fi
  exit 0
fi
if [ "$1" = "-u" ]; then
  user="$2"; shift 2
  if [ "${1:-}" = "-H" ]; then shift; export HOME="$FAKE_HOMES/$user"; fi
  export FAKE_RUNAS="$user"
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
    # systemd's session manager. There is none here, so it records the call and
    # succeeds - or fails on demand, which is the box that has no user manager
    # to linger.
    "loginctl": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/loginctl.log"
[ "${FAKE_LOGINCTL_FAILS:-0}" = "1" ] && exit 1
exit 0
""",
    "chown": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/chown.log"
""",
    # The account-teardown pair. `userdel -r` takes the home with it, which is
    # what makes a purge different from a plain detach.
    "usermod": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/usermod.log"
""",
    "userdel": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/userdel.log"
user="${@: -1}"
case " $* " in *" -r "*) rm -rf "$FAKE_HOMES/$user" ;; esac
""",
    "pkill": r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/pkill.log"
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
    """One fake box: the payload dir, the fake $HOME, and the run() entry point.

    `agent=""` builds a BOX run (the box's own playbook, no attachment), which is
    exactly how the driver tells the two kinds of run apart."""

    def __init__(self, root, mods, repos=(), agent=AGENT,
                 workspace=WORKSPACE, scope=None, host=HOST):
        self.root = root
        self.log_dir = root / "log"
        self.homes = root / "homes"
        self.payload = root / "payload"
        self.bin = root / "bin"
        self.etc = root / "etc"
        # The box's nix state directory (/nix/var/nix on a real box), where the
        # daemon keeps the roots it owns.
        self.nix_state = root / "nix-var"
        self.agent = agent
        for d in (self.log_dir, self.homes, self.payload, self.bin, self.etc,
                  self.nix_state, self.payload / "mods"):
            d.mkdir(parents=True, exist_ok=True)
        # The privileged user already exists (the init script made it at boot);
        # an agent does not - the driver's preamble is what creates it. root has
        # a home on any real box, and a root-target mod runs from it.
        (self.homes / ADMIN).mkdir(exist_ok=True)
        (self.homes / "root").mkdir(exist_ok=True)

        for name, body in _FAKES.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

        scope = scope or (devbox.SCOPE_WORKSPACE if agent else devbox.SCOPE_BOX)
        (self.payload / "provision-driver.sh").write_text(devbox.DRIVER_PATH.read_text())
        # Real renderers, so this also pins the Python->bash format agreement.
        (self.payload / "manifest").write_text(devbox.render_manifest(mods, scope))
        (self.payload / "context.env").write_text(devbox.render_env_file({
            "DEVBOX_ADMIN_USER": ADMIN,
            "DEVBOX_AGENT_USER": agent,
            "DEVBOX_WORKSPACE": workspace,
            "DEVBOX_ATTACH_HOST": host,
            "DEVBOX_SSH_PUBKEY": "ssh-ed25519 AAAAtest user@host",
            "DEVBOX_REPO_DIRS": ",".join(r.dir for r in repos),
        }))
        # Real renderer again: the `repos` file is the other half of the
        # Python->bash format agreement (see render_repos).
        (self.payload / "repos").write_text(devbox.render_repos(repos))
        for spec in mods:
            path = self.payload / "mods" / f"{spec.name}.sh"
            path.write_text(_MOD.format(name=spec.name,
                                        extra=getattr(spec, "_extra", "")))
            path.chmod(0o755)

    def _env(self, **overrides):
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.homes / ADMIN),
            "FAKE_HOMES": str(self.homes),
            "FAKE_LOG": str(self.log_dir),
            # The real markers are root-owned under /etc; point them at the fake
            # box so the driver's own writes and reads can run unprivileged.
            "DEVBOX_WORKSPACES_DIR": str(self.etc / "workspaces"),
            "DEVBOX_LEGACY_MARKERS_DIR": str(self.etc / "projects"),
            # Same seam for /nix/var/nix, which detach clears of this account.
            "NIX_STATE_DIR": str(self.nix_state),
            **overrides,
        }

    def run(self, *args, **env_overrides):
        return subprocess.run(
            ["bash", str(self.payload / "provision-driver.sh"), *args],
            capture_output=True, text=True, env=self._env(**env_overrides))

    def detach(self, agent=None, purge=False, **env_overrides):
        """The root-owned teardown `devbox detach` sends to the box, run here.

        It is a shell command built host-side (cli._detach_script) rather than a
        driver mode - detach ships no payload, so there is no driver on the box
        to call - but it is the same kind of privileged account surgery the
        driver does, against the same fakes."""
        script = devbox.cli._detach_script(agent or self.agent, purge)
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=self._env(**env_overrides))

    def seed_nix_state(self, agent=None):
        """What an account looks like after a `home-manager switch`: the
        generation profile and one link per generation it has had, the nix
        profile `~/.nix-profile` points at, and home-manager's own GC root -
        every one of them inside the account's own home, which is where nix
        2.35 and home-manager 26.05 put a non-root user's. The per-user
        directories under the box's nix state are the other place a root for a
        named account can sit."""
        home = self.homes / (agent or self.agent)
        gen = "/nix/store/00000000-home-manager-generation"
        profiles = home / ".local" / "state" / "nix" / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        for link in ("home-manager", "home-manager-1-link", "home-manager-2-link",
                     "profile", "profile-1-link"):
            (profiles / link).symlink_to(gen)
        (home / ".nix-profile").symlink_to(profiles / "profile")
        gcroots = home / ".local" / "state" / "home-manager" / "gcroots"
        gcroots.mkdir(parents=True, exist_ok=True)
        (gcroots / "current-home").symlink_to(gen)
        for d in ("profiles", "gcroots"):
            (self.nix_state / d / "per-user" / (agent or self.agent)).mkdir(
                parents=True, exist_ok=True)

    def nix_roots(self, agent=None):
        """Every path this account's GC roots would live at, existing or not."""
        home = self.homes / (agent or self.agent)
        return [home / ".local" / "state" / "nix" / "profiles",
                home / ".local" / "state" / "home-manager",
                home / ".nix-profile",
                self.nix_state / "profiles" / "per-user" / (agent or self.agent),
                self.nix_state / "gcroots" / "per-user" / (agent or self.agent)]

    def log(self, name):
        path = self.log_dir / f"{name}.log"
        return path.read_text() if path.exists() else ""

    def provision_logs(self, agent=None):
        """The driver's per-mod log directory for one attachment."""
        return self.homes / ADMIN / "provision" / (agent or self.agent or "box")

    def marker(self, agent=None):
        """The ownership record the driver wrote for one account."""
        path = self.etc / "workspaces" / (agent or self.agent)
        return path.read_text() if path.exists() else ""

    def write_legacy_marker(self, owner, agent=None):
        """The claim a devbox that still called a workspace a project left."""
        d = self.etc / "projects"
        d.mkdir(parents=True, exist_ok=True)
        (d / (agent or self.agent)).write_text(
            f"project={owner}\nagent_user={agent or self.agent}\nhost={HOST}\n")

    def legacy_marker_exists(self, agent=None):
        return (self.etc / "projects" / (agent or self.agent)).exists()

    def ran(self):
        """[(mod, account)] in the order the driver ran them."""
        return [tuple(line.split(":")) for line in self.log("mods").splitlines()]


def _repo(name, mode="push", dir=None, url=None):
    """One entry of the workspace's `repos` payload file."""
    return devbox.RepoSpec(name=name, mode=mode, dir=dir or name,
                           host=devbox.Path(f"/host/{name}"), url=url)


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


def test_preamble_puts_nix_on_the_non_interactive_path(box):
    # nix's installer wires /etc/profile.d and the interactive shell rc files;
    # `ssh box '<cmd>'` - what devbox and an agent's Bash tool run - reads
    # neither, so ~/.bash_env is where the store and this account's
    # home-manager generation have to be named. Both accounts: the file is one
    # per user and the admin's shells need the store too.
    b = box([_spec("jj")])
    b.run("--ensure-users")
    for user in (ADMIN, AGENT):
        content = (b.homes / user / ".bash_env").read_text()
        assert "/etc/profile.d/nix.sh" in content
        assert "$HOME/.nix-profile/bin" in content
        # home.sessionVariables reach a non-interactive shell through this.
        assert ".nix-profile/etc/profile.d/hm-session-vars.sh" in content


def test_bash_env_puts_the_home_manager_generation_ahead_of_local_bin(box):
    # Source the real file and read the PATH it produces: the generation is the
    # declarative answer, so a binary an imperative mod left in ~/.local/bin
    # before its conversion must not shadow it.
    b = box([_spec("jj")])
    b.run("--ensure-users")
    home = b.homes / AGENT
    proc = subprocess.run(
        ["bash", "-c", f'. "{home}/.bash_env"; printf "%s" "$PATH"'],
        capture_output=True, text=True, env={**os.environ, "HOME": str(home),
                                             "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    entries = proc.stdout.split(":")
    assert entries.index(f"{home}/.nix-profile/bin") < \
        entries.index(f"{home}/.local/bin")


def test_preamble_enables_lingering_for_the_agent(box):
    # A lingering account keeps a systemd user manager with no session open, so
    # a service the agent enables outlives its SSH connection.
    b = box([_spec("jj")])
    assert b.run("--ensure-users").returncode == 0
    assert f"enable-linger {AGENT}" in b.log("loginctl")
    assert ADMIN not in b.log("loginctl")


def test_a_box_run_lingers_nobody(box):
    # No attachment, so there is no unprivileged account to keep a user manager
    # for.
    b = box([_spec("dotfiles", runas=["admin"])], agent="")
    assert b.run().returncode == 0
    assert b.log("loginctl") == ""


def test_lingering_failure_does_not_stop_the_attachment(box):
    # A box with no systemd has no user manager to linger; provisioning is
    # otherwise unaffected, so it is a note rather than a failure.
    b = box([_spec("jj")])
    r = b.run(FAKE_LOGINCTL_FAILS="1")
    assert r.returncode == 0, r.stderr
    assert "lingering" in r.stderr
    assert b.ran() == [("jj", AGENT)]


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
    # A checkout in the privileged user's home means the box predates the split.
    b = box([_spec("jj")], repos=[_repo("binius64")])
    (b.homes / ADMIN / "binius64").mkdir()
    r = b.run("--ensure-users")
    assert r.returncode == 0
    assert "the two-user split" in r.stderr
    assert "binius64" in r.stderr


def test_preamble_warns_once_per_stale_checkout(box):
    # A workspace holds several checkouts, so the warning is per repo, not per
    # attachment - a box can predate the split for some of them and not others.
    b = box([_spec("jj")], repos=[_repo("frontend"), _repo("backend"),
                                 _repo("protos")])
    (b.homes / ADMIN / "frontend").mkdir()
    (b.homes / ADMIN / "protos").mkdir()
    r = b.run("--ensure-users")
    assert r.returncode == 0
    assert "frontend" in r.stderr and "protos" in r.stderr
    assert "backend" not in r.stderr


# ---- mod dispatch -----------------------------------------------------------
def test_mods_run_in_manifest_order_as_the_agent_by_default(box):
    b = box([_spec("jj"), _spec("claude-code"), _spec("repo-bootstrap")])
    r = b.run()
    assert r.returncode == 0, r.stderr
    assert b.ran() == [("jj", AGENT), ("claude-code", AGENT), ("repo-bootstrap", AGENT)]


def test_runas_root_and_admin_dispatch_to_the_right_accounts(box):
    b = box([_spec("box-packages", runas=["root"]),
             _spec("claude-code", runas=["agent"]),
             _spec("something-admin", runas=["admin"])])
    assert b.run().returncode == 0
    assert b.ran() == [("box-packages", "root"),
                       ("claude-code", AGENT),
                       ("something-admin", ADMIN)]


# ---- box runs (no attachment) -----------------------------------------------
def test_a_box_run_creates_no_agent_account(box):
    # The box's own playbook: a machine that no workspace has attached to yet.
    b = box([_spec("box-packages", runas=["root"]),
             _spec("dotfiles", runas=["admin", "agent"])], agent="")
    r = b.run()
    assert r.returncode == 0, r.stderr
    assert b.log("useradd") == ""
    assert not (b.etc / "workspaces").exists()
    # `dotfiles` keeps only its admin half - the agent half is an attachment's.
    assert b.ran() == [("box-packages", "root"), ("dotfiles", ADMIN)]


def test_a_box_run_still_wires_the_privileged_account(box):
    b = box([_spec("dotfiles", runas=["admin"])], agent="")
    assert b.run().returncode == 0
    assert ".local/bin" in (b.homes / ADMIN / ".bash_env").read_text()


def test_a_box_run_refuses_an_agent_target_it_cannot_satisfy(box):
    # Unreachable through the host-side scope filter; a payload that got it
    # wrong anyway must stop rather than run the mod as somebody else.
    b = box([_spec("jj", runas=["agent"])], agent="", scope=devbox.SCOPE_WORKSPACE)
    r = b.run()
    assert r.returncode == 2
    assert "no agent account" in r.stderr
    assert b.ran() == []


# ---- attachment ownership ---------------------------------------------------
def test_attaching_records_which_workspace_owns_the_account(box):
    b = box([_spec("jj")], repos=[_repo("frontend"), _repo("backend")])
    assert b.run("--ensure-agent").returncode == 0
    marker = b.marker()
    assert f"workspace={WORKSPACE}" in marker
    assert f"agent_user={AGENT}" in marker
    assert "repo_dirs=frontend,backend" in marker
    assert f"host={HOST}" in marker


def test_a_legacy_marker_still_identifies_the_owner(box):
    # A box last attached by a devbox that wrote `project=` must not read as an
    # account nobody claims - that would demand --adopt to reach your own home.
    b = box([_spec("jj")])
    (b.homes / AGENT).mkdir()           # exists, from a previous attachment
    b.write_legacy_marker(WORKSPACE)
    assert b.run("--ensure-agent").returncode == 0
    assert f"workspace={WORKSPACE}" in b.marker()


def test_a_legacy_marker_owned_by_another_workspace_still_refuses(box):
    b = box([_spec("jj")])
    (b.homes / AGENT).mkdir()           # exists, from a previous attachment
    b.write_legacy_marker("someone-else")
    r = b.run("--ensure-agent")
    assert r.returncode != 0
    assert "someone-else" in r.stderr


def test_attaching_retires_the_legacy_marker(box):
    # Both markers surviving would have `--attached` count the account twice.
    b = box([_spec("jj")])
    (b.homes / AGENT).mkdir()           # exists, from a previous attachment
    b.write_legacy_marker(WORKSPACE)
    assert b.run("--ensure-agent").returncode == 0
    assert not b.legacy_marker_exists()


def test_re_attaching_the_same_workspace_is_a_no_op(box):
    b = box([_spec("jj")])
    assert b.run("--ensure-agent").returncode == 0
    first = b.log("useradd")
    r = b.run("--ensure-agent")
    assert r.returncode == 0, r.stderr
    assert b.log("useradd") == first          # no second useradd
    assert f"workspace={WORKSPACE}" in b.marker()


def test_attaching_refuses_an_account_owned_by_another_workspace(box):
    # Two workspaces sharing one home would share a checkout and a creds file, so
    # "someone already lives here" has to be an error, not silent co-tenancy.
    b = box([_spec("jj")])
    assert b.run("--ensure-agent").returncode == 0
    other = box([_spec("jj")], workspace="other-workspace")
    r = other.run("--ensure-agent")
    assert r.returncode != 0
    assert WORKSPACE in r.stderr          # names the workspace that already has it
    assert "agent_user" in r.stderr     # and says how to resolve it


def test_attaching_refuses_an_unclaimed_pre_existing_account(box):
    # A box built before devbox tracked attachments, or an account made by hand.
    b = box([_spec("jj")])
    (b.homes / AGENT).mkdir()           # exists, but no ownership marker
    r = b.run("--ensure-agent")
    assert r.returncode != 0
    assert "--adopt" in r.stderr


def test_adopt_claims_an_unclaimed_pre_existing_account(box):
    b = box([_spec("jj")])
    (b.homes / AGENT).mkdir()
    r = b.run("--ensure-agent", DEVBOX_ATTACH_ADOPT="1")
    assert r.returncode == 0, r.stderr
    assert f"workspace={WORKSPACE}" in b.marker()


def test_re_attaching_from_another_workstation_is_noted_not_refused(box):
    b = box([_spec("jj")])
    assert b.run("--ensure-agent").returncode == 0
    elsewhere = box([_spec("jj")], host="desktop")
    r = elsewhere.run("--ensure-agent")
    assert r.returncode == 0, r.stderr
    assert "laptop" in r.stderr


def test_two_workspaces_can_share_one_box(box, tmp_path):
    # The whole point: the preamble touches only its own accounts, so attaching
    # a second workspace leaves the first's home, key and marker intact.
    first = _Box(tmp_path, [_spec("jj")])
    assert first.run().returncode == 0
    second = _Box(tmp_path, [_spec("jj")], agent="webbot", workspace="web")
    r = second.run()
    assert r.returncode == 0, r.stderr

    for agent in (AGENT, "webbot"):
        assert (first.homes / agent).is_dir()
        assert "ssh-ed25519" in (first.homes / agent / ".ssh" / "authorized_keys").read_text()
        assert ".local/bin" in (first.homes / agent / ".bash_env").read_text()
    assert f"workspace={WORKSPACE}" in first.marker(AGENT)
    assert "workspace=web" in first.marker("webbot")
    # The second attach must not have re-created the first workspace's account.
    assert first.log("useradd").count(AGENT) == 1


def test_ensure_admin_alone_touches_no_agent_account(box):
    b = box([_spec("jj")])
    assert b.run("--ensure-admin").returncode == 0
    assert not (b.homes / AGENT).exists()
    assert ".local/bin" in (b.homes / ADMIN / ".bash_env").read_text()


def test_a_multi_target_mod_runs_once_per_account_with_separate_logs(box):
    # A mod that names no agent target keeps all of them in either scope - a
    # workspace mod that has both root and privileged-user work, say.
    b = box([_spec("cuda", runas=["root", "admin"])])
    assert b.run().returncode == 0
    assert b.ran() == [("cuda", "root"), ("cuda", ADMIN)]
    assert (b.provision_logs() / "cuda.root.log").exists()
    assert (b.provision_logs() / f"cuda.{ADMIN}.log").exists()


def test_a_single_target_mod_keeps_the_familiar_log_path(box):
    b = box([_spec("jj")])
    assert b.run().returncode == 0
    assert (b.provision_logs() / "jj.log").exists()


def test_per_mod_logs_are_scoped_to_the_attachment(box):
    # Two workspaces reconverging on one box must not overwrite each other's logs.
    b = box([_spec("jj")])
    assert b.run().returncode == 0
    assert (b.homes / ADMIN / "provision" / AGENT / "jj.log").exists()


def test_the_mod_body_runs_with_the_target_users_home(box):
    b = box([_spec("jj", extra='echo "home=$HOME" >> "$FAKE_LOG/homes.log"')])
    b.run()
    assert f"home={b.homes / AGENT}" in b.log("homes")


def test_the_mod_body_runs_from_the_target_users_home(box):
    # sudo hands the caller's working directory to the target shell, and the
    # driver's is the privileged user's home - which the agent cannot enter. A
    # mod whose tools forward their own cwd (sccache -> rustc) fails there, so
    # the drop has to land in the account's own home.
    b = box([_spec("jj", extra='echo "pwd=$PWD" >> "$FAKE_LOG/cwd.log"')])
    b.run()
    assert f"pwd={b.homes / AGENT}" in b.log("cwd")


def test_each_mod_reads_the_creds_of_the_account_that_owns_them(box):
    # The two accounts hold different [env] entries (`owner`, host-side), so a
    # mod must read its own account's file and nothing else. A root mod reads
    # the privileged user's: root has no file, and that is where a cred a
    # privileged mod needs lives.
    echo = 'echo "{name}:agent=${{AGENT_CRED:-}}:admin=${{ADMIN_CRED:-}}" >> "$FAKE_LOG/creds.log"'
    b = box([_spec("claude-code", extra=echo.format(name="claude-code")),
             _spec("docker", runas=["root"], extra=echo.format(name="docker"))])
    b.run("--ensure-users")           # creates the agent home the file goes in
    for user, var, value in ((AGENT, "AGENT_CRED", "a_tok"), (ADMIN, "ADMIN_CRED", "x_tok")):
        env_dir = b.homes / user / ".config" / "devbox"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "env").write_text(f"export {var}={value}\n")
    assert b.run().returncode == 0
    assert "claude-code:agent=a_tok:admin=" in b.log("creds")
    assert "docker:agent=:admin=x_tok" in b.log("creds")


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


# ---- detach -----------------------------------------------------------------
# `devbox detach` is the other half of `ensure_agent`: root-owned account
# surgery on the box. A home-manager generation, its profile and every past
# generation are nix GC roots, so an account that is detached but still holds
# them pins its whole store closure forever - a box that has hosted a dozen
# workspaces then fills with the closures of workspaces nobody uses.
def test_detach_removes_the_agents_profile_and_gc_roots(box):
    b = box([_spec("home-manager")])
    assert b.run("--ensure-users").returncode == 0
    b.seed_nix_state()
    r = b.detach()
    assert r.returncode == 0, r.stderr
    for path in b.nix_roots():
        assert not os.path.lexists(path), path


def test_detach_keeps_the_account_files_it_drops_the_generation_from(box):
    # The default detach is non-destructive: a generation is derived and gets
    # rebuilt on re-attach, so it goes; work does not.
    b = box([_spec("home-manager")])
    assert b.run("--ensure-users").returncode == 0
    b.seed_nix_state()
    work = b.homes / AGENT / "repo"
    work.mkdir()
    (work / "notes.txt").write_text("uncommitted\n")
    assert b.detach().returncode == 0
    assert (work / "notes.txt").read_text() == "uncommitted\n"
    assert not os.path.lexists(b.homes / AGENT / ".nix-profile")
    assert b.marker() == ""                      # the claim is given up
    assert "--lock" in b.log("usermod")           # and nothing can log in


def test_detach_is_clean_on_a_box_with_no_nix(box):
    # An older box, or one whose `nix` mod never ran: there is no store, no
    # profile and no state directory, and the teardown still has to succeed.
    b = box([_spec("jj")])
    assert b.run("--ensure-users").returncode == 0
    r = b.detach()
    assert r.returncode == 0, r.stderr
    assert b.marker() == ""
    assert (b.homes / AGENT).is_dir()


def test_detach_runs_no_garbage_collection(box):
    # Dropping the roots is the part only detach can do; freeing the space is
    # the daemon's, under the min-free/max-free policy the box installs. A sweep
    # here would discard byproducts the other workspaces are about to reuse.
    b = box([_spec("home-manager")])
    assert b.run("--ensure-users").returncode == 0
    b.seed_nix_state()
    assert b.detach().returncode == 0
    assert "nix-collect-garbage" not in b.log("sudo")
    assert "nix-store" not in b.log("sudo")


def test_purging_takes_the_whole_home_and_its_roots(box):
    b = box([_spec("home-manager")])
    assert b.run("--ensure-users").returncode == 0
    b.seed_nix_state()
    r = b.detach(purge=True)
    assert r.returncode == 0, r.stderr
    assert not (b.homes / AGENT).exists()
    for path in b.nix_roots():
        assert not os.path.lexists(path), path


def test_detaching_one_workspace_leaves_the_others_profile_intact(box, tmp_path):
    # Two workspaces on one box share most of their closure, and nix keeps the
    # shared paths alive for whoever still roots them. What devbox has to get
    # right is narrower: detach must touch only the account it was given.
    first = _Box(tmp_path, [_spec("home-manager")])
    assert first.run().returncode == 0
    first.seed_nix_state()
    second = _Box(tmp_path, [_spec("home-manager")], agent="webbot", workspace="web")
    assert second.run().returncode == 0
    second.seed_nix_state()

    assert first.detach().returncode == 0
    for path in second.nix_roots("webbot"):
        assert os.path.lexists(path), path
    assert "workspace=web" in second.marker("webbot")
    assert (second.homes / "webbot" / ".bash_env").exists()
