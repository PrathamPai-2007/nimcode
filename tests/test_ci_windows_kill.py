import sys
import time
import types

import pytest

from nimcode.tools.cmd import execute_command_tool


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill /T tree kill")
def test_timeout_kills_windows_process_tree(tmp_path):
    # ping is a grandchild (shell -> cmd -> ping) holding the output pipe; only a
    # tree kill lets communicate() return near the timeout instead of after 30s.
    cfg = types.SimpleNamespace(disable_docker=True, timeout=30, output_cap=100000)
    start = time.monotonic()
    out = execute_command_tool(
        {"command": 'cmd /c "ping -n 30 127.0.0.1"', "timeout": 2}, tmp_path, cfg
    )
    assert "timed out" in out
    assert time.monotonic() - start < 8
