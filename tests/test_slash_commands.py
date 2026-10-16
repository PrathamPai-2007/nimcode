import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.config import Config
from nimcode.core.agent_tui import SLASH_COMMANDS


def test_slash_catalog_sane():
    assert len(SLASH_COMMANDS) == len(set(SLASH_COMMANDS)), "duplicate entries"
    for cmd in SLASH_COMMANDS:
        assert cmd.startswith("/"), cmd
        assert " " not in cmd, cmd
    # Core commands the loop dispatches must all be advertised.
    for required in (
        "/help",
        "/plan",
        "/build",
        "/yolo",
        "/model",
        "/config",
        "/changes",
        "/restore",
        "/test",
        "/team",
        "/kill",
        "/compact",
        "/stats",
        "/skills",
        "/init",
        "/resume",
        "/exit",
    ):
        assert required in SLASH_COMMANDS, required


def test_push_completions_payload(monkeypatch):
    from nimcode.ui.rust_bridge import RustTUI, find_rust_binary

    assert find_rust_binary() is not None, "bundled nim-tui binary missing"
    tui = RustTUI(Config())
    assert tui._pending == []

    import nimcode.skills as skills

    monkeypatch.setattr(skills, "list_skills", lambda: ["recap"])
    tui.push_completions()

    assert len(tui._pending) == 1
    payload = tui._pending[0]
    assert payload["type"] == "set_completions"
    for cmd in SLASH_COMMANDS:
        assert cmd in payload["items"]
    assert "/recap" in payload["items"]
