import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.cmd import execute_command_tool
from nimcode.config import Config


def test_execute_command_host():
    config = Config()
    config.disable_docker = True
    workspace = Path.cwd()
    result = execute_command_tool(
        {"command": "echo hello from host"}, workspace, config
    )
    assert "hello from host" in result
    assert "[exit code: 0]" in result


def test_execute_command_timeout():
    config = Config()
    config.disable_docker = True
    config.timeout = 1
    workspace = Path.cwd()
    result = execute_command_tool(
        {"command": 'python -c "import time; time.sleep(5)"', "timeout": 1},
        workspace,
        config,
    )
    assert "timed out" in result.lower() or "timeout" in result.lower()


def test_execute_command_invalid():
    config = Config()
    config.disable_docker = True
    workspace = Path.cwd()
    result = execute_command_tool({"command": ""}, workspace, config)
    assert "Error: No command provided" in result
