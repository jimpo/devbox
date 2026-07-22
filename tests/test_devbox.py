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
    _write_marker(tmp_path, 'project = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.project == "myproj"
    assert project.provider == "libvirt"
    assert project.cloud_config is None  # no .devbox/cloud-config.yaml written
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


def test_resolve_project_github_token(tmp_path, global_config):
    _write_marker(tmp_path, '[github]\ntoken = "ghp_projecttoken"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.github_token == "ghp_projecttoken"


def test_resolve_project_github_token_defaults_none(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.github_token is None


def test_resolve_project_legacy_name_field_dies(tmp_path, global_config):
    # `name` used to be the VM; it's now a migration error, not silently ignored.
    _write_marker(tmp_path, 'name = "myproj"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_invalid_provider_dies(tmp_path, global_config):
    _write_marker(tmp_path, 'provider = "gcp"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_cloud_config_present(tmp_path, global_config):
    _write_marker(tmp_path, 'project = "myproj"\n')
    cloud_config = tmp_path / devbox.PROJECT_MARKER_DIR / devbox.PROJECT_CLOUD_CONFIG_FILE
    cloud_config.write_text("packages: []\n")
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.cloud_config == cloud_config


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
    monkeypatch.setattr(devbox.cloudconfig, "COMMON_CLOUD_CONFIG_PATH", path)
    # The standalone provisioning script devbox injects into write_files; keep
    # it tiny (the real one needs a booted VM) but token-bearing so we can check
    # substitution and injection.
    script = tmp_path / "provision-common.sh"
    script.write_text("#!/usr/bin/env bash\necho hello %DEVBOX_USER% %DEVBOX_REPO_MODE%\n")
    monkeypatch.setattr(devbox.cloudconfig, "PROVISION_SCRIPT_PATH", script)
    return path


@pytest.fixture
def pubkey(tmp_path):
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 AAAAtest user@host\n")
    return path


def _project(cloud_config=None, mode="none", dir="", url=None, github_token=None):
    return devbox.ProjectConfig(
        project="test-project", provider="libvirt", cloud_config=cloud_config, user="alice",
        vcpus=6, memory_mib=1024, disk_gb=10,
        repo=devbox.RepoConfig(mode=mode, dir=dir, url=url),
        github_token=github_token,
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


def test_render_cloud_config_project_token_overrides_global(common_template, pubkey):
    gc = _global_config(pubkey, github_token="ghp_global")
    rendered = devbox.render_cloud_config(_project(github_token="ghp_project"), gc)
    assert "github-token: ghp_project" in rendered


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
def test_resolve_github_token_from_global_config():
    gc = _global_config(devbox.Path("/nonexistent"), github_token="ghp_fromconfig")
    assert devbox.resolve_github_token(_project(), gc) == "ghp_fromconfig"


def test_resolve_github_token_project_overrides_global():
    gc = _global_config(devbox.Path("/nonexistent"), github_token="ghp_global")
    assert devbox.resolve_github_token(_project(github_token="ghp_project"), gc) == "ghp_project"


def test_resolve_github_token_falls_back_to_global_when_project_unset():
    gc = _global_config(devbox.Path("/nonexistent"), github_token="ghp_global")
    assert devbox.resolve_github_token(_project(github_token=None), gc) == "ghp_global"


def test_resolve_github_token_none_when_unset_is_not_fatal():
    # An unresolved GitHub token must NOT raise - it's best-effort (public
    # repos / non-"clone" [repo] modes never need it).
    gc = _global_config(devbox.Path("/nonexistent"), github_token=None)
    assert devbox.resolve_github_token(_project(github_token=None), gc) is None


def test_render_cloud_config_empty_token_when_unset(common_template, pubkey, capsys):
    gc = _global_config(pubkey, github_token=None)
    rendered = devbox.render_cloud_config(_project(), gc)
    import yaml
    content = yaml.safe_load(rendered)["write_files"][0]["content"]
    assert content.splitlines()[1] == "github-token: "
    assert "no GitHub token found" in capsys.readouterr().out


# ---- instance identity / resolution ----------------------------------------
def test_resolve_instance_defaults_to_project():
    import argparse
    args = argparse.Namespace(name=None)
    assert devbox.resolve_instance(args, _project()) == "test-project"


def test_resolve_instance_name_overrides():
    import argparse
    args = argparse.Namespace(name="foo-gpu")
    assert devbox.resolve_instance(args, _project()) == "foo-gpu"


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
        devbox.get_provider("aws", gc)


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


# ---- jj sync ----------------------------------------------------------------
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


def test_cmd_sync_without_repo_dies():
    import argparse
    gc = _global_config(devbox.Path("/nonexistent"))
    with pytest.raises(SystemExit):
        devbox.cli.cmd_sync(argparse.Namespace(name=None, project_dir=None), gc, _project(mode="none"))


# ---- AWS config -------------------------------------------------------------
def test_global_config_aws_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[aws]\n"
        'region = "us-west-2"\n'
        'profile = "dev"\n'
        'default_security_groups = ["sg-1", "sg-2"]\n'
        'default_subnets = ["subnet-a", "subnet-b"]\n'
        "spot = true\n"
    )
    gc = devbox.GlobalConfig.load(path)
    assert gc.aws is not None
    assert gc.aws.region == "us-west-2"
    assert gc.aws.profile == "dev"
    assert gc.aws.security_groups == ["sg-1", "sg-2"]
    assert gc.aws.subnets == ["subnet-a", "subnet-b"]
    assert gc.aws.spot is True


def test_global_config_no_aws_section_is_none(tmp_path):
    assert devbox.GlobalConfig.load(tmp_path / "missing.toml").aws is None


def test_global_config_aws_requires_region(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[aws]\nprofile = "dev"\n')
    with pytest.raises(SystemExit):
        devbox.GlobalConfig.load(path)


def test_resolve_project_aws_section(tmp_path, global_config):
    _write_marker(
        tmp_path,
        'provider = "aws"\n[aws]\ninstance_type = "g5.xlarge"\narchitecture = "x86_64"\nspot = true\n',
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.provider == "aws"
    assert project.aws.instance_type == "g5.xlarge"
    assert project.aws.architecture == "x86_64"
    assert project.aws.spot is True


def test_resolve_project_aws_requires_instance_type(tmp_path, global_config):
    _write_marker(tmp_path, '[aws]\narchitecture = "arm64"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_aws_invalid_architecture_dies(tmp_path, global_config):
    _write_marker(tmp_path, '[aws]\ninstance_type = "c7i.large"\narchitecture = "ppc64"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


# ---- AWS provider -----------------------------------------------------------
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


def _aws_gc(security_groups=("sg-1",), subnets=(), spot=False):
    gc = devbox.GlobalConfig.load(devbox.Path("/nonexistent"))
    gc.aws = devbox.AwsConfig(region="us-east-1", profile=None,
                              security_groups=list(security_groups),
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


def _aws_spec(name="foo-gpu", instance_type="g5.xlarge", architecture="x86_64",
              spot=None, ami=None, disk_gb=64):
    return devbox.InstanceSpec(
        name=name, project="proj", provider="aws", disk_gb=disk_gb,
        user_data="#cloud-config\n", created_at="2026-07-21T12:00:00+00:00",
        aws=devbox.AwsProjectConfig(instance_type=instance_type, architecture=architecture,
                                    spot=spot, ami=ami),
    )


def test_get_provider_aws_requires_config():
    with pytest.raises(SystemExit):
        devbox.get_provider("aws", devbox.GlobalConfig.load(devbox.Path("/nonexistent")))


def test_get_provider_aws_configured():
    assert isinstance(devbox.get_provider("aws", _aws_gc()), devbox.AwsProvider)


def test_all_providers_gates_aws_on_config():
    names = lambda gc: [p.name for p in devbox.all_providers(gc)]
    assert names(devbox.GlobalConfig.load(devbox.Path("/nonexistent"))) == ["libvirt"]
    assert names(_aws_gc()) == ["libvirt", "aws"]


def test_aws_list_parses_and_flags_cost():
    client = _FakeEC2(describe_instances=_reservations(
        _ec2_instance(name="foo", state="running", itype="c7i.large"),
        _ec2_instance(name=None),  # no devbox:name tag -> skipped
    ))
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    instances = provider.list()
    assert len(instances) == 1
    i = instances[0]
    assert (i.name, i.provider, i.project, i.state, i.resources) == \
        ("foo", "aws", "proj", "running", "c7i.large")
    assert i.cost_hint is True


def test_aws_state_and_address():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(state="running")))
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    assert provider.state("foo") == "running"
    assert provider.address("foo") == "1.2.3.4"


def test_aws_state_none_when_absent():
    provider = devbox.AwsProvider(_aws_gc(), client=_FakeEC2())
    assert provider.state("nope") is None
    assert provider.address("nope") is None


def test_aws_create_on_demand_request_shape():
    client = _FakeEC2()  # describe_instances empty (no existing), images present
    provider = devbox.AwsProvider(_aws_gc(security_groups=["sg-1"]), client=client)
    provider.create(_aws_spec(name="foo-gpu", instance_type="g5.xlarge", disk_gb=64))
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


def test_aws_create_spot_adds_market_options():
    client = _FakeEC2()
    provider = devbox.AwsProvider(_aws_gc(spot=False), client=client)
    provider.create(_aws_spec(spot=True))  # project overrides global default
    params = [kw for (op, kw) in client.calls if op == "run_instances"][0]
    assert params["InstanceMarketOptions"]["MarketType"] == "spot"
    assert params["InstanceMarketOptions"]["SpotOptions"]["SpotInstanceType"] == "persistent"


def test_aws_create_explicit_ami_skips_lookup():
    client = _FakeEC2()
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    provider.create(_aws_spec(ami="ami-pinned"))
    assert not any(op == "describe_images" for (op, _) in client.calls)
    params = [kw for (op, kw) in client.calls if op == "run_instances"][0]
    assert params["ImageId"] == "ami-pinned"


def test_aws_create_no_ami_found_dies():
    client = _FakeEC2(images=[])
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_aws_spec())


def test_aws_create_existing_dies():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(name="foo-gpu")))
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    with pytest.raises(SystemExit):
        provider.create(_aws_spec(name="foo-gpu"))


def test_aws_create_subnet_capacity_fallback():
    attempts = []

    def run_side_effect(kw):
        attempts.append(kw.get("SubnetId"))
        if len(attempts) == 1:
            raise _FakeClientError("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": "i-2", "State": {"Name": "pending"},
                               "InstanceType": kw["InstanceType"]}]}

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.AwsProvider(_aws_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    provider.create(_aws_spec())
    assert attempts == ["subnet-a", "subnet-b"]


def test_aws_create_all_subnets_exhausted_dies():
    def run_side_effect(kw):
        raise _FakeClientError("InsufficientInstanceCapacity")

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.AwsProvider(_aws_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    with pytest.raises(SystemExit):
        provider.create(_aws_spec())


def test_aws_create_non_capacity_error_reraises():
    def run_side_effect(kw):
        raise _FakeClientError("UnauthorizedOperation")

    client = _FakeEC2(run_side_effect=run_side_effect)
    provider = devbox.AwsProvider(_aws_gc(subnets=["subnet-a", "subnet-b"]), client=client)
    with pytest.raises(_FakeClientError):
        provider.create(_aws_spec())
    # Not retried against the second subnet.
    assert len([1 for (op, _) in client.calls if op == "run_instances"]) == 1


def test_aws_start_stop_destroy_by_name():
    client = _FakeEC2(describe_instances=_reservations(_ec2_instance(name="foo", state="stopped")))
    provider = devbox.AwsProvider(_aws_gc(), client=client)
    provider.start("foo")
    provider.stop("foo")
    provider.destroy("foo")
    ops = {op: kw for (op, kw) in client.calls if op != "describe_instances"}
    assert ops["start_instances"]["InstanceIds"] == ["i-123"]
    assert ops["stop_instances"]["InstanceIds"] == ["i-123"]
    assert ops["terminate_instances"]["InstanceIds"] == ["i-123"]
