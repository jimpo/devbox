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
    assert gc.github_token is None


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

[github]
token = "ghp_exampletoken"
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
    assert gc.github_token == "ghp_exampletoken"


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
    _write_marker(tmp_path, 'name = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.name == "myproj"
    assert project.cloud_config is None  # no .devbox/cloud-config.yaml written
    assert project.vcpus == global_config.vcpus
    assert project.memory_mib == global_config.memory_mib
    assert project.disk_gb == global_config.disk_gb
    assert project.repo.mode == "none"


def test_resolve_project_overrides_defaults(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'name = "myproj"\nvcpus = 2\nmemory_mib = 2048\ndisk_gb = 10\nuser = "alice"\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.vcpus == 2
    assert project.memory_mib == 2048
    assert project.disk_gb == 10
    assert project.user == "alice"


def test_resolve_project_missing_name_dies(tmp_path, global_config):
    _write_marker(tmp_path, "")
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_cloud_config_present(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n')
    cloud_config = tmp_path / devbox.PROJECT_MARKER_DIR / devbox.PROJECT_CLOUD_CONFIG_FILE
    cloud_config.write_text("packages: []\n")
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.cloud_config == cloud_config


# ---- [repo] validation -----------------------------------------------------
def test_resolve_project_repo_defaults_to_none(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="none", dir="", url=None)


def test_resolve_project_repo_clone_mode(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'name = "myproj"\n[repo]\nmode = "clone"\ndir = "myrepo"\nurl = "https://example.com/myrepo"\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="clone", dir="myrepo", url="https://example.com/myrepo")


def test_resolve_project_repo_push_mode(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n[repo]\nmode = "push"\ndir = "myrepo"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.repo == devbox.RepoConfig(mode="push", dir="myrepo", url=None)


def test_resolve_project_repo_invalid_mode_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n[repo]\nmode = "bogus"\ndir = "myrepo"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_clone_missing_url_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n[repo]\nmode = "clone"\ndir = "myrepo"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_missing_dir_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'name = "myproj"\n[repo]\nmode = "push"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_repo_push_with_url_dies(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'name = "myproj"\n[repo]\nmode = "push"\ndir = "myrepo"\nurl = "https://example.com/myrepo"\n',
    )
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


# ---- merge_cloud_config -----------------------------------------------------
def test_merge_cloud_config_concatenates_list_keys_common_first():
    common = {"packages": ["zsh", "vim"], "runcmd": ["common-step"]}
    project = {"packages": ["clang"], "runcmd": ["project-step"]}
    merged = devbox.merge_cloud_config(common, project)
    assert merged["packages"] == ["zsh", "vim", "clang"]
    assert merged["runcmd"] == ["common-step", "project-step"]


def test_merge_cloud_config_scalar_key_project_overrides():
    common = {"package_update": True, "hostname": "common-host"}
    project = {"hostname": "project-host"}
    merged = devbox.merge_cloud_config(common, project)
    assert merged["hostname"] == "project-host"
    assert merged["package_update"] is True


def test_merge_cloud_config_keeps_common_only_keys():
    common = {"packages": ["zsh"]}
    merged = devbox.merge_cloud_config(common, {})
    assert merged["packages"] == ["zsh"]


def test_merge_cloud_config_type_mismatch_dies():
    common = {"packages": ["zsh"]}
    project = {"packages": "vim"}  # should be a list, not a string
    with pytest.raises(SystemExit):
        devbox.merge_cloud_config(common, project)


# ---- render_cloud_config ----------------------------------------------------
@pytest.fixture
def common_template(tmp_path, monkeypatch):
    path = tmp_path / "common-cloud-config.yaml"
    path.write_text(
        "packages:\n  - zsh\n"
        "users:\n  - name: \"%DEVBOX_USER%\"\n    ssh_authorized_keys:\n      - \"%DEVBOX_SSH_PUBKEY%\"\n"
        "write_files:\n"
        "  - path: /tmp/jj-identity\n"
        "    content: |\n"
        "      %DEVBOX_JJ_NAME% <%DEVBOX_JJ_EMAIL%>\n"
        "      github-token: %DEVBOX_GITHUB_TOKEN%\n"
        "runcmd:\n"
        "  - [\"echo\", \"%DEVBOX_REPO_MODE%\", \"%DEVBOX_REPO_DIR%\", \"%DEVBOX_REPO_URL%\"]\n"
    )
    monkeypatch.setattr(devbox, "COMMON_CLOUD_CONFIG_PATH", path)
    # The standalone provisioning script devbox injects into write_files; keep
    # it tiny (the real one needs a booted VM) but token-bearing so we can check
    # substitution and injection.
    script = tmp_path / "provision-common.sh"
    script.write_text("#!/usr/bin/env bash\necho hello %DEVBOX_USER% %DEVBOX_REPO_MODE%\n")
    monkeypatch.setattr(devbox, "PROVISION_SCRIPT_PATH", script)
    return path


@pytest.fixture
def pubkey(tmp_path):
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest user@host\n")
    return path


def _project(cloud_config=None, mode="none", dir="", url=None):
    return devbox.ProjectConfig(
        name="test-project", cloud_config=cloud_config, user="alice",
        vcpus=6, memory_mib=1024, disk_gb=10,
        repo=devbox.RepoConfig(mode=mode, dir=dir, url=url),
    )


def _global_config(pubkey_path, jj_name="Alice Example", jj_email="alice@example.com",
                    github_token="ghp_testtoken"):
    return devbox.GlobalConfig(
        libvirt_uri="qemu:///system", network="default", cache_dir=devbox.Path("/tmp"),
        base_image_url="http://example/x.img", vcpus=6, memory_mib=1024, disk_gb=10,
        ssh_pubkey_path=pubkey_path, jj_name=jj_name, jj_email=jj_email, github_token=github_token,
    )


def test_render_cloud_config_substitutes_tokens(common_template, pubkey):
    gc = _global_config(pubkey)
    rendered = devbox.render_cloud_config(_project(mode="clone", dir="myrepo", url="https://example.com/x"), gc)
    assert "%DEVBOX_" not in rendered
    assert "name: alice" in rendered
    assert "ssh-ed25519 AAAAtest user@host" in rendered
    assert "Alice Example <alice@example.com>" in rendered
    assert "github-token: ghp_testtoken" in rendered
    import yaml
    assert yaml.safe_load(rendered)["runcmd"][0] == ["echo", "clone", "myrepo", "https://example.com/x"]


def test_render_cloud_config_injects_provision_script(common_template, pubkey):
    import yaml
    rendered = devbox.render_cloud_config(_project(mode="clone", dir="r", url="https://x/y"), _global_config(pubkey))
    parsed = yaml.safe_load(rendered)
    entries = [w for w in parsed["write_files"] if w["path"] == devbox.CLOUD_INIT_PROVISION_PATH]
    assert len(entries) == 1
    assert entries[0]["content"] == "#!/usr/bin/env bash\necho hello alice clone\n"
    # Injected after the template's own write_files, so /tmp/jj-identity stays first.
    assert parsed["write_files"][0]["path"] == "/tmp/jj-identity"


def test_render_provision_script_substitutes_tokens(common_template, pubkey):
    script = devbox.render_provision_script(_project(mode="push", dir="r"), _global_config(pubkey))
    assert script == "#!/usr/bin/env bash\necho hello alice push\n"
    assert "%DEVBOX_" not in script


def test_render_cloud_config_has_cloud_config_header(common_template, pubkey):
    rendered = devbox.render_cloud_config(_project(), _global_config(pubkey))
    assert rendered.startswith("#cloud-config\n")


def test_render_cloud_config_hostname_defaults_from_project_name(common_template, pubkey):
    rendered = devbox.render_cloud_config(_project(), _global_config(pubkey))
    import yaml
    assert yaml.safe_load(rendered)["hostname"] == "test-project"


def test_render_cloud_config_merges_project_fragment(common_template, pubkey, tmp_path):
    cloud_config = tmp_path / "cloud-config.yaml"
    cloud_config.write_text("packages:\n  - build-essential\nhostname: project-hostname\n")
    rendered = devbox.render_cloud_config(_project(cloud_config=cloud_config), _global_config(pubkey))
    import yaml
    parsed = yaml.safe_load(rendered)
    assert parsed["packages"] == ["zsh", "build-essential"]
    assert parsed["hostname"] == "project-hostname"  # project overrides the setdefault


def test_render_cloud_config_missing_jj_name_dies_when_no_host_fallback(common_template, pubkey, monkeypatch):
    # No [jj] name in global config, and no `jj` binary on the host to fall back to.
    monkeypatch.setattr(devbox.shutil, "which", lambda name: None)
    gc = _global_config(pubkey, jj_name=None)
    with pytest.raises(SystemExit):
        devbox.render_cloud_config(_project(), gc)


def test_render_cloud_config_falls_back_to_host_jj_config(common_template, pubkey, monkeypatch):
    # No [jj] name/email in global config - falls back to `jj config get`.
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/jj")
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(
            args, 0, {"user.name": "Host Jj Name", "user.email": "host@example.com"}[args[-1]] + "\n", "",
        ),
    )
    gc = _global_config(pubkey, jj_name=None, jj_email=None)
    rendered = devbox.render_cloud_config(_project(), gc)
    assert "Host Jj Name <host@example.com>" in rendered


def test_render_cloud_config_missing_pubkey_dies(common_template, tmp_path):
    gc = _global_config(tmp_path / "missing.pub")
    with pytest.raises(SystemExit):
        devbox.render_cloud_config(_project(), gc)


# ---- resolve_github_token ---------------------------------------------------
def test_resolve_github_token_from_config():
    gc = _global_config(devbox.Path("/nonexistent"), github_token="ghp_fromconfig")
    assert devbox.resolve_github_token(gc) == "ghp_fromconfig"


def test_resolve_github_token_none_when_unset_is_not_fatal():
    # An unresolved GitHub token must NOT raise - it's best-effort (public
    # repos / non-"clone" [repo] modes never need it).
    gc = _global_config(devbox.Path("/nonexistent"), github_token=None)
    assert devbox.resolve_github_token(gc) is None


def test_render_cloud_config_empty_token_when_unset(common_template, pubkey, capsys):
    gc = _global_config(pubkey, github_token=None)
    rendered = devbox.render_cloud_config(_project(), gc)
    import yaml
    content = yaml.safe_load(rendered)["write_files"][0]["content"]
    assert content.splitlines()[1] == "github-token: "
    assert "no GitHub token found" in capsys.readouterr().out


# ---- fetch_verified_checksum -----------------------------------------------
def test_fetch_verified_checksum_missing_gpgv(monkeypatch):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


def test_fetch_verified_checksum_missing_keyring(monkeypatch, tmp_path):
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox, "CLOUDIMAGE_KEYRING_PATH", tmp_path / "missing.gpg")
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")


def test_fetch_verified_checksum_success(monkeypatch, tmp_path):
    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"fake-keyring")
    monkeypatch.setattr(devbox.shutil, "which", lambda name: "/usr/bin/gpgv")
    monkeypatch.setattr(devbox, "CLOUDIMAGE_KEYRING_PATH", keyring)

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
    monkeypatch.setattr(devbox, "CLOUDIMAGE_KEYRING_PATH", keyring)
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
    monkeypatch.setattr(devbox, "CLOUDIMAGE_KEYRING_PATH", keyring)
    sums = b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef *other-file.img\n"
    monkeypatch.setattr(devbox.urllib.request, "urlopen", lambda url: _FakeResponse(sums))
    monkeypatch.setattr(
        devbox.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    with pytest.raises(SystemExit):
        devbox.fetch_verified_checksum("https://example.com/dir/image.img")
