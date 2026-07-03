import pytest

import devbox


# ---- GlobalConfig -------------------------------------------------------
def test_global_config_defaults_when_missing(tmp_path):
    gc = devbox.GlobalConfig.load(tmp_path / "does-not-exist.toml")
    assert gc.libvirt_uri == devbox.DEFAULT_LIBVIRT_URI
    assert gc.network == devbox.DEFAULT_NETWORK
    assert gc.cache_dir == devbox.Path(devbox.DEFAULT_CACHE_DIR)
    assert gc.base_image_url == devbox.DEFAULT_BASE_IMAGE_URL
    assert gc.base_image_sha256 == devbox.DEFAULT_BASE_IMAGE_SHA256
    assert gc.vcpus == devbox.DEFAULT_VCPUS
    assert gc.memory_mib == devbox.DEFAULT_MEMORY_MIB
    assert gc.disk_gb == devbox.DEFAULT_DISK_GB


def test_global_config_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
libvirt_uri = "qemu:///session"
network = "custom-net"
cache_dir = "/tmp/custom-cache"

[base_image]
url = "https://example.com/image.img"
sha256 = "deadbeef"

[defaults]
vcpus = 2
memory_mib = 4096
disk_gb = 20

[ssh]
pubkey_path = "~/.ssh/other.pub"
""")
    gc = devbox.GlobalConfig.load(path)
    assert gc.libvirt_uri == "qemu:///session"
    assert gc.network == "custom-net"
    assert gc.cache_dir == devbox.Path("/tmp/custom-cache")
    assert gc.base_image_url == "https://example.com/image.img"
    assert gc.base_image_sha256 == "deadbeef"
    assert gc.vcpus == 2
    assert gc.memory_mib == 4096
    assert gc.disk_gb == 20
    assert gc.ssh_pubkey_path == devbox.Path("~/.ssh/other.pub").expanduser()


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
    return devbox.GlobalConfig.load(devbox.Path("/nonexistent"))


def test_find_marker_dir_at_start(tmp_path):
    (tmp_path / devbox.PROJECT_MARKER).write_text('name = "x"\n')
    assert devbox.find_marker_dir(tmp_path) == tmp_path.resolve()


def test_find_marker_dir_walks_up(tmp_path):
    (tmp_path / devbox.PROJECT_MARKER).write_text('name = "x"\n')
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert devbox.find_marker_dir(deep) == tmp_path.resolve()


def test_find_marker_dir_missing_dies(tmp_path):
    with pytest.raises(SystemExit):
        devbox.find_marker_dir(tmp_path)


def test_resolve_project_basic(tmp_path, global_config):
    (tmp_path / devbox.PROJECT_MARKER).write_text(
        'name = "myproj"\ncloud_init = "cloud-init/user-data.yaml"\n'
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.name == "myproj"
    assert project.cloud_init == tmp_path.resolve() / "cloud-init" / "user-data.yaml"
    assert project.vcpus == global_config.vcpus
    assert project.memory_mib == global_config.memory_mib
    assert project.disk_gb == global_config.disk_gb


def test_resolve_project_overrides_defaults(tmp_path, global_config):
    (tmp_path / devbox.PROJECT_MARKER).write_text(
        'name = "myproj"\ncloud_init = "ci.yaml"\nvcpus = 2\nmemory_mib = 2048\ndisk_gb = 10\nuser = "alice"\n'
    )
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.vcpus == 2
    assert project.memory_mib == 2048
    assert project.disk_gb == 10
    assert project.user == "alice"


def test_resolve_project_local_overrides_name(tmp_path, global_config):
    (tmp_path / devbox.PROJECT_MARKER).write_text(
        'name = "shared-name"\ncloud_init = "ci.yaml"\n'
    )
    (tmp_path / devbox.PROJECT_MARKER_LOCAL).write_text('name = "my-own-vm"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.name == "my-own-vm"
    # fields not present in .local are still inherited from the committed marker
    assert project.cloud_init == tmp_path.resolve() / "ci.yaml"


def test_resolve_project_local_only_directory_wins_over_parent_base(tmp_path, global_config):
    # A directory with only .devbox.local.toml still wins over a .devbox.toml
    # further up the tree - the walk stops at the first directory with *either* marker.
    (tmp_path / devbox.PROJECT_MARKER).write_text('name = "parent"\ncloud_init = "ci.yaml"\n')
    child = tmp_path / "child"
    child.mkdir()
    (child / devbox.PROJECT_MARKER_LOCAL).write_text('name = "child-local"\n')
    project = devbox.resolve_project(child, global_config)
    assert project.name == "child-local"


def test_resolve_project_missing_name_dies(tmp_path, global_config):
    (tmp_path / devbox.PROJECT_MARKER).write_text('cloud_init = "ci.yaml"\n')
    with pytest.raises(SystemExit):
        devbox.resolve_project(tmp_path, global_config)


def test_resolve_project_no_cloud_init_is_none(tmp_path, global_config):
    (tmp_path / devbox.PROJECT_MARKER).write_text('name = "myproj"\n')
    project = devbox.resolve_project(tmp_path, global_config)
    assert project.cloud_init is None


# ---- cloud-init templating -------------------------------------------------
def test_render_cloud_init_substitutes_tokens(tmp_path):
    ci = tmp_path / "user-data.yaml"
    ci.write_text("users:\n  - name: %DEVBOX_USER%\n    ssh_authorized_keys:\n      - %DEVBOX_SSH_PUBKEY%\n")
    pubkey = tmp_path / "id_ed25519.pub"
    pubkey.write_text("ssh-ed25519 AAAAtest user@host\n")

    rendered = devbox.render_cloud_init(ci, "alice", pubkey)
    assert "name: alice" in rendered
    assert "ssh-ed25519 AAAAtest user@host" in rendered
    assert "%DEVBOX_USER%" not in rendered
    assert "%DEVBOX_SSH_PUBKEY%" not in rendered


def test_render_cloud_init_missing_file_dies(tmp_path):
    pubkey = tmp_path / "id_ed25519.pub"
    pubkey.write_text("key\n")
    with pytest.raises(SystemExit):
        devbox.render_cloud_init(tmp_path / "missing.yaml", "alice", pubkey)


def test_render_cloud_init_missing_pubkey_dies(tmp_path):
    ci = tmp_path / "user-data.yaml"
    ci.write_text("x: %DEVBOX_USER%\n")
    with pytest.raises(SystemExit):
        devbox.render_cloud_init(ci, "alice", tmp_path / "missing.pub")
