"""Backup manifest + restore (plan item 6)."""

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

import nimcode.ui.rust_bridge as rb
from nimcode.config import Config, get_project_nimcode_dir
from nimcode.tools.base import BACKUP_MANIFEST, backup_file, list_backups


@pytest.fixture
def ws(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "ws"
    (root / "src" / "my_pkg").mkdir(parents=True)
    return root.resolve()


def test_manifest_records_real_original_path(ws):
    f = ws / "src" / "my_pkg" / "a_b.py"
    f.write_text("v1", encoding="utf-8")
    backup_file(f, ws, tag="blocks")
    (b,) = list_backups(ws)
    # Underscores in names no longer make the original path ambiguous.
    assert b["original"] == "src/my_pkg/a_b.py"
    assert b["tag"] == "blocks"
    assert b["path"].read_text(encoding="utf-8") == "v1"


def test_legacy_backup_without_manifest_falls_back_to_name(ws):
    bdir = get_project_nimcode_dir(ws) / "backups"
    bdir.mkdir(parents=True)
    (bdir / "1700000000_src_x.py").write_text("old", encoding="utf-8")
    (b,) = list_backups(ws)
    assert b["original"] is None and b["hint"] == "src_x.py"
    assert b["when"].startswith("2023")


def test_concurrent_backups_keep_manifest_valid(ws):
    files = []
    for i in range(20):
        f = ws / f"f{i}.txt"
        f.write_text(str(i), encoding="utf-8")
        files.append(f)
    threads = [threading.Thread(target=backup_file, args=(f, ws)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    manifest = get_project_nimcode_dir(ws) / "backups" / BACKUP_MANIFEST
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20 and all(json.loads(ln)["original"] for ln in lines)
    assert {b["original"] for b in list_backups(ws)} == {f.name for f in files}


def test_restore_uses_manifest_path_without_prompting(ws, monkeypatch):
    f = ws / "src" / "my_pkg" / "a_b.py"
    f.write_text("good", encoding="utf-8")
    backup_file(f, ws)
    f.write_text("broken", encoding="utf-8")

    monkeypatch.setattr(rb, "find_rust_binary", lambda: Path(sys.executable))
    tui = rb.RustTUI(Config())

    async def pick_first(title, labels):
        assert "src/my_pkg/a_b.py" in labels[0]
        return 0

    async def no_prompt(*a, **k):
        raise AssertionError("manifest backups must not prompt for a path")

    monkeypatch.setattr(tui, "_select", pick_first)
    monkeypatch.setattr(tui, "get_user_input", no_prompt)
    asyncio.run(tui.restore_backup(ws))
    assert f.read_text(encoding="utf-8") == "good"
