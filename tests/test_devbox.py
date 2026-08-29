import os
import dataclasses
import argparse
import subprocess

import pytest

import devbox


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._data


# ---- GlobalConfig -------------------------------------------------------
def test_global_config_defaults_when_missing(tmp_path):
    gc = devbox.GlobalConfig.load(tmp_path / "does-not-exist.toml")
    assert gc.libvirt_uri == devbox.DEFAULT_LIBVIRT_URI
    assert gc.network == devbox.DEFAULT_NETWORK
    assert gc.cache_dir == devbox.Path(devbox.DEFAULT_CACHE_DIR)
    assert gc.base_image_url == devbox.DEFAULT_BASE_IMAGE_URL
    assert gc.vcpus == devbox.DEFAULT_VCPUS
    assert gc.memory_mib == devbox.DEFAULT_MEMORY_MIB
    assert gc.disk_gb == devbox.DEFAULT_DISK_GB
    assert gc.jj_name is None
    assert gc.jj_email is None
    assert gc.env == {}
    assert gc.sync == []


def test_global_config_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
libvirt_uri = "qemu:///session"
network = "custom-net"
cache_dir = "/tmp/custom-cache"

[base_image]
url = "https://example.com/image.img"

[defaults]
vcpus = 2
memory_mib = 4096
disk_gb = 20

[ssh]
pubkey_path = "~/.ssh/other.pub"

[jj]
name = "Alice Example"
email = "alice@example.com"

[env]
GITHUB_TOKEN = "github_pat_example"
SOME_API_KEY = "sk-123"
""")
    gc = devbox.GlobalConfig.load(path)
    assert gc.libvirt_uri == "qemu:///session"
    assert gc.network == "custom-net"
    assert gc.cache_dir == devbox.Path("/tmp/custom-cache")
    assert gc.base_image_url == "https://example.com/image.img"
    assert gc.vcpus == 2
    assert gc.memory_mib == 4096
    assert gc.disk_gb == 20
    assert gc.ssh_pubkey_path == devbox.Path("~/.ssh/other.pub").expanduser()
    assert gc.jj_name == "Alice Example"
    assert gc.jj_email == "alice@example.com"
    assert gc.env == {"GITHUB_TOKEN": devbox.EnvVar("github_pat_example"),
                      "SOME_API_KEY": devbox.EnvVar("sk-123")}


def test_global_config_parses_sync_entries(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[sync]]\n'
        'src = "$HOME/.claude/skills"\n'
        'include = ["jujutsu"]\n'
    )
    gc = devbox.GlobalConfig.load(path)
    assert gc.sync == [
        devbox.SyncEntry(src="$HOME/.claude/skills", dst="$HOME/.claude/skills",
                         include=["jujutsu"], anchor=devbox.ANCHOR_HOME),
    ]


def test_global_config_sync_rejects_repo_relative_paths(tmp_path):
    # There is no [repo] at global scope, so a bare-relative src (the shape
    # [[repo.sync]] takes) is rejected the same way a workspace's own [[sync]]
    # rejects it.
    path = tmp_path / "config.toml"
    path.write_text('[[sync]]\nsrc = "relative/skills"\n')
    with pytest.raises(SystemExit):
        devbox.GlobalConfig.load(path)


def test_global_config_partial_override_keeps_other_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('network = "custom-net"\n')
    gc = devbox.GlobalConfig.load(path)
    assert gc.network == "custom-net"
    assert gc.libvirt_uri == devbox.DEFAULT_LIBVIRT_URI
    assert gc.vcpus == devbox.DEFAULT_VCPUS


# ---- workspace marker resolution -------------------------------------------
@pytest.fixture
def global_config():
    gc = devbox.GlobalConfig.load(devbox.Path("/nonexistent"))
    gc.jj_name = "Alice Example"
    gc.jj_email = "alice@example.com"
    return gc


def _write_marker(root, content, local=False):
    """The `.devbox/config.toml` spelling: the workspace root IS a repo."""
    devbox_dir = root / devbox.REPO_MARKER_DIR
    devbox_dir.mkdir(parents=True, exist_ok=True)
    (devbox_dir / devbox.REPO_MARKER_CONFIG).write_text(content)


def _write_visible_marker(root, content):
    """The `devbox.toml` spelling: the workspace root HOLDS repos."""
    root.mkdir(parents=True, exist_ok=True)
    (root / devbox.WORKSPACE_MARKER_FILE).write_text(content)


def test_find_workspace_at_start(tmp_path):
    _write_marker(tmp_path, 'name = "x"\n')
    assert devbox.find_workspace(tmp_path).root == tmp_path.resolve()


def test_find_workspace_walks_up(tmp_path):
    _write_marker(tmp_path, 'name = "x"\n')
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert devbox.find_workspace(deep).root == tmp_path.resolve()


def test_find_workspace_missing_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.find_workspace(tmp_path)


def test_find_workspace_repo_marker_shape(tmp_path):
    # `.devbox/config.toml`: the marker hides inside a repo, so the root IS one.
    _write_marker(tmp_path, 'box = "dev"\n')
    marker = devbox.find_workspace(tmp_path)
    assert marker.root_is_repo is True
    assert marker.config_path == tmp_path.resolve() / ".devbox" / "config.toml"
    assert marker.mods_dir == tmp_path.resolve() / ".devbox" / "mods"


def test_find_workspace_visible_marker_shape(tmp_path):
    # `devbox.toml`: the root holds repos, so there is nothing to hide from and
    # the mods sit beside the config file rather than under a dot directory.
    _write_visible_marker(tmp_path, 'box = "dev"\n')
    marker = devbox.find_workspace(tmp_path)
    assert marker.root_is_repo is False
    assert marker.config_path == tmp_path.resolve() / "devbox.toml"
    assert marker.mods_dir == tmp_path.resolve() / "mods"


def test_find_workspace_finds_the_visible_marker_walking_up(tmp_path):
    _write_visible_marker(tmp_path, 'box = "dev"\n')
    deep = tmp_path / "frontend" / "src"
    deep.mkdir(parents=True)
    assert devbox.find_workspace(deep).root == tmp_path.resolve()


def test_find_workspace_nearest_level_wins(tmp_path):
    # A workspace nested inside another resolves to itself, like git and .git.
    _write_visible_marker(tmp_path, 'box = "outer"\n')
    inner = tmp_path / "inner"
    _write_marker(inner, 'box = "inner"\n')
    assert devbox.find_workspace(inner).root == inner.resolve()


def test_find_workspace_both_spellings_at_one_level_dies(tmp_path):
    # Two spellings of one marker in one directory: which shape the workspace has
    # would be a coin toss, and it decides every repo's host path.
    _write_marker(tmp_path, 'box = "dev"\n')
    _write_visible_marker(tmp_path, 'box = "dev"\n')
    with pytest.raises(SystemExit):
        devbox.find_workspace(tmp_path)


def test_resolve_workspace_basic(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "myproj"\n')
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    assert workspace.workspace == "myproj"
    assert workspace.repos == []


def test_resolve_workspace_box_defaults_to_the_workspace_name(tmp_path, global_config):
    # A workspace that never mentions a box gets one of its own, named after it -
    # which is what keeps the single-box case working with no configuration.
    _write_marker(tmp_path, 'workspace = "myproj"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).box == "myproj"


def test_resolve_workspace_box_explicit_wins(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "myproj"\nbox = "ec2-shared"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).box == "ec2-shared"


@pytest.mark.parametrize("key,value", [
    ("provider", '"ec2"'),
    ("user", '"alice"'),
    ("vcpus", "4"),
    ("memory_mib", "2048"),
    ("disk_gb", "40"),
])
def test_resolve_workspace_rejects_box_level_scalars(tmp_path, global_config, key, value):
    # Hardware describes a box, which is now created independently and may host
    # several workspaces. Silently ignoring `vcpus = 16` would boot the wrong-sized
    # machine, so it is a hard error that names the flag to use instead.
    _write_marker(tmp_path, f'workspace = "p"\n{key} = {value}\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


@pytest.mark.parametrize("section", [
    '[ec2]\ninstance_type = "t3.micro"\n',
    '[lightsail]\nbundle = "small_3_0"\n',
])
def test_resolve_workspace_rejects_box_level_sections(tmp_path, global_config, section):
    _write_marker(tmp_path, f'workspace = "p"\n{section}')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_defaults_workspace_to_dirname(tmp_path, global_config):
    # No `workspace` key -> defaults to the marker directory's basename.
    _write_marker(tmp_path, "")
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    assert workspace.workspace == tmp_path.resolve().name


# ---- agent_user (the unprivileged account) ----------------------------------
def test_resolve_workspace_agent_user_defaults_to_the_workspace_name(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "myproj"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).agent_user == "myproj"


def test_resolve_workspace_agent_user_explicit_wins(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "myproj"\nagent_user = "botty"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).agent_user == "botty"


@pytest.mark.parametrize("name", [
    "MyProject",    # uppercase
    "foo.bar",      # dots
    "2fa-lib",      # leading digit
    "-dashed",      # leading dash
    "a" * 33,       # too long for a Linux username
    "has space",
])
def test_resolve_workspace_agent_user_invalid_username_dies(tmp_path, global_config, name):
    # Workspace names are directory basenames, so plenty of legal workspace names are
    # illegal usernames. Catching it here costs a second; catching it at useradd
    # time costs a boot.
    _write_marker(tmp_path, f'workspace = "p"\nagent_user = "{name}"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_agent_user_from_an_invalid_workspace_name_dies(tmp_path, global_config):
    # Same check, reached via the default rather than an explicit key.
    _write_marker(tmp_path, 'workspace = "My.Workspace"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_cannot_check_agent_user_against_the_box_here(
        tmp_path, global_config):
    # "agent_user must differ from the box's privileged account" is a real rule,
    # but the box's account is a property of the machine, not of this file - so
    # it is enforced at attach time (cmd_create), and resolving must not guess.
    _write_marker(tmp_path, 'workspace = "p"\nagent_user = "alice"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).agent_user == "alice"


@pytest.mark.parametrize("name", ["root", "ubuntu", "ec2-user", "nobody"])
def test_resolve_workspace_agent_user_reserved_dies(tmp_path, global_config, name):
    # `ubuntu` is the cloud images' own sudo-capable default user.
    _write_marker(tmp_path, f'workspace = "p"\nagent_user = "{name}"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_agent_user_known_key_no_warning(tmp_path, global_config, capsys):
    _write_marker(tmp_path, 'workspace = "p"\nagent_user = "botty"\n')
    devbox.resolve_workspace(tmp_path, global_config)
    assert capsys.readouterr().err == ""


def test_resolve_workspace_env(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\nGITHUB_TOKEN = "github_pat_proj"\n')
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    # A bare string is the agent's - the account that does the work.
    assert workspace.env == {"GITHUB_TOKEN": devbox.EnvVar("github_pat_proj", "agent")}


def test_resolve_workspace_env_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "myproj"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).env == {}


def test_resolve_workspace_env_non_string_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\nPORT = 8080\n')  # values must be strings
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_env_table_form_carries_an_owner(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\n'
                            'CLOUDFLARE_TOKEN = { value = "cf_tok", owner = "admin" }\n'
                            'PLAIN = { value = "p" }\n')
    assert devbox.resolve_workspace(tmp_path, global_config).env == {
        "CLOUDFLARE_TOKEN": devbox.EnvVar("cf_tok", "admin"),
        "PLAIN": devbox.EnvVar("p", "agent"),    # owner defaults to the agent
    }


def test_resolve_workspace_env_unknown_owner_dies(tmp_path, global_config):
    # No "root" owner: a `runas = ["root"]` mod reads the admin's env file.
    _write_marker(tmp_path, '[env]\nK = { value = "v", owner = "root" }\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_env_unknown_entry_key_dies(tmp_path, global_config):
    # A typo in an entry's own keys is fatal, unlike an unknown [env] KEY (which
    # is user-defined by design) - it would otherwise silently mean "agent".
    _write_marker(tmp_path, '[env]\nK = { value = "v", admin = true }\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_env_table_without_a_value_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\nK = { owner = "admin" }\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_legacy_name_field_dies(tmp_path, global_config):
    # `name` used to be the VM; it's now a migration error, not silently ignored.
    _write_marker(tmp_path, 'name = "myproj"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


# ---- unknown-key warnings -----------------------------------------------
def test_resolve_workspace_unknown_top_level_key_warns(tmp_path, global_config, capsys):
    _write_marker(tmp_path, 'workspace = "myproj"\nvpcus = 4\n')  # typo: vpcus
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    assert workspace.workspace == "myproj"  # still loads
    err = capsys.readouterr().err
    assert "unknown key 'vpcus'" in err


def test_resolve_workspace_unknown_section_warns(tmp_path, global_config, capsys):
    _write_marker(tmp_path, '[bogus]\nkey = "value"\n')
    devbox.resolve_workspace(tmp_path, global_config)
    assert "unknown key 'bogus'" in capsys.readouterr().err


def test_resolve_workspace_known_config_no_warning(tmp_path, global_config, capsys):
    _write_marker(tmp_path, 'workspace = "myproj"\nbox = "dev"\nagent_user = "botty"\n'
                            'mods = ["rust"]\n[[repos]]\nmode = "push"\n')
    devbox.resolve_workspace(tmp_path, global_config)
    assert capsys.readouterr().err == ""


def test_resolve_workspace_unknown_env_key_does_not_warn(tmp_path, global_config, capsys):
    # [env] holds arbitrary user-defined keys; they must not be flagged.
    _write_marker(tmp_path, '[env]\nANYTHING_GOES = "value"\n')
    devbox.resolve_workspace(tmp_path, global_config)
    assert capsys.readouterr().err == ""


def test_global_config_unknown_top_level_key_warns(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('netwrok = "custom-net"\n')  # typo: netwrok
    gc = devbox.GlobalConfig.load(path)
    assert gc.network == devbox.DEFAULT_NETWORK  # typo ignored, default kept
    err = capsys.readouterr().err
    assert "unknown key 'netwrok'" in err


def test_global_config_known_config_no_warning(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('network = "custom-net"\n[jj]\nname = "Alice"\n[env]\nX = "y"\n')
    devbox.GlobalConfig.load(path)
    assert capsys.readouterr().err == ""


def test_global_config_aws_ec2_split_known_keys_no_warning(tmp_path, capsys):
    # [aws] = creds only; the EC2 knobs live in [ec2]. Both fully recognized.
    path = tmp_path / "config.toml"
    path.write_text(
        '[aws]\nregion = "us-east-1"\nprofile = "dev"\n'
        '[ec2]\ndefault_security_groups = ["sg-1"]\ndefault_subnets = ["subnet-a"]\n'
        "spot = true\n"
    )
    devbox.GlobalConfig.load(path)
    assert capsys.readouterr().err == ""


def test_global_config_ec2_key_under_aws_warns(tmp_path, capsys):
    # An EC2 knob left behind in [aws] is no longer a known key -> warned, not
    # silently ignored (the JIM-72 guard catching the JIM-79 config move).
    path = tmp_path / "config.toml"
    path.write_text('[aws]\nregion = "us-east-1"\nspot = true\n')
    devbox.GlobalConfig.load(path)
    assert "unknown key 'aws.spot'" in capsys.readouterr().err


def test_global_config_unknown_ec2_key_warns(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('[ec2]\ndefault_subnet = "subnet-a"\n')  # typo: singular
    devbox.GlobalConfig.load(path)
    assert "unknown key 'ec2.default_subnet'" in capsys.readouterr().err


def test_resolve_workspace_invalid_provider_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'provider = "gcp"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


# ---- [[repos]] validation ---------------------------------------------------
def test_resolve_workspace_no_repos_by_default(tmp_path, global_config):
    # A workspace with nothing to check out lists no repos - the shape that used
    # to be spelled `[repo] mode = "none"`.
    _write_marker(tmp_path, 'workspace = "myproj"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).repos == []


def test_resolve_workspace_nameless_repo_is_the_root(tmp_path, global_config):
    # Under a `.devbox/` marker the root IS the repo, so an entry needs no name:
    # the host side is the root and the guest directory takes its basename.
    _write_marker(tmp_path, '[[repos]]\nmode = "push"\n')
    repos = devbox.resolve_workspace(tmp_path, global_config).repos
    assert repos == [devbox.RepoSpec(name=tmp_path.name, mode="push",
                                     dir=tmp_path.name, host=tmp_path.resolve(),
                                     url=None)]


def test_resolve_workspace_repo_clone_mode(tmp_path, global_config):
    _write_marker(
        tmp_path,
        '[[repos]]\nname = "myrepo"\nmode = "clone"\n'
        'url = "https://example.com/myrepo"\n',
    )
    assert devbox.resolve_workspace(tmp_path, global_config).repos == [
        devbox.RepoSpec(name="myrepo", mode="clone", dir="myrepo",
                        host=tmp_path.resolve() / "myrepo",
                        url="https://example.com/myrepo")]


def test_resolve_workspace_repo_name_supplies_host_and_dir(tmp_path, global_config):
    # One `name` is the whole of a repo entry in the common case: it addresses
    # the repo, and doubles as its host path and its guest directory.
    _write_visible_marker(tmp_path, '[[repos]]\nname = "myrepo"\nmode = "push"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).repos == [
        devbox.RepoSpec(name="myrepo", mode="push", dir="myrepo",
                        host=tmp_path.resolve() / "myrepo", url=None)]


def test_resolve_workspace_repo_host_and_dir_override_the_name(tmp_path, global_config):
    _write_visible_marker(
        tmp_path,
        '[[repos]]\nname = "api"\nmode = "push"\nhost = "vendor/api"\ndir = "srv"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).repos == [
        devbox.RepoSpec(name="api", mode="push", dir="srv",
                        host=tmp_path.resolve() / "vendor" / "api", url=None)]


def test_resolve_workspace_holds_several_repos(tmp_path, global_config):
    # The point of a workspace: several checkouts in one account, so one set of
    # git/jj credentials covers all of them.
    _write_visible_marker(
        tmp_path,
        '[[repos]]\nname = "frontend"\nmode = "push"\n'
        '[[repos]]\nname = "backend"\nmode = "push"\n'
        '[[repos]]\nname = "protos"\nmode = "clone"\nurl = "https://x/protos"\n')
    repos = devbox.resolve_workspace(tmp_path, global_config).repos
    assert [r.name for r in repos] == ["frontend", "backend", "protos"]
    assert [r.dir for r in repos] == ["frontend", "backend", "protos"]


def test_resolve_workspace_repo_lookup_by_name(tmp_path, global_config):
    _write_visible_marker(
        tmp_path,
        '[[repos]]\nname = "frontend"\nmode = "push"\n'
        '[[repos]]\nname = "backend"\nmode = "push"\n')
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    assert workspace.repo("backend").dir == "backend"
    assert workspace.repo("nope") is None


def test_resolve_workspace_nameless_repo_under_a_visible_marker_dies(tmp_path, global_config):
    # `devbox.toml` says the root holds repos, so "the root is the repo" is a
    # contradiction rather than a default worth guessing at.
    _write_visible_marker(tmp_path, '[[repos]]\nmode = "push"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_invalid_mode_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[[repos]]\nname = "myrepo"\nmode = "bogus"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_mode_none_dies(tmp_path, global_config):
    # "none" is not a mode any more; a workspace with no checkout lists no repos.
    _write_marker(tmp_path, '[[repos]]\nname = "myrepo"\nmode = "none"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_clone_missing_url_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[[repos]]\nname = "myrepo"\nmode = "clone"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_push_with_url_dies(tmp_path, global_config):
    _write_marker(
        tmp_path,
        '[[repos]]\nname = "myrepo"\nmode = "push"\nurl = "https://example.com/x"\n',
    )
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_unknown_key_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[[repos]]\nmode = "push"\nbranch = "main"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


@pytest.mark.parametrize("body", [
    # Two entries may share neither the name they are addressed by ...
    '[[repos]]\nname = "a"\nmode = "push"\ndir = "x"\n'
    '[[repos]]\nname = "a"\nmode = "push"\ndir = "y"\n',
    # ... nor the guest directory, which would put both in one tree.
    '[[repos]]\nname = "a"\nmode = "push"\ndir = "x"\n'
    '[[repos]]\nname = "b"\nmode = "push"\ndir = "x"\n',
])
def test_resolve_workspace_repos_must_be_distinct(tmp_path, global_config, body):
    _write_visible_marker(tmp_path, body)
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_may_not_be_the_mods_dir(tmp_path, global_config):
    # Under a visible marker the mods directory is a sibling of the repos, so it
    # shares their namespace - a collision has to be an error, not a surprise.
    _write_visible_marker(tmp_path, '[[repos]]\nname = "mods"\nmode = "push"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_rejects_the_old_singular_repo_table(tmp_path, global_config):
    _write_marker(tmp_path, '[repo]\nmode = "push"\ndir = "myrepo"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_rejects_the_old_project_key(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


# ---- render_init_script -----------------------------------------------------
@pytest.fixture
def pubkey(tmp_path):
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest user@host\n")
    return path


def _marker(root=None, root_is_repo=True):
    """A resolved Marker of either spelling, without touching the filesystem."""
    root = devbox.Path(root) if root is not None else devbox.Path("/ws")
    if root_is_repo:
        return devbox.Marker(root=root,
                             config_path=root / devbox.REPO_MARKER_DIR / devbox.REPO_MARKER_CONFIG,
                             mods_dir=root / devbox.REPO_MARKER_DIR / devbox.MODS_SUBDIR,
                             root_is_repo=True)
    return devbox.Marker(root=root, config_path=root / devbox.WORKSPACE_MARKER_FILE,
                         mods_dir=root / devbox.MODS_SUBDIR, root_is_repo=False)


def _repo(name="r", mode="push", dir=None, url=None, host=None, root=None):
    root = devbox.Path(root) if root is not None else devbox.Path("/ws")
    return devbox.RepoSpec(name=name, mode=mode,
                           dir=dir if dir is not None else name,
                           host=devbox.Path(host) if host is not None else root / name,
                           url=url)


def _workspace(mode="none", dir="", url=None, env=None,
               marker_dir=None, mods=None, sync=None, agent_user="agentbot",
               workspace="test-workspace", box=None, repos=None,
               root_is_repo=True):
    """A resolved workspace. `mode`/`dir`/`url` are the shorthand for the single
    repo most tests want (mode="none" meaning none at all); `repos=` is the way
    to build a workspace that holds several."""
    marker = _marker(marker_dir, root_is_repo=root_is_repo)
    if repos is None:
        repos = ([] if mode == "none" else
                 [_repo(name=dir or "r", mode=mode, dir=dir or "r", url=url,
                        root=marker.root)])
    return devbox.WorkspaceConfig(
        workspace=workspace,
        # A workspace with no `box` of its own attaches to one named after it -
        # the zero-config case, and what most tests want.
        box=box if box is not None else workspace,
        agent_user=agent_user,
        marker=marker,
        repos=repos,
        mods=mods if mods is not None else [],
        sync=sync if sync is not None else [],
        env=env if env is not None else {},
    )


def _box(name="test-box", provider="libvirt", admin_user="alice", vcpus=6,
         memory_mib=1024, disk_gb=10, ec2=None, lightsail=None):
    return devbox.BoxSpec(
        name=name, provider=provider, admin_user=admin_user, vcpus=vcpus,
        memory_mib=memory_mib, disk_gb=disk_gb, ec2=ec2, lightsail=lightsail)


def _box_args(name="dev", **kw):
    """A `devbox box create` argv namespace, with every flag defaulted off."""
    fields = dict(provider=None, user=None, vcpus=None, memory=None, disk=None,
                  instance_type=None, arch=None, ami=None, bundle=None,
                  blueprint=None, zone=None, spot=False, on_demand=False)
    fields.update(kw)
    return argparse.Namespace(name=name, **fields)


def _global_config(pubkey_path, jj_name="Alice Example", jj_email="alice@example.com",
                    env=None):
    return devbox.GlobalConfig(
        libvirt_uri="qemu:///system", network="default", cache_dir=devbox.Path("/tmp"),
        base_image_url="http://example/x.img", vcpus=6, memory_mib=1024, disk_gb=10,
        ssh_pubkey_path=pubkey_path, jj_name=jj_name, jj_email=jj_email,
        env=env if env is not None else {},
    )


def test_substitute_tokens_replaces_every_occurrence():
    text = "%A% and %A% and %B%"
    assert devbox.substitute_tokens(text, {"A": "x", "B": "y"}) == "x and x and y"


def test_render_init_script_is_a_bash_script(pubkey):
    # The user-data every provider gets is a shell script (Lightsail runs
    # user-data natively; cloud-init runs a `#!` script on libvirt/ec2), so the
    # shebang and the abort-on-failure flags are load-bearing.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert rendered.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in rendered
    assert not rendered.startswith("#cloud-config")


def test_render_init_script_substitutes_user_and_pubkey(pubkey):
    gc = _global_config(pubkey)
    rendered = devbox.render_init_script(_box(), gc)
    assert "%DEVBOX_" not in rendered  # every token filled
    assert "DEVBOX_USER='alice'" in rendered
    assert "DEVBOX_HOME='/home/alice'" in rendered
    assert "DEVBOX_SSH_PUBKEY='ssh-ed25519 AAAAtest user@host'" in rendered


def test_render_init_script_creates_the_user_with_key_and_sudo(pubkey):
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert "useradd" in rendered
    assert '"$DEVBOX_HOME/.ssh/authorized_keys"' in rendered
    assert "NOPASSWD:ALL" in rendered
    assert "passwd --lock" in rendered
    assert "usermod --shell /usr/bin/zsh" in rendered


def test_render_init_script_creates_the_user_before_installing_packages(pubkey):
    # SSH access must not be gated on the (slow, network-dependent) package
    # step: a failure there still leaves a reachable, debuggable box.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert rendered.index("useradd") < rendered.index("apt-get")
    assert rendered.index("authorized_keys") < rendered.index("apt-get")


def test_render_init_script_installs_the_base_packages(pubkey):
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    for pkg in ("zsh", "vim", "python3", "curl", "git", "git-lfs",
                "ca-certificates", "gh", "rsync", "build-essential", "pkg-config"):
        assert f"\n  {pkg} " in rendered or f"\n  {pkg}\n" in rendered
    assert 'apt-get -o DPkg::Lock::Timeout=600 install -y "${DEVBOX_PACKAGES[@]}"' in rendered
    assert "apt-get upgrade" not in rendered  # matched the old package_upgrade: false


def test_render_init_script_adds_the_github_cli_repo_before_installing(pubkey):
    # `gh` is not in the 24.04 archive (the Lightsail blueprint) and a missing
    # package would abort first boot under `set -e`.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert "https://cli.github.com/packages" in rendered
    assert rendered.index("cli.github.com") < rendered.index("install -y")


def test_render_init_script_wires_bash_env_through_a_shared_stub(pubkey):
    # /etc/environment is ONE system-wide table and the box has two accounts, so
    # it must not name a per-user path there (one user would source the other's
    # file). It points at a stub that resolves $HOME in bash, where expansion
    # actually works - pam_env's /etc/environment parsing is plain KEY=VALUE.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert "BASH_ENV_STUB='/etc/devbox/bash_env'" in rendered
    assert "/home/alice/.bash_env" not in rendered
    assert "/home/agentbot/.bash_env" not in rendered
    assert 'if [ -r "$HOME/.bash_env" ]; then . "$HOME/.bash_env"; fi' in rendered
    assert "/etc/environment" in rendered


def test_render_init_script_creates_only_the_privileged_user(pubkey):
    # A workspace's agent account is deliberately NOT a first-boot concern:
    # user-data runs once, so an account born here could never reach an existing
    # box, and attaching a second workspace would need a rebuild. The driver
    # creates them instead - which is why first boot takes a box, not a workspace.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert "agentbot" not in rendered
    assert rendered.count("useradd") == 1


def test_render_init_script_locks_down_the_home_directory(pubkey):
    # Ubuntu's default HOME_MODE is 0755, which would let the unprivileged agent
    # read the privileged user's entire home.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert "--key HOME_MODE=0750" in rendered


def test_render_init_script_hostname_is_the_box_name(pubkey):
    # The hostname is the box's, not any workspace's: several workspaces share it.
    rendered = devbox.render_init_script(_box(name="aws-mybox"),
                                         _global_config(pubkey))
    assert "DEVBOX_HOSTNAME='aws-mybox'" in rendered


def test_render_init_script_carries_no_provisioning_payload(pubkey):
    # Provisioning is over SSH: the init script only does base setup - no
    # driver, no mods, no [env] creds.
    gc = _global_config(pubkey, env={"GITHUB_TOKEN": "github_pat_x"})
    rendered = devbox.render_init_script(_box(), gc)
    assert "provision-driver.sh" not in rendered
    assert "github_pat_x" not in rendered


def test_render_init_script_missing_pubkey_dies(tmp_path):
    gc = _global_config(tmp_path / "missing.pub")
    with pytest.raises(SystemExit):
        devbox.render_init_script(_box(), gc)


def test_render_init_script_quote_in_a_token_dies(tmp_path):
    # Tokens land inside single-quoted shell assignments; a value with a quote
    # would break out of the assignment and mangle first boot.
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest o'brien@host\n")
    with pytest.raises(SystemExit):
        devbox.render_init_script(_box(), _global_config(path))


def test_render_init_script_parses_as_bash(pubkey, tmp_path):
    rendered = devbox.render_init_script(_box(name="aws-mybox"),
                                         _global_config(pubkey))
    script = tmp_path / "init.sh"
    script.write_text(rendered)
    assert devbox.subprocess.run(["bash", "-n", str(script)]).returncode == 0


GUARD = 'if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi'


def test_render_init_script_re_execs_under_bash_before_any_bashism(pubkey):
    # Lightsail runs user-data under /bin/sh (dash) and ignores the shebang, so
    # the script re-execs itself under bash. The guard is only load-bearing if
    # it precedes EVERY bashism - `set -o pipefail` and the packages array both
    # abort dash on the spot ("set: Illegal option -o pipefail").
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    assert GUARD in rendered
    assert 0 < rendered.index(GUARD) < rendered.index("set -euo pipefail")
    assert rendered.index(GUARD) < rendered.index("DEVBOX_PACKAGES=(")


def test_render_init_script_prefix_up_to_the_guard_is_posix_sh(pubkey, tmp_path):
    # Everything dash executes before the re-exec must be POSIX sh: slice the
    # script from the top through the guard line and hand it to `sh -n`.
    rendered = devbox.render_init_script(_box(), _global_config(pubkey))
    prefix = rendered[:rendered.index(GUARD) + len(GUARD)] + "\n"
    script = tmp_path / "prefix.sh"
    script.write_text(prefix)
    proc = devbox.subprocess.run(["/bin/sh", "-n", str(script)],
                                 capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- build_payload (the provisioning payload, delivered over SSH) -----------
@pytest.fixture
def isolated_mods(monkeypatch):
    # Resolve mods against the real built-ins + driver, but keep a developer's
    # own ~/.config/devbox/mods out of the test.
    monkeypatch.setattr(devbox.provision, "GLOBAL_MODS_DIR", devbox.Path("/nonexistent-global-mods"))


def _payload_map(workspace, gc, admin_user="alice"):
    return {pf.path: pf for pf in devbox.build_payload(workspace, gc, admin_user)}


def test_build_payload_has_driver_manifest_context_and_mods(isolated_mods, pubkey):
    payload = _payload_map(_workspace(mode="clone", dir="r", url="https://x/y"), _global_config(pubkey))
    assert payload["provision-driver.sh"].content == devbox.DRIVER_PATH.read_text()
    manifest_names = [line.split("\t")[0]
                      for line in payload["manifest"].content.splitlines()]
    assert manifest_names == list(devbox.WORKSPACE_BASE_MODS)
    ctx = payload["context.env"].content
    # Both accounts, named explicitly - a bare DEVBOX_USER would be ambiguous now.
    assert "export DEVBOX_ADMIN_USER=alice" in ctx
    assert "export DEVBOX_AGENT_USER=agentbot" in ctx
    assert "export DEVBOX_USER=" not in ctx
    # Which workspace owns the account, so the driver can refuse to hand it to a
    # second one rather than silently sharing a home.
    assert "export DEVBOX_WORKSPACE=test-workspace" in ctx
    # The host's public key, so the driver can authorize it for the agent account
    # it creates. Public by definition, so it belongs in the payload.
    assert "export DEVBOX_SSH_PUBKEY='ssh-ed25519 AAAAtest user@host'" in ctx
    assert "export DEVBOX_JJ_NAME='Alice Example'" in ctx   # shell-quoted (space)
    # The checkouts themselves travel as their own payload file, since there can
    # be any number of them; the context carries only their guest directories.
    assert "export DEVBOX_REPO_MODE=" not in ctx
    assert "export DEVBOX_REPO_DIRS=r" in ctx
    assert payload["repos"].content == "r\tclone\tr\thttps://x/y\n"
    # Creds ([env]) are NOT in the payload - they're delivered separately as the
    # box's 0600 ~/.config/devbox/env (see run_envvars).
    assert "secret.env" not in payload
    assert "GITHUB_TOKEN" not in ctx
    for name in devbox.WORKSPACE_BASE_MODS:
        assert f"mods/{name}.sh" in payload


def test_render_repos_is_tab_separated_in_config_order(pubkey):
    # Same shape as the manifest, for the same reason: repo-bootstrap reads it
    # with plain IFS splitting and has no parser to spare.
    repos = [_repo("frontend", mode="push"),
             _repo("protos", mode="clone", url="https://x/protos")]
    assert devbox.render_repos(repos) == (
        "frontend\tpush\tfrontend\t\n"
        "protos\tclone\tprotos\thttps://x/protos\n")


def test_render_repos_empty_when_a_workspace_has_none(pubkey):
    assert devbox.render_repos([]) == ""


def test_build_payload_repos_file_lists_every_checkout(isolated_mods, pubkey):
    ws = _workspace(repos=[_repo("frontend"), _repo("backend")])
    payload = _payload_map(ws, _global_config(pubkey))
    assert payload["repos"].content == "frontend\tpush\tfrontend\t\nbackend\tpush\tbackend\t\n"
    # ... and the context names their guest directories, nothing more.
    assert "export DEVBOX_REPO_DIRS=frontend,backend" in payload["context.env"].content


def test_build_payload_resolves_mods_from_the_markers_own_directory(isolated_mods, tmp_path):
    # A `devbox.toml` workspace keeps its mods in `<root>/mods`, not under a dot
    # directory - the marker carries which, so nothing re-derives it.
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "rust.sh").write_text("#!/usr/bin/env bash\necho rust\n")
    ws = _workspace(marker_dir=tmp_path, root_is_repo=False, mods=["rust"])
    assert devbox.resolve_mod("rust", ws.marker.mods_dir).read_text().endswith("echo rust\n")


def test_build_payload_carries_no_creds(isolated_mods, pubkey):
    # Even with a global [env] token set, build_payload never embeds it.
    gc = _global_config(pubkey, env={"GITHUB_TOKEN": "github_pat_x"})
    payload = _payload_map(_workspace(env={"SOME_KEY": "v"}), gc)
    assert "secret.env" not in payload
    assert not any("github_pat_x" in pf.content for pf in payload.values())


def test_build_payload_appends_workspace_mods(isolated_mods, pubkey, tmp_path):
    (tmp_path / ".devbox" / "mods").mkdir(parents=True)
    (tmp_path / ".devbox" / "mods" / "rust.sh").write_text("#!/usr/bin/env bash\necho rust\n")
    payload = _payload_map(_workspace(marker_dir=tmp_path, mods=["rust"]), _global_config(pubkey))
    manifest_names = [line.split("\t")[0]
                      for line in payload["manifest"].content.splitlines()]
    assert manifest_names == [*devbox.WORKSPACE_BASE_MODS, "rust"]
    assert payload["mods/rust.sh"].content == "#!/usr/bin/env bash\necho rust\n"


def test_build_payload_missing_jj_identity_dies(isolated_mods, pubkey, monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: None)  # no host jj fallback
    gc = _global_config(pubkey, jj_name=None)
    with pytest.raises(SystemExit):
        devbox.build_payload(_workspace(), gc, "alice")


def test_build_payload_jj_identity_host_fallback(isolated_mods, pubkey, monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/jj")
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(
            args, 0, {"user.name": "Host Jj Name", "user.email": "host@example.com"}[args[-1]] + "\n", ""),
    )
    gc = _global_config(pubkey, jj_name=None, jj_email=None)
    ctx = _payload_map(_workspace(), gc)["context.env"].content
    assert "export DEVBOX_JJ_NAME='Host Jj Name'" in ctx
    assert "export DEVBOX_JJ_EMAIL=host@example.com" in ctx


# ---- resolve_env (global + workspace [env], workspace overrides per-key) --------
def test_resolve_env_merges_global_and_workspace():
    gc = _global_config(devbox.Path("/nonexistent"),
                        env={"GITHUB_TOKEN": devbox.EnvVar("g_tok"),
                             "SHARED": devbox.EnvVar("global")})
    workspace = _workspace(env={"SHARED": devbox.EnvVar("workspace"),
                            "PROJECT_ONLY": devbox.EnvVar("p")})
    assert devbox.resolve_env(workspace, gc) == {
        "GITHUB_TOKEN": devbox.EnvVar("g_tok"),     # global-only key kept
        "SHARED": devbox.EnvVar("workspace"),         # workspace overrides per-key
        "PROJECT_ONLY": devbox.EnvVar("p"),         # workspace-only key added
    }


def test_resolve_env_workspace_override_replaces_the_owner_too():
    # The override is per entry, not per field: re-declaring a key hands it to
    # the account the workspace names, whatever the global config said.
    gc = _global_config(devbox.Path("/nonexistent"),
                        env={"TOKEN": devbox.EnvVar("g_tok", "admin")})
    workspace = _workspace(env={"TOKEN": devbox.EnvVar("p_tok")})
    assert devbox.resolve_env(workspace, gc) == {"TOKEN": devbox.EnvVar("p_tok", "agent")}


def test_resolve_env_empty_when_neither_set():
    gc = _global_config(devbox.Path("/nonexistent"))
    assert devbox.resolve_env(_workspace(), gc) == {}


def test_env_for_owner_slices_by_account():
    env = {"GITHUB_TOKEN": devbox.EnvVar("g_tok"),
           "CLOUDFLARE_TOKEN": devbox.EnvVar("cf_tok", "admin")}
    assert devbox.env_for_owner(env, "agent") == {"GITHUB_TOKEN": "g_tok"}
    assert devbox.env_for_owner(env, "admin") == {"CLOUDFLARE_TOKEN": "cf_tok"}


# ---- provisioning: mods, env, payload ---------------------------------------
def test_workspace_playbook_appends_workspace_mods_after_the_base():
    assert devbox.workspace_playbook(_workspace(mods=["rust", "wasm"])) == \
        [*devbox.WORKSPACE_BASE_MODS, "rust", "wasm"]


def test_workspace_playbook_base_only_when_no_workspace_mods():
    assert devbox.workspace_playbook(_workspace()) == list(devbox.WORKSPACE_BASE_MODS)


def test_box_playbook_is_fixed():
    # A box takes no config, so its playbook is not extensible per box; a
    # workspace that needs more system state ships a `runas = ["root"]` mod.
    assert devbox.box_playbook() == list(devbox.BOX_BASE_MODS)


def test_the_two_playbooks_split_the_apt_half_from_the_per_user_half():
    # dotfiles-packages and tree-sitter-cli are box-level mods (root, system-
    # wide); everything else in the base installs into somebody's home.
    assert "dotfiles-packages" in devbox.BOX_BASE_MODS
    assert "dotfiles-packages" not in devbox.WORKSPACE_BASE_MODS
    for per_user in ("jj", "claude-code", "repo-bootstrap"):
        assert per_user in devbox.WORKSPACE_BASE_MODS
        assert per_user not in devbox.BOX_BASE_MODS


# ---- scope: which `runas` targets run in which playbook --------------------
@pytest.mark.parametrize("runas,box,workspace", [
    # A mod with an agent half: the box runs everything else, the attachment
    # runs only the agent. This is what stops a second workspace reinstalling the
    # privileged account's oh-my-zsh.
    (["admin", "agent"], ["admin"], ["agent"]),
    (["agent"], [], ["agent"]),
    # No agent target at all: pure system work, which runs whole in whichever
    # playbook lists it - that is how a workspace ships docker onto a shared box.
    (["root"], ["root"], ["root"]),
    (["root", "admin"], ["root", "admin"], ["root", "admin"]),
])
def test_scoped_runas(runas, box, workspace):
    assert devbox.scoped_runas(runas, devbox.SCOPE_BOX) == box
    assert devbox.scoped_runas(runas, devbox.SCOPE_WORKSPACE) == workspace


def test_resolve_mod_precedence_workspace_over_global_over_builtin(tmp_path):
    proj = tmp_path / "ws" / ".devbox" / "mods"
    glob = tmp_path / "global"
    built = tmp_path / "builtin"
    for d in (proj, glob, built):
        d.mkdir(parents=True)
    # `dotfiles` in all three -> workspace wins; `shared` in global+builtin ->
    # global wins; `basic` only built-in -> built-in.
    (proj / "dotfiles.sh").write_text("proj")
    (glob / "dotfiles.sh").write_text("glob")
    (built / "dotfiles.sh").write_text("built")
    (glob / "shared.sh").write_text("glob")
    (built / "shared.sh").write_text("built")
    (built / "basic.sh").write_text("built")

    assert devbox.resolve_mod("dotfiles", proj, glob, built).read_text() == "proj"
    assert devbox.resolve_mod("shared", proj, glob, built).read_text() == "glob"
    assert devbox.resolve_mod("basic", proj, glob, built).read_text() == "built"


def test_resolve_mod_missing_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.resolve_mod("nope", tmp_path / "proj", tmp_path / "g", tmp_path / "b")


# ---- parse_mod_metadata (the `# /// mod` block, static parse) ---------------
def _mod(*meta_lines, body="set -e\n"):
    """A mod script with a `# /// mod` block holding `meta_lines`."""
    block = "".join(f"# {line}\n" for line in meta_lines)
    return f"#!/bin/bash\n#\n# /// mod\n{block}# ///\n#\n{body}"


def test_parse_mod_metadata_reads_every_key():
    meta = devbox.parse_mod_metadata(_mod(
        'requires = ["jj", "github-auth"]',
        'runas = ["admin", "agent"]',
        'packages = ["jq", "unzip"]',
    ))
    assert meta == {"requires": ["jj", "github-auth"],
                    "runas": ["admin", "agent"],
                    "packages": ["jq", "unzip"]}


def test_parse_mod_metadata_no_block_is_empty():
    assert devbox.parse_mod_metadata("#!/bin/bash\n# mod: x\nset -e\n") == {}


def test_parse_mod_metadata_ignores_prose_outside_the_fence():
    # The whole point of fencing: prose that discusses the metadata keys is no
    # longer metadata. The old `^# <key>:` scan turned each of these into data.
    text = _mod('requires = ["jj"]', body=(
        "# packages: jq unzip\n"
        "# runas: root\n"
        "# This mod cannot use `packages` for its deps.\n"
        "set -e\n"))
    assert devbox.parse_mod_metadata(text) == {"requires": ["jj"]}


def test_parse_mod_metadata_blank_comment_lines_are_allowed():
    # A bare `#` inside the fence unprefixes to an empty line, not to "#".
    text = "#!/bin/bash\n# /// mod\n# requires = [\"jj\"]\n#\n# runas = [\"root\"]\n# ///\n"
    assert devbox.parse_mod_metadata(text) == {"requires": ["jj"], "runas": ["root"]}


def test_parse_mod_metadata_rejects_a_second_block():
    text = _mod('requires = ["jj"]') + '# /// mod\n# runas = ["root"]\n# ///\n'
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(text)


def test_parse_mod_metadata_rejects_a_mislabeled_fence():
    # `# /// script` is PEP 723's type, and an easy thing to write by mistake.
    # Silently treating it as "no metadata" would run the mod with every default.
    text = "#!/bin/bash\n# /// script\n# requires = [\"jj\"]\n# ///\n"
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(text)


def test_parse_mod_metadata_rejects_the_old_header_format():
    # A `# runas: root` line with no fence would otherwise parse as no metadata
    # and quietly run a privileged mod as the agent - the one silent default
    # that must not happen. Erroring names the file and shows the replacement.
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata("#!/bin/bash\n# runas: root\nset -e\n")
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata("#!/bin/bash\n# requires: jj\nset -e\n")


def test_parse_mod_metadata_legacy_check_ignores_prose_inside_a_fenced_mod():
    # A mod WITH a block may still discuss the old syntax in its prose (several
    # built-ins do); only a file with no block at all is checked.
    text = _mod('runas = ["root"]', body="# packages: jq\nset -e\n")
    assert devbox.parse_mod_metadata(text) == {"runas": ["root"]}


def test_parse_mod_metadata_rejects_invalid_toml():
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(_mod("requires = [unquoted]"))


def test_parse_mod_metadata_rejects_an_unknown_key():
    # Catches a typo (`require`) that would otherwise be silently ignored.
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(_mod('require = ["jj"]'))


def test_parse_mod_metadata_rejects_a_non_list_value():
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(_mod('runas = "root"'))
    with pytest.raises(SystemExit):
        devbox.parse_mod_metadata(_mod("packages = [1, 2]"))


# ---- validate_playbooks (`requires` ordering across both playbooks) ---------
def _mods_dir(root):
    """The mods directory of a `.devbox/`-marked workspace rooted at `root`."""
    return root / devbox.REPO_MARKER_DIR / devbox.MODS_SUBDIR


def _write_workspace_mod(marker_dir, name, text):
    d = _mods_dir(marker_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.sh").write_text(text)


def test_validate_playbooks_base_passes(isolated_mods):
    # The shipped base playbooks (with their `requires` metadata) are correctly
    # ordered - no dep is missing or listed after its dependent.
    devbox.validate_playbooks(_workspace())


def test_validate_playbooks_workspace_mod_requiring_base_mod_passes(isolated_mods, tmp_path):
    _write_workspace_mod(tmp_path, "rust", _mod('requires = ["jj"]'))
    devbox.validate_playbooks(_workspace(marker_dir=tmp_path, mods=["rust"]))


def test_validate_playbooks_workspace_mod_requiring_a_box_mod_passes(isolated_mods, tmp_path):
    # A box mod always ran earlier - it belongs to the machine, which exists
    # before any attachment - so it satisfies a dep from the other playbook.
    _write_workspace_mod(tmp_path, "rust", _mod('requires = ["dotfiles-packages"]'))
    devbox.validate_playbooks(_workspace(marker_dir=tmp_path, mods=["rust"]))


def test_validate_playbooks_missing_dep_dies(isolated_mods, tmp_path):
    _write_workspace_mod(tmp_path, "rust", _mod('requires = ["nonexistent"]'))
    with pytest.raises(SystemExit):
        devbox.validate_playbooks(_workspace(marker_dir=tmp_path, mods=["rust"]))


def test_validate_playbooks_dep_listed_after_dies(isolated_mods, tmp_path):
    # `a` requires `b`, but `b` is appended after `a` -> misordered -> die.
    _write_workspace_mod(tmp_path, "a", _mod('requires = ["b"]'))
    _write_workspace_mod(tmp_path, "b", "#!/bin/bash\nset -e\n")
    with pytest.raises(SystemExit):
        devbox.validate_playbooks(_workspace(marker_dir=tmp_path, mods=["a", "b"]))


def test_validate_playbooks_dep_listed_before_passes(isolated_mods, tmp_path):
    # Same two mods, `b` ahead of `a` -> ordering satisfied.
    _write_workspace_mod(tmp_path, "a", _mod('requires = ["b"]'))
    _write_workspace_mod(tmp_path, "b", "#!/bin/bash\nset -e\n")
    devbox.validate_playbooks(_workspace(marker_dir=tmp_path, mods=["b", "a"]))


def test_build_payload_validates_mods(isolated_mods, pubkey, tmp_path):
    # build_payload is the choke point: a bad `requires` fails there (so a
    # selective `devbox sync --mods=a` still validates the full playbook).
    _write_workspace_mod(tmp_path, "rust", _mod('requires = ["nonexistent"]'))
    with pytest.raises(SystemExit):
        devbox.build_payload(_workspace(marker_dir=tmp_path, mods=["rust"]),
                             _global_config(pubkey), "alice")


# ---- `runas` / `packages` in a resolved ModSpec ------------------------------
def test_load_mod_specs_runas_defaults_to_the_agent(isolated_mods, tmp_path):
    # The default target is the unprivileged account, because that is what a mod
    # almost always configures: a user's home directory.
    _write_workspace_mod(tmp_path, "rust", "#!/bin/bash\nset -e\n")
    spec, = devbox.load_mod_specs(["rust"], _mods_dir(tmp_path))
    assert spec.runas == ["agent"]
    assert spec.requires == [] and spec.packages == []


def test_load_mod_specs_carries_the_declared_metadata(isolated_mods, tmp_path):
    _write_workspace_mod(tmp_path, "rust", _mod(
        'requires = ["jj"]', 'runas = ["admin", "agent"]', 'packages = ["clang"]'))
    spec, = devbox.load_mod_specs(["rust"], _mods_dir(tmp_path))
    assert (spec.requires, spec.runas, spec.packages) == \
        (["jj"], ["admin", "agent"], ["clang"])


def test_load_mod_specs_rejects_an_unknown_runas_target(isolated_mods, tmp_path):
    _write_workspace_mod(tmp_path, "rust", _mod('runas = ["nobody-special"]'))
    with pytest.raises(SystemExit):
        devbox.load_mod_specs(["rust"], _mods_dir(tmp_path))


def test_build_payload_validates_runas(isolated_mods, pubkey, tmp_path):
    # Same choke point as `requires`: a bad target fails before any boot/SSH.
    _write_workspace_mod(tmp_path, "rust", _mod('runas = ["wheel"]'))
    with pytest.raises(SystemExit):
        devbox.build_payload(_workspace(marker_dir=tmp_path, mods=["rust"]),
                             _global_config(pubkey), "alice")


def _manifest_specs():
    return [
        devbox.ModSpec(name="jj", path=devbox.Path("/x/jj.sh"), requires=[],
                       runas=["agent"], packages=[]),
        devbox.ModSpec(name="dotfiles", path=devbox.Path("/x/dotfiles.sh"),
                       requires=["dotfiles-packages"], runas=["admin", "agent"],
                       packages=["jq", "unzip"]),
    ]


def test_render_manifest_is_tab_separated_with_comma_joined_lists():
    # The driver parses this with plain IFS splitting - it has no YAML/JSON
    # parser and should not grow one.
    assert devbox.render_manifest(_manifest_specs(), devbox.SCOPE_WORKSPACE) == (
        "jj\tagent\t\n"
        "dotfiles\tagent\tjq,unzip\n"
    )


def test_render_manifest_narrows_the_runas_column_to_the_scope():
    # The scope filter is entirely host-side: what reaches the driver is already
    # narrowed, so the driver never has to know that scopes exist.
    assert devbox.render_manifest(_manifest_specs(), devbox.SCOPE_BOX) == (
        "dotfiles\tadmin\tjq,unzip\n"    # jj is agent-only -> nothing to do here
    )


def test_base_playbook_mods_declare_valid_metadata(isolated_mods):
    # Every shipped mod's `# /// mod` block parses, and its `runas` targets are
    # known. Guards the metadata itself, which nothing else type-checks.
    names = [*devbox.box_playbook(), *devbox.workspace_playbook(_workspace())]
    specs = devbox.load_mod_specs(names, None)
    assert [s.name for s in specs] == names
    for spec in specs:
        assert spec.runas, f"{spec.name} resolved to no runas target"
        for target in spec.runas:
            assert target in devbox.RUNAS_TARGETS


def test_shipped_mods_never_call_sudo(isolated_mods):
    # A mod body runs as an account without sudo. `sudo` in a mod is a bug now:
    # apt goes through `packages`, and real privilege through `runas = ["root"]`.
    names = [*devbox.box_playbook(), *devbox.workspace_playbook(_workspace())]
    for spec in devbox.load_mod_specs(names, None):
        for line in spec.path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # prose about sudo is fine; calling it is not
            assert "sudo " not in stripped, f"{spec.name}: {stripped}"


def test_dotfiles_admin_half_is_the_boxs_and_its_agent_half_the_workspaces(isolated_mods):
    # The one mod that lives in both playbooks, and the reason scoping exists:
    # the privileged user gets the dotfiles once per box (so `devbox box ssh`,
    # the debugging shell, is usable), and each attachment gets them in its own
    # account - without redoing the admin's on every attach.
    box = devbox.load_mod_specs(devbox.box_playbook(), None)
    workspace = devbox.load_mod_specs(devbox.workspace_playbook(_workspace()), None)
    assert devbox.render_manifest(box, devbox.SCOPE_BOX).splitlines() == [
        "dotfiles-packages\troot\t",   # the apt half: once, as root, per box
        "tree-sitter-cli\troot\tunzip",
        "dotfiles\tadmin\t",
        "vim\tadmin\t",
        "github-auth\troot\t",
    ]
    by_name = {s.name: s for s in workspace}
    assert devbox.scoped_runas(by_name["dotfiles"].runas, devbox.SCOPE_WORKSPACE) == ["agent"]
    assert "dotfiles-packages" in by_name["dotfiles"].requires
    assert "tree-sitter-cli" in by_name["dotfiles"].requires


def test_github_auth_is_a_root_box_mod_writing_etc_gitconfig(isolated_mods):
    # The GitHub credential helper is box-wide state, so it belongs to the box
    # playbook: one root-written /etc/gitconfig line, correct for every account
    # because the helper answers from the CALLING account's own GITHUB_TOKEN.
    #
    # That location is also the regression guard. `dotfiles` copies configenv's
    # own gitconfig over ~/.gitconfig, so a helper written into a home directory
    # is erased before repo-bootstrap ever clones, and every private clone
    # fails. Nothing reaches /etc, so the mod carries no ordering constraint.
    assert "github-auth" in devbox.box_playbook()
    assert "github-auth" not in devbox.WORKSPACE_BASE_MODS
    spec = {s.name: s
            for s in devbox.load_mod_specs(devbox.box_playbook(), None)}["github-auth"]
    assert spec.runas == ["root"]
    code = [line for line in spec.path.read_text().splitlines()
            if not line.strip().startswith("#")]   # prose may discuss ~/.gitconfig
    assert any("/etc/gitconfig" in line for line in code)
    assert not any("$HOME" in line or "~/" in line for line in code)
    # `gh auth setup-git` is the specific way back into a home directory: it
    # writes the helper into ~/.gitconfig, naming no path this test could see.
    assert not any("setup-git" in line for line in code)


def test_repo_bootstrap_gets_github_auth_from_the_box_playbook(isolated_mods):
    # A private `jj git clone` still depends on github-auth; the dependency now
    # crosses playbooks. The box playbook runs first, so it is satisfied - and
    # `requires` keeps saying so rather than the fact going undocumented.
    workspace = _workspace(mode="clone", dir="r", url="https://x/y")
    specs = {s.name: s
             for s in devbox.load_mod_specs(devbox.workspace_playbook(workspace), None)}
    assert "github-auth" in specs["repo-bootstrap"].requires
    assert "github-auth" not in specs
    devbox.validate_playbooks(workspace)   # must not die on the absent dependency


def test_render_env_file_shell_quotes_values():
    out = devbox.render_env_file({"A": "plain", "B": "has space", "C": "a'b"})
    assert out == "export A=plain\nexport B='has space'\nexport C='a'\"'\"'b'\n"


def test_resolve_workspace_parses_mods(tmp_path, global_config):
    _write_marker(tmp_path, 'mods = ["rust", "wasm-tools"]\n')
    workspace = devbox.resolve_workspace(tmp_path, global_config)
    assert workspace.mods == ["rust", "wasm-tools"]
    assert workspace.marker.root == tmp_path.resolve()


def test_resolve_workspace_mods_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "p"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).mods == []


def test_resolve_workspace_mods_must_be_string_list(tmp_path, global_config):
    _write_marker(tmp_path, 'mods = [1, 2]\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_build_tarball_roundtrips_payload():
    import io
    import tarfile
    payload = [
        devbox.PayloadFile("provision-driver.sh", "#!/bin/sh\n", 0o755),
        devbox.PayloadFile("context.env", "export DEVBOX_USER=alice\n", 0o644),
        devbox.PayloadFile("mods/jj.sh", "echo jj\n", 0o755),
    ]
    tar_bytes = devbox.build_tarball(payload)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members) == {"provision-driver.sh", "context.env", "mods/jj.sh"}
        assert members["context.env"].mode == 0o644
        assert members["provision-driver.sh"].mode == 0o755
        assert tar.extractfile("mods/jj.sh").read() == b"echo jj\n"


def test_build_tarball_is_deterministic():
    payload = [devbox.PayloadFile("manifest", "jj\n", 0o644)]
    assert devbox.build_tarball(payload) == devbox.build_tarball(payload)


# ---- instance identity / resolution ----------------------------------------
def test_resolve_instance_defaults_to_the_workspace_name():
    # A workspace that never mentioned a box owns one named after itself.
    args = argparse.Namespace(box=None)
    assert devbox.resolve_instance(args, _workspace()) == "test-workspace"


def test_resolve_instance_uses_the_workspaces_box_key():
    args = argparse.Namespace(box=None)
    assert devbox.resolve_instance(args, _workspace(box="ec2-shared")) == "ec2-shared"


def test_resolve_instance_flag_overrides_the_box_key():
    args = argparse.Namespace(box="ec2-foo-gpu")
    assert devbox.resolve_instance(args, _workspace(box="ec2-shared")) == "ec2-foo-gpu"


def test_parse_instance_name_bare_is_libvirt():
    # No known-provider prefix -> libvirt (the workspace's default), name unchanged.
    assert devbox.parse_instance_name("binius-dev") == ("libvirt", "binius-dev")


def test_parse_instance_name_provider_prefixed():
    assert devbox.parse_instance_name("ec2-binius-arm") == ("ec2", "binius-arm")


def test_parse_instance_name_aws_prefixed():
    # The Lightsail-backed provider is prefixed too, so aws-* routes to it.
    assert devbox.parse_instance_name("aws-foo") == ("aws", "foo")
    assert devbox.parse_instance_name("aws-binius-arm") == ("aws", "binius-arm")


def test_parse_instance_name_unknown_prefix_is_libvirt():
    # A hyphen whose left segment isn't a known prefixed provider is just part of
    # a bare libvirt name (e.g. 'gcp-x' before gcp is a real provider).
    assert devbox.parse_instance_name("gcp-x") == ("libvirt", "gcp-x")


def test_parse_instance_name_no_hyphen():
    assert devbox.parse_instance_name("scratch") == ("libvirt", "scratch")


def test_compose_instance_name_libvirt_is_bare():
    assert devbox.compose_instance_name("libvirt", "binius-dev") == "binius-dev"


def test_compose_instance_name_cloud_is_prefixed():
    assert devbox.compose_instance_name("ec2", "binius-arm") == "ec2-binius-arm"


def test_compose_parse_round_trip():
    for provider in devbox.PROVIDERS:
        canonical = devbox.compose_instance_name(provider, "my-inst")
        assert devbox.parse_instance_name(canonical) == (provider, "my-inst")


def test_resolve_target_routes_by_box_name(monkeypatch):
    # A canonical ec2-* name routes to the EC2 provider from the name alone - no
    # config lookup and no API call to find the owner.
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: f"prov:{name}")
    gc = _global_config(devbox.Path("/nonexistent"))
    args = argparse.Namespace(box="ec2-binius-arm")
    assert devbox.cli._resolve_target(args, _workspace(), gc) == (
        "ec2-binius-arm", "ec2", "prov:ec2")
    args = argparse.Namespace(box=None)  # bare default -> libvirt
    assert devbox.cli._resolve_target(args, _workspace(), gc) == (
        "test-workspace", "libvirt", "prov:libvirt")


def _create_args(box=None, adopt=False, **kw):
    """A `devbox create` argv namespace: the box-create flags plus its own."""
    args = _box_args(name=None, **kw)
    args.box = box
    args.workspace_dir = None
    args.adopt = adopt
    return args


class _CreateStop(Exception):
    """Ends a create at provider.create(); the rest is SSH/provisioning."""


class _FakeCreateProvider:
    """A provider that records the InstanceSpec and stops there. `state` answers
    whether the box already exists, which is what decides create-vs-attach."""

    def __init__(self, name="libvirt", state=None):
        self.name = name
        self._state = state
        self.seen = {}

    def state(self, name):
        return self._state

    def create(self, spec):
        self.seen["spec"] = spec
        raise _CreateStop


def _patch_create(monkeypatch, provider, pubkey):
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: provider)
    monkeypatch.setattr(devbox.cli, "_check_name_unique", lambda gc, name: None)
    monkeypatch.setattr(devbox.cli, "build_box_payload", lambda admin, gc: [])
    monkeypatch.setattr(devbox.cli, "build_payload", lambda p, gc, admin: [])
    return dataclasses.replace(_global_config(pubkey),
                               aws=devbox.AwsConfig(region="us-east-1"))


def test_cmd_box_create_passes_lightsail_flags_into_the_spec(monkeypatch, pubkey):
    # The Lightsail flags have to reach the provider: without this, dropping
    # `lightsail=box.lightsail` from the InstanceSpec would stay green, because
    # every Lightsail field has a default.
    provider = _FakeCreateProvider(name="aws")
    gc = _patch_create(monkeypatch, provider, pubkey)
    args = _box_args("foo-gpu", provider="aws", bundle="medium_3_0")
    with pytest.raises(_CreateStop):
        devbox.cli.cmd_box_create(args, gc)
    spec = provider.seen["spec"]
    assert spec.name == "aws-foo-gpu" and spec.provider == "aws"
    assert spec.lightsail.bundle == "medium_3_0"
    assert spec.admin_user == os.environ.get("USER", "")
    # Lightsail runs user-data as a shell script, so create must hand it one
    # (hostname = the box's canonical name).
    assert spec.user_data.startswith("#!/bin/bash\n")
    assert "DEVBOX_HOSTNAME='aws-foo-gpu'" in spec.user_data


def test_cmd_box_create_on_an_existing_box_is_a_no_op(monkeypatch, pubkey, capsys):
    # `devbox create` is porcelain over this, so it has to be re-runnable.
    provider = _FakeCreateProvider(state="running")
    gc = _patch_create(monkeypatch, provider, pubkey)
    devbox.cli.cmd_box_create(_box_args("dev"), gc)     # no _CreateStop raised
    assert "already exists" in capsys.readouterr().out
    assert provider.seen == {}


def test_cmd_create_boots_the_workspaces_own_box_when_it_is_missing(monkeypatch, pubkey):
    # The zero-config path: a workspace that never named a box owns one called
    # after itself, and `devbox create` boots it on the global [defaults].
    provider = _FakeCreateProvider()
    gc = _patch_create(monkeypatch, provider, pubkey)
    with pytest.raises(_CreateStop):
        devbox.cli.cmd_create(_create_args(), gc, _workspace(workspace="myproj"))
    assert provider.seen["spec"].name == "myproj"


def test_cmd_create_refuses_to_invent_a_missing_shared_box(monkeypatch, pubkey):
    # A workspace pointing at a box that isn't there is a different matter:
    # guessing the shape of somebody else's shared machine is worse than saying
    # so, since hardware can't be changed afterwards.
    provider = _FakeCreateProvider()
    gc = _patch_create(monkeypatch, provider, pubkey)
    with pytest.raises(SystemExit):
        devbox.cli.cmd_create(_create_args(), gc, _workspace(box="ec2-shared"))
    assert provider.seen == {}


def test_cmd_create_boots_a_named_box_when_given_box_flags(monkeypatch, pubkey):
    # ...unless this command line said what shape it should be.
    provider = _FakeCreateProvider()
    gc = _patch_create(monkeypatch, provider, pubkey)
    with pytest.raises(_CreateStop):
        devbox.cli.cmd_create(_create_args(vcpus=8), gc, _workspace(box="shared"))
    assert provider.seen["spec"].name == "shared"
    assert provider.seen["spec"].vcpus == 8


def test_cmd_create_refuses_an_agent_user_that_is_the_boxs_admin(monkeypatch, pubkey):
    # The rule config.py can't check: the box's privileged account is a property
    # of the machine, so the collision only shows up at attach time.
    provider = _FakeCreateProvider(state="running")
    gc = _patch_create(monkeypatch, provider, pubkey)
    monkeypatch.setattr(devbox.cli, "_admin_user", lambda provider, name: "alice")
    with pytest.raises(SystemExit):
        devbox.cli.cmd_create(_create_args(), gc, _workspace(agent_user="alice"))


# ---- libvirt <metadata> stamp -----------------------------------------------
def _domain_xml(metadata_inner="", vcpu=6, memory_kib=12582912):
    return (
        "<domain type='kvm'>"
        "<name>demo</name>"
        f"<memory unit='KiB'>{memory_kib}</memory>"
        f"<vcpu placement='static'>{vcpu}</vcpu>"
        f"<metadata>{metadata_inner}</metadata>"
        "</domain>"
    )


def test_build_metadata_xml_roundtrips_through_reader(monkeypatch):
    stamp = devbox.build_metadata_xml("alice", "libvirt", "2026-07-21T10:00:00+00:00")
    dumpxml = _domain_xml(metadata_inner=stamp, vcpu=4, memory_kib=8388608)
    monkeypatch.setattr(
        devbox.providers.libvirt, "virsh",
        lambda uri, *a, **k: subprocess.CompletedProcess(a, 0, dumpxml, ""),
    )
    info = devbox.read_domain_instance("qemu:///system", "demo")
    assert info == {
        "name": "demo", "admin_user": "alice", "provider": "libvirt",
        "created_at": "2026-07-21T10:00:00+00:00", "vcpus": 4, "memory_kib": 8388608,
    }


def test_read_domain_instance_skips_unmanaged(monkeypatch):
    # A domain with no devbox stamp is not ours -> None (filtered out of `list`).
    monkeypatch.setattr(
        devbox.providers.libvirt, "virsh",
        lambda uri, *a, **k: subprocess.CompletedProcess(a, 0, _domain_xml(), ""),
    )
    assert devbox.read_domain_instance("qemu:///system", "demo") is None


def test_read_domain_instance_none_when_undefined(monkeypatch):
    monkeypatch.setattr(
        devbox.providers.libvirt, "virsh",
        lambda uri, *a, **k: subprocess.CompletedProcess(a, 1, "", "no domain"),
    )
    assert devbox.read_domain_instance("qemu:///system", "demo") is None


# ---- list formatting --------------------------------------------------------
def test_format_resources():
    assert devbox._format_resources(6, 12582912) == "6 vCPU / 12 GiB"
    assert devbox._format_resources(None, None) == "?"


def test_format_age_buckets():
    from datetime import datetime, timezone, timedelta
    assert devbox._format_age(None) == "?"
    assert devbox._format_age("not-a-date") == "?"
    now = datetime.now(timezone.utc)
    assert devbox._format_age((now - timedelta(minutes=30)).isoformat()).endswith("m")
    assert devbox._format_age((now - timedelta(hours=3)).isoformat()) == "3h"
    assert devbox._format_age((now - timedelta(days=2)).isoformat()) == "2d"


# ---- provider seam ----------------------------------------------------------
def test_get_provider_libvirt():
    gc = _global_config(devbox.Path("/nonexistent"))
    assert isinstance(devbox.get_provider("libvirt", gc), devbox.LibvirtProvider)


def test_get_provider_unknown_dies():
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):
        devbox.get_provider("gcp", gc)


def test_libvirt_provider_list_returns_instances(monkeypatch):
    stamp = devbox.build_metadata_xml("alice", "libvirt", "2026-07-21T10:00:00+00:00")
    dumpxml = _domain_xml(metadata_inner=stamp, vcpu=6, memory_kib=12582912)

    def fake_virsh(uri, *a, **k):
        sub = a[0]
        if sub == "list":
            return subprocess.CompletedProcess(a, 0, "demo\n", "")
        if sub == "dumpxml":
            return subprocess.CompletedProcess(a, 0, dumpxml, "")
        if sub == "domstate":
            return subprocess.CompletedProcess(a, 0, "running\n", "")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(devbox.providers.libvirt, "virsh", fake_virsh)
    provider = devbox.LibvirtProvider(_global_config(devbox.Path("/nonexistent")))
    instances = provider.list()
    assert len(instances) == 1
    i = instances[0]
    assert isinstance(i, devbox.Instance)
    assert (i.name, i.provider, i.admin_user, i.state) == ("demo", "libvirt", "alice", "running")
    assert i.resources == "6 vCPU / 12 GiB"
    assert i.created_at == "2026-07-21T10:00:00+00:00"


def test_libvirt_provider_state_and_address_delegate(monkeypatch):
    def fake_virsh(uri, *a, **k):
        if a[0] == "domstate":
            return subprocess.CompletedProcess(a, 0, "shut off\n", "")
        if a[0] == "domifaddr":
            return subprocess.CompletedProcess(a, 0, "vnet0 52:54:00:aa ipv4 192.168.1.5/24\n", "")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(devbox.providers.libvirt, "virsh", fake_virsh)
    provider = devbox.LibvirtProvider(_global_config(devbox.Path("/nonexistent")))
    assert provider.state("demo") == "shut off"
    assert provider.address("demo") == "192.168.1.5"


# ---- fetch_verified_checksum -----------------------------------------------
def test_fetch_verified_checksum_missing_gpgv(monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


def test_fetch_verified_checksum_missing_keyring(monkeypatch, tmp_path):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox.providers.libvirt, "CLOUDIMAGE_KEYRING_PATH", tmp_path / "missing.gpg")
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


def test_fetch_verified_checksum_success(monkeypatch, tmp_path):
    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"fake-keyring")
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox.providers.libvirt, "CLOUDIMAGE_KEYRING_PATH", keyring)

    sums = (
        b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa *other-file.img\n"
        b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef *image.img\n"
    )

    def fake_urlopen(url):
        return _FakeResponse(b"fake-signature" if url.endswith("SHA256SUMS.gpg") else sums)

    monkeypatch.setattr(devbox.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )

    checksum = devbox.fetch_verified_checksum("https://example.com/dir/image.img")
    assert checksum == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_fetch_verified_checksum_gpg_verification_fails(monkeypatch, tmp_path):
    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"fake-keyring")
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox.providers.libvirt, "CLOUDIMAGE_KEYRING_PATH", keyring)
    monkeypatch.setattr(devbox.urllib.request, "urlopen", lambda url: _FakeResponse(b"data"))
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "bad signature"),
    )
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


def test_fetch_verified_checksum_filename_not_listed(monkeypatch, tmp_path):
    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"fake-keyring")
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox.providers.libvirt, "CLOUDIMAGE_KEYRING_PATH", keyring)
    sums = b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef *other-file.img\n"
    monkeypatch.setattr(devbox.urllib.request, "urlopen", lambda url: _FakeResponse(sums))
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


# ---- jj fetch ---------------------------------------------------------------
def _cp(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


# ---- cmd_herdr (argv construction; the exec itself is stubbed) --------------
def _herdr_env(monkeypatch, execs, which=lambda p: f"/usr/bin/{p}"):
    """Stub everything cmd_herdr touches outside the process: target resolution,
    the provider address, the alias refresh, `which`, and the exec."""
    monkeypatch.setattr(devbox.cli, "_resolve_target",
                        lambda args, workspace, gc: ("box1", None, _FakeAddr()))
    monkeypatch.setattr(devbox.cli, "_admin_user", lambda provider, name: "admin")
    monkeypatch.setattr(devbox.cli, "_refresh_ssh_aliases",
                        lambda *a, **kw: ("devbox-proj", "devbox-box1-admin"))
    monkeypatch.setattr(devbox.cli.shutil, "which", which)
    monkeypatch.setattr(devbox.cli.os, "execvp",
                        lambda file, argv: execs.append((file, argv)))


class _FakeAddr:
    def address(self, name):
        return "10.0.0.5"


def test_cmd_herdr_execs_against_the_agent_alias(monkeypatch):
    # The agent alias, not the admin one: herdr drives a shell in the account the
    # checkout and the agents live in. This is what `herdr --remote $(devbox
    # host)` did by hand.
    execs = []
    _herdr_env(monkeypatch, execs)
    devbox.cli.cmd_herdr(argparse.Namespace(herdr_args=[]), None, None)
    assert execs == [("herdr", ["herdr", "--remote", "devbox-proj"])]


def test_cmd_herdr_forwards_trailing_args(monkeypatch):
    execs = []
    _herdr_env(monkeypatch, execs)
    devbox.cli.cmd_herdr(argparse.Namespace(herdr_args=["--session", "work"]), None, None)
    assert execs == [("herdr", ["herdr", "--remote", "devbox-proj", "--session", "work"])]


def test_cmd_herdr_dies_when_herdr_is_not_installed(monkeypatch):
    # herdr runs on the HOST, so a missing binary is a host problem and has to
    # say so - the box is provisioned either way.
    execs = []
    _herdr_env(monkeypatch, execs, which=lambda p: None)
    with pytest.raises(SystemExit):
        devbox.cli.cmd_herdr(argparse.Namespace(herdr_args=[]), None, None)
    assert execs == []


def test_cmd_herdr_dies_without_an_address(monkeypatch):
    class NoAddr:
        def address(self, name):
            return ""

    execs = []
    _herdr_env(monkeypatch, execs)
    monkeypatch.setattr(devbox.cli, "_resolve_target",
                        lambda args, workspace, gc: ("box1", None, NoAddr()))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_herdr(argparse.Namespace(herdr_args=[]), None, None)
    assert execs == []


def test_ensure_jj_remote_adds_when_missing(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:4] == ["jj", "git", "remote", "list"]:
            return _cp(argv, 0, "origin git@github.com:me/x\n")
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.cli._ensure_jj_remote(tmp_path, "devbox-foo", "ssh://devbox-foo/home/u/x")
    assert ["jj", "git", "remote", "add", "devbox-foo", "ssh://devbox-foo/home/u/x"] in calls


def test_ensure_jj_remote_updates_when_url_differs(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:4] == ["jj", "git", "remote", "list"]:
            return _cp(argv, 0, "devbox-foo ssh://devbox-foo/home/u/OLD\n")
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.cli._ensure_jj_remote(tmp_path, "devbox-foo", "ssh://devbox-foo/home/u/NEW")
    assert ["jj", "git", "remote", "set-url", "devbox-foo", "ssh://devbox-foo/home/u/NEW"] in calls


def test_ensure_jj_remote_noop_when_same(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:4] == ["jj", "git", "remote", "list"]:
            return _cp(argv, 0, "devbox-foo ssh://same\n")
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.cli._ensure_jj_remote(tmp_path, "devbox-foo", "ssh://same")
    assert calls == [["jj", "git", "remote", "list"]]  # no add/set-url


def test_ensure_jj_remote_not_a_repo_dies(monkeypatch, tmp_path):
    monkeypatch.setattr(devbox.subprocess, "run", lambda argv, **kw: _cp(argv, 1, "", "not a repo"))
    with pytest.raises(SystemExit):
        devbox.cli._ensure_jj_remote(tmp_path, "devbox-foo", "ssh://x")


def test_cmd_jj_fetch_without_repos_dies():
    import argparse
    gc = _global_config(devbox.Path("/nonexistent"))
    args = argparse.Namespace(name=None, workspace_dir=None, repo=None)
    with pytest.raises(SystemExit):
        devbox.cli.cmd_jj_fetch(args, gc, _workspace(mode="none"))


# ---- _select_repo: which checkout a repo-scoped command acts on -------------
def _multi(tmp_path):
    return _workspace(marker_dir=tmp_path, root_is_repo=False, repos=[
        _repo("frontend", root=tmp_path), _repo("backend", root=tmp_path)])


def test_select_repo_by_name(tmp_path):
    repo = devbox.cli._select_repo(_multi(tmp_path), "backend", tmp_path)
    assert repo.name == "backend"


def test_select_repo_by_name_unknown_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.cli._select_repo(_multi(tmp_path), "nope", tmp_path)


def test_select_repo_from_the_cwd(tmp_path):
    # The cwd decides, the way `jj` itself works: from anywhere inside a
    # checkout, that checkout is the one meant.
    deep = tmp_path / "frontend" / "src" / "components"
    deep.mkdir(parents=True)
    assert devbox.cli._select_repo(_multi(tmp_path), None, deep).name == "frontend"


def test_select_repo_outside_any_checkout_dies(tmp_path):
    # Several to choose from and nothing to choose by: better to ask than guess.
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(SystemExit):
        devbox.cli._select_repo(_multi(tmp_path), None, tmp_path / "elsewhere")


def test_select_repo_falls_back_to_a_lone_repo(tmp_path):
    # One checkout is never ambiguous, so `devbox jj fetch` still works from
    # anywhere in the workspace - which is how the single-repo case behaves today.
    workspace = _workspace(marker_dir=tmp_path, repos=[_repo("only", root=tmp_path)])
    assert devbox.cli._select_repo(workspace, None, tmp_path).name == "only"


# ---- host->box sync ---------------------------------------------------------
def test_resolve_workspace_parses_sync_entries(tmp_path, global_config):
    _write_marker(tmp_path, (
        '[[sync]]\n'
        'src = "$HOME/.claude/skills"\n'
        'include = ["jujutsu"]\n\n'
        '[[repos]]\n'
        'name = "binius64"\n'
        'mode = "clone"\n'
        'url = "https://github.com/x/binius64"\n\n'
        '[[repos.sync]]\n'
        'src = ".claude/skills"\n'
    ))
    entries = devbox.resolve_workspace(tmp_path, global_config).sync
    assert entries == [
        devbox.SyncEntry(src="$HOME/.claude/skills", dst="$HOME/.claude/skills",
                         include=["jujutsu"], anchor=devbox.ANCHOR_HOME),
        devbox.SyncEntry(src=".claude/skills", dst=".claude/skills",
                         include=[], anchor=devbox.ANCHOR_REPO, repo="binius64"),
    ]


def test_resolve_workspace_repo_sync_names_its_own_repo(tmp_path, global_config):
    # Each entry is nested under the repo it anchors at, so it never has to name
    # it - and two repos' rules cannot be confused for each other.
    _write_visible_marker(tmp_path, (
        '[[repos]]\nname = "frontend"\nmode = "push"\n'
        '[[repos.sync]]\nsrc = ".claude/skills"\n\n'
        '[[repos]]\nname = "backend"\nmode = "push"\n'
        '[[repos.sync]]\nsrc = ".env.local"\n'
    ))
    entries = devbox.resolve_workspace(tmp_path, global_config).sync
    assert [(e.repo, e.src) for e in entries] == [
        ("frontend", ".claude/skills"), ("backend", ".env.local")]


def test_resolve_workspace_workspace_relative_sync(tmp_path, global_config):
    # Under a visible marker the root holds the repos, so a bare-relative path
    # means the same child name on both sides.
    _write_visible_marker(tmp_path, '[[sync]]\nsrc = "notes"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).sync == [
        devbox.SyncEntry(src="notes", dst="notes", include=[],
                         anchor=devbox.ANCHOR_WORKSPACE)]


def test_resolve_workspace_relative_sync_under_a_repo_marker_dies(tmp_path, global_config):
    # The root is a repo here, so the host root and the agent's home are not the
    # same place; anchoring across them would silently mean two things.
    _write_marker(tmp_path, '[[sync]]\nsrc = "notes"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_sync_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'workspace = "p"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).sync == []


def test_resolve_workspace_repo_sync_without_a_repo_entry_dies(tmp_path, global_config):
    # `[[repos.sync]]` is nested under a `[[repos]]` entry, so writing one alone
    # declares a repo with nothing but sync rules - caught as a repo missing its
    # `mode`, which is what it is.
    _write_marker(tmp_path, '[[repos.sync]]\nsrc = ".claude/skills"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_sync_relative_path_dies(tmp_path, global_config):
    # [[sync]] paths must be $HOME/~/absolute-anchored, not bare-relative.
    _write_marker(tmp_path, '[[sync]]\nsrc = "relative/skills"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def test_resolve_workspace_repo_sync_anchored_path_dies(tmp_path, global_config):
    _write_marker(tmp_path, (
        '[repo]\nmode = "push"\ndir = "x"\n\n'
        '[[repo.sync]]\nsrc = "$HOME/skills"\n'  # repo.sync must be repo-relative
    ))
    with pytest.raises(SystemExit):
        devbox.resolve_workspace(tmp_path, global_config)


def _sync(src, dst=None, include=None, anchor=devbox.ANCHOR_HOME, repo=""):
    return devbox.SyncEntry(src=src, dst=dst if dst is not None else src,
                            include=include or [], anchor=anchor, repo=repo)


def test_resolve_workspace_prepends_global_sync_defaults(tmp_path, global_config):
    # The global [[sync]] defaults apply to every workspace, ahead of that
    # workspace's own entries - the same order mods use for the built-in base.
    global_config.sync = [_sync("$HOME/.claude/skills")]
    _write_marker(tmp_path, '[[sync]]\nsrc = "$HOME/.ssh/config"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).sync == [
        _sync("$HOME/.claude/skills"),
        _sync("$HOME/.ssh/config"),
    ]


def test_resolve_workspace_with_no_own_sync_keeps_only_global_defaults(tmp_path, global_config):
    global_config.sync = [_sync("$HOME/.claude/skills")]
    _write_marker(tmp_path, 'workspace = "p"\n')
    assert devbox.resolve_workspace(tmp_path, global_config).sync == [
        _sync("$HOME/.claude/skills"),
    ]


def test_plan_sync_home_expansion_per_side(tmp_path):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    proj = _workspace(sync=[_sync("$HOME/.claude/skills")])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert len(jobs) == 1
    assert jobs[0].host_path == f"{tmp_path}/.claude/skills"     # host home
    assert jobs[0].guest_path == "/home/alice/.claude/skills"    # guest home
    assert jobs[0].mirror is True


def test_plan_sync_include_expands_to_per_child_units(tmp_path):
    skills = tmp_path / "skills"
    (skills / "jujutsu").mkdir(parents=True)
    proj = _workspace(sync=[_sync("$HOME/skills", include=["jujutsu"])])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert len(jobs) == 1
    assert jobs[0].host_path == f"{skills}/jujutsu"
    assert jobs[0].guest_path == "/home/alice/skills/jujutsu"


def test_plan_sync_repo_anchoring(tmp_path):
    # A repo-anchored rule starts at that checkout on each side: its host path,
    # and ~/<dir> in the agent's home.
    (tmp_path / "binius64" / ".claude" / "skills").mkdir(parents=True)
    ws = _workspace(marker_dir=tmp_path, root_is_repo=False,
                    repos=[_repo("binius64", mode="clone", url="https://x/y",
                                 root=tmp_path)],
                    sync=[_sync(".claude/skills", anchor=devbox.ANCHOR_REPO,
                                repo="binius64")])
    jobs = devbox.plan_sync(ws, host_home="/host", guest_home="/home/alice")
    assert jobs[0].host_path == f"{tmp_path}/binius64/.claude/skills"
    assert jobs[0].guest_path == "/home/alice/binius64/.claude/skills"


def test_plan_sync_repo_anchoring_picks_the_named_repo(tmp_path):
    # Two checkouts in one account: each rule has to land in its own tree.
    for name in ("frontend", "backend"):
        (tmp_path / name / ".claude").mkdir(parents=True)
    ws = _workspace(marker_dir=tmp_path, root_is_repo=False,
                    repos=[_repo("frontend", root=tmp_path), _repo("backend", root=tmp_path)],
                    sync=[_sync(".claude", anchor=devbox.ANCHOR_REPO, repo="backend")])
    jobs = devbox.plan_sync(ws, host_home="/host", guest_home="/home/alice")
    assert jobs[0].host_path == f"{tmp_path}/backend/.claude"
    assert jobs[0].guest_path == "/home/alice/backend/.claude"


def test_plan_sync_workspace_anchoring(tmp_path):
    # The workspace root against the agent's home - the shape that lines up only
    # when the root holds the repos.
    (tmp_path / "notes").mkdir()
    ws = _workspace(marker_dir=tmp_path, root_is_repo=False,
                    sync=[_sync("notes", anchor=devbox.ANCHOR_WORKSPACE)])
    jobs = devbox.plan_sync(ws, host_home="/host", guest_home="/home/alice")
    assert jobs[0].host_path == f"{tmp_path}/notes"
    assert jobs[0].guest_path == "/home/alice/notes"


def test_plan_sync_file_src_is_not_mirrored(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    proj = _workspace(sync=[_sync("$HOME/f.txt")])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert jobs[0].mirror is False


def test_plan_sync_missing_src_dies(tmp_path):
    proj = _workspace(sync=[_sync("$HOME/nope")])
    with pytest.raises(SystemExit):
        devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")


def test_run_sync_rsync_argv_mirror_dir(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    proj = _workspace(sync=[_sync("$HOME/skills")])
    calls = []
    monkeypatch.setattr(devbox.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or _cp(argv, 0))
    devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")
    # dir -> --delete + trailing slashes (mirror the contents).
    assert calls == [["rsync", "-az", "--mkpath", "--delete",
                      f"{tmp_path}/skills/", "devbox-foo:/home/alice/skills/"]]


def test_run_sync_rsync_argv_file(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("x")
    proj = _workspace(sync=[_sync("$HOME/f.txt", dst="$HOME/g.txt")])
    calls = []
    monkeypatch.setattr(devbox.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or _cp(argv, 0))
    devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")
    # file -> no --delete, no trailing slash.
    assert calls == [["rsync", "-az", "--mkpath",
                      f"{tmp_path}/f.txt", "devbox-foo:/home/alice/g.txt"]]


def test_run_sync_dies_on_rsync_failure(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    proj = _workspace(sync=[_sync("$HOME/skills")])
    monkeypatch.setattr(devbox.subprocess, "run", lambda argv, **k: _cp(argv, 23))
    with pytest.raises(SystemExit):
        devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")


def _envvars_calls(monkeypatch, env):
    """run_envvars against a fake subprocess: [(remote command, stdin)] per file,
    agent's first."""
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("input")))
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.run_envvars("devbox-foo-admin", env, "agentbot")
    assert [argv[:4] for argv, _ in calls] == [["ssh", "devbox-foo-admin", "bash", "-c"]] * 2
    return [(argv[4], stdin) for argv, stdin in calls]


def test_run_envvars_writes_one_managed_file_per_account(monkeypatch):
    (agent_cmd, agent_in), (admin_cmd, admin_in) = _envvars_calls(monkeypatch, {
        "GITHUB_TOKEN": devbox.EnvVar("github_pat_x"),
        "K": devbox.EnvVar("v w"),
        "CLOUDFLARE_TOKEN": devbox.EnvVar("cf_tok", "admin"),
    })
    # Both connect as the PRIVILEGED user: it owns one of the files, and writing
    # the agent's home needs sudo.
    assert "/home/agentbot/.config/devbox/env" in agent_cmd
    assert "-o agentbot -g agentbot -m 0600" in agent_cmd
    assert "/home/agentbot/.zshenv" in agent_cmd
    # Its own home needs no sudo at all.
    assert "sudo" not in admin_cmd
    assert "$HOME/.config/devbox/env" in admin_cmd
    # Each account gets its own entries and no others...
    assert agent_in == b"export GITHUB_TOKEN=github_pat_x\nexport K='v w'\n"
    assert admin_in == b"export CLOUDFLARE_TOKEN=cf_tok\n"
    # ...and the creds are piped over stdin (never in the command / argv / a log).
    assert "github_pat_x" not in agent_cmd
    assert "cf_tok" not in admin_cmd


def test_run_envvars_rewrites_both_files_even_when_one_is_empty(monkeypatch):
    # A full rewrite of both is what makes a key moved to the other owner (or
    # dropped from config) disappear from the account that used to hold it.
    (_, agent_in), (_, admin_in) = _envvars_calls(
        monkeypatch, {"GITHUB_TOKEN": devbox.EnvVar("github_pat_x")})
    assert agent_in == b"export GITHUB_TOKEN=github_pat_x\n"
    assert admin_in == b""


def test_run_envvars_dies_on_failure(monkeypatch):
    monkeypatch.setattr(devbox.subprocess, "run", lambda argv, **k: _cp(argv, 1))
    with pytest.raises(SystemExit):
        devbox.run_envvars("devbox-foo-admin", {"K": devbox.EnvVar("v")}, "agentbot")


# ---- payload delivery -------------------------------------------------------
def _captured_driver_run(monkeypatch, *args, **kw):
    """Run `_run_driver` against a fake subprocess and return what it sent.
    `remote` is the command the box's shell will actually see - argv[4] is that
    string shlex-quoted for ssh, so it gets unwrapped here."""
    import shlex
    captured = {}

    def fake_run(argv, **k):
        captured["argv"] = argv
        captured["remote"] = shlex.split(argv[4])[0]
        captured["input"] = k.get("input")
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.cli._run_driver(*args, **kw)
    return captured


def test_run_driver_makes_the_payload_readable_by_the_agent(monkeypatch):
    # The driver drops into the unprivileged agent to run a mod, so the payload
    # has to be readable by it. mktemp -d is always 0700 whatever the umask, and
    # tar applies the umask to what it creates - including the `mods/` directory,
    # which the archive only implies. Both need pinning or every mod EACCESes.
    c = _captured_driver_run(monkeypatch, "devbox-foo-admin", b"tar", [], log=True)
    remote = c["remote"]
    assert "umask 022" in remote
    assert 'chmod 0755 "$d"' in remote
    assert remote.index("umask 022") < remote.index("tar -xzf")
    assert c["input"] == b"tar"


def test_run_driver_tees_only_when_asked(monkeypatch):
    # The full run's log is what `devbox status` tails; the --ensure-agent
    # preamble must not clobber it with its own two lines of output.
    with_log = _captured_driver_run(monkeypatch, "a", b"", [],
                                    log=devbox.provision_log("botty"))["remote"]
    without = _captured_driver_run(monkeypatch, "a", b"", ["--ensure-agent"])["remote"]
    assert "provision-botty.log" in with_log
    assert "tee" not in without                # nothing tee'd, so nothing clobbered
    assert "--ensure-agent" in without


def test_provision_log_is_per_attachment():
    # Two workspaces reconverging on one box must not overwrite each other's log.
    assert devbox.provision_log("botty") == "provision-botty.log"
    assert devbox.provision_log("webbot") == "provision-webbot.log"
    assert devbox.provision_log() == "provision.log"      # the box's own


def test_run_driver_passes_adopt_through_the_environment(monkeypatch):
    # Not argv: the driver's own argv is "which mods to run", and nothing else.
    remote = _captured_driver_run(monkeypatch, "a", b"", ["--ensure-agent"],
                                  env={"DEVBOX_ATTACH_ADOPT": "1"})["remote"]
    assert "export DEVBOX_ATTACH_ADOPT=1" in remote
    assert remote.index("export DEVBOX_ATTACH_ADOPT=1") < remote.index("provision-driver.sh")


def test_run_driver_quotes_mod_names(monkeypatch):
    remote = _captured_driver_run(monkeypatch, "a", b"", ["rust", "a b"], log=True)["remote"]
    assert "rust 'a b'" in remote


# ---- SSH aliases (one per attachment) ---------------------------------------
def test_alias_names_collapse_when_the_workspace_owns_the_box():
    # The one-workspace case keeps the exact alias every existing jj remote, doc
    # and finger already names - the box and the workspace are the same word.
    assert devbox.cli._alias_names("binius", _workspace(workspace="binius")) == \
        ("devbox-binius", "devbox-binius-admin")


def test_alias_names_are_per_attachment_on_a_shared_box():
    assert devbox.cli._alias_names("dev", _workspace(workspace="binius", box="dev")) == \
        ("devbox-dev-binius", "devbox-dev-admin")


def test_alias_names_without_a_workspace_are_admin_only():
    assert devbox.cli._alias_names("dev") == (None, "devbox-dev-admin")


@pytest.fixture
def ssh_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(devbox.cli, "SSH_DIR", tmp_path / "ssh")
    monkeypatch.setattr(devbox.config, "SSH_DIR", tmp_path / "ssh")
    monkeypatch.setattr(devbox.cli, "SSH_USER_CONFIG", tmp_path / "user_ssh_config")
    return tmp_path / "ssh"


def test_refresh_ssh_aliases_writes_one_stanza_per_file(ssh_dir):
    aliases = devbox.cli._refresh_ssh_aliases(
        "binius", "10.0.0.5", "alice", _workspace(workspace="binius"))
    assert aliases == ("devbox-binius", "devbox-binius-admin")

    agent = (ssh_dir / "binius.binius.conf").read_text()
    admin = (ssh_dir / "binius.admin.conf").read_text()
    assert "Host devbox-binius\n    HostName 10.0.0.5\n    User agentbot\n" in agent
    assert "Host devbox-binius-admin\n    HostName 10.0.0.5\n    User alice\n" in admin
    # Each points at the box's own known-hosts file.
    assert f"UserKnownHostsFile {ssh_dir}/known_hosts.binius" in agent
    assert f"UserKnownHostsFile {ssh_dir}/known_hosts.binius" in admin


def test_refresh_ssh_aliases_keeps_attachments_independent(ssh_dir):
    # The regression that makes sharing a box safe: one file per box would have
    # each workspace's refresh overwrite the last one's account, pointing you at
    # the wrong home without ever erroring.
    first = _workspace(workspace="binius", box="dev", agent_user="biniusbot")
    second = _workspace(workspace="web", box="dev", agent_user="webbot")
    devbox.cli._refresh_ssh_aliases("dev", "10.0.0.5", "alice", first)
    before = (ssh_dir / "dev.binius.conf").read_text()
    devbox.cli._refresh_ssh_aliases("dev", "10.0.0.5", "alice", second)

    assert (ssh_dir / "dev.binius.conf").read_text() == before   # untouched
    assert "User webbot" in (ssh_dir / "dev.web.conf").read_text()
    # ...and exactly one admin stanza between them. `Include .../*.conf` is
    # first-match-wins, so a second copy could shadow the current address.
    assert sorted(p.name for p in ssh_dir.glob("*.conf")) == [
        "dev.admin.conf", "dev.binius.conf", "dev.web.conf"]


def test_refresh_ssh_aliases_rewrites_cleanly_on_a_new_address(ssh_dir):
    # Written whole each time, so a re-created box can't leave a stale duplicate
    # stanza behind.
    workspace = _workspace(workspace="binius")
    devbox.cli._refresh_ssh_aliases("binius", "10.0.0.5", "alice", workspace)
    devbox.cli._refresh_ssh_aliases("binius", "10.0.0.9", "alice", workspace)
    conf = (ssh_dir / "binius.binius.conf").read_text()
    assert conf.count("Host devbox-binius\n") == 1
    assert "10.0.0.5" not in conf


def test_truncate_known_hosts_is_scoped_to_one_box(ssh_dir):
    # Creating box B must not invalidate box A's pinned key - which, on a shared
    # box, would be every workspace attached to it.
    devbox.cli._refresh_ssh_aliases("a", "10.0.0.5", "alice")
    devbox.config.known_hosts_path("a").write_text("10.0.0.5 ssh-ed25519 AAAA\n")
    devbox.cli.truncate_known_hosts("b")
    assert devbox.config.known_hosts_path("a").read_text() != ""
    assert devbox.config.known_hosts_path("b").read_text() == ""


def test_plan_sync_defaults_the_guest_home_to_the_agent(tmp_path):
    # Synced files are agent skills and workspace config: they belong to the
    # account that does the work, not the privileged one.
    (tmp_path / "skills").mkdir()
    proj = _workspace(sync=[_sync("$HOME/skills")], agent_user="botty")
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path))
    assert jobs[0].guest_path == "/home/botty/skills"


def _sync_args(envvars=False, files=False, mods=None):
    import argparse
    return argparse.Namespace(name=None, workspace_dir=None, envvars=envvars, files=files, mods=mods)


def test_split_mods_parsing():
    assert devbox.cli._split_mods("") == []               # --mods with no value -> all
    assert devbox.cli._split_mods("rust") == ["rust"]
    assert devbox.cli._split_mods("a, b ,c") == ["a", "b", "c"]


def test_cmd_sync_requires_a_flag():
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):  # none of --envvars/--mods/--files
        devbox.cli.cmd_sync(_sync_args(), gc, _workspace())


def test_cmd_sync_files_without_entries_dies():
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):  # --files but the workspace has no [[sync]]
        devbox.cli.cmd_sync(_sync_args(files=True), gc, _workspace())


# ---- AWS creds / EC2 config --------------------------------------------------
def test_global_config_aws_section_is_creds_only(tmp_path):
    # [aws] is the shared AWS account config: region/profile, nothing EC2-specific.
    path = tmp_path / "config.toml"
    path.write_text('[aws]\nregion = "us-west-2"\nprofile = "dev"\n')
    gc = devbox.GlobalConfig.load(path)
    assert gc.aws is not None
    assert gc.aws.region == "us-west-2"
    assert gc.aws.profile == "dev"
    assert gc.ec2 is None  # no [ec2] section => no EC2 globals


def test_global_config_ec2_section(tmp_path):
    # The EC2-specific globals live in their own section, separate from the creds.
    path = tmp_path / "config.toml"
    path.write_text(
        "[aws]\n"
        'region = "us-west-2"\n'
        "[ec2]\n"
        'default_security_groups = ["sg-1", "sg-2"]\n'
        'default_subnets = ["subnet-a", "subnet-b"]\n'
        "spot = true\n"
    )
    gc = devbox.GlobalConfig.load(path)
    assert gc.aws.region == "us-west-2"
    assert gc.ec2 is not None
    assert gc.ec2.security_groups == ["sg-1", "sg-2"]
    assert gc.ec2.subnets == ["subnet-a", "subnet-b"]
    assert gc.ec2.spot is True


def test_global_config_ec2_section_defaults(tmp_path):
    # An [ec2] section with nothing in it => empty lists, on-demand.
    path = tmp_path / "config.toml"
    path.write_text("[ec2]\n")
    gc = devbox.GlobalConfig.load(path)
    assert gc.ec2.security_groups == [] and gc.ec2.subnets == [] and gc.ec2.spot is False


def test_global_config_no_aws_section_is_none(tmp_path):
    gc = devbox.GlobalConfig.load(tmp_path / "missing.toml")
    assert gc.aws is None and gc.ec2 is None


def test_global_config_aws_requires_region(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[aws]\nprofile = "dev"\n')
    with pytest.raises(SystemExit):
        devbox.GlobalConfig.load(path)


# ---- resolve_box_spec (a box's shape, from flags) ----------------------------
def test_resolve_box_spec_defaults_to_libvirt_and_the_global_defaults(global_config,
                                                                     monkeypatch):
    monkeypatch.setenv("USER", "alice")
    box = devbox.resolve_box_spec(_box_args("dev"), global_config, "dev")
    assert box.name == "dev"                 # libvirt boxes stay bare-named
    assert box.provider == "libvirt"
    assert box.admin_user == "alice"
    assert (box.vcpus, box.memory_mib, box.disk_gb) == (
        global_config.vcpus, global_config.memory_mib, global_config.disk_gb)
    assert box.ec2 is None and box.lightsail is None


def test_resolve_box_spec_flags_win_over_the_defaults(global_config, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    args = _box_args("dev", vcpus=2, memory=2048, disk=10, user="bob")
    box = devbox.resolve_box_spec(args, global_config, "dev")
    assert (box.vcpus, box.memory_mib, box.disk_gb) == (2, 2048, 10)
    assert box.admin_user == "bob"


def test_resolve_box_spec_composes_the_canonical_cloud_name(global_config, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    args = _box_args("dev", provider="ec2", instance_type="g5.xlarge", arch="arm64")
    box = devbox.resolve_box_spec(args, global_config, "dev")
    assert box.name == "ec2-dev"
    assert box.ec2.instance_type == "g5.xlarge"
    assert box.ec2.architecture == "arm64"
    assert box.ec2.spot is None              # neither --spot nor --on-demand


@pytest.mark.parametrize("flags,expected", [
    ({"spot": True}, True),
    ({"on_demand": True}, False),
])
def test_resolve_box_spec_market_flags(global_config, monkeypatch, flags, expected):
    # Neither flag leaves it None, which defers to the global [ec2].spot.
    monkeypatch.setenv("USER", "alice")
    args = _box_args("dev", provider="ec2", instance_type="t3.micro", **flags)
    assert devbox.resolve_box_spec(args, global_config, "dev").ec2.spot is expected


def test_resolve_box_spec_ec2_requires_an_instance_type(global_config, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    with pytest.raises(SystemExit):
        devbox.resolve_box_spec(_box_args("dev", provider="ec2"), global_config, "dev")


def test_resolve_box_spec_lightsail_defaults(global_config, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    box = devbox.resolve_box_spec(_box_args("dev", provider="aws"), global_config, "dev")
    assert box.name == "aws-dev"
    assert box.lightsail.bundle is None      # the provider substitutes the default
    assert box.lightsail.blueprint == devbox.DEFAULT_LIGHTSAIL_BLUEPRINT


def test_resolve_box_spec_lightsail_flags(global_config, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    args = _box_args("dev", provider="aws", bundle="small_3_0",
                     blueprint="ubuntu_22_04", zone="us-east-1c")
    box = devbox.resolve_box_spec(args, global_config, "dev")
    assert box.lightsail.bundle == "small_3_0"
    assert box.lightsail.blueprint == "ubuntu_22_04"
    assert box.lightsail.availability_zone == "us-east-1c"


def test_resolve_box_spec_rejects_a_name_that_already_has_a_provider_prefix(
        global_config, monkeypatch):
    # The prefix is composed in automatically; passing one would double it.
    monkeypatch.setenv("USER", "alice")
    args = _box_args("ec2-dev", provider="ec2", instance_type="t3.micro")
    with pytest.raises(SystemExit):
        devbox.resolve_box_spec(args, global_config, "ec2-dev")


def test_resolve_box_spec_rejects_an_invalid_admin_username(global_config, monkeypatch):
    monkeypatch.setenv("USER", "")
    with pytest.raises(SystemExit):
        devbox.resolve_box_spec(_box_args("dev"), global_config, "dev")


# ---- EC2 provider -----------------------------------------------------------
class _FakeClientError(Exception):
    """Stands in for botocore.exceptions.ClientError (duck-typed .response)."""
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeEC2:
    def __init__(self, describe_instances=None, images=None, run_side_effect=None):
        self.calls = []
        self._describe_instances = describe_instances or {"Reservations": []}
        self._images = images if images is not None else [
            {"ImageId": "ami-old", "CreationDate": "2024-01-01T00:00:00.000Z"},
            {"ImageId": "ami-new", "CreationDate": "2024-06-01T00:00:00.000Z"},
        ]
        self._run_side_effect = run_side_effect

    def describe_instances(self, **kw):
        self.calls.append(("describe_instances", kw))
        return self._describe_instances

    def describe_images(self, **kw):
        self.calls.append(("describe_images", kw))
        return {"Images": list(self._images)}

    def run_instances(self, **kw):
        self.calls.append(("run_instances", kw))
        if self._run_side_effect is not None:
            return self._run_side_effect(kw)
        return {"Instances": [{"InstanceId": "i-new", "State": {"Name": "pending"},
                               "InstanceType": kw["InstanceType"]}]}

    def start_instances(self, **kw):
        self.calls.append(("start_instances", kw))
        return {}

    def stop_instances(self, **kw):
        self.calls.append(("stop_instances", kw))
        return {}

    def terminate_instances(self, **kw):
        self.calls.append(("terminate_instances", kw))
        return {}


def _ec2_gc(security_groups=("sg-1",), subnets=(), spot=False):
    """A global config with AWS creds ([aws]) plus the EC2 globals ([ec2])."""
    gc = devbox.GlobalConfig.load(devbox.Path("/nonexistent"))
    gc.aws = devbox.AwsConfig(region="us-east-1", profile=None)
    gc.ec2 = devbox.Ec2Config(security_groups=list(security_groups),
                              subnets=list(subnets), spot=spot)
    return gc


def _ec2_instance(name="foo", state="running", itype="c7i.large", admin_user="alice", public_ip="1.2.3.4"):
    tags = ([{"Key": "devbox:name", "Value": name}] if name is not None else []) + [
        {"Key": "devbox:managed", "Value": "true"},
        {"Key": "devbox:admin-user", "Value": admin_user},
        {"Key": "devbox:created-at", "Value": "2026-07-21T10:00:00+00:00"},
    ]
    d = {"InstanceId": "i-123", "State": {"Name": state}, "InstanceType": itype, "Tags": tags}
    if public_ip:
        d["PublicIpAddress"] = public_ip
    return d


def _reservations(*instances):
    return {"Reservations": [{"Instances": list(instances)}]}


def _ec2_spec(name="foo-gpu", instance_type="g5.xlarge", architecture="x86_64",
              spot=None, ami=None, disk_gb=64):
    return devbox.InstanceSpec(
        name=name, admin_user="alice", provider="ec2", disk_gb=disk_gb,
        user_data="#cloud-config\n", created_at="2026-07-21T12:00:00+00:00",
        ec2=devbox.Ec2BoxConfig(instance_type=instance_type, architecture=architecture,
                                    spot=spot, ami=ami),
    )


def test_get_provider_ec2_requires_aws_creds():
    with pytest.raises(SystemExit):
        devbox.get_provider("ec2", devbox.GlobalConfig.load(devbox.Path("/nonexistent")))


def test_get_provider_ec2_configured():
    assert isinstance(devbox.get_provider("ec2", _ec2_gc()), devbox.Ec2Provider)


def test_ec2_provider_without_ec2_section_uses_empty_defaults():
    # [ec2] is optional: no security groups, AWS picks the subnet, on-demand.
    import dataclasses
    gc = dataclasses.replace(_ec2_gc(), ec2=None)
    provider = devbox.Ec2Provider(gc, client=_FakeEC2())
    provider.create(_ec2_spec())
    params = [kw for (op, kw) in provider.ec2.calls if op == "run_instances"][0]
    assert "SecurityGroupIds" not in params
    assert "SubnetId" not in params
    assert "InstanceMarketOptions" not in params


def test_all_providers_gates_aws_family_on_aws_creds():
    # Both AWS-backed providers (ec2 + aws/Lightsail) appear only with creds.
    names = lambda gc: [p.name for p in devbox.all_providers(gc)]
    assert names(devbox.GlobalConfig.load(devbox.Path("/nonexistent"))) == ["libvirt"]
    assert names(_ec2_gc()) == ["libvirt", "ec2", "aws"]


def test_ec2_list_parses_and_flags_cost():
    client = _FakeEC2(describe_instances=_reservations(
        _ec2_instance(name="foo", state="running", itype="c7i.large"),
        _ec2_instance(name=None),  # no devbox:name tag -> skipped
    ))
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    instances = provider.list()
    assert len(instances) == 1
    i = instances[0]
    assert (i.name, i.provider, i.admin_user, i.state, i.resources) == \
        ("foo", "ec2", "alice", "running", "c7i.large")
    assert i.cost_hint is True


def test_ec2_state_and_address():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(state="running")))
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    assert provider.state("foo") == "running"
    assert provider.address("foo") == "1.2.3.4"


def test_ec2_state_none_when_absent():
    provider = devbox.Ec2Provider(_ec2_gc(), client=_FakeEC2())
    assert provider.state("nope") is None
    assert provider.address("nope") is None


def test_ec2_create_on_demand_request_shape():
    client = _FakeEC2()  # describe_instances empty (no existing), images present
    provider = devbox.Ec2Provider(_ec2_gc(security_groups=["sg-1"]), client=client)
    provider.create(_ec2_spec(name="foo-gpu", instance_type="g5.xlarge", disk_gb=64))
    run = [kw for (op, kw) in client.calls if op == "run_instances"]
    assert len(run) == 1
    params = run[0]
    assert params["ImageId"] == "ami-new"  # newest by CreationDate
    assert params["InstanceType"] == "g5.xlarge"
    assert params["UserData"] == "#cloud-config\n"
    assert params["SecurityGroupIds"] == ["sg-1"]
    assert "InstanceMarketOptions" not in params  # on-demand
    ebs = params["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["VolumeSize"] == 64 and ebs["VolumeType"] == "gp3"
    tags = {t["Key"]: t["Value"] for t in params["TagSpecifications"][0]["Tags"]}
    assert tags["devbox:managed"] == "true"
    assert tags["devbox:name"] == "foo-gpu"
    assert tags["devbox:admin-user"] == "alice"
    assert tags["devbox:created-at"] == "2026-07-21T12:00:00+00:00"
    assert tags["Name"] == "devbox-foo-gpu"


def test_ec2_create_name_tag_strips_provider_prefix():
    # The canonical devbox name is 'ec2-binius-arm' (how commands address it),
    # but the AWS-side console Name drops the redundant provider prefix.
    client = _FakeEC2()
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    provider.create(_ec2_spec(name="ec2-binius-arm"))
    params = [kw for (op, kw) in client.calls if op == "run_instances"][0]
    tags = {t["Key"]: t["Value"] for t in params["TagSpecifications"][0]["Tags"]}
    assert tags["devbox:name"] == "ec2-binius-arm"  # addressing tag: canonical
    assert tags["Name"] == "devbox-binius-arm"       # console display: bare


def test_ec2_create_spot_adds_market_options():
    client = _FakeEC2()
    provider = devbox.Ec2Provider(_ec2_gc(spot=False), client=client)
    provider.create(_ec2_spec(spot=True))  # workspace overrides global default
    params = [kw for (op, kw) in client.calls if op == "run_instances"][0]
    assert params["InstanceMarketOptions"]["MarketType"] == "spot"
    assert params["InstanceMarketOptions"]["SpotOptions"]["SpotInstanceType"] == "persistent"


def test_ec2_create_explicit_ami_skips_lookup():
    client = _FakeEC2()
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    provider.create(_ec2_spec(ami="ami-pinned"))
    assert not any(op == "describe_images" for (op, _) in client.calls)
    params = [kw for (op, kw) in client.calls if op == "run_instances"][0]
    assert params["ImageId"] == "ami-pinned"


def test_ec2_create_no_ami_found_dies():
    client = _FakeEC2(images=[])
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_ec2_spec())


def test_ec2_create_existing_dies():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(name="foo-gpu")))
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_ec2_spec(name="foo-gpu"))


def test_ec2_create_subnet_capacity_fallback():
    attempts = []

    def run_side_effect(kw):
        attempts.append(kw.get("SubnetId"))
        if len(attempts) == 1:
            raise _FakeClientError("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": "i-2", "State": {"Name": "pending"},
                               "InstanceType": kw["InstanceType"]}]}

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.Ec2Provider(_ec2_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    provider.create(_ec2_spec())
    assert attempts == ["subnet-a", "subnet-b"]


def test_ec2_create_all_subnets_exhausted_dies():
    def run_side_effect(kw):
        raise _FakeClientError("InsufficientInstanceCapacity")

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.Ec2Provider(_ec2_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    with pytest.raises(SystemExit):
        provider.create(_ec2_spec())


def test_ec2_create_non_capacity_error_reraises():
    def run_side_effect(kw):
        raise _FakeClientError("UnauthorizedOperation")

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.Ec2Provider(_ec2_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    with pytest.raises(_FakeClientError):
        provider.create(_ec2_spec())
    # Not retried against the second subnet.
    assert len([1 for (op, _) in client.calls if op == "run_instances"]) == 1


def test_ec2_start_stop_destroy_by_name():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(name="foo", state="stopped")))
    provider = devbox.Ec2Provider(_ec2_gc(), client=client)
    provider.start("foo")
    provider.stop("foo")
    provider.destroy("foo")
    ops = {op: kw for (op, kw) in client.calls if op != "describe_instances"}
    assert ops["start_instances"]["InstanceIds"] == ["i-123"]
    assert ops["stop_instances"]["InstanceIds"] == ["i-123"]
    assert ops["terminate_instances"]["InstanceIds"] == ["i-123"]


# ---- aws (Lightsail) provider -----------------------------------------------
class _FakeLightsail:
    """Fake boto3 lightsail client: records calls, returns canned dicts.
    `pages` is the list of instance-list pages get_instances walks (one page by
    default), so pagination is exercised the way the real API paginates."""
    def __init__(self, instances=None, pages=None):
        self.calls = []
        self._pages = pages if pages is not None else [list(instances or [])]

    def get_instances(self, **kw):
        self.calls.append(("get_instances", kw))
        # pageToken is the index of the page to return; absent = the first page.
        idx = int(kw.get("pageToken", 0))
        nxt = idx + 1
        return {"instances": list(self._pages[idx]),
                "nextPageToken": str(nxt) if nxt < len(self._pages) else None}

    def create_instances(self, **kw):
        self.calls.append(("create_instances", kw))
        return {"operations": [{"status": "Started"}]}

    def start_instance(self, **kw):
        self.calls.append(("start_instance", kw))
        return {}

    def stop_instance(self, **kw):
        self.calls.append(("stop_instance", kw))
        return {}

    def delete_instance(self, **kw):
        self.calls.append(("delete_instance", kw))
        return {}


def _aws_gc(region="us-west-2"):
    """A global config with only the shared [aws] creds - the `aws` provider
    needs no global section of its own."""
    gc = devbox.GlobalConfig.load(devbox.Path("/nonexistent"))
    gc.aws = devbox.AwsConfig(region=region, profile=None)
    return gc


def _ls_instance(canonical="aws-foo", bare="foo", state="running",
                 bundle="xlarge_3_0", admin_user="alice", public_ip="1.2.3.4",
                 managed="true"):
    """A get_instances entry. Lightsail tags are lowercase key/value (unlike
    EC2's Key/Value), and the Lightsail-side `name` is the bare name while the
    canonical devbox name lives in the devbox:name tag."""
    tags = ([{"key": "devbox:name", "value": canonical}] if canonical is not None else []) + [
        {"key": "devbox:admin-user", "value": admin_user},
        {"key": "devbox:created-at", "value": "2026-07-21T10:00:00+00:00"},
    ]
    if managed is not None:
        tags.append({"key": "devbox:managed", "value": managed})
    inst = {"name": bare, "state": {"code": 16, "name": state},
            "bundleId": bundle, "blueprintId": "ubuntu_24_04", "tags": tags}
    if public_ip:
        inst["publicIpAddress"] = public_ip
    return inst


_LS_DEFAULT_CFG = object()  # sentinel: "not passed" (None means "no section")


def _ls_spec(name="aws-foo-gpu", lightsail=_LS_DEFAULT_CFG):
    """An InstanceSpec for the aws provider. `lightsail` defaults to a plain
    LightsailBoxConfig(); pass None for the no-[lightsail]-flags case."""
    cfg = devbox.LightsailBoxConfig() if lightsail is _LS_DEFAULT_CFG else lightsail
    return devbox.InstanceSpec(
        name=name, admin_user="alice", provider="aws", disk_gb=64,
        user_data="#cloud-config\n", created_at="2026-07-21T12:00:00+00:00",
        lightsail=cfg,
    )


def _create_params(client):
    return [kw for (op, kw) in client.calls if op == "create_instances"][0]


def test_get_provider_aws_requires_aws_creds():
    with pytest.raises(SystemExit):
        devbox.get_provider("aws", devbox.GlobalConfig.load(devbox.Path("/nonexistent")))


def test_get_provider_aws_configured():
    assert isinstance(devbox.get_provider("aws", _aws_gc()), devbox.LightsailProvider)


def test_lightsail_create_default_request_shape():
    client = _FakeLightsail()
    devbox.LightsailProvider(_aws_gc(region="us-west-2"), client=client).create(
        _ls_spec(name="aws-foo-gpu"))
    params = _create_params(client)
    assert params["instanceNames"] == ["foo-gpu"]        # bare, prefix dropped
    assert params["bundleId"] == "xlarge_3_0"            # 16 GB / 4 vCPU / 320 GB
    assert params["blueprintId"] == "ubuntu_24_04"
    assert params["availabilityZone"] == "us-west-2a"    # <region> + "a"
    assert params["ipAddressType"] == "ipv4"
    # Lightsail takes userData as plain text (EC2 base64s, Lightsail does not).
    assert params["userData"] == "#cloud-config\n"
    assert "keyPairName" not in params                   # SSH comes from cloud-init
    tags = {t["key"]: t["value"] for t in params["tags"]}
    assert tags["devbox:managed"] == "true"
    assert tags["devbox:name"] == "aws-foo-gpu"          # addressing tag: canonical
    assert tags["devbox:admin-user"] == "alice"
    assert tags["devbox:created-at"] == "2026-07-21T12:00:00+00:00"


def test_lightsail_create_explicit_overrides_win():
    client = _FakeLightsail()
    cfg = devbox.LightsailBoxConfig(bundle="medium_3_0", blueprint="debian_12",
                                        availability_zone="eu-central-1c")
    devbox.LightsailProvider(_aws_gc(), client=client).create(_ls_spec(lightsail=cfg))
    params = _create_params(client)
    # An explicit bundle wins over DEFAULT_LIGHTSAIL_BUNDLE.
    assert params["bundleId"] == "medium_3_0"
    assert params["blueprintId"] == "debian_12"
    assert params["availabilityZone"] == "eu-central-1c"


def test_lightsail_create_without_lightsail_section_uses_defaults():
    # [lightsail] is entirely optional: `provider = "aws"` alone is a complete
    # workspace (unlike [ec2], which requires an instance_type).
    client = _FakeLightsail()
    devbox.LightsailProvider(_aws_gc(), client=client).create(_ls_spec(lightsail=None))
    params = _create_params(client)
    assert params["bundleId"] == "xlarge_3_0"
    assert params["blueprintId"] == "ubuntu_24_04"
    assert params["availabilityZone"] == "us-west-2a"


def test_lightsail_create_ignores_disk_gb():
    # The bundle fixes the disk, so there is no volume parameter to send.
    client = _FakeLightsail()
    devbox.LightsailProvider(_aws_gc(), client=client).create(_ls_spec())
    assert not any(k in _create_params(client) for k in ("diskSize", "addOns", "disk_gb"))


def test_lightsail_create_existing_dies():
    client = _FakeLightsail([_ls_instance(canonical="aws-foo-gpu", bare="foo-gpu")])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_ls_spec(name="aws-foo-gpu"))
    assert not any(op == "create_instances" for (op, _) in client.calls)


def test_lightsail_create_bare_name_collision_dies(capsys):
    # devbox drops the 'aws-' prefix AWS-side, so aws-foo-gpu wants the Lightsail
    # name 'foo-gpu'. A pre-existing instance holding that name - even one devbox
    # doesn't manage - must fail with our message, not a raw boto3 error.
    client = _FakeLightsail([{"name": "foo-gpu", "state": {"name": "running"}, "tags": []}])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_ls_spec(name="aws-foo-gpu"))
    assert not any(op == "create_instances" for (op, _) in client.calls)
    assert "--name" in capsys.readouterr().err


def test_lightsail_state_and_address_match_canonical_tag():
    # Lookup is by the devbox:name tag, not the Lightsail-side bare name.
    client = _FakeLightsail([_ls_instance(canonical="aws-foo", bare="foo")])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    assert provider.state("aws-foo") == "running"
    assert provider.address("aws-foo") == "1.2.3.4"
    assert provider.state("foo") is None  # the bare name is not the devbox name


def test_lightsail_state_and_address_none_when_absent():
    provider = devbox.LightsailProvider(_aws_gc(), client=_FakeLightsail())
    assert provider.state("aws-nope") is None
    assert provider.address("aws-nope") is None


def test_lightsail_address_none_before_ip_assigned():
    # A pending instance has no publicIpAddress yet -> None (the create poll waits).
    client = _FakeLightsail([_ls_instance(state="pending", public_ip=None)])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    assert provider.state("aws-foo") == "pending"
    assert provider.address("aws-foo") is None


def test_lightsail_find_pages_through_results():
    # get_instances has no filter param, so discovery pages the whole list.
    client = _FakeLightsail(pages=[
        [_ls_instance(canonical="aws-other", bare="other")],
        [_ls_instance(canonical="aws-foo", bare="foo", public_ip="5.6.7.8")],
    ])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    assert provider.address("aws-foo") == "5.6.7.8"
    assert len([1 for (op, _) in client.calls if op == "get_instances"]) == 2


def test_lightsail_list_filters_and_flags_cost():
    client = _FakeLightsail([
        _ls_instance(canonical="aws-foo", bare="foo", bundle="xlarge_3_0"),
        _ls_instance(canonical="aws-unmanaged", bare="unmanaged", managed=None),  # not ours
        _ls_instance(canonical=None, bare="notag"),  # no devbox:name tag -> skipped
    ])
    instances = devbox.LightsailProvider(_aws_gc(), client=client).list()
    assert len(instances) == 1
    i = instances[0]
    assert (i.name, i.provider, i.admin_user, i.state, i.resources) == \
        ("aws-foo", "aws", "alice", "running", "xlarge_3_0")
    assert i.cost_hint is True


def test_lightsail_start_stop_destroy_use_bare_instance_name():
    client = _FakeLightsail([_ls_instance(canonical="aws-foo", bare="foo", state="stopped")])
    provider = devbox.LightsailProvider(_aws_gc(), client=client)
    provider.start("aws-foo")
    provider.stop("aws-foo")
    provider.destroy("aws-foo")
    ops = {op: kw for (op, kw) in client.calls if op != "get_instances"}
    assert ops["start_instance"] == {"instanceName": "foo"}
    assert ops["stop_instance"] == {"instanceName": "foo"}
    assert ops["delete_instance"] == {"instanceName": "foo"}


def test_lightsail_destroy_missing_is_a_no_op(capsys):
    client = _FakeLightsail()
    devbox.LightsailProvider(_aws_gc(), client=client).destroy("aws-gone")
    assert not any(op == "delete_instance" for (op, _) in client.calls)
    assert "not found" in capsys.readouterr().out


def test_lightsail_start_missing_dies():
    provider = devbox.LightsailProvider(_aws_gc(), client=_FakeLightsail())
    with pytest.raises(SystemExit):
        provider.start("aws-gone")


# ---- destroy confirmation guard (JIM-69) ------------------------------------
class _FakeStdin:
    """Minimal stdin stand-in: only `isatty()` matters to _confirm_destroy."""
    def __init__(self, isatty: bool):
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def test_confirm_destroy_assume_yes_bypasses_even_non_tty(monkeypatch):
    # --yes short-circuits before any TTY check or prompt.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() must not be called"))
    assert devbox.cli._confirm_destroy("foo", "libvirt", assume_yes=True,
                                       stdin=_FakeStdin(isatty=False)) is True


def test_confirm_destroy_non_tty_without_yes_dies(monkeypatch):
    # No TTY and no --yes -> die (can't safely prompt); input() never reached.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() must not be called"))
    with pytest.raises(SystemExit):
        devbox.cli._confirm_destroy("foo", "libvirt", assume_yes=False,
                                    stdin=_FakeStdin(isatty=False))


def test_confirm_destroy_tty_full_yes_confirms(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    assert devbox.cli._confirm_destroy("foo", "libvirt", assume_yes=False,
                                       stdin=_FakeStdin(isatty=True)) is True


@pytest.mark.parametrize("response", ["y", "no", "", "YES", "yes please"])
def test_confirm_destroy_tty_anything_but_yes_aborts(monkeypatch, response):
    # Only the exact word 'yes' proceeds; 'y' / 'no' / '' / etc. abort.
    monkeypatch.setattr("builtins.input", lambda *a: response)
    assert devbox.cli._confirm_destroy("foo", "libvirt", assume_yes=False,
                                       stdin=_FakeStdin(isatty=True)) is False


class _RecordingProvider:
    """Provider seam stub recording whether destroy() was invoked."""
    name = "libvirt"

    def __init__(self):
        self.destroyed = []

    def destroy(self, name):
        self.destroyed.append(name)


def _destroy_args(name=None, yes=False):
    return argparse.Namespace(name=name, box=name, yes=yes)


def _patch_destroy(monkeypatch, provider, ssh_dir, attached=()):
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: provider)
    monkeypatch.setattr(devbox.cli, "_attached_workspaces",
                        lambda provider, name: list(attached))


def test_cmd_destroy_yes_flag_destroys(monkeypatch, capsys, ssh_dir):
    provider = _RecordingProvider()
    _patch_destroy(monkeypatch, provider, ssh_dir)
    gc = _global_config(devbox.Path("/nonexistent"))
    devbox.cli.cmd_destroy(_destroy_args(yes=True), gc, _workspace())
    assert provider.destroyed == ["test-workspace"]
    assert "Destroyed 'test-workspace'" in capsys.readouterr().out


def test_cmd_destroy_declined_leaves_untouched(monkeypatch, capsys, ssh_dir):
    # A TTY that answers 'no' aborts: provider.destroy is never called.
    provider = _RecordingProvider()
    _patch_destroy(monkeypatch, provider, ssh_dir)
    monkeypatch.setattr(devbox.cli.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    gc = _global_config(devbox.Path("/nonexistent"))
    devbox.cli.cmd_destroy(_destroy_args(yes=False), gc, _workspace())
    assert provider.destroyed == []
    assert "Aborted" in capsys.readouterr().out


def test_cmd_destroy_refuses_while_other_workspaces_are_attached(monkeypatch, ssh_dir):
    # A shared box is somebody else's machine too: destroying it would take
    # their checkouts with it, so the blast radius has to be spelled out.
    provider = _RecordingProvider()
    _patch_destroy(monkeypatch, provider, ssh_dir, attached=["test-workspace", "web"])
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_destroy(_destroy_args(yes=True), gc, _workspace())
    assert provider.destroyed == []


def test_cmd_destroy_proceeds_when_this_workspace_is_the_only_one(monkeypatch, ssh_dir):
    provider = _RecordingProvider()
    _patch_destroy(monkeypatch, provider, ssh_dir, attached=["test-workspace"])
    gc = _global_config(devbox.Path("/nonexistent"))
    devbox.cli.cmd_destroy(_destroy_args(yes=True), gc, _workspace())
    assert provider.destroyed == ["test-workspace"]


def test_confirm_destroy_names_the_attached_workspaces(monkeypatch, capsys):
    # What is about to be deleted, in the prompt: `destroy` now takes N
    # workspaces' uncommitted work with it, not one.
    monkeypatch.setattr(devbox.cli.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    devbox.cli._confirm_destroy("dev", "libvirt", False, attached=["binius", "web"])
    out = capsys.readouterr().out
    assert "binius, web" in out


# ---- AWS credential handling (JIM-97) ---------------------------------------
class SSOError(Exception):
    """Stands in for botocore.exceptions.SSOError. awsauth recognises botocore
    exceptions by class name (it never imports botocore), so the fakes carry the
    real names - that naming *is* the contract under test."""


class UnauthorizedSSOTokenError(SSOError):
    """Stands in for botocore's expired-SSO-session error, which awsauth matches
    only through its SSOError base."""


class _FakeCredentials:
    """boto3 credentials stand-in. An SSO profile resolves to a *deferred*
    credentials object, so the token only turns out to be stale when
    get_frozen_credentials() forces the refresh."""
    def __init__(self, frozen_error=None):
        self._frozen_error = frozen_error

    def get_frozen_credentials(self):
        if self._frozen_error is not None:
            raise self._frozen_error
        return ("AKIAEXAMPLE", "secret")


class _FakeSession:
    """boto3.Session stand-in: only credential resolution matters here."""
    def __init__(self, credentials=None, get_error=None):
        self.credentials = credentials
        self.get_error = get_error
        self.clients = []

    def get_credentials(self):
        if self.get_error is not None:
            raise self.get_error
        return self.credentials

    def client(self, service):
        self.clients.append(service)
        return f"{service}-client"


class LoginRefreshRequired(Exception):
    """Stands in for botocore.exceptions.LoginRefreshRequired - the IAM Identity
    Center `aws login` refresh-token-expired error, raised straight from
    get_frozen_credentials rather than as an SSOError subclass (JIM-97)."""


_EXPIRED_SSO = UnauthorizedSSOTokenError(
    "The SSO session associated with this profile has expired or is otherwise "
    "invalid. To refresh this SSO session run aws sso login with the "
    "corresponding profile.")

_LOGIN_REFRESH_REQUIRED = LoginRefreshRequired(
    "Your session has expired or credentials have changed. Please reauthenticate "
    "using 'aws login'.")


def _ok_session():
    return _FakeSession(credentials=_FakeCredentials())


def _expired_session():
    return _FakeSession(credentials=_FakeCredentials(frozen_error=_EXPIRED_SSO))


@pytest.fixture
def aws_config_file(tmp_path, monkeypatch):
    """An ~/.aws/config with one SSO profile, one static-keys profile, and an
    SSO default. AWS_PROFILE is cleared so the host's env can't leak in."""
    cfg = tmp_path / "aws-config"
    cfg.write_text(
        "[profile dev]\n"
        "sso_session = corp\n"
        "sso_account_id = 1234\n"
        "region = us-east-1\n\n"
        "[profile keys]\n"
        "region = us-east-1\n\n"
        "[default]\n"
        "sso_start_url = https://example.awsapps.com/start\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return cfg


def _aws(profile="dev"):
    return devbox.AwsConfig(region="us-east-1", profile=profile)


# -- is_auth_error (what counts as "log in again") --
def test_is_auth_error_matches_sso_subclass():
    # Matched through the MRO: UnauthorizedSSOTokenError is only listed via SSOError.
    assert devbox.is_auth_error(_EXPIRED_SSO)


def test_is_auth_error_matches_client_error_codes():
    assert devbox.is_auth_error(_FakeClientError("ExpiredToken"))
    assert devbox.is_auth_error(_FakeClientError("UnauthenticatedException"))


def test_is_auth_error_ignores_other_failures():
    # A capacity error or a plain bug is not a credentials problem.
    assert not devbox.is_auth_error(_FakeClientError("InsufficientInstanceCapacity"))
    assert not devbox.is_auth_error(RuntimeError("boom"))


def test_is_auth_error_matches_login_refresh_required():
    # Not an SSOError subclass, so it needs its own name in the set (JIM-97).
    assert devbox.is_auth_error(_LOGIN_REFRESH_REQUIRED)


# -- credential_problem (the up-front check) --
def test_credential_problem_none_when_credentials_resolve():
    assert devbox.credential_problem(_ok_session()) is None


def test_credential_problem_reports_expired_sso_token():
    problem = devbox.credential_problem(_expired_session())
    # First sentence only - the "run aws sso login" one is ours to say.
    assert problem == ("The SSO session associated with this profile has expired "
                       "or is otherwise invalid")


def test_credential_problem_reports_missing_credentials():
    assert devbox.credential_problem(_FakeSession(credentials=None)) == "no credentials found"


def test_credential_problem_reports_load_time_failure():
    session = _FakeSession(get_error=SSOError("token file is unreadable"))
    assert devbox.credential_problem(session) == "token file is unreadable"


def test_credential_problem_reports_login_refresh_required():
    # Regression (JIM-97): get_frozen_credentials raising LoginRefreshRequired
    # used to be re-raised as a non-auth error instead of caught here.
    session = _FakeSession(credentials=_FakeCredentials(frozen_error=_LOGIN_REFRESH_REQUIRED))
    assert devbox.credential_problem(session) == (
        "Your session has expired or credentials have changed")


def test_credential_problem_reraises_non_auth_errors():
    session = _FakeSession(get_error=RuntimeError("disk on fire"))
    with pytest.raises(RuntimeError):
        devbox.credential_problem(session)


# -- login_command (only SSO profiles have one) --
def test_login_command_for_sso_profile(aws_config_file):
    assert devbox.login_command(_aws("dev")) == ["aws", "sso", "login", "--profile", "dev"]


def test_login_command_for_sso_default_profile(aws_config_file):
    assert devbox.login_command(_aws(None)) == ["aws", "sso", "login"]


def test_login_command_none_for_static_key_profile(aws_config_file):
    # Nothing devbox could run would refresh static keys - say so instead.
    assert devbox.login_command(_aws("keys")) is None


def test_login_command_none_for_unknown_profile(aws_config_file):
    assert devbox.login_command(_aws("nope")) is None


def test_login_command_falls_back_to_aws_profile_env(aws_config_file, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "dev")
    assert devbox.login_command(_aws(None)) == ["aws", "sso", "login", "--profile", "dev"]


# -- prompt_login --
@pytest.fixture
def aws_cli_present(monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


def _record_runs(monkeypatch, returncode=0):
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    return runs


def test_prompt_login_runs_the_login_when_accepted(aws_config_file, aws_cli_present,
                                                   monkeypatch, capsys):
    runs = _record_runs(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # bare Enter = yes
    devbox.prompt_login(_aws("dev"), "expired", stdin=_FakeStdin(isatty=True))
    assert runs == [["aws", "sso", "login", "--profile", "dev"]]
    assert "are missing or expired (expired)" in capsys.readouterr().out


def test_prompt_login_declined_raises_with_the_command(aws_config_file, aws_cli_present,
                                                       monkeypatch):
    runs = _record_runs(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    with pytest.raises(devbox.AwsAuthError, match="aws sso login --profile dev"):
        devbox.prompt_login(_aws("dev"), "expired", stdin=_FakeStdin(isatty=True))
    assert runs == []


def test_prompt_login_non_tty_raises_without_prompting(aws_config_file, aws_cli_present,
                                                       monkeypatch):
    # No TTY to ask on (a script, a pipe): say what to run rather than hang.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() must not be called"))
    with pytest.raises(devbox.AwsAuthError, match="aws sso login --profile dev"):
        devbox.prompt_login(_aws("dev"), "expired", stdin=_FakeStdin(isatty=False))


def test_prompt_login_without_a_login_command_raises(aws_config_file, aws_cli_present,
                                                     monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() must not be called"))
    with pytest.raises(devbox.AwsAuthError, match="refresh them and re-run"):
        devbox.prompt_login(_aws("keys"), "expired", stdin=_FakeStdin(isatty=True))


def test_prompt_login_without_the_aws_cli_raises(aws_config_file, monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda cmd: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() must not be called"))
    with pytest.raises(devbox.AwsAuthError, match="not on \\$PATH"):
        devbox.prompt_login(_aws("dev"), "expired", stdin=_FakeStdin(isatty=True))


def test_prompt_login_failed_login_raises(aws_config_file, aws_cli_present, monkeypatch):
    _record_runs(monkeypatch, returncode=1)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    with pytest.raises(devbox.AwsAuthError, match="failed \\(exit 1\\)"):
        devbox.prompt_login(_aws("dev"), "expired", stdin=_FakeStdin(isatty=True))


# -- ensure_credentials (check -> prompt -> re-check on a fresh session) --
def test_ensure_credentials_passes_through_when_valid(monkeypatch):
    monkeypatch.setattr(devbox.awsauth, "prompt_login",
                        lambda *a, **kw: pytest.fail("must not prompt"))
    session = _ok_session()
    assert devbox.ensure_credentials(lambda: session, _aws("dev")) is session


def test_ensure_credentials_prompts_then_uses_a_fresh_session(monkeypatch):
    # The stale session caches what it resolved, so the post-login re-check has
    # to run against a newly built one.
    sessions = [_expired_session(), _ok_session()]
    prompted = []
    monkeypatch.setattr(devbox.awsauth, "prompt_login",
                        lambda aws, reason, stdin=None: prompted.append(reason))
    got = devbox.ensure_credentials(lambda: sessions.pop(0), _aws("dev"))
    assert got is not None and not sessions          # both sessions were built
    assert prompted and "expired" in prompted[0]


def test_ensure_credentials_still_broken_after_login_raises(monkeypatch):
    monkeypatch.setattr(devbox.awsauth, "prompt_login", lambda *a, **kw: None)
    with pytest.raises(devbox.AwsAuthError, match="still unusable after logging in"):
        devbox.ensure_credentials(_expired_session, _aws("dev"))


# -- make_client (the provider entry point) --
def test_make_client_returns_a_client_for_good_credentials(monkeypatch):
    session = _ok_session()
    monkeypatch.setattr(devbox.awsauth, "ensure_credentials",
                        lambda new_session, aws: session)
    assert devbox.make_client(_aws("dev"), "ec2", "EC2 provider") == "ec2-client"


def test_make_client_unknown_profile_dies_cleanly(tmp_path, monkeypatch, capsys):
    # boto3's ProfileNotFound is a config mistake, not a login problem: point at
    # the config rather than prompting or dumping a traceback.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "empty-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "empty-creds"))
    with pytest.raises(SystemExit):
        devbox.make_client(_aws("no-such-profile"), "ec2", "EC2 provider")
    assert "is not configured" in capsys.readouterr().err


# -- the CLI boundary --
def _run_args(func):
    import argparse
    return argparse.Namespace(func=func)


def test_cli_run_turns_aws_auth_error_into_one_line(capsys):
    def boom(args, gc, workspace):
        raise devbox.AwsAuthError("AWS credentials are missing; run `aws sso login`")

    with pytest.raises(SystemExit):
        devbox.cli._run(_run_args(boom), _ec2_gc(), None)
    err = capsys.readouterr().err
    assert err.startswith("error: AWS credentials are missing")
    assert "Traceback" not in err


def test_cli_run_turns_mid_command_expiry_into_a_login_hint(aws_config_file, capsys):
    # An expired token the server rejects can't be caught up front; too late to
    # prompt and resume, so say what to run.
    def boom(args, gc, workspace):
        raise _FakeClientError("ExpiredToken")

    gc = _ec2_gc()
    gc.aws = _aws("dev")
    with pytest.raises(SystemExit):
        devbox.cli._run(_run_args(boom), gc, None)
    assert "aws sso login --profile dev" in capsys.readouterr().err


def test_cli_run_reraises_unrelated_errors():
    def boom(args, gc, workspace):
        raise RuntimeError("unrelated")

    with pytest.raises(RuntimeError):
        devbox.cli._run(_run_args(boom), _ec2_gc(), None)


def test_check_name_unique_degrades_auth_failure_to_a_warning(monkeypatch, capsys):
    # A stale AWS session must not block a libvirt create: the collision check
    # is best-effort, and AwsAuthError carries a message worth printing.
    class _StaleProvider:
        name = "ec2"

        def list(self):
            raise devbox.AwsAuthError("credentials expired; run `aws sso login`")

    monkeypatch.setattr(devbox.cli, "all_providers", lambda gc: [_StaleProvider()])
    devbox.cli._check_name_unique(_ec2_gc(), "foo")
    out = capsys.readouterr().out
    assert "could not check ec2 for name collisions" in out
    assert "aws sso login" in out
