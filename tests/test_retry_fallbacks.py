import sys
from pathlib import Path

import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.config import Config
from nimcode.core.agent import call_nim_with_retry

FUNCTION_404 = (
    "Error code: 404 - {'status': 404, 'title': 'Not Found', 'detail': "
    "\"Function '04174188-f742-4069-9e72-d77c2b77d3cb': Not found for "
    "account 'JnYycKug2VQjDazQoXI6HwKrI6P0lioecX2ml5jZBzA'\"}"
)


def _api_error(status, msg):
    """A real openai exception, as the SDK raises it for an HTTP status."""
    req = httpx.Request("POST", "https://nim.test/v1/chat/completions")
    resp = httpx.Response(status, request=req, json={"error": {"message": msg}})
    cls = openai.RateLimitError if status == 429 else openai.APIStatusError
    return cls(msg, response=resp, body=None)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    import nimcode.core.agent as agent

    monkeypatch.setattr(agent.time, "sleep", lambda s: None)


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
            raise _as_exc(self.always_error)
        if self.error_on_tools is not None and kwargs.get("tools") is not None:
            raise _as_exc(self.error_on_tools)
        return _FakeResponse()


def _as_exc(err):
    return err if isinstance(err, BaseException) else Exception(err)


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
    comp = _FakeCompletions(error_on_tools=_api_error(404, FUNCTION_404))
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
    comp = _FakeCompletions(always_error=_api_error(404, FUNCTION_404))
    client = _FakeClient(comp)

    with pytest.raises(RuntimeError, match="without function calling"):
        call_nim_with_retry(
            client, _messages(), None, "some-model-xyz", cfg, max_retries=3
        )

    assert len(comp.calls) == 1


def test_function_404_without_tools_skips_fallback():
    cfg = Config()
    comp = _FakeCompletions(always_error=_api_error(404, FUNCTION_404))
    client = _FakeClient(comp)

    with pytest.raises(RuntimeError, match="some-model-xyz"):
        call_nim_with_retry(
            client, _messages(), None, "some-model-xyz", cfg, max_retries=3
        )

    assert cfg.disable_native_tools is False
    assert len(comp.calls) == 1


def test_plain_404_fails_fast_without_retry():
    cfg = Config()
    comp = _FakeCompletions(
        always_error=_api_error(404, "Error code: 404 - model not found")
    )
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="404"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    assert len(comp.calls) == 1


def test_transient_500_still_retries_then_raises():
    cfg = Config()
    comp = _FakeCompletions(
        always_error=_api_error(500, "Error code: 500 - internal server error")
    )
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="500"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    assert len(comp.calls) == 3


def test_rate_limit_503_retries_then_tips():
    cfg = Config()
    comp = _FakeCompletions(
        always_error=_api_error(
            503,
            "Error code: 503 - {'error': {'message': "
            "'ResourceExhausted: Worker local total request limit reached', "
            "'type': 'Service Unavailable', 'code': 503}}",
        )
    )
    client = _FakeClient(comp)

    with pytest.raises(Exception, match="503"):
        call_nim_with_retry(client, _messages(), None, cfg.fast_model, cfg)

    # Transient: full retry budget used, no fast-fail.
    assert len(comp.calls) == 3


def test_4000_tokens_in_5xx_body_is_retried():
    cfg = Config()
    comp = _FakeCompletions(
        always_error=_api_error(500, "upstream failed after generating 4000 tokens")
    )
    with pytest.raises(openai.APIStatusError):
        call_nim_with_retry(_FakeClient(comp), _messages(), None, cfg.fast_model, cfg)
    assert len(comp.calls) == 3


def test_4000_tokens_without_status_is_retried():
    cfg = Config()
    comp = _FakeCompletions(always_error="worker crashed at 4000 tokens")
    with pytest.raises(Exception, match="4000"):
        call_nim_with_retry(_FakeClient(comp), _messages(), None, cfg.fast_model, cfg)
    assert len(comp.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_client_errors_fail_fast(status):
    cfg = Config()
    comp = _FakeCompletions(always_error=_api_error(status, "nope"))
    with pytest.raises(openai.APIStatusError):
        call_nim_with_retry(_FakeClient(comp), _messages(), None, cfg.fast_model, cfg)
    assert len(comp.calls) == 1


def test_rate_limit_and_connection_errors_retry():
    cfg = Config()
    req = httpx.Request("POST", "https://nim.test/v1/chat/completions")
    for err in (
        _api_error(429, "slow down"),
        openai.APIConnectionError(request=req),
        openai.APITimeoutError(request=req),
    ):
        comp = _FakeCompletions(always_error=err)
        with pytest.raises(type(err)):
            call_nim_with_retry(
                _FakeClient(comp), _messages(), None, cfg.fast_model, cfg
            )
        assert len(comp.calls) == 3


def test_context_text_in_5xx_is_not_treated_as_overflow():
    cfg = Config()
    comp = _FakeCompletions(
        always_error=_api_error(503, "context window service unavailable")
    )
    with pytest.raises(openai.APIStatusError):
        call_nim_with_retry(_FakeClient(comp), _messages(), None, cfg.fast_model, cfg)
    # Retried as transient; never recursed with a halved budget.
    assert len(comp.calls) == 3


def test_context_overflow_400_prunes_and_retries():
    cfg = Config()
    calls = []

    class _Comp:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise _api_error(400, "This model's max context length is 8192")
            return _FakeResponse()

    res = call_nim_with_retry(
        _FakeClient(_Comp()), _messages(), None, cfg.fast_model, cfg
    )
    assert isinstance(res, _FakeResponse)
    assert len(calls) == 2
