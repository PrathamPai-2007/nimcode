import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.config import Config
from nimcode.core.agent import call_nim_with_retry

FUNCTION_404 = (
    "Error code: 404 - {'status': 404, 'title': 'Not Found', 'detail': "
    "\"Function '04174188-f742-4069-9e72-d77c2b77d3cb': Not found for "
    "account 'JnYycKug2VQjDazQoXI6HwKrI6P0lioecX2ml5jZBzA'\"}"
)


class _FakeMessage:
    content = "hello"
    tool_calls = None


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


class _FakeCompletions:
    def __init__(self, error_on_tools=None, always_error=None):
        self.error_on_tools = error_on_tools
        self.always_error = always_error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.always_error is not None:
            raise Exception(self.always_error)
        if self.error_on_tools is not None and kwargs.get("tools") is not None:
            raise Exception(self.error_on_tools)
        return _FakeResponse()


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


def _messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do the thing"},
    ]


def test_function_404_falls_back_to_xml_mode():
    cfg = Config()
    comp = _FakeCompletions(error_on_tools=FUNCTION_404)
    client = _FakeClient(comp)
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    res = call_nim_with_retry(
        client, _messages(), tools, cfg.fast_model, cfg, max_retries=3
    )

    assert isinstance(res, _FakeResponse)
    assert cfg.disable_native_tools is True
    # First attempt sent native tools, retry went out with tools=None.
    assert comp.calls[0].get("tools") == tools
    assert "tools" not in comp.calls[1]
    assert len(comp.calls) == 2


def test_function_404_in_xml_mode_raises_actionable():
    cfg = Config()
    cfg.disable_native_tools = True
    comp = _FakeCompletions(always_error=FUNCTION_404)
    client = _FakeClient(comp)

    with pytest.raises(RuntimeError, match="without function calling"):
        call_nim_with_retry(
            client, _messages(), None, "some-model-xyz", cfg, max_retries=3
        )

    assert len(comp.calls) == 1


def test_function_404_without_tools_skips_fallback():
    cfg = Config()
    comp = _FakeCompletions(always_error=FUNCTION_404)
    client = _FakeClient(comp)

    with pytest.raises(RuntimeError, match="some-model-xyz"):
        call_nim_with_retry(
            client, _messages(), None, "some-model-xyz", cfg, max_retries=3
        )

    assert cfg.disable_native_tools is False
    assert len(comp.calls) == 1


def test_plain_404_fails_fast_without_retry():
    cfg = Config()
    comp = _FakeCompletions(always_error="Error code: 404 - model not found")
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="404"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    assert len(comp.calls) == 1


def test_transient_500_still_retries_then_raises():
    cfg = Config()
    comp = _FakeCompletions(always_error="Error code: 500 - internal server error")
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="500"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    assert len(comp.calls) == 3


def test_rate_limit_503_retries_then_tips():
    cfg = Config()
    comp = _FakeCompletions(
        always_error="Error code: 503 - {'error': {'message': "
        "'ResourceExhausted: Worker local total request limit reached', "
        "'type': 'Service Unavailable', 'code': 503}}"
    )
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="503"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    # Transient: full retry budget used, no fast-fail.
    assert len(comp.calls) == 3
