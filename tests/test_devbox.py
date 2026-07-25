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
    assert gc.env == {"GITHUB_TOKEN": "github_pat_example", "SOME_API_KEY": "sk-123"}


def test_global_config_partial_override_keeps_other_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('network = "custom-net"\n')
    gc = devbox.GlobalConfig.load(path)
    assert gc.network == "custom-net"
    assert gc.libvirt_uri == devbox.DEFAULT_LIBVIRT_URI
    assert gc.vcpus == devbox.DEFAULT_VCPUS


# ---- project marker resolution -------------------------------------------
@pytest.fixture
def global_config():
    gc = devbox.GlobalConfig.load(devbox.Path("/nonexistent"))
    gc.jj_name = "Alice Example"
    gc.jj_email = "alice@example.com"
    return gc


def _write_marker(root, content, local=False):
    devbox_dir = root / devbox.PROJECT_MARKER_DIR
    devbox_dir.mkdir(parents=True, exist_ok=True)
    (devbox_dir / devbox.PROJECT_CONFIG_FILE).write_text(content)


def test_find_project_dir_at_start(tmp_path):
    _write_marker(tmp_path, 'name = "x"\n')
    assert devbox.find_project_dir(tmp_path) == tmp_path.resolve()


def test_find_project_dir_walks_up(tmp_path):
    _write_marker(tmp_path, 'name = "x"\n')
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert devbox.find_project_dir(deep) == tmp_path.resolve()


def test_find_project_dir_missing_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.find_project_dir(tmp_path)


def test_resolve_project_basic(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.project == "myproj"
    assert project.provider == "libvirt"
    assert project.vcpus == global_config.vcpus
    assert project.memory_mib == global_config.memory_mib
    assert project.disk_gb == global_config.disk_gb
    assert project.repo.mode == "none"


def test_resolve_project_defaults_project_to_dirname(tmp_path, global_config):
    # No `project` key -> defaults to the marker directory's basename.
    _write_marker(tmp_path, "")
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.project == tmp_path.resolve().name


def test_resolve_project_overrides_defaults(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'project = "myproj"\nvcpus = 2\nmemory_mib = 2048\ndisk_gb = 10\nuser = "alice"\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.vcpus == 2
    assert project.memory_mib == 2048
    assert project.disk_gb == 10
    assert project.user == "alice"


def test_resolve_project_env(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\nGITHUB_TOKEN = "github_pat_proj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.env == {"GITHUB_TOKEN": "github_pat_proj"}


def test_resolve_project_env_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    assert devbox.resolve_project(tmp_path, global_config).env == {}


def test_resolve_project_env_non_string_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[env]\nPORT = 8080\n')  # values must be strings
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_legacy_name_field_dies(tmp_path, global_config):
    # `name` used to be the VM; it's now a migration error, not silently ignored.
    _write_marker(tmp_path, 'name = "myproj"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


# ---- unknown-key warnings -----------------------------------------------
def test_resolve_project_unknown_top_level_key_warns(tmp_path, global_config, capsys):
    _write_marker(tmp_path, 'project = "myproj"\nvpcus = 4\n')  # typo: vpcus
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.project == "myproj"  # still loads
    err = capsys.readouterr().err
    assert "unknown key 'vpcus'" in err


def test_resolve_project_unknown_section_key_warns(tmp_path, global_config, capsys):
    _write_marker(tmp_path, '[ec2]\ninstance_type = "t3.micro"\nname = "x"\n')
    devbox.resolve_project(tmp_path, global_config)
    err = capsys.readouterr().err
    assert "unknown key 'ec2.name'" in err


def test_resolve_project_known_config_no_warning(tmp_path, global_config, capsys):
    _write_marker(tmp_path, 'project = "myproj"\nvcpus = 2\n[ec2]\ninstance_type = "t3.micro"\n')
    devbox.resolve_project(tmp_path, global_config)
    assert capsys.readouterr().err == ""


def test_resolve_project_unknown_env_key_does_not_warn(tmp_path, global_config, capsys):
    # [env] holds arbitrary user-defined keys; they must not be flagged.
    _write_marker(tmp_path, '[env]\nANYTHING_GOES = "value"\n')
    devbox.resolve_project(tmp_path, global_config)
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


def test_resolve_project_invalid_provider_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'provider = "gcp"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


# ---- [repo] validation -----------------------------------------------------
def test_resolve_project_repo_defaults_to_none(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="none", dir="", url=None)


def test_resolve_project_repo_clone_mode(tmp_path, global_config):
    _write_marker(
        tmp_path,
        '[repo]\nmode = "clone"\ndir = "myrepo"\nurl = "https://example.com/myrepo"\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="clone", dir="myrepo", url="https://example.com/myrepo")


def test_resolve_project_repo_push_mode(tmp_path, global_config):
    _write_marker(tmp_path, '[repo]\nmode = "push"\ndir = "myrepo"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="push", dir="myrepo", url=None)


def test_resolve_project_repo_invalid_mode_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[repo]\nmode = "bogus"\ndir = "myrepo"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_clone_missing_url_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[repo]\nmode = "clone"\ndir = "myrepo"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_missing_dir_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[repo]\nmode = "push"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_push_with_url_dies(tmp_path, global_config):
    _write_marker(
        tmp_path,
        '[repo]\nmode = "push"\ndir = "myrepo"\nurl = "https://example.com/myrepo"\n',
    )
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


# ---- render_init_script -----------------------------------------------------
@pytest.fixture
def pubkey(tmp_path):
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest user@host\n")
    return path


def _project(mode="none", dir="", url=None, env=None,
             marker_dir=None, mods=None, sync=None):
    return devbox.ProjectConfig(
        project="test-project", provider="libvirt", user="alice",
        vcpus=6, memory_mib=1024, disk_gb=10,
        repo=devbox.RepoConfig(mode=mode, dir=dir, url=url),
        marker_dir=marker_dir or devbox.Path("/proj"),
        mods=mods if mods is not None else [],
        sync=sync if sync is not None else [],
        env=env if env is not None else {},
    )


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
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert rendered.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in rendered
    assert not rendered.startswith("#cloud-config")


def test_render_init_script_substitutes_user_and_pubkey(pubkey):
    gc = _global_config(pubkey)
    rendered = devbox.render_init_script(
        _project(mode="clone", dir="myrepo", url="https://example.com/x"), gc)
    assert "%DEVBOX_" not in rendered  # every token filled
    assert "DEVBOX_USER='alice'" in rendered
    assert "DEVBOX_HOME='/home/alice'" in rendered
    assert "DEVBOX_SSH_PUBKEY='ssh-ed25519 AAAAtest user@host'" in rendered


def test_render_init_script_creates_the_user_with_key_and_sudo(pubkey):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert "useradd" in rendered
    assert '"$DEVBOX_HOME/.ssh/authorized_keys"' in rendered
    assert "NOPASSWD:ALL" in rendered
    assert "passwd --lock" in rendered
    assert "usermod --shell /usr/bin/zsh" in rendered


def test_render_init_script_creates_the_user_before_installing_packages(pubkey):
    # SSH access must not be gated on the (slow, network-dependent) package
    # step: a failure there still leaves a reachable, debuggable box.
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert rendered.index("useradd") < rendered.index("apt-get")
    assert rendered.index("authorized_keys") < rendered.index("apt-get")


def test_render_init_script_installs_the_base_packages(pubkey):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    for pkg in ("zsh", "vim", "python3", "curl", "git", "git-lfs",
                "ca-certificates", "gh", "rsync"):
        assert f"\n  {pkg} " in rendered or f"\n  {pkg}\n" in rendered
    assert 'apt-get -o DPkg::Lock::Timeout=600 install -y "${DEVBOX_PACKAGES[@]}"' in rendered
    assert "apt-get upgrade" not in rendered  # matched the old package_upgrade: false


def test_render_init_script_adds_the_github_cli_repo_before_installing(pubkey):
    # `gh` is not in the 24.04 archive (the Lightsail blueprint) and a missing
    # package would abort first boot under `set -e`.
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert "https://cli.github.com/packages" in rendered
    assert rendered.index("cli.github.com") < rendered.index("install -y")


def test_render_init_script_wires_bash_env(pubkey):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert "BASH_ENV_LINE='BASH_ENV=/home/alice/.bash_env'" in rendered
    assert "/etc/environment" in rendered


def test_render_init_script_hostname_defaults_from_project_name(pubkey):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert "DEVBOX_HOSTNAME='test-project'" in rendered


def test_render_init_script_hostname_uses_the_instance_name(pubkey):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey),
                                         instance_name="aws-mybox")
    assert "DEVBOX_HOSTNAME='aws-mybox'" in rendered


def test_render_init_script_carries_no_provisioning_payload(pubkey):
    # Provisioning is over SSH: the init script only does base setup - no
    # driver, no mods, no [env] creds.
    gc = _global_config(pubkey, env={"GITHUB_TOKEN": "github_pat_x"})
    rendered = devbox.render_init_script(_project(mods=["wasm-tools"]), gc)
    assert "provision-driver.sh" not in rendered
    assert "github_pat_x" not in rendered
    assert "wasm-tools" not in rendered


def test_render_init_script_missing_pubkey_dies(tmp_path):
    gc = _global_config(tmp_path / "missing.pub")
    with pytest.raises(SystemExit):
        devbox.render_init_script(_project(), gc)


def test_render_init_script_quote_in_a_token_dies(tmp_path):
    # Tokens land inside single-quoted shell assignments; a value with a quote
    # would break out of the assignment and mangle first boot.
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest o'brien@host\n")
    with pytest.raises(SystemExit):
        devbox.render_init_script(_project(), _global_config(path))


def test_render_init_script_parses_as_bash(pubkey, tmp_path):
    rendered = devbox.render_init_script(_project(), _global_config(pubkey),
                                         instance_name="aws-mybox")
    script = tmp_path / "init.sh"
    script.write_text(rendered)
    assert devbox.subprocess.run(["bash", "-n", str(script)]).returncode == 0


GUARD = 'if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi'


def test_render_init_script_re_execs_under_bash_before_any_bashism(pubkey):
    # Lightsail runs user-data under /bin/sh (dash) and ignores the shebang, so
    # the script re-execs itself under bash. The guard is only load-bearing if
    # it precedes EVERY bashism - `set -o pipefail` and the packages array both
    # abort dash on the spot ("set: Illegal option -o pipefail").
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
    assert GUARD in rendered
    assert 0 < rendered.index(GUARD) < rendered.index("set -euo pipefail")
    assert rendered.index(GUARD) < rendered.index("DEVBOX_PACKAGES=(")


def test_render_init_script_prefix_up_to_the_guard_is_posix_sh(pubkey, tmp_path):
    # Everything dash executes before the re-exec must be POSIX sh: slice the
    # script from the top through the guard line and hand it to `sh -n`.
    rendered = devbox.render_init_script(_project(), _global_config(pubkey))
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


def _payload_map(project, gc):
    return {pf.path: pf for pf in devbox.build_payload(project, gc)}


def test_build_payload_has_driver_manifest_context_and_mods(isolated_mods, pubkey):
    payload = _payload_map(_project(mode="clone", dir="r", url="https://x/y"), _global_config(pubkey))
    assert payload["provision-driver.sh"].content == devbox.DRIVER_PATH.read_text()
    assert payload["manifest"].content == "".join(f"{m}\n" for m in devbox.DEFAULT_BASE_MODS)
    ctx = payload["context.env"].content
    assert "export DEVBOX_USER=alice" in ctx
    assert "export DEVBOX_JJ_NAME='Alice Example'" in ctx   # shell-quoted (space)
    assert "export DEVBOX_REPO_MODE=clone" in ctx
    # Creds ([env]) are NOT in the payload - they're delivered separately as the
    # box's 0600 ~/.config/devbox/env (see run_envvars).
    assert "secret.env" not in payload
    assert "GITHUB_TOKEN" not in ctx
    for name in devbox.DEFAULT_BASE_MODS:
        assert f"mods/{name}.sh" in payload


def test_build_payload_carries_no_creds(isolated_mods, pubkey):
    # Even with a global [env] token set, build_payload never embeds it.
    gc = _global_config(pubkey, env={"GITHUB_TOKEN": "github_pat_x"})
    payload = _payload_map(_project(env={"SOME_KEY": "v"}), gc)
    assert "secret.env" not in payload
    assert not any("github_pat_x" in pf.content for pf in payload.values())


def test_build_payload_appends_project_mods(isolated_mods, pubkey, tmp_path):
    (tmp_path / ".devbox" / "mods").mkdir(parents=True)
    (tmp_path / ".devbox" / "mods" / "rust.sh").write_text("#!/usr/bin/env bash\necho rust\n")
    payload = _payload_map(_project(marker_dir=tmp_path, mods=["rust"]), _global_config(pubkey))
    assert payload["manifest"].content == "".join(f"{m}\n" for m in [*devbox.DEFAULT_BASE_MODS, "rust"])
    assert payload["mods/rust.sh"].content == "#!/usr/bin/env bash\necho rust\n"


def test_build_payload_missing_jj_identity_dies(isolated_mods, pubkey, monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: None)  # no host jj fallback
    gc = _global_config(pubkey, jj_name=None)
    with pytest.raises(SystemExit):
        devbox.build_payload(_project(), gc)


def test_build_payload_jj_identity_host_fallback(isolated_mods, pubkey, monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/jj")
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(
            args, 0, {"user.name": "Host Jj Name", "user.email": "host@example.com"}[args[-1]] + "\n", ""),
    )
    gc = _global_config(pubkey, jj_name=None, jj_email=None)
    ctx = _payload_map(_project(), gc)["context.env"].content
    assert "export DEVBOX_JJ_NAME='Host Jj Name'" in ctx
    assert "export DEVBOX_JJ_EMAIL=host@example.com" in ctx


# ---- resolve_env (global + project [env], project overrides per-key) --------
def test_resolve_env_merges_global_and_project():
    gc = _global_config(devbox.Path("/nonexistent"),
                        env={"GITHUB_TOKEN": "g_tok", "SHARED": "global"})
    project = _project(env={"SHARED": "project", "PROJECT_ONLY": "p"})
    assert devbox.resolve_env(project, gc) == {
        "GITHUB_TOKEN": "g_tok",     # global-only key kept
        "SHARED": "project",         # project overrides global per-key
        "PROJECT_ONLY": "p",         # project-only key added
    }


def test_resolve_env_empty_when_neither_set():
    gc = _global_config(devbox.Path("/nonexistent"))
    assert devbox.resolve_env(_project(), gc) == {}


# ---- provisioning: mods, env, payload ---------------------------------------
def test_effective_mods_appends_project_after_base():
    assert devbox.effective_mods(_project(mods=["rust", "wasm"])) == \
        [*devbox.DEFAULT_BASE_MODS, "rust", "wasm"]


def test_effective_mods_base_only_when_no_project_mods():
    assert devbox.effective_mods(_project()) == list(devbox.DEFAULT_BASE_MODS)


def test_resolve_mod_precedence_project_over_global_over_builtin(tmp_path):
    marker = tmp_path / "proj"
    proj = marker / ".devbox" / "mods"
    glob = tmp_path / "global"
    built = tmp_path / "builtin"
    for d in (proj, glob, built):
        d.mkdir(parents=True)
    # `dotfiles` in all three -> project wins; `shared` in global+builtin ->
    # global wins; `basic` only built-in -> built-in.
    (proj / "dotfiles.sh").write_text("proj")
    (glob / "dotfiles.sh").write_text("glob")
    (built / "dotfiles.sh").write_text("built")
    (glob / "shared.sh").write_text("glob")
    (built / "shared.sh").write_text("built")
    (built / "basic.sh").write_text("built")

    assert devbox.resolve_mod("dotfiles", marker, glob, built).read_text() == "proj"
    assert devbox.resolve_mod("shared", marker, glob, built).read_text() == "glob"
    assert devbox.resolve_mod("basic", marker, glob, built).read_text() == "built"


def test_resolve_mod_missing_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.resolve_mod("nope", tmp_path / "proj", tmp_path / "g", tmp_path / "b")


# ---- parse_requires (`# requires:` header, static parse) --------------------
def test_parse_requires_single_line_single_dep():
    assert devbox.parse_requires("#!/bin/bash\n# requires: jj\nset -e\n") == ["jj"]


def test_parse_requires_space_separated():
    assert devbox.parse_requires("# requires: jj github-auth\n") == ["jj", "github-auth"]


def test_parse_requires_comma_separated():
    assert devbox.parse_requires("# requires: jj, github-auth\n") == ["jj", "github-auth"]


def test_parse_requires_multiple_lines_unioned():
    text = "# requires: jj\n# mod: x\n# requires: github-auth, dotfiles\n"
    assert devbox.parse_requires(text) == ["jj", "github-auth", "dotfiles"]


def test_parse_requires_dedupes_preserving_order():
    assert devbox.parse_requires("# requires: jj github-auth\n# requires: jj\n") == \
        ["jj", "github-auth"]


def test_parse_requires_none_present_is_empty():
    assert devbox.parse_requires("#!/bin/bash\n# mod: x\nset -e\n") == []


def test_parse_requires_trailing_comment_caveat():
    # Documents the names-only rule: the capture runs to end of line, so a trailing
    # inline comment is (wrongly) parsed as a dep name. Authors must not add one.
    assert devbox.parse_requires("# requires: jj  # needs jj first\n") == \
        ["jj", "#", "needs", "first"]


# ---- validate_mods (`# requires:` ordering over the effective playbook) ------
def _write_project_mod(marker_dir, name, text):
    d = marker_dir / ".devbox" / "mods"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.sh").write_text(text)


def test_validate_mods_base_playbook_passes(isolated_mods):
    # The shipped base playbook (with its backfilled `# requires:` headers) is
    # correctly ordered - no dep is missing or listed after its dependent.
    devbox.validate_mods(_project())


def test_validate_mods_project_mod_requiring_base_mod_passes(isolated_mods, tmp_path):
    _write_project_mod(tmp_path, "rust", "#!/bin/bash\n# requires: jj\nset -e\n")
    devbox.validate_mods(_project(marker_dir=tmp_path, mods=["rust"]))


def test_validate_mods_missing_dep_dies(isolated_mods, tmp_path):
    _write_project_mod(tmp_path, "rust", "#!/bin/bash\n# requires: nonexistent\nset -e\n")
    with pytest.raises(SystemExit):
        devbox.validate_mods(_project(marker_dir=tmp_path, mods=["rust"]))


def test_validate_mods_dep_listed_after_dies(isolated_mods, tmp_path):
    # `a` requires `b`, but `b` is appended after `a` -> misordered -> die.
    _write_project_mod(tmp_path, "a", "#!/bin/bash\n# requires: b\nset -e\n")
    _write_project_mod(tmp_path, "b", "#!/bin/bash\nset -e\n")
    with pytest.raises(SystemExit):
        devbox.validate_mods(_project(marker_dir=tmp_path, mods=["a", "b"]))


def test_validate_mods_dep_listed_before_passes(isolated_mods, tmp_path):
    # Same two mods, `b` ahead of `a` -> ordering satisfied.
    _write_project_mod(tmp_path, "a", "#!/bin/bash\n# requires: b\nset -e\n")
    _write_project_mod(tmp_path, "b", "#!/bin/bash\nset -e\n")
    devbox.validate_mods(_project(marker_dir=tmp_path, mods=["b", "a"]))


def test_build_payload_validates_mods(isolated_mods, pubkey, tmp_path):
    # build_payload is the choke point: a bad `# requires:` fails there (so a
    # selective `devbox sync <subset>` still validates the full playbook).
    _write_project_mod(tmp_path, "rust", "#!/bin/bash\n# requires: nonexistent\nset -e\n")
    with pytest.raises(SystemExit):
        devbox.build_payload(_project(marker_dir=tmp_path, mods=["rust"]), _global_config(pubkey))


def test_render_env_file_shell_quotes_values():
    out = devbox.render_env_file({"A": "plain", "B": "has space", "C": "a'b"})
    assert out == "export A=plain\nexport B='has space'\nexport C='a'\"'\"'b'\n"


def test_resolve_project_parses_mods(tmp_path, global_config):
    _write_marker(tmp_path, 'mods = ["rust", "wasm-tools"]\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.mods == ["rust", "wasm-tools"]
    assert project.marker_dir == tmp_path.resolve()


def test_resolve_project_mods_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "p"\n')
    assert devbox.resolve_project(tmp_path, global_config).mods == []


def test_resolve_project_mods_must_be_string_list(tmp_path, global_config):
    _write_marker(tmp_path, 'mods = [1, 2]\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


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
def test_resolve_instance_defaults_to_project():
    import argparse
    args = argparse.Namespace(name=None)
    assert devbox.resolve_instance(args, _project()) == "test-project"


def test_resolve_instance_name_overrides():
    import argparse
    args = argparse.Namespace(name="ec2-foo-gpu")
    assert devbox.resolve_instance(args, _project()) == "ec2-foo-gpu"


def test_parse_instance_name_bare_is_libvirt():
    # No known-provider prefix -> libvirt (the project's default), name unchanged.
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


def test_resolve_target_routes_by_instance_name(monkeypatch):
    # The heart of JIM-71: a canonical ec2-* name routes to the EC2 provider even
    # when the project's default provider is libvirt, from the name alone.
    import argparse
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: f"prov:{name}")
    gc = _global_config(devbox.Path("/nonexistent"))
    args = argparse.Namespace(name="ec2-binius-arm")
    assert devbox.cli._resolve_target(args, _project(), gc) == (
        "ec2-binius-arm", "ec2", "prov:ec2")
    args = argparse.Namespace(name=None)  # bare default -> libvirt
    assert devbox.cli._resolve_target(args, _project(), gc) == (
        "test-project", "libvirt", "prov:libvirt")


def _create_args(provider=None, name=None):
    import argparse
    return argparse.Namespace(provider=provider, name=name, spot=False,
                              on_demand=False, project_dir=None)


def test_cmd_create_rejects_provider_prefixed_project_name():
    # A project literally named 'ec2-foo' would make its bare libvirt name
    # misparse as the ec2 provider -> rejected at create.
    import dataclasses
    gc = _global_config(devbox.Path("/nonexistent"))
    project = dataclasses.replace(_project(), project="ec2-foo")
    with pytest.raises(SystemExit):
        devbox.cli.cmd_create(_create_args(), gc, project)


def test_cmd_create_rejects_name_with_provider_prefix():
    # --name for a cloud create is the bare segment; the prefix is composed in.
    # Passing an already-prefixed name would double it -> rejected.
    import dataclasses
    gc = dataclasses.replace(_global_config(devbox.Path("/nonexistent")),
                             aws=devbox.AwsConfig(region="us-east-1"))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_create(_create_args(provider="ec2", name="ec2-foo"), gc, _project())


def test_cmd_create_rejects_aws_name_with_provider_prefix():
    # Same guard for the aws provider: --name aws-foo would compose aws-aws-foo.
    import dataclasses
    gc = dataclasses.replace(_global_config(devbox.Path("/nonexistent")),
                             aws=devbox.AwsConfig(region="us-east-1"))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_create(_create_args(provider="aws", name="aws-foo"), gc, _project())


def test_cmd_create_passes_lightsail_config_into_spec(monkeypatch, tmp_path, pubkey):
    # The [lightsail] section has to reach the provider: without this, dropping
    # `lightsail=project.lightsail` from the InstanceSpec would stay green,
    # because every Lightsail field has a default.
    import dataclasses

    class _Stop(Exception):
        """Ends cmd_create at create(); everything after it is SSH/provisioning."""

    seen = {}

    class _FakeProvider:
        name = "aws"

        def create(self, spec):
            seen["spec"] = spec
            raise _Stop

    gc = dataclasses.replace(_global_config(pubkey),
                             aws=devbox.AwsConfig(region="us-east-1"))
    project = dataclasses.replace(
        _project(marker_dir=tmp_path),
        provider="aws",
        lightsail=devbox.LightsailProjectConfig(bundle="medium_3_0"),
    )
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: _FakeProvider())
    monkeypatch.setattr(devbox.cli, "_check_name_unique", lambda gc, name: None)
    monkeypatch.setattr(devbox.cli, "build_payload", lambda project, gc: [])

    with pytest.raises(_Stop):
        devbox.cli.cmd_create(_create_args(provider="aws", name="foo-gpu"), gc, project)
    spec = seen["spec"]
    assert spec.name == "aws-foo-gpu" and spec.provider == "aws"
    assert spec.lightsail is project.lightsail
    # Lightsail runs user-data as a shell script, so create must hand it one
    # (hostname = the instance's canonical name).
    assert spec.user_data.startswith("#!/bin/bash\n")
    assert "DEVBOX_HOSTNAME='aws-foo-gpu'" in spec.user_data


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
    stamp = devbox.build_metadata_xml("myproj", "libvirt", "2026-07-21T10:00:00+00:00")
    dumpxml = _domain_xml(metadata_inner=stamp, vcpu=4, memory_kib=8388608)
    monkeypatch.setattr(
        devbox.providers.libvirt, "virsh",
        lambda uri, *a, **k: subprocess.CompletedProcess(a, 0, dumpxml, ""),
    )
    info = devbox.read_domain_instance("qemu:///system", "demo")
    assert info == {
        "name": "demo", "project": "myproj", "provider": "libvirt",
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
    stamp = devbox.build_metadata_xml("proj", "libvirt", "2026-07-21T10:00:00+00:00")
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
    assert (i.name, i.provider, i.project, i.state) == ("demo", "libvirt", "proj", "running")
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


def test_cmd_jj_fetch_without_repo_dies():
    import argparse
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_jj_fetch(argparse.Namespace(name=None, project_dir=None), gc, _project(mode="none"))


# ---- host->box sync ---------------------------------------------------------
def test_resolve_project_parses_sync_entries(tmp_path, global_config):
    _write_marker(tmp_path, (
        '[[sync]]\n'
        'src = "$HOME/.claude/skills"\n'
        'include = ["jujutsu"]\n\n'
        '[repo]\n'
        'mode = "clone"\n'
        'dir = "binius64"\n'
        'url = "https://github.com/x/binius64"\n\n'
        '[[repo.sync]]\n'
        'src = ".claude/skills"\n'
    ))
    entries = devbox.resolve_project(tmp_path, global_config).sync
    assert entries == [
        devbox.SyncEntry(src="$HOME/.claude/skills", dst="$HOME/.claude/skills",
                         include=["jujutsu"], repo_relative=False),
        devbox.SyncEntry(src=".claude/skills", dst=".claude/skills",
                         include=[], repo_relative=True),
    ]


def test_resolve_project_sync_defaults_empty(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "p"\n')
    assert devbox.resolve_project(tmp_path, global_config).sync == []


def test_resolve_project_repo_sync_without_repo_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[[repo.sync]]\nsrc = ".claude/skills"\n')  # no [repo]
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_sync_relative_path_dies(tmp_path, global_config):
    # [[sync]] paths must be $HOME/~/absolute-anchored, not bare-relative.
    _write_marker(tmp_path, '[[sync]]\nsrc = "relative/skills"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_sync_anchored_path_dies(tmp_path, global_config):
    _write_marker(tmp_path, (
        '[repo]\nmode = "push"\ndir = "x"\n\n'
        '[[repo.sync]]\nsrc = "$HOME/skills"\n'  # repo.sync must be repo-relative
    ))
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def _sync(src, dst=None, include=None, repo_relative=False):
    return devbox.SyncEntry(src=src, dst=dst if dst is not None else src,
                            include=include or [], repo_relative=repo_relative)


def test_plan_sync_home_expansion_per_side(tmp_path):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    proj = _project(sync=[_sync("$HOME/.claude/skills")])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert len(jobs) == 1
    assert jobs[0].host_path == f"{tmp_path}/.claude/skills"     # host home
    assert jobs[0].guest_path == "/home/alice/.claude/skills"    # guest home
    assert jobs[0].mirror is True


def test_plan_sync_include_expands_to_per_child_units(tmp_path):
    skills = tmp_path / "skills"
    (skills / "jujutsu").mkdir(parents=True)
    proj = _project(sync=[_sync("$HOME/skills", include=["jujutsu"])])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert len(jobs) == 1
    assert jobs[0].host_path == f"{skills}/jujutsu"
    assert jobs[0].guest_path == "/home/alice/skills/jujutsu"


def test_plan_sync_repo_relative_anchoring(tmp_path):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    proj = _project(mode="clone", dir="binius64", url="https://x/y", marker_dir=tmp_path,
                    sync=[_sync(".claude/skills", repo_relative=True)])
    jobs = devbox.plan_sync(proj, host_home="/host", guest_home="/home/alice")
    assert jobs[0].host_path == f"{tmp_path}/.claude/skills"                 # marker dir
    assert jobs[0].guest_path == "/home/alice/binius64/.claude/skills"       # ~/<repo.dir>


def test_plan_sync_file_src_is_not_mirrored(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    proj = _project(sync=[_sync("$HOME/f.txt")])
    jobs = devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")
    assert jobs[0].mirror is False


def test_plan_sync_missing_src_dies(tmp_path):
    proj = _project(sync=[_sync("$HOME/nope")])
    with pytest.raises(SystemExit):
        devbox.plan_sync(proj, host_home=str(tmp_path), guest_home="/home/alice")


def test_run_sync_rsync_argv_mirror_dir(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    proj = _project(sync=[_sync("$HOME/skills")])
    calls = []
    monkeypatch.setattr(devbox.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or _cp(argv, 0))
    devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")
    # dir -> --delete + trailing slashes (mirror the contents).
    assert calls == [["rsync", "-az", "--mkpath", "--delete",
                      f"{tmp_path}/skills/", "devbox-foo:/home/alice/skills/"]]


def test_run_sync_rsync_argv_file(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("x")
    proj = _project(sync=[_sync("$HOME/f.txt", dst="$HOME/g.txt")])
    calls = []
    monkeypatch.setattr(devbox.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or _cp(argv, 0))
    devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")
    # file -> no --delete, no trailing slash.
    assert calls == [["rsync", "-az", "--mkpath",
                      f"{tmp_path}/f.txt", "devbox-foo:/home/alice/g.txt"]]


def test_run_sync_dies_on_rsync_failure(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    proj = _project(sync=[_sync("$HOME/skills")])
    monkeypatch.setattr(devbox.subprocess, "run", lambda argv, **k: _cp(argv, 23))
    with pytest.raises(SystemExit):
        devbox.run_sync(proj, "devbox-foo", host_home=str(tmp_path), guest_home="/home/alice")


def test_run_envvars_writes_managed_file_and_wires_zshenv(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input")
        return _cp(argv, 0)

    monkeypatch.setattr(devbox.subprocess, "run", fake_run)
    devbox.run_envvars("devbox-foo", {"GITHUB_TOKEN": "github_pat_x", "K": "v w"})
    assert captured["argv"][:4] == ["ssh", "devbox-foo", "bash", "-c"]
    remote = captured["argv"][4]
    # writes the managed 0600 file and ensures ~/.zshenv sources it
    assert "$HOME/.config/devbox/env" in remote
    assert "umask 077" in remote
    assert "$HOME/.zshenv" in remote
    # the creds are piped over stdin (never in the command / a log), shell-quoted
    assert captured["input"] == b"export GITHUB_TOKEN=github_pat_x\nexport K='v w'\n"


def test_run_envvars_dies_on_failure(monkeypatch):
    monkeypatch.setattr(devbox.subprocess, "run", lambda argv, **k: _cp(argv, 1))
    with pytest.raises(SystemExit):
        devbox.run_envvars("devbox-foo", {"K": "v"})


def _sync_args(envvars=False, files=False, mods=None):
    import argparse
    return argparse.Namespace(name=None, project_dir=None, envvars=envvars, files=files, mods=mods)


def test_split_mods_parsing():
    assert devbox.cli._split_mods("") == []               # --mods with no value -> all
    assert devbox.cli._split_mods("rust") == ["rust"]
    assert devbox.cli._split_mods("a, b ,c") == ["a", "b", "c"]


def test_cmd_sync_requires_a_flag():
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):  # none of --envvars/--mods/--files
        devbox.cli.cmd_sync(_sync_args(), gc, _project())


def test_cmd_sync_files_without_entries_dies():
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):  # --files but the project has no [[sync]]
        devbox.cli.cmd_sync(_sync_args(files=True), gc, _project())


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


def test_resolve_project_ec2_section(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'provider = "ec2"\n[ec2]\ninstance_type = "g5.xlarge"\narchitecture = "x86_64"\nspot = true\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.provider == "ec2"
    assert project.ec2.instance_type == "g5.xlarge"
    assert project.ec2.architecture == "x86_64"
    assert project.ec2.spot is True


def test_resolve_project_no_ec2_section_is_none(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    assert devbox.resolve_project(tmp_path, global_config).ec2 is None


def test_resolve_project_ec2_requires_instance_type(tmp_path, global_config):
    _write_marker(tmp_path, '[ec2]\narchitecture = "arm64"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_ec2_invalid_architecture_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[ec2]\ninstance_type = "c7i.large"\narchitecture = "ppc64"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


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


def _ec2_instance(name="foo", state="running", itype="c7i.large", project="proj", public_ip="1.2.3.4"):
    tags = ([{"Key": "devbox:name", "Value": name}] if name is not None else []) + [
        {"Key": "devbox:managed", "Value": "true"},
        {"Key": "devbox:project", "Value": project},
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
        name=name, project="proj", provider="ec2", disk_gb=disk_gb,
        user_data="#cloud-config\n", created_at="2026-07-21T12:00:00+00:00",
        ec2=devbox.Ec2ProjectConfig(instance_type=instance_type, architecture=architecture,
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
    assert (i.name, i.provider, i.project, i.state, i.resources) == \
        ("foo", "ec2", "proj", "running", "c7i.large")
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
    assert tags["devbox:project"] == "proj"
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
    provider.create(_ec2_spec(spot=True))  # project overrides global default
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
                 bundle="xlarge_3_0", project="proj", public_ip="1.2.3.4",
                 managed="true"):
    """A get_instances entry. Lightsail tags are lowercase key/value (unlike
    EC2's Key/Value), and the Lightsail-side `name` is the bare name while the
    canonical devbox name lives in the devbox:name tag."""
    tags = ([{"key": "devbox:name", "value": canonical}] if canonical is not None else []) + [
        {"key": "devbox:project", "value": project},
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
    LightsailProjectConfig(); pass None for the no-[lightsail]-section case."""
    cfg = devbox.LightsailProjectConfig() if lightsail is _LS_DEFAULT_CFG else lightsail
    return devbox.InstanceSpec(
        name=name, project="proj", provider="aws", disk_gb=64,
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
    assert tags["devbox:project"] == "proj"
    assert tags["devbox:created-at"] == "2026-07-21T12:00:00+00:00"


def test_lightsail_create_explicit_overrides_win():
    client = _FakeLightsail()
    cfg = devbox.LightsailProjectConfig(bundle="medium_3_0", blueprint="debian_12",
                                        availability_zone="eu-central-1c")
    devbox.LightsailProvider(_aws_gc(), client=client).create(_ls_spec(lightsail=cfg))
    params = _create_params(client)
    # An explicit bundle wins over DEFAULT_LIGHTSAIL_BUNDLE.
    assert params["bundleId"] == "medium_3_0"
    assert params["blueprintId"] == "debian_12"
    assert params["availabilityZone"] == "eu-central-1c"


def test_lightsail_create_without_lightsail_section_uses_defaults():
    # [lightsail] is entirely optional: `provider = "aws"` alone is a complete
    # project (unlike [ec2], which requires an instance_type).
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
    assert (i.name, i.provider, i.project, i.state, i.resources) == \
        ("aws-foo", "aws", "proj", "running", "xlarge_3_0")
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


# ---- [lightsail] project config ---------------------------------------------
def test_resolve_project_lightsail_section(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'provider = "aws"\n[lightsail]\nbundle = "medium_3_0"\n'
        'blueprint = "debian_12"\navailability_zone = "us-east-1b"\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.provider == "aws"
    assert project.lightsail.bundle == "medium_3_0"
    assert project.lightsail.blueprint == "debian_12"
    assert project.lightsail.availability_zone == "us-east-1b"


def test_resolve_project_lightsail_defaults(tmp_path, global_config):
    # An empty [lightsail] section => default bundle / Ubuntu / region AZ.
    _write_marker(tmp_path, 'provider = "aws"\n[lightsail]\n')
    ls = devbox.resolve_project(tmp_path, global_config).lightsail
    assert ls.bundle is None  # DEFAULT_LIGHTSAIL_BUNDLE is applied at create
    assert ls.blueprint == devbox.DEFAULT_LIGHTSAIL_BLUEPRINT == "ubuntu_24_04"
    assert ls.availability_zone is None  # defaulted to <region>a at create


def test_resolve_project_no_lightsail_section_is_none(tmp_path, global_config):
    # provider = "aws" with no section at all is valid; the provider defaults it.
    _write_marker(tmp_path, 'provider = "aws"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.provider == "aws" and project.lightsail is None


def test_resolve_project_lightsail_known_keys_no_warning(tmp_path, global_config, capsys):
    _write_marker(
        tmp_path,
        '[lightsail]\nbundle = "medium_3_0"\n'
        'blueprint = "ubuntu_24_04"\navailability_zone = "us-west-2b"\n',
    )
    devbox.resolve_project(tmp_path, global_config)
    assert "unknown key" not in capsys.readouterr().err


def test_resolve_project_lightsail_architecture_key_warns(tmp_path, global_config, capsys):
    # Lightsail's Linux bundles are x86-only, so there is no architecture knob
    # (unlike [ec2]); the key is unknown rather than silently accepted.
    _write_marker(tmp_path, '[lightsail]\narchitecture = "arm64"\n')
    devbox.resolve_project(tmp_path, global_config)
    assert "unknown key 'lightsail.architecture'" in capsys.readouterr().err


def test_resolve_project_unknown_lightsail_key_warns(tmp_path, global_config, capsys):
    _write_marker(tmp_path, '[lightsail]\ninstance_type = "t3.micro"\n')  # an EC2 knob
    devbox.resolve_project(tmp_path, global_config)
    assert "unknown key 'lightsail.instance_type'" in capsys.readouterr().err


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
    def __init__(self):
        self.destroyed = []

    def destroy(self, name):
        self.destroyed.append(name)


def _destroy_args(name=None, yes=False):
    import argparse
    return argparse.Namespace(name=name, yes=yes)


def test_cmd_destroy_yes_flag_destroys(monkeypatch, capsys):
    provider = _RecordingProvider()
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: provider)
    gc = _global_config(devbox.Path("/nonexistent"))
    devbox.cli.cmd_destroy(_destroy_args(yes=True), gc, _project())
    assert provider.destroyed == ["test-project"]
    assert "Destroyed 'test-project'" in capsys.readouterr().out


def test_cmd_destroy_declined_leaves_untouched(monkeypatch, capsys):
    # A TTY that answers 'no' aborts: provider.destroy is never called.
    provider = _RecordingProvider()
    monkeypatch.setattr(devbox.cli, "get_provider", lambda name, gc: provider)
    monkeypatch.setattr(devbox.cli.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    gc = _global_config(devbox.Path("/nonexistent"))
    devbox.cli.cmd_destroy(_destroy_args(yes=False), gc, _project())
    assert provider.destroyed == []
    assert "Aborted" in capsys.readouterr().out
