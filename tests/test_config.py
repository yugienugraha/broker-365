import pytest

from broker365 import config
from broker365.errors import Broker365Error


def test_home_prefers_broker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER365_HOME", str(tmp_path / "b"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert config.broker_home() == tmp_path / "b"


def test_home_falls_back_to_hermes(tmp_path, monkeypatch):
    monkeypatch.delenv("BROKER365_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert config.broker_home() == tmp_path / "broker365"


def test_scopes_include_core_and_dedupe():
    assert config.scopes_for(["files", "files"]) == ["User.Read", "Files.ReadWrite"]


def test_unknown_bundle_rejected():
    with pytest.raises(Broker365Error):
        config.scopes_for(["nope"])


def test_change_settings_merges_atomically(tmp_path):
    import threading

    config.update_settings(excel_backend={"u@x.com": "openpyxl"}, default_account="u@x.com")
    dirs = [tmp_path / f"d{i}" for i in range(20)]
    for d in dirs:
        d.mkdir()
    barrier = threading.Barrier(len(dirs))

    def add(d):
        barrier.wait()
        config.change_settings(add_dirs=[str(d)])

    threads = [threading.Thread(target=add, args=(d,)) for d in dirs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert config.load_settings()["allowed_dirs"] == sorted(str(d.resolve()) for d in dirs)

    changed = config.change_settings(
        read_only=True, remove_dirs=[str(d) for d in dirs[1:]], reset_excel_probe=True
    )
    assert changed == {
        "read_only": True,
        "dirs_removed": sorted(str(d.resolve()) for d in dirs[1:]),
        "excel_probe_reset": True,
    }
    assert config.load_settings() == {
        "default_account": "u@x.com",
        "read_only": True,
        "allowed_dirs": [str(dirs[0].resolve())],
    }
    assert config.change_settings(read_only=True, add_dirs=[str(dirs[0])]) == {}
    view = config.settings_view()
    assert view["read_only"] is True and view["allowed_dirs"] == [str(dirs[0].resolve())]


@pytest.fixture
def no_resolve(monkeypatch):
    """Windows rules on any OS; fail loudly if anything resolves (touches) a path."""
    from pathlib import Path

    monkeypatch.setattr(config, "_WINDOWS", True)

    def boom(self, *a, **k):
        raise AssertionError(f"resolve() must not run for {self}")

    monkeypatch.setattr(Path, "resolve", boom)


@pytest.mark.parametrize(
    "path", ["\\\\evil.example\\share", "//evil.example/share", "\\\\?\\C:\\x", "C:docs"]
)
def test_allowed_dirs_refuse_remote_paths_without_resolving(path, no_resolve):
    with pytest.raises(Broker365Error, match="Network shares"):
        config.change_settings(add_dirs=[path])
    with pytest.raises(Broker365Error, match="UNC"):
        config.normalize_dir(path)
    assert "allowed_dirs" not in config.load_settings()


def test_allowed_dirs_must_be_existing_absolute_folders(tmp_path):
    with pytest.raises(Broker365Error, match="absolute"):
        config.change_settings(add_dirs=["relative/dir"])
    with pytest.raises(Broker365Error, match="existing folder"):
        config.change_settings(add_dirs=[str(tmp_path / "missing")])
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(Broker365Error, match="existing folder"):
        config.change_settings(add_dirs=[str(tmp_path / "f.txt")])


@pytest.mark.parametrize("form", ["raw", "expanded"])
def test_remove_matches_raw_or_normalized(form, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "Reports").mkdir()
    config.update_settings(allowed_dirs=["//nas/share", "~/Reports"])
    request = "~/Reports" if form == "raw" else str(Path("~/Reports").expanduser())
    assert config.change_settings(remove_dirs=[request]) == {"dirs_removed": ["~/Reports"]}
    assert config.load_settings()["allowed_dirs"] == ["//nas/share"]
