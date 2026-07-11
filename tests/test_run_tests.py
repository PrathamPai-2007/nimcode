import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.cmd import run_tests_tool
from nimcode.config import Config


def test_run_tests_host():
    config = Config()
    config.disable_docker = True
    workspace = Path.cwd()
    result = run_tests_tool({"command": "echo test command"}, workspace, config)
    assert "test command" in result
    assert "[exit code: 0]" in result


def test_run_tests_default_command():
    config = Config()
    config.disable_docker = True
    config.timeout = 5
    workspace = Path.cwd()
    result = run_tests_tool(
        {"command": "pytest --collect-only", "timeout": 5}, workspace, config
    )
    assert "[exit code:" in result
