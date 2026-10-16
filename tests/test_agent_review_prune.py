import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.config import Config
from nimcode.core.agent import (
    call_nim_with_retry,
    count_message_tokens,
    parse_xml_tool_calls,
    prune_history,
)


def test_prune_keeps_newest_and_respects_budget():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
    ]
    for i in range(40):
        msgs.append({"role": "assistant", "content": f"old reply {i} " + "x" * 400})
        msgs.append({"role": "user", "content": f"q {i}"})
    res = prune_history(msgs, context_budget_tokens=300, max_turns_retained=5)
    assert res[:2] == msgs[:2]
    assert res[-1] == msgs[-1]  # live turn never dropped
    assert len(res) < len(msgs)  # older units actually dropped


def test_prune_does_not_skip_messages_right_after_first_user():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    # tiny max_turns forces the pruning path; budget fits everything
    res = prune_history(msgs, context_budget_tokens=10_000, max_turns_retained=1)
    assert [m["content"] for m in res] == ["sys", "first", "a", "b"]


def test_token_count_tolerates_special_tokens():
    assert count_message_tokens([{"role": "user", "content": "<|endoftext|>"}]) > 0


def test_xml_empty_args_are_valid_json():
    calls = parse_xml_tool_calls('<tool_call name="list_files"></tool_call>')
    assert calls and calls[0].function.arguments == "{}"


class _Completions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise Exception("maximum context length exceeded")


class _Client:
    def __init__(self):
        self.chat = type("C", (), {})()
        self.chat.completions = _Completions()


def test_context_overflow_terminates():
    cfg = Config()
    cfg.context_budget_tokens = 16000
    client = _Client()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    with pytest.raises(Exception, match="context length"):
        call_nim_with_retry(client, msgs, None, cfg.fast_model, cfg)
    # 16000 -> 8000 -> 4000 -> 2000 -> 1000 then stop
    assert client.chat.completions.calls <= 6
