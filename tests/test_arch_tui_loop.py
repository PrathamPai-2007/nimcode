"""Agent-loop wiring: off-loop tools, mid-batch Shift+Tab, skill install gate,
sidecar binary discovery (plan items 2, 9, 10)."""

import asyncio
import os
import queue
import sys
from pathlib import Path

import pytest

import nimcode.core.agent_tui as at
import nimcode.ui.rust_bridge as rb
from nimcode.config import Config
from nimcode.core.agent import MockFunction, MockToolCall


class FakeTUI:
    """Just enough of RustTUI for _run_agent_loop_tui_internal."""

    def __init__(self, config, inputs=(), confirms=()):
        self.config = config
        self.mode = "Build Mode"
        self._interrupted = False
        self.inputs = list(inputs)
        self.confirms = list(confirms)
        self.confirm_prompts = []
        self.messages = []
        self.polls = 0
        self.on_poll = None

    def add_message(self, role, content, markdown=True):
        self.messages.append((role, content))

    def poll_events(self):
        self.polls += 1
        if self.on_poll:
            self.on_poll(self)

    async def get_user_input(self, prompt="", _drop_typeahead=False):
        return self.inputs.pop(0) if self.inputs else None

    async def get_user_confirmation(self, prompt):
        self.confirm_prompts.append(prompt)
        return self.confirms.pop(0) if self.confirms else "n"

    def update_mode(self, mode):
        self.mode = mode

    def __getattr__(self, name):  # update_tokens, set_working, ...
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
    (root / "a.txt").write_text("hello", encoding="utf-8")
    return root.resolve()


def _run(tui, cfg, ws, prompt, state=None):
    asyncio.run(
        at._run_agent_loop_tui_internal(
            tui, None, prompt, cfg, ws, "", initial_state=state
        )
    )


def test_shift_tab_into_plan_mid_batch_refuses_remaining_writes(ws, monkeypatch):
    cfg = Config()
    cfg.disable_native_tools = False
    calls = [
        MockToolCall(
            f"c{i}",
            "function",
            MockFunction("write_file", '{"path": "a.txt", "content": "x"}'),
        )
        for i in range(2)
    ]
    responses = [_Resp("", calls), _Resp("done")]
    monkeypatch.setattr(at, "call_nim_with_retry", lambda *a, **k: responses.pop(0))
    ran = []

    def fake_exec(name, args, root, config):
        ran.append(threading_name())
        return "ok"

    monkeypatch.setattr(at, "execute_local_tool", fake_exec)

    tui = FakeTUI(cfg, inputs=["", None])

    def flip_after_first_tool(t):
        if t.polls == 2:  # user pressed Shift+Tab while tool 1 ran
            t.config.mode = "plan"

    tui.on_poll = flip_after_first_tool
    _run(tui, cfg, ws, "do it", state="EXECUTING")

    assert len(ran) == 1  # tool 1 auto-ran in Build, tool 2 refused in Plan
    assert tui.confirm_prompts == []
    # Tools run off the event loop thread.
    assert ran[0] != "MainThread"


def threading_name():
    import threading

    return threading.current_thread().name


def test_install_skill_previews_and_discards_on_no(ws, monkeypatch):
    import nimcode.skills as skills

    staged = ws / "staging"
    events = []
    monkeypatch.setattr(skills, "fetch_skill", lambda url: (staged, "PREVIEW", "abc"))
    monkeypatch.setattr(
        skills, "discard_skill", lambda d: events.append(("discard", d))
    )
    monkeypatch.setattr(
        skills, "commit_skill", lambda *a: events.append(("commit",) + a) or "ok"
    )
    cfg = Config()
    tui = FakeTUI(cfg, confirms=["n"])
    _run(tui, cfg, ws, "/install-skill https://example.com/my-skill.zip")
    assert ("tool", "PREVIEW") in tui.messages
    assert events == [("discard", staged)]

    events.clear()
    tui = FakeTUI(cfg, confirms=["y"])
    _run(tui, cfg, ws, "/install-skill https://example.com/my-skill.zip")
    assert events and events[0][0] == "commit" and events[0][1] == staged


def test_install_skill_fetch_error_is_shown(ws, monkeypatch):
    import nimcode.skills as skills

    def boom(url):
        raise ValueError("refusing to install skill: private host")

    monkeypatch.setattr(skills, "fetch_skill", boom)
    cfg = Config()
    tui = FakeTUI(cfg)
    _run(tui, cfg, ws, "/install-skill http://10.0.0.1/x.zip")
    assert any("private host" in c for _, c in tui.messages)
    assert not tui.confirm_prompts


def test_unreadable_skill_shows_error_not_prompt_injection(ws, monkeypatch):
    import nimcode.skills as skills

    monkeypatch.setattr(skills, "list_skills", lambda: ["broken"])
    monkeypatch.setattr(skills, "get_skill_content", lambda name: None)
    monkeypatch.setattr(
        at, "call_nim_with_retry", lambda *a, **k: pytest.fail("must not call model")
    )
    cfg = Config()
    tui = FakeTUI(cfg)
    _run(tui, cfg, ws, "/broken do stuff")
    assert any("could not be read" in c for _, c in tui.messages)


def test_poll_events_applies_mode_and_interrupt_keeps_input(monkeypatch):
    monkeypatch.setattr(rb, "find_rust_binary", lambda: Path(sys.executable))
    cfg = Config()
    cfg.mode = "plan"
    tui = rb.RustTUI(cfg)
    for ev in (
        {"type": "mode_cycle", "mode": "Build Mode"},
        {"type": "input", "text": "later"},
        {"type": "interrupt"},
    ):
        tui._events.put(ev)
    tui.poll_events()
    assert cfg.mode == "build" and tui._interrupted is True
    assert tui._events.get_nowait() == {"type": "input", "text": "later"}
    with pytest.raises(queue.Empty):
        tui._events.get_nowait()


def test_find_rust_binary_order_and_error(monkeypatch, tmp_path):
    fake_mod = tmp_path / "src" / "nimcode" / "ui" / "rust_bridge.py"
    bundled = tmp_path / "src" / "nimcode" / "bin" / rb.BINARY_NAME
    built = tmp_path / "tui-rs" / "target" / "release" / rb.BINARY_NAME
    for p in (bundled, built):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    monkeypatch.setattr(rb, "__file__", str(fake_mod))
    monkeypatch.delenv("NIM_TUI_BIN", raising=False)
    monkeypatch.setattr(rb.shutil, "which", lambda name: None)

    assert rb.BINARY_NAME == ("nim-tui.exe" if os.name == "nt" else "nim-tui")
    assert rb.find_rust_binary() == bundled
    env_bin = tmp_path / "custom"
    env_bin.write_bytes(b"")
    monkeypatch.setenv("NIM_TUI_BIN", str(env_bin))
    assert rb.find_rust_binary() == env_bin
    monkeypatch.delenv("NIM_TUI_BIN")
    bundled.unlink()
    assert rb.find_rust_binary() == built
    built.unlink()
    assert rb.find_rust_binary() is None
    with pytest.raises(RuntimeError) as ei:
        rb.make_tui()
    msg = str(ei.value)
    assert "NIM_TUI_BIN" in msg and "cargo build --release" in msg and "wheel" in msg


def _scripted(monkeypatch, responses):
    sent = []

    def fake_call(client, messages, tools, *a, **k):
        sent.append(tools)
        return responses.pop(0)

    monkeypatch.setattr(at, "call_nim_with_retry", fake_call)
    ran = []

    def fake_exec(name, args, root, config):
        ran.append(name)
        return "ok"

    monkeypatch.setattr(at, "execute_local_tool", fake_exec)
    return sent, ran


def _call(name, args):
    return MockToolCall("c1", "function", MockFunction(name, args))


def test_plan_mode_reads_without_prompt_then_proposes(ws, monkeypatch):
    cfg = Config()
    cfg.disable_native_tools = False
    sent, ran = _scripted(
        monkeypatch,
        [
            _Resp("", [_call("read_file", '{"path": "a.txt"}')]),
            _Resp("<plan>1. edit a.txt</plan>"),
        ],
    )
    tui = FakeTUI(cfg, inputs=[None], confirms=["n"])
    _run(tui, cfg, ws, "look around", state="AWAITING_PLAN_APPROVAL")

    assert ran == ["read_file"]
    names = {t["function"]["name"] for t in sent[0]}
    assert "read_file" in names and "write_file" not in names
    assert (
        len(tui.confirm_prompts) == 1 and "Approve this plan" in tui.confirm_prompts[0]
    )


def test_plan_mode_refuses_a_hallucinated_write(ws, monkeypatch):
    cfg = Config()
    cfg.disable_native_tools = False
    _, ran = _scripted(
        monkeypatch,
        [
            _Resp("", [_call("write_file", '{"path": "x.py", "content": "1"}')]),
            _Resp("sorry"),
        ],
    )
    tui = FakeTUI(cfg, inputs=[None])
    _run(tui, cfg, ws, "do it", state="AWAITING_PLAN_APPROVAL")

    assert ran == [] and tui.confirm_prompts == []
    assert any("Plan mode is read-only" in c for _, c in tui.messages)


def test_build_mode_writes_silently_but_asks_for_env(ws, monkeypatch):
    cfg = Config()
    cfg.disable_native_tools = False
    cfg.auto_test = False
    _, ran = _scripted(
        monkeypatch,
        [
            _Resp("", [_call("write_file", '{"path": "x.py", "content": "1"}')]),
            _Resp("", [_call("write_file", '{"path": ".env", "content": "K=1"}')]),
            _Resp("done"),
        ],
    )
    tui = FakeTUI(cfg, inputs=[None], confirms=["y"])
    _run(tui, cfg, ws, "do it", state="EXECUTING")

    assert ran == ["write_file", "write_file"]
    assert len(tui.confirm_prompts) == 1 and "sensitive file" in tui.confirm_prompts[0]
