import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.base import (  # noqa: E402
    is_valid_tool_call,
    resolve_and_validate_path,
    scrub_env,
    scrub_text,
    truncate_output,
)
from nimcode.tools.cmd import (  # noqa: E402
    _docker_run_argv,
    _resolve_use_docker,
    _safe_docker_extra_args,
    execute_command_tool,
    run_tests_tool,
)
from nimcode.tools.registry import ToolSandboxError  # noqa: E402


def _cfg(**kw):
    base = dict(disable_docker=True, timeout=30, output_cap=100000)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_scrub_text_does_not_mangle_hyphenated_words_or_flags():
    for s in (
        "mkdir -p build",
        "pre-processing done",
        "find . -name x -print",
        "docker run -p 3000:3000 img",
        "basic usage: run it",
    ):
        assert scrub_text(s) == s


def test_scrub_text_redacts_secrets():
    cases = [
        "mysql -uroot -phunter2 db",
        "curl --password=hunter2 x",
        "curl --token hunter2 x",
        "Authorization: Bearer abc+def/ghi==",
        "Authorization: Basic dXNlcjpwYXNz",
        "NVIDIA_API_KEY=nvapi-AbCdEf_0123456789-xyzXYZ",
        "postgresql+psycopg2://u:hunter2@db/x",
        "https://user:hunter2@example.com/repo.git",
        "DB_PASSWORD=hunter2",
    ]
    for s in cases:
        out = scrub_text(s)
        assert "hunter2" not in out and "dXNlcjpwYXNz" not in out, out
        assert "nvapi-AbCd" not in out and "def/ghi" not in out, out


def test_scrub_env_drops_sensitive_keys():
    env = scrub_env({"GITHUB_TOKEN": "x", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_truncate_output_exact_length_and_garbage_cap():
    text = "A" * 1000
    assert len(truncate_output(text, 300)) == 300
    assert truncate_output("abc", "5000") == "abc"  # str cap from toml


def test_is_valid_tool_call_non_object_args():
    tc = types.SimpleNamespace(
        function=types.SimpleNamespace(name="read_file", arguments="[1, 2]")
    )
    ok, name, args = is_valid_tool_call(tc)
    assert not ok and name == "read_file" and isinstance(args, dict)


def test_resolve_and_validate_path_rejects_escape_and_nul(tmp_path):
    with pytest.raises(ToolSandboxError):
        resolve_and_validate_path("../x", tmp_path)
    with pytest.raises(ToolSandboxError):
        resolve_and_validate_path("a\x00b", tmp_path)


def test_resolve_and_validate_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    ws = tmp_path / "ws"
    outside.mkdir()
    ws.mkdir()
    try:
        (ws / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted")
    with pytest.raises(ToolSandboxError):
        resolve_and_validate_path("link/secret.txt", ws)


def test_docker_extra_args_denylist():
    cfg = _cfg(
        docker_extra_args=[
            "--memory=512m",
            "-v/:/host",
            "-itv=/:/h",
            "--network",
            "host",
            "--pid=host",
            "--net host",
            "--device=/dev/sda",
            "--security-opt=seccomp=unconfined",
            "--privileged",
            "--network=bridge",
        ]
    )
    assert _safe_docker_extra_args(cfg) == ["--memory=512m", "--network=bridge"]


def test_docker_image_cannot_be_a_flag(tmp_path):
    argv = _docker_run_argv(
        ["--rm"], "echo", tmp_path, _cfg(docker_image="--privileged")
    )
    assert "--privileged" not in argv
    assert argv[-4] == "python:3.11-slim"


def test_subagent_cannot_force_host_when_docker_usable(monkeypatch):
    import nimcode.tools.cmd as cmd

    monkeypatch.setattr(cmd, "is_docker_available", lambda: True)
    assert _resolve_use_docker(False, _cfg(disable_docker=False)) is False
    sub = _cfg(disable_docker=False, _subagent_depth=1)
    assert _resolve_use_docker(False, sub) is True
    assert _resolve_use_docker("false", _cfg(disable_docker=True)) is False


def test_host_command_does_not_inherit_stdin(tmp_path):
    out = execute_command_tool(
        {
            "command": f'"{sys.executable}" -c "import sys; print(repr(sys.stdin.read()))"'
        },
        tmp_path,
        _cfg(),
    )
    assert "''" in out and "[exit code: 0]" in out


def test_run_tests_empty_command_defaults_to_pytest(tmp_path, monkeypatch):
    import nimcode.tools.cmd as cmd

    seen = {}

    def fake(command, *a, **k):
        seen["cmd"] = command
        return {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False}

    monkeypatch.setattr(cmd, "_execute_command", fake)
    run_tests_tool({"command": ""}, tmp_path, _cfg())
    assert seen["cmd"] == "pytest"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group kill")
def test_timeout_kills_grandchildren_holding_pipe(tmp_path):
    import time

    start = time.monotonic()
    out = execute_command_tool(
        {"command": "sleep 30 & sleep 30", "timeout": 1}, tmp_path, _cfg()
    )
    assert "timed out" in out
    assert time.monotonic() - start < 15


def test_tests_passed_uses_final_exit_marker():
    from nimcode.tools import tests_passed

    assert tests_passed("ok\n[exit code: 0]")
    assert not tests_passed("log says exit code: 0\n[exit code: 1]")
    assert not tests_passed("[exit code: 0] echoed\n[exit code: 2]")
    assert not tests_passed("no marker")
