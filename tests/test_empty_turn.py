import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.core.agent_tui import MAX_EMPTY_TURNS, is_empty_turn


class _FakeCall:
    pass


def test_empty_turn_detection():
    assert is_empty_turn("", None) is True
    assert is_empty_turn("   ", []) is True
    assert is_empty_turn(None, None) is True
    assert is_empty_turn("hi", None) is False
    assert is_empty_turn("", [_FakeCall()]) is False
    assert is_empty_turn("  text  ", []) is False


def test_empty_retry_budget_sane():
    assert MAX_EMPTY_TURNS >= 1
