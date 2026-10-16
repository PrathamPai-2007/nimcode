import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import nimcode.ui.rust_bridge as rb
from nimcode.config import Config
from nimcode.core.agent_tui import close_dangling_tool_calls


def _tui(monkeypatch):
    monkeypatch.setattr(rb, "find_rust_binary", lambda: Path(sys.executable))
    return rb.RustTUI(Config())


def test_parse_legacy_backup_names():
    from nimcode.tools.base import _parse_backup_name

    assert _parse_backup_name("1712_4321_src_a.py")[1:] == ("src_a.py", "")
    assert _parse_backup_name("1712_4321_blocks_src_a.py")[1:] == ("src_a.py", "blocks")
    assert _parse_backup_name("1712_src_a.py")[1:] == ("src_a.py", "")


def test_stale_interrupt_does_not_answer_next_prompt(monkeypatch):
    tui = _tui(monkeypatch)
    # Ctrl+C pressed while the model was working, then the user types.
    tui._events.put({"type": "interrupt"})

    async def feed():
        await asyncio.sleep(0.01)
        tui._events.put({"type": "input", "text": "hello"})

    async def main():
        asyncio.ensure_future(feed())
        return await tui.get_user_input()

    assert asyncio.run(main()) == "hello"


def test_confirmation_ignores_typeahead(monkeypatch):
    tui = _tui(monkeypatch)
    tui._events.put({"type": "input", "text": "y"})  # typed before the gate

    async def feed():
        await asyncio.sleep(0.01)
        tui._events.put({"type": "input", "text": "n"})

    async def main():
        asyncio.ensure_future(feed())
        return await tui.get_user_confirmation("Approve?")

    assert asyncio.run(main()) == "n"


def test_select_rejects_out_of_range(monkeypatch):
    tui = _tui(monkeypatch)

    async def feed():
        await asyncio.sleep(0.01)
        tui._events.put({"type": "select_result", "index": 7})

    async def main():
        asyncio.ensure_future(feed())
        return await tui._select("t", ["a", "b"])

    assert asyncio.run(main()) is None


def test_sidecar_dead_is_sticky(monkeypatch):
    tui = _tui(monkeypatch)
    tui._events.put({"type": "sidecar_dead"})
    for _ in range(2):
        try:
            asyncio.run(tui._next_event())
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "nim-tui terminated" in str(e)


def test_exit_before_start_prevents_spawn_and_buffering(monkeypatch):
    tui = _tui(monkeypatch)
    tui.exit()
    asyncio.run(tui.start_async())  # must return without spawning
    assert tui._proc is None
    tui.add_message("system", "late")
    assert tui._pending == []


def test_close_dangling_tool_calls():
    msgs = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "a"}, {"id": "b"}],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert close_dangling_tool_calls(msgs) == 1
    assert msgs[-1]["tool_call_id"] == "b"
    assert close_dangling_tool_calls(msgs) == 0
    # Non-trailing / non-tool assistant turns are left alone.
    assert close_dangling_tool_calls([{"role": "user", "content": "x"}]) == 0
