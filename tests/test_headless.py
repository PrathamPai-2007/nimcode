"""`nimcode run --headless`: real agent loop, no TUI, auto-approve, clean exit + summary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import nimcode.core.agent_tui as at
from nimcode import cli
from nimcode.config import Config
from nimcode.tools import cmd as _cmd
from nimcode.ui.headless import HeadlessTUI, UsageMeter


def _resp(content, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tool_call(name, args):
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


@pytest.fixture
def ws(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def test_headless_run_writes_file_and_summary(ws, monkeypatch):
    replies = [
        _resp(
            "Writing.",
            [_tool_call("write_file", {"path": "out.txt", "content": "hi\n"})],
        ),
        _resp("Done."),
    ]
    monkeypatch.setattr(at, "call_nim_with_retry", lambda *a, **k: replies.pop(0))
    cfg = Config()
    cfg.disable_docker = True
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=None))
    )
    monkeypatch.setattr(
        cli, "_get_client_and_setup", lambda *a, **k: (client, cfg, ws, "")
    )
    out = ws / "summary.json"

    res = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--headless",
            "--mode",
            "build",
            "--model",
            "m/x",
            "--json-out",
            str(out),
            "go",
        ],
    )

    assert res.exit_code == 0, res.output
    assert (ws / "out.txt").read_text() == "hi\n"
    summary = json.loads(out.read_text())
    assert summary["status"] == "agent_finished" and summary["model"] == "m/x"
    assert cfg.fast_model == cfg.plan_model == "m/x"


def test_loop_limit_resumes_then_stops():
    tui = HeadlessTUI(max_resumes=1)

    async def answers():
        return [
            await tui.get_user_confirmation("Resume loop? (y ...)"),
            await tui.get_user_confirmation("Resume loop? (y ...)"),
            await tui.get_user_confirmation("Approve execution of write_file?"),
        ]

    import asyncio

    assert asyncio.run(answers()) == ["y", "n", "y"]
    assert tui.ended_by == "step_limit"


def test_usage_meter_totals_reported_usage():
    usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
    completions = SimpleNamespace(create=lambda **k: SimpleNamespace(usage=usage))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    meter = UsageMeter(client)
    client.chat.completions.create(model="x")
    client.chat.completions.create(model="x")
    assert meter.as_dict() == {
        "api_calls": 2,
        "prompt_tokens": 14,
        "completion_tokens": 6,
        "total_tokens": 20,
    }


def test_no_tests_collected_is_not_a_failure():
    assert _cmd.no_tests_found("Ran 0 tests\nNO TESTS RAN\n[exit code: 5]")
    assert not _cmd.tests_passed("[exit code: 5]")
    assert not _cmd.no_tests_found("1 failed\n[exit code: 1]")
