"""Track P UX: --version, `nimcode config` sources, new Config keys,
subagent timeout / transcripts / reaping (no network)."""

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nimcode.tools.swarm as swarm
from nimcode import __version__
from nimcode.cli import app
from nimcode.config import Config, get_project_nimcode_dir, load_config, save_config


@pytest.fixture
def ws(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    return root.resolve()


def test_version_flag():
    res = CliRunner().invoke(app, ["--version"])
    assert res.exit_code == 0 and res.output.strip() == f"nimcode {__version__}"


def test_config_command_shows_sources(ws):
    (ws / "nimcode.toml").write_text("timeout = 42\n", encoding="utf-8")
    (get_project_nimcode_dir(ws) / "model.json").write_text(
        json.dumps({"fast_model": "a/b"}), encoding="utf-8"
    )
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    lines = {ln.split()[0]: ln for ln in res.output.splitlines() if "=" in ln}
    assert "42" in lines["timeout"] and "[nimcode.toml]" in lines["timeout"]
    assert "'a/b'" in lines["fast_model"] and "[model.json]" in lines["fast_model"]
    assert "[default]" in lines["output_cap"]
    assert "subagent_timeout_s" in lines and "searxng_url" in lines


def test_new_config_keys_validate_and_roundtrip(ws):
    (ws / "nimcode.toml").write_text(
        'subagent_timeout_s = "-5"\nvision_screenshots = "yes"\n'
        'searxng_url = "ftp://nope"\n',
        encoding="utf-8",
    )
    with pytest.warns(UserWarning):
        cfg = load_config()
    assert cfg.subagent_timeout_s == 900
    assert cfg.vision_screenshots is True
    assert cfg.searxng_url == ""

    cfg.subagent_timeout_s = 120
    cfg.searxng_url = "https://searx.example/"
    save_config(cfg, ws)
    again = load_config()
    assert again.subagent_timeout_s == 120
    assert again.vision_screenshots is True
    assert again.searxng_url == "https://searx.example"


def test_reap_finished_subagents():
    now = time.time()
    swarm.ACTIVE_SUBAGENTS.update(
        {
            "sub_old": {"status": "COMPLETED", "ended_at": now - 601},
            "sub_new": {"status": "KILLED", "ended_at": now - 10},
            "sub_live": {"status": "RUNNING"},
        }
    )
    try:
        assert swarm.reap_finished_subagents(now) == 1
        assert "sub_old" not in swarm.ACTIVE_SUBAGENTS
        assert "sub_new" in swarm.ACTIVE_SUBAGENTS
        assert "sub_live" in swarm.ACTIVE_SUBAGENTS
    finally:
        for k in ("sub_old", "sub_new", "sub_live"):
            swarm.ACTIVE_SUBAGENTS.pop(k, None)


class _SlowClient:
    """Each completion takes 1.1s and asks for another (harmless) tool call."""

    def __init__(self, *a, **k):
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        assert kwargs["timeout"] <= 1.0 + 1e-6
        time.sleep(1.1)
        from nimcode.core.agent import MockFunction, MockToolCall

        tc = MockToolCall("t1", "function", MockFunction("list_agents", "{}"))
        msg = type("M", (), {"content": "working", "tool_calls": [tc]})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


def test_subagent_times_out_and_writes_transcript(ws, monkeypatch):
    import openai

    monkeypatch.setenv("NVIDIA_API_KEY", "dummy")
    monkeypatch.setattr(openai, "OpenAI", _SlowClient)
    cfg = Config()
    cfg.subagent_timeout_s = 1
    cfg.mode = "build"
    res = swarm.spawn_agent_logic("Tester", "do the thing", ws, cfg)
    agent_id = res.split("ID: ")[1].split(".")[0]
    try:
        sub = swarm.ACTIVE_SUBAGENTS[agent_id]
        for _ in range(100):
            if sub.get("ended_at"):
                break
            time.sleep(0.05)
        assert sub["status"] == "TIMED_OUT"
        assert sub.get("ended_at")
        path = Path(sub["transcript"])
        assert path.parent == get_project_nimcode_dir(ws) / "subagents"
        lines = [json.loads(ln) for ln in path.read_text("utf-8").splitlines()]
        assert lines[0] == {"role": "user", "content": "do the thing"}
        assert [m["role"] for m in lines[1:3]] == ["assistant", "tool"]
    finally:
        swarm.ACTIVE_SUBAGENTS.pop(agent_id, None)
