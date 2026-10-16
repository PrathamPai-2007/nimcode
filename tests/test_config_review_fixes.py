from pathlib import Path

import pytest

import nimcode.config as config_mod
import nimcode.config_models as cm
import nimcode.skills as skills
from nimcode.config import (
    Config,
    get_project_nimcode_dir,
    load_config,
    save_config,
)
from nimcode.context import is_ignored


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _ign(rel, rules, root, is_dir=False):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir() if is_dir else p.write_text("x")
    return is_ignored(p, rules, root=root)


def test_gitignore_semantics(tmp_path):
    assert not _ign("src/a.py", ["build/**"], tmp_path)  # used to ignore all
    assert _ign("build/out.js", ["build/**"], tmp_path)
    assert _ign("a/b/c.log", ["*.log"], tmp_path)
    assert _ign("build/x.txt", ["/build"], tmp_path)
    assert not _ign("src/build/x.txt", ["/build"], tmp_path)
    assert not _ign("src/a/b.py", ["src/*.py"], tmp_path)  # * stops at /
    assert _ign("a/x/y/b", ["a/**/b"], tmp_path)
    assert not _ign("keep.log", ["*.log", "!keep.log"], tmp_path)
    # A file inside an ignored dir can't be re-included (git semantics).
    assert _ign("logs/keep.log", ["logs/", "!keep.log"], tmp_path)
    # Trailing slash = directories only.
    assert not _ign("cache", ["cache/"], tmp_path)
    assert _ign("cache2/", ["cache2/"], tmp_path, is_dir=True)


def test_save_config_escapes_strings(fake_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = Config()
    cfg.docker_image = 'weird"img\\tag'
    cfg.api_base_url = "http://localhost:8000/v1"
    save_config(cfg, ws)
    loaded = load_config(ws)
    assert loaded.docker_image == 'weird"img\\tag'
    assert loaded.api_base_url == "http://localhost:8000/v1"


def test_model_json_values_are_validated(fake_home, tmp_path):
    import json

    ws = tmp_path / "ws"
    ws.mkdir()
    (get_project_nimcode_dir(ws) / "model.json").write_text(
        json.dumps(
            {
                "docker_extra_args": "--memory=1g",
                "disable_docker": "false",
                "docker_expose_ports": ["abc", 8080],
            }
        )
    )
    cfg = load_config(ws)
    assert cfg.docker_extra_args == ["--memory=1g"]
    assert cfg.disable_docker is False
    assert cfg.docker_expose_ports == [8080]


def test_migration_spares_global_skills_and_home(fake_home):
    skills_dir = fake_home / ".nimcode" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "s.md").write_text("skill")
    ws = fake_home / "work" / "skills"  # project named like a global entry
    ws.mkdir(parents=True)
    get_project_nimcode_dir(ws)
    assert (skills_dir / "s.md").exists()
    # Workspace == $HOME: ~/.nimcode is the global dir, not a local one.
    get_project_nimcode_dir(fake_home)
    assert (skills_dir / "s.md").exists()


def test_migration_never_overwrites_newer_state(fake_home):
    ws = fake_home / "proj"
    ws.mkdir()
    dst = get_project_nimcode_dir(ws)
    (dst / "model.json").write_text("new")
    legacy = fake_home / ".nimcode" / "proj"
    legacy.mkdir()
    (legacy / "model.json").write_text("old")
    (legacy / "extra.txt").write_text("e")
    get_project_nimcode_dir(ws)
    assert (dst / "model.json").read_text() == "new"
    assert (dst / "extra.txt").read_text() == "e"
    assert not legacy.exists()


def test_backup_name_strips_pid():
    from nimcode.tools.base import _parse_backup_name

    ts, hint, tag = _parse_backup_name("1700000000123456_4242_blocks_src_a.py")
    assert (hint, tag) == ("src_a.py", "blocks")
    assert ts == 1700000000.123456


def test_reconcile_ignores_static_fallback(monkeypatch):
    monkeypatch.setattr(config_mod, "save_last_used_model", lambda *a, **k: None)
    cfg = Config()
    cfg.fast_model = "meta/llama-custom"
    assert cm.reconcile_saved_model(cfg, cm.static_catalog()) is None
    assert cfg.fast_model == "meta/llama-custom"


def test_failed_download_leaves_no_partial_file(monkeypatch, tmp_path):
    import urllib.request

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    dest = tmp_path / "x.md"
    with pytest.raises(OSError):
        skills._download_with_limits("https://example.com/x.md", dest, 100)
    assert list(tmp_path.iterdir()) == []
