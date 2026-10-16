from types import SimpleNamespace

from nimcode.tools.fs import (
    glob_files_tool,
    patch_file_blocks_logic,
    search_grep_tool,
    write_file_tool,
)
from nimcode.tools.patch import patch_file_ast_logic
from nimcode.tools.browser import _is_url_allowed
from nimcode.tools.web import _parse_duckduckgo_results
from nimcode.tools import swarm

CFG = SimpleNamespace(auto_backup=False, output_cap=100000)


def test_patch_blocks_empty_replace_does_not_swallow_next_block(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    blocks = (
        "<<<<<<< SEARCH\ntwo\n=======\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\nthree\n=======\nTHREE\n>>>>>>> REPLACE\n"
    )
    res = patch_file_blocks_logic(tmp_path, "a.txt", blocks, config=CFG)
    assert "Successfully applied 2" in res
    assert (tmp_path / "a.txt").read_text() == "one\n\nTHREE\n"


def test_patch_blocks_crlf_no_double_cr(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    blocks = (
        "<<<<<<< SEARCH\r\ny = 2\r\n=======\r\ny = 3\r\nz = 4\r\n>>>>>>> REPLACE\r\n"
    )
    assert "Successfully" in patch_file_blocks_logic(tmp_path, "a.txt", blocks, CFG)
    assert f.read_bytes() == b"x = 1\r\ny = 3\r\nz = 4\r\n"


def test_patch_blocks_refuses_non_utf8(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"caf\xe9 = 1\n")
    blocks = "<<<<<<< SEARCH\n= 1\n=======\n= 2\n>>>>>>> REPLACE"
    assert "not valid UTF-8" in patch_file_blocks_logic(tmp_path, "a.txt", blocks, CFG)
    assert f.read_bytes() == b"caf\xe9 = 1\n"


def test_write_file_crlf_content_into_crlf_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"old\r\n")
    write_file_tool({"path": "a.txt", "content": "a\r\nb\n"}, tmp_path, CFG)
    assert f.read_bytes() == b"a\r\nb\r\n"


def test_patch_ast_qualified_name(tmp_path):
    src = "class A:\n    def run(self):\n        return 1\n\n\nclass B:\n    def run(self):\n        return 2\n"
    (tmp_path / "m.py").write_text(src)
    assert "Class.method" in patch_file_ast_logic(
        tmp_path, "m.py", "run", "def run(self):\n    return 9\n", CFG
    )
    res = patch_file_ast_logic(
        tmp_path, "m.py", "B.run", "def run(self):\n    return 9\n", CFG
    )
    assert "Successfully" in res
    out = (tmp_path / "m.py").read_text()
    assert "return 1" in out and "return 9" in out and "return 2" not in out


def test_glob_prunes_ignored_and_rejects_escape(tmp_path):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "top.py").write_text("")
    (tmp_path / "pkg" / "sub" / "deep.py").write_text("")
    (tmp_path / "node_modules" / "x" / "skip.py").write_text("")
    out = glob_files_tool({"pattern": "**/*.py"}, tmp_path, CFG)
    assert "top.py" in out and "pkg/sub/deep.py" in out and "skip.py" not in out
    assert glob_files_tool({"pattern": "*.py"}, tmp_path, CFG).strip() == "top.py"
    assert "pkg/sub/deep.py" in glob_files_tool(
        {"pattern": "pkg/**/*.py"}, tmp_path, CFG
    )
    assert "Error" in glob_files_tool({"pattern": "../*"}, tmp_path, CFG)


def test_grep_unresolved_root_and_binary_skip(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("needle here\n")
    (tmp_path / "b.dat").write_bytes(b"\x00\x01needle")
    unresolved = tmp_path / "sub" / ".."
    out = search_grep_tool({"query": "needle"}, unresolved, CFG)
    assert "a.txt:1: needle here" in out
    assert "b.dat" not in out


def test_ssrf_guard_gaps():
    for bad in (
        "http://100.64.0.1/",  # CGNAT, not is_private
        "http://0.0.0.0:8000/",
        "http://[::ffff:10.0.0.1]/",
        "http://10.0.0.5\\@example.com/",  # urllib/Chromium parser differential
        "file:///etc/passwd",
        "http://[::1/",
    ):
        assert _is_url_allowed(bad)[0] is False, bad
    assert _is_url_allowed("http://localhost:3000")[0] is True
    assert _is_url_allowed("http://127.0.0.1:5173/x")[0] is True


def test_ddg_parser_prefers_title_and_unescapes():
    html = (
        '<div class="result results_links results_links_deep web-result ">'
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fexample.com%2Fp&amp;rut=abc">Tom &amp; Jerry&#x27;s</a>'
        '<a class="result__url" href="x">example.com/p</a>'
        '<a class="result__snippet" href="x">A <b>great</b>   snippet</a></div>'
    )
    (r,) = _parse_duckduckgo_results(html, 5)
    assert r == {
        "title": "Tom & Jerry's",
        "url": "https://example.com/p",
        "snippet": "A great snippet",
    }


def test_swarm_message_to_busy_agent_goes_to_inbox(monkeypatch):
    started = []
    monkeypatch.setattr(
        swarm.threading, "Thread", lambda *a, **k: started.append(1) or None
    )
    swarm.ACTIVE_SUBAGENTS["sub_busy"] = {
        "id": "sub_busy",
        "role": "r",
        "status": "RUNNING",
        "output": "",
        "messages": [],
        "inbox": [],
        "runner": object(),  # a worker is live
    }
    swarm.ACTIVE_SUBAGENTS["sub_done"] = {"status": "COMPLETED", "role": "r"}
    try:
        res = swarm.send_message_to_agent_logic("sub_busy", "hi", ".", None)
        assert "Message sent" in res
        sub = swarm.ACTIVE_SUBAGENTS["sub_busy"]
        assert sub["inbox"] == [{"role": "user", "content": "hi"}]
        assert sub["messages"] == [] and not started  # live worker drains it
        assert swarm._running_count() >= 1
        before = swarm._running_count()
        swarm.ACTIVE_SUBAGENTS["sub_done2"] = {"status": "COMPLETED"}
        assert swarm._running_count() == before
    finally:
        for k in ("sub_busy", "sub_done", "sub_done2"):
            swarm.ACTIVE_SUBAGENTS.pop(k, None)
