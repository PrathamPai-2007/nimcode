"""Track W: patch hints, ripgrep search, nested gitignore, workspace map,
screenshot path line, SearXNG fallback. No real network."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import nimcode.context as ctx
from nimcode.context import generate_workspace_map, is_ignored, load_gitignore_rules
from nimcode.tools import browser, web
from nimcode.tools.base import is_valid_tool_call, render_preview
from nimcode.tools.fs import patch_file_blocks_logic, search_grep_tool

_real_which = shutil.which


def _no_tool(monkeypatch, *names):
    monkeypatch.setattr(
        shutil, "which", lambda n, *a, **k: None if n in names else _real_which(n)
    )


def _grep(root, **args):
    return search_grep_tool(args, root, SimpleNamespace(output_cap=100_000))


# --- Item 1: patch hint, search_grep, gitignore cache -----------------------


def test_patch_not_found_shows_closest_window(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def one():\n    return 1\n\ndef two():\n    return 2\n")
    blocks = "<<<<<<< SEARCH\ndef two():\n  return 2\n=======\nx\n>>>>>>> REPLACE"
    out = patch_file_blocks_logic(tmp_path, "a.py", blocks)
    assert "SEARCH block not found" in out
    assert "Closest match (lines 4-5" in out
    assert "    4| def two():" in out
    assert f.read_text().startswith("def one")  # untouched


def _tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("Needle here\n")
    (tmp_path / "src" / "b.txt").write_text("needle lower\n")
    (tmp_path / "src" / "gen").mkdir()
    (tmp_path / "src" / "gen" / "c.py").write_text("needle generated\n")
    (tmp_path / "src" / ".gitignore").write_text("gen/\n")
    return tmp_path


def test_search_grep_python_include_and_case(tmp_path, monkeypatch):
    _no_tool(monkeypatch, "rg")
    root = _tree(tmp_path)
    out = _grep(root, query="needle")
    assert "src/a.py:1" in out and "src/b.txt:1" in out
    assert "gen/c.py" not in out  # nested .gitignore honoured
    out = _grep(root, query="needle", include="*.py")
    assert "src/a.py" in out and "b.txt" not in out
    out = _grep(root, query="needle", case_sensitive=True)
    assert "a.py" not in out and "src/b.txt" in out
    out = _grep(root, query="needle", include="src/*.txt")
    assert "src/b.txt" in out and "a.py" not in out


class _Lines(list):
    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines, rc):
        self.stdout = _Lines(lines)
        self.returncode = rc

    def wait(self):
        return self.returncode

    def kill(self):
        pass


def test_search_grep_uses_rg_json(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        ev = {
            "type": "match",
            "data": {
                "path": {"text": os.path.join("src", "a.py")},
                "line_number": 7,
                "lines": {"text": "from rg\n"},
            },
        }
        lines = [json.dumps({"type": "begin"}).encode(), json.dumps(ev).encode()]
        return _FakeProc(lines, 0)

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/rg")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("MY_API_KEY", "s3cret")
    out = _grep(root, query="needle", include="*.py", case_sensitive=True)
    assert out == "src/a.py:7: from rg"
    cmd = seen["cmd"]
    assert "--json" in cmd and "--case-sensitive" in cmd
    assert any(cmd[i : i + 2] == ["--glob", "*.py"] for i in range(len(cmd)))
    assert seen["cwd"] == str(root.resolve())
    assert "MY_API_KEY" not in seen["env"]


def test_search_grep_rg_error_falls_back(tmp_path, monkeypatch):
    root = _tree(tmp_path)

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/rg")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakeProc([], 2))
    out = _grep(root, query="needle")
    assert "src/a.py:1: Needle here" in out  # Python walk answered


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_real_rg_matches_python(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    (root / ".env").write_text("needle secret\n")
    (root / "notes.log").write_text("needle log\n")
    (root / ".gitignore").write_text("*.log\n")
    via_rg = _grep(root, query="needle")
    _no_tool(monkeypatch, "rg")
    assert via_rg == _grep(root, query="needle")
    assert ".env" not in via_rg and "notes.log" not in via_rg


def test_grep_validation_and_preview():
    def call(args):
        fn = SimpleNamespace(name="search_grep", arguments=json.dumps(args))
        return SimpleNamespace(function=fn)

    assert is_valid_tool_call(call({"query": "x", "include": "*.py"}))[0]
    assert not is_valid_tool_call(call({"query": "x", "include": 3}))[0]
    prev = render_preview(
        "search_grep", {"query": "x", "include": "*.py", "case_sensitive": True}, None
    )
    assert "Include: *.py" in prev and "Case-sensitive" in prev


def test_gitignore_cache_keyed_by_mtime(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("*.log\n")
    assert load_gitignore_rules(tmp_path) == ["*.log"]
    gi.write_text("*.tmp\n")
    st = gi.stat()
    os.utime(gi, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert load_gitignore_rules(tmp_path) == ["*.tmp"]


# --- Item 3: nested gitignore, info/exclude, workspace map ------------------


def test_nested_gitignore_and_info_exclude(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("secret.txt\n")
    (tmp_path / "pkg" / "build").mkdir(parents=True)
    (tmp_path / "pkg" / ".gitignore").write_text("*.out\n/build\n!keep.out\n")
    for rel in ["pkg/a.out", "pkg/sub/b.out", "pkg/keep.out", "a.out", "secret.txt"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    (tmp_path / "pkg" / "build" / "x.py").write_text("x")
    (tmp_path / "build").mkdir()
    rules = load_gitignore_rules(tmp_path)

    def ign(rel):
        return is_ignored(tmp_path / rel, rules, root=tmp_path)

    assert ign("pkg/a.out") and ign("pkg/sub/b.out")
    assert not ign("pkg/keep.out")
    assert not ign("a.out")  # rule scoped to pkg/
    assert ign("pkg/build/x.py") and not ign("build")
    assert ign("secret.txt")


def test_workspace_map_ranks_entry_points_and_recent(tmp_path, monkeypatch):
    _no_tool(monkeypatch, "git")
    now = time.time()
    for i in range(10):
        p = tmp_path / "deep" / "nest" / f"old{i}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        os.utime(p, (now - 10_000 - i, now - 10_000 - i))
    (tmp_path / "zz").mkdir()
    (tmp_path / "zz" / "main.py").write_text("x")
    os.utime(tmp_path / "zz" / "main.py", (now - 99_999, now - 99_999))
    (tmp_path / "fresh.txt").write_text("x")
    out = generate_workspace_map(str(tmp_path), max_files=3)
    assert "main.py" in out  # entry point despite being oldest
    assert "fresh.txt" in out  # most recent
    assert "old0.txt" in out and "old1.txt" not in out
    assert "[9 more file(s) not shown" in out


def test_workspace_map_uses_git_ls_files(tmp_path, monkeypatch):
    (tmp_path / "tracked.py").write_text("x")
    (tmp_path / "ignored_by_git.py").write_text("x")
    monkeypatch.setattr(ctx, "_git_list_files", lambda root: ["tracked.py", "gone.py"])
    out = generate_workspace_map(str(tmp_path))
    assert "tracked.py" in out
    assert "ignored_by_git.py" not in out and "gone.py" not in out


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_list_files_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.log").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("skip.py\n")
    (tmp_path / "sub" / "skip.py").write_text("x")
    files = ctx._git_list_files(tmp_path)
    assert files is not None
    assert "a.py" in files and "b.log" not in files and "sub/skip.py" not in files
    assert ctx._git_list_files(tmp_path / "sub") is not None
    assert ctx._git_list_files(Path(tmp_path.anchor) / "nonexistent_dir_xyz") is None


# --- Item 2: screenshot path line, SearXNG fallback --------------------------


def test_screenshot_returns_path_line(tmp_path, monkeypatch):
    import nimcode.config as config_mod

    monkeypatch.setattr(browser, "sync_playwright", object())
    monkeypatch.setattr(
        browser,
        "_fetch_with_retry",
        lambda *a, **k: {"success": True, "image_bytes": b"\x89PNG" + b"0" * 200_000},
    )
    monkeypatch.setattr(config_mod, "get_project_nimcode_dir", lambda root: tmp_path)
    (tmp_path / "index.html").write_text("<p>hi</p>")
    out = browser.screenshot_page({"html_file": "index.html"}, tmp_path, None)
    line = next(ln for ln in out.splitlines() if ln.startswith("[screenshot: "))
    saved = Path(line[len("[screenshot: ") : -1])
    assert saved.is_absolute() and saved.read_bytes().startswith(b"\x89PNG")
    assert "inline base64 omitted" in out


_SEARX_JSON = json.dumps(
    {"results": [{"title": "Doc", "url": "https://x.dev", "content": "hi &amp; bye"}]}
)


@pytest.mark.parametrize("ddg", ["captcha", "error"])
def test_web_search_falls_back_to_searxng(monkeypatch, ddg):
    urls = []

    def fake_fetch(url, headers, **kw):
        urls.append(url)
        if "duckduckgo" in url:
            if ddg == "error":
                raise OSError("boom")
            return "<html>Please solve this captcha</html>"
        return _SEARX_JSON

    monkeypatch.setattr(web, "_fetch_with_overall_timeout", fake_fetch)
    cfg = SimpleNamespace(searxng_url="http://localhost:8888/")
    out = web.web_search({"query": "q"}, Path("."), cfg)
    assert "1. Doc" in out and "URL: https://x.dev" in out and "hi & bye" in out
    assert urls[1].startswith("http://localhost:8888/search?")
    assert "format=json" in urls[1]


def test_web_search_no_searxng_keeps_old_messages(monkeypatch):
    monkeypatch.setattr(
        web, "_fetch_with_overall_timeout", lambda *a, **k: "<html>captcha</html>"
    )
    out = web.web_search({"query": "q"}, Path("."), SimpleNamespace(searxng_url=""))
    assert "bot-check" in out
    out = web.web_search({"query": "q"}, Path("."), None)
    assert "bot-check" in out
