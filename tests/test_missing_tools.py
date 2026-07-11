import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.base import is_valid_tool_call
from nimcode.tools.fs import list_files_tool, glob_files_tool
from nimcode.config import Config
from nimcode.core.agent import MockToolCall, MockFunction


def test_is_valid_tool_call():
    # Valid read_file call
    func = MockFunction("read_file", '{"path": "file.txt"}')
    tc = MockToolCall("1", "function", func)
    is_valid, name, args = is_valid_tool_call(tc)
    assert is_valid is True
    assert name == "read_file"
    assert args == {"path": "file.txt"}

    # Invalid read_file call (missing arguments)
    func_invalid = MockFunction("read_file", "{}")
    tc_invalid = MockToolCall("2", "function", func_invalid)
    is_valid, name, args = is_valid_tool_call(tc_invalid)
    assert is_valid is False


def test_list_files_tool():
    config = Config()
    workspace = Path.cwd()
    res = list_files_tool({"path": "."}, workspace, config)
    assert "pyproject.toml (File)" in res or "src (Directory)" in res


def test_glob_files_tool():
    config = Config()
    workspace = Path.cwd()
    res = glob_files_tool({"pattern": "pyproject.toml"}, workspace, config)
    assert "pyproject.toml" in res


def test_read_file_tool_line_range():
    from nimcode.tools.fs import read_file_tool

    config = Config()
    workspace = Path.cwd()
    res = read_file_tool(
        {"path": "pyproject.toml", "start_line": 1, "end_line": 3}, workspace, config
    )
    assert "[File: pyproject.toml | Lines 1 to 3" in res
    assert len(res.splitlines()) >= 3


def test_patch_file_blocks_tool():
    from nimcode.tools.fs import patch_file_blocks_tool

    config = Config()
    workspace = Path.cwd()
    dummy_file = workspace / "test_dummy_patch.txt"
    try:
        dummy_file.write_text("aaa\nbbb\nccc\n", encoding="utf-8")

        # Test valid block replacement
        blocks = "<<<<<<< SEARCH\nbbb\n=======\nbbb modified\n>>>>>>> REPLACE"
        res = patch_file_blocks_tool(
            {"path": "test_dummy_patch.txt", "edit_blocks": blocks}, workspace, config
        )
        assert "Successfully applied 1 edit block" in res
        assert dummy_file.read_text(encoding="utf-8") == "aaa\nbbb modified\nccc\n"

        # Test SEARCH block not found
        blocks_missing = "<<<<<<< SEARCH\nmissing\n=======\nfoo\n>>>>>>> REPLACE"
        res_fail = patch_file_blocks_tool(
            {"path": "test_dummy_patch.txt", "edit_blocks": blocks_missing},
            workspace,
            config,
        )
        assert "SEARCH block not found" in res_fail
    finally:
        if dummy_file.exists():
            dummy_file.unlink()


def test_search_grep_tool():
    from nimcode.tools.fs import search_grep_tool

    config = Config()
    workspace = Path.cwd()
    res = search_grep_tool({"query": "dependencies", "path": "."}, workspace, config)
    assert "pyproject.toml" in res


def test_search_symbols_tool():
    from nimcode.tools.fs import search_symbols_tool

    config = Config()
    workspace = Path.cwd()
    res = search_symbols_tool({"query": "is_valid_tool_call"}, workspace, config)
    assert "base.py" in res


def test_gitignore_matching():
    from nimcode.context import is_ignored
    from pathlib import Path

    rules = ["build"]
    assert is_ignored(Path("build"), rules) is True
    assert is_ignored(Path("src/build/index.js"), rules, root=Path(".")) is True
    assert is_ignored(Path("src/builder.py"), rules, root=Path(".")) is False
    assert is_ignored(Path("src/build_utils.py"), rules, root=Path(".")) is False

    rules_slash = ["src/build"]
    assert is_ignored(Path("src/build/file.txt"), rules_slash, root=Path(".")) is True
    assert is_ignored(Path("other/src/build/file.txt"), rules_slash, root=Path(".")) is False

