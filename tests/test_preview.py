import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.base import render_preview, is_docker_available
from nimcode.config import Config


def test_render_preview_execute_command_host():
    config = Config()
    config.disable_docker = True
    workspace = Path.cwd()
    preview = render_preview(
        "execute_command", {"command": "echo hello"}, workspace, config=config
    )
    assert "execute_command (Host)" in preview
    assert "echo hello" in preview


def test_render_preview_run_tests_host():
    config = Config()
    config.disable_docker = True
    workspace = Path.cwd()
    preview = render_preview(
        "run_tests", {"command": "pytest"}, workspace, config=config
    )
    assert "run_tests (Host)" in preview
    assert "pytest" in preview


def test_is_docker_available():
    # Just test it runs without error
    result = is_docker_available()
    assert isinstance(result, bool)


def test_render_preview_patch_file_ast(tmp_path):
    # Create a dummy python file in tmp_path
    py_file = tmp_path / "dummy.py"
    py_file.write_text("def hello():\n    print('hi')\n", encoding="utf-8")

    args = {
        "path": "dummy.py",
        "target_symbol": "hello",
        "replacement_code": "def hello():\n    print('hello world')\n",
    }

    preview = render_preview("patch_file_ast", args, tmp_path)
    assert "[Tool Preview: patch_file_ast]" in preview
    assert "+    print('hello world')" in preview


def test_render_preview_patch_file_blocks(tmp_path):
    # Create a dummy file in tmp_path
    f = tmp_path / "dummy.txt"
    f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")

    blocks = "<<<<<<< SEARCH\nbbb\n=======\nbbb modified\n>>>>>>> REPLACE"
    args = {"path": "dummy.txt", "edit_blocks": blocks}

    preview = render_preview("patch_file_blocks", args, tmp_path)
    assert "[Tool Preview: patch_file_blocks]" in preview
    assert "+bbb modified" in preview
