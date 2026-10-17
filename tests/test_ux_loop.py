"""Track P UX: in-place /resume, real /compact, /help coverage, one-line
welcome, vision screenshots (fake TUI, mocked model; no network)."""

import asyncio
import json
from pathlib import Path

import pytest

import nimcode.core.agent as agent
import nimcode.core.agent_tui as at
from nimcode.config import Config, get_project_nimcode_dir
from nimcode.core.agent import (
    MockFunction,
    MockToolCall,
    count_message_tokens,
    load_session,
    log_turn,
    normalize_messages,
    prepare_resumed_messages,
)
from nimcode.ui.rust_bridge import SUMMARY_PROMPT


class FakeTUI:
    def __init__(self, config, inputs=(), session=None):
        self.config = config
        self.mode = "Build Mode"
        self._interrupted = False
        self.inputs = list(inputs)
        self.session = session
        self.messages = []
        self.cleared = 0

    def add_message(self, role, content, markdown=True):
        self.messages.append((role, content))

    def clear_messages(self):
        self.cleared += 1
        self.messages = []

    async def get_user_input(self, prompt="", _drop_typeahead=False):
        return self.inputs.pop(0) if self.inputs else None

    async def get_user_confirmation(self, prompt):
        return "n"

    async def select_session(self, config, workspace_root):
        return self.session

    def update_mode(self, mode):
        self.mode = mode

    def __getattr__(self, name):  # update_tokens, set_working, poll_events...
        return lambda *a, **k: None


class _Resp:
    def __init__(self, content, tool_calls=None):
        msg = type("M", (), {"content": content, "tool_calls": tool_calls})()
        self.choices = [type("C", (), {"message": msg})()]


@pytest.fixture
def ws(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def _run(tui, cfg, ws, prompt, state=None):
    asyncio.run(
        at._run_agent_loop_tui_internal(
            tui, None, prompt, cfg, ws, "", initial_state=state
        )
    )


def _sessions(ws):
    return get_project_nimcode_dir(ws) / "sessions"


def test_resume_in_place_rerenders_and_continues_selected_file(ws, monkeypatch):
    sdir = _sessions(ws)
    sdir.mkdir(parents=True, exist_ok=True)
    old = sdir / "session_20200101_000000.jsonl"
    for m in (
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ):
        log_turn(m, old)

    seen = []

    def fake_call(client, messages, *a, **k):
        seen.append([m.get("content") for m in messages])
        return _Resp("new answer")

    monkeypatch.setattr(at, "call_nim_with_retry", fake_call)
    tui = FakeTUI(Config(), inputs=["/resume", "follow up", None], session=old)
    _run(tui, Config(), ws, "")

    assert tui.cleared == 1
    shown = [c for _, c in tui.messages]
    assert "old question" in shown and "old answer" in shown
    # The model saw the resumed history, and new turns went to the old file.
    assert "old question" in seen[0] and "follow up" in seen[0]
    contents = [m["content"] for m in load_session(old)]
    assert contents[-2:] == ["follow up", "new answer"]


def test_compact_writes_summary_and_marker(ws, monkeypatch):
    def fake_call(client, messages, tools, model, config, **k):
        if messages[-1].get("content") == SUMMARY_PROMPT:
            return _Resp("# Summary\nuser asked for X")
        return _Resp("did X")

    monkeypatch.setattr(at, "call_nim_with_retry", fake_call)
    monkeypatch.setattr(agent, "call_nim_with_retry", fake_call)
    cfg = Config()
    tui = FakeTUI(cfg, inputs=["/compact", None])
    _run(tui, cfg, ws, "do X")

    summary = get_project_nimcode_dir(ws) / "summary.md"
    assert summary.read_text(encoding="utf-8") == "# Summary\nuser asked for X"
    (session,) = _sessions(ws).glob("*.jsonl")
    logged = load_session(session)
    assert any(m.get(agent.COMPACT_MARKER_KEY) for m in logged)
    resumed = prepare_resumed_messages(logged)
    assert resumed[0]["role"] == "system" and resumed[0]["content"] == ""
    assert len(resumed) == 2 and "user asked for X" in resumed[1]["content"]
    assert "do X" not in json.dumps(resumed)
    assert any("Compacted" in c for _, c in tui.messages)


def test_compact_failure_leaves_history(ws, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(at, "call_nim_with_retry", lambda *a, **k: _Resp("ok"))
    monkeypatch.setattr(agent, "call_nim_with_retry", boom)
    tui = FakeTUI(Config(), inputs=["/compact", None])
    _run(tui, Config(), ws, "hello")
    assert any("Compact failed" in c for _, c in tui.messages)
    assert not (get_project_nimcode_dir(ws) / "summary.md").exists()


def test_help_covers_every_command_and_key_list():
    from nimcode.ui.console import COMMAND_HELP, KEY_BINDINGS, show_help

    text = " ".join(c for c, _ in COMMAND_HELP)
    for cmd in at.SLASH_COMMANDS:
        assert cmd in text.split(), cmd
    keys = " ".join(k for k, _ in KEY_BINDINGS)
    for k in ("Shift+Enter", "Ctrl+O", "Ctrl+W", "PgUp", "Ctrl+T", "Ctrl+D"):
        assert k in keys

    class T:
        out = []

        def add_message(self, role, content, markdown=True):
            self.out.append(content)

    show_help(tui=T())
    assert "/models-refresh" in T.out[0] and "Ctrl+O" in T.out[0]


def test_welcome_is_one_line(monkeypatch, tmp_path):
    from nimcode.ui.rust_bridge import RustTUI

    fake_bin = tmp_path / "nim-tui"
    fake_bin.write_text("")
    monkeypatch.setenv("NIM_TUI_BIN", str(fake_bin))
    tui = RustTUI(Config())
    tui.show_welcome("C:/ws")
    (msg,) = tui._pending
    assert msg["type"] == "add_message" and "\n" not in msg["content"]


@pytest.mark.parametrize(
    "result",
    [
        "Saved.\n[screenshot: shots/a.png]",
        "[Screenshot:   shots/a.png  ]\nok",
        "x\n  [screenshot: `shots/a.png`]  \n",
    ],
)
def test_parse_screenshot_path_tolerant(result):
    assert at.parse_screenshot_path(result) == "shots/a.png"


def test_parse_screenshot_path_absent():
    assert at.parse_screenshot_path("no image here") is None
    assert at.parse_screenshot_path(None) is None


def test_vision_attaches_image_and_logs_path(ws, monkeypatch):
    png = ws / "shot.png"
    png.write_bytes(b"\x89PNG fake")
    call = MockToolCall(
        "c1",
        "function",
        MockFunction("screenshot_page", '{"url": "http://localhost:3000"}'),
    )
    responses = [_Resp("", [call]), _Resp("looks fine")]
    seen = []

    def fake_call(client, messages, *a, **k):
        seen.append(json.loads(json.dumps(messages)))
        return responses.pop(0)

    monkeypatch.setattr(at, "call_nim_with_retry", fake_call)
    monkeypatch.setattr(
        at,
        "execute_local_tool",
        lambda *a, **k: "Captured.\n[screenshot: shot.png]",
    )
    monkeypatch.setattr(at, "render_preview", lambda *a, **k: "preview")
    cfg = Config()
    cfg.vision_screenshots = True
    tui = FakeTUI(cfg, inputs=[None])
    _run(tui, cfg, ws, "check the page", state="EXECUTING")

    last = seen[1][-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    url = last["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # Session log stores the path, never the base64 payload.
    (session,) = _sessions(ws).glob("*.jsonl")
    raw = session.read_text(encoding="utf-8")
    assert "base64" not in raw and "shot.png" in raw
    # Resuming flattens the logged image part into text.
    resumed = prepare_resumed_messages(load_session(session))
    assert all(isinstance(m.get("content"), str) for m in resumed)


def test_vision_off_by_default(ws, monkeypatch):
    call = MockToolCall(
        "c1", "function", MockFunction("screenshot_page", '{"url": "http://x.io"}')
    )
    responses = [_Resp("", [call]), _Resp("ok")]
    seen = []
    monkeypatch.setattr(
        at,
        "call_nim_with_retry",
        lambda c, m, *a, **k: seen.append(list(m)) or responses.pop(0),
    )
    (ws / "s.png").write_bytes(b"x")
    monkeypatch.setattr(at, "execute_local_tool", lambda *a, **k: "[screenshot: s.png]")
    monkeypatch.setattr(at, "render_preview", lambda *a, **k: "preview")
    _run(FakeTUI(Config(), inputs=[None]), Config(), ws, "go", state="EXECUTING")
    assert seen[1][-1]["role"] == "tool"


def test_image_parts_counted_and_merged():
    img = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "A" * 400000},
    }
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, img]}]
    assert count_message_tokens(msgs) < 2000
    merged = normalize_messages(
        [{"role": "user", "content": "first"}] + msgs, disable_native_tools=True
    )
    assert merged[-1]["content"][0] == {"type": "text", "text": "first"}
    assert merged[-1]["content"][-1]["type"] == "image_url" and len(merged) == 1
