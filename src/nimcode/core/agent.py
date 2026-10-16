import sys
import json
import re
import time
import functools
import typer
from pathlib import Path
from typing import List, Dict, Any, Optional
from openai import APIConnectionError, OpenAI, RateLimitError

# Optional tiktoken for accurate token counting
try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

from nimcode.config import Config, get_project_nimcode_dir
from nimcode.config_models import effective_budget
from nimcode.core.prompts import (
    build_system_prompt,
    strip_reasoning_spans,
)
from nimcode.ui.output import echo, secho


SESSION_DIR = "sessions"


class MockFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class MockToolCall:
    def __init__(self, id: str, type: str, function: MockFunction):
        self.id = id
        self.type = type
        self.function = function


class MockMessage:
    def __init__(
        self, content: Optional[str], tool_calls: Optional[List[MockToolCall]]
    ):
        self.content = content
        self.tool_calls = tool_calls


def _clean_json_args(raw: str) -> str:
    """Strip markdown code fences and whitespace that models sometimes add around JSON."""
    import re

    # Only strip leading/trailing fences — never touch backticks inside JSON strings.
    raw = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```\s*$", "", raw)
    return raw.strip()


def parse_xml_tool_calls(text: str) -> List[MockToolCall]:
    import re
    import uuid

    tool_calls = []
    # Match <tool_call name="...">...</tool_call> with quoted names (strict).
    matches = re.finditer(
        r"<tool_call\s+name=(?:\"([^\"]+)\"|'([^']+)')\s*>(.*?)</tool_call>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    for match in matches:
        name = (match.group(1) or match.group(2) or "").strip()
        if not name:
            continue
        args_str = _clean_json_args(match.group(3)) or "{}"
        try:
            json.loads(args_str)
        except Exception:
            continue
        tc_id = f"call_xml_{uuid.uuid4().hex[:8]}"
        tool_calls.append(MockToolCall(tc_id, "function", MockFunction(name, args_str)))
    return tool_calls


def clean_surrogates(val: Any) -> Any:
    if isinstance(val, str):
        try:
            val = val.encode("utf-16", "surrogatepass").decode("utf-16")
        except Exception:
            pass
        return "".join(c for c in val if not (0xD800 <= ord(c) <= 0xDFFF))
    elif isinstance(val, list):
        return [clean_surrogates(x) for x in val]
    elif isinstance(val, tuple):
        return tuple(clean_surrogates(x) for x in val)
    elif isinstance(val, dict):
        return {clean_surrogates(k): clean_surrogates(v) for k, v in val.items()}
    return val


def normalize_messages(
    messages: List[Dict[str, Any]], disable_native_tools: bool = False
) -> List[Dict[str, Any]]:
    """
    Normalizes the message list to satisfy API constraints like alternating user/assistant roles,
    and requirements of specific models (like Zhipu GLM and MiniMax) where system messages must
    appear exactly once at the beginning, and the sequence of roles must be strictly alternating.
    """
    messages = clean_surrogates(messages)
    if not messages:
        return []

    # 1. Map messages (handle tool disabling if active)
    mapped_messages = []
    for msg in messages:
        msg_copy = dict(msg)
        if disable_native_tools:
            if msg_copy.get("role") == "assistant":
                content = msg_copy.get("content") or ""
                if msg_copy.get("tool_calls"):
                    for tc in msg_copy["tool_calls"]:
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            name = (
                                func.get("name", "")
                                if isinstance(func, dict)
                                else getattr(func, "name", "")
                            )
                            args = (
                                func.get("arguments", "")
                                if isinstance(func, dict)
                                else getattr(func, "arguments", "")
                            )
                        else:
                            func = getattr(tc, "function", None)
                            name = getattr(func, "name", "") if func else ""
                            args = getattr(func, "arguments", "") if func else ""
                        content += f'\n<tool_call name="{name}">{args}</tool_call>'
                    msg_copy.pop("tool_calls", None)
                msg_copy["content"] = content.strip()
            elif msg_copy.get("role") == "tool":
                msg_copy["role"] = "user"
                msg_copy["content"] = (
                    f"[Tool Response] Output:\n{msg_copy.get('content') or ''}"
                )
                msg_copy.pop("tool_call_id", None)
        mapped_messages.append(msg_copy)

    # 2. Extract and combine all system messages into a single one
    system_contents = []
    other_messages = []
    for msg in mapped_messages:
        if msg.get("role") == "system":
            content = msg.get("content") or ""
            if content.strip():
                system_contents.append(content.strip())
        else:
            other_messages.append(msg)

    # 3. Merge consecutive messages with the same role in other_messages
    # Never drop "tool" messages (orphaned tool_calls break the OpenAI API),
    # and only merge assistant messages when neither side carries tool_calls.
    merged_others: List[Dict[str, Any]] = []
    for msg in other_messages:
        if (
            not msg.get("content")
            and not msg.get("tool_calls")
            and msg.get("role") not in ("assistant", "tool")
        ):
            # Skip empty content (but keep assistant/tool placeholders for pairing)
            continue
        if msg.get("role") == "tool" and not msg.get("content"):
            msg["content"] = "[empty tool result]"
        if not merged_others:
            merged_others.append(msg)
            continue

        last = merged_others[-1]
        can_merge = (
            last.get("role") == msg.get("role")
            and last.get("role") in ("user", "assistant")
            and not last.get("tool_calls")
            and not msg.get("tool_calls")
        )
        if can_merge:
            last_content = last.get("content") or ""
            msg_content = msg.get("content") or ""
            if isinstance(last_content, list) or isinstance(msg_content, list):
                # Multimodal (image_url) parts: concatenate part lists.
                def _parts(c):
                    if isinstance(c, list):
                        return c
                    return [{"type": "text", "text": c}] if c else []

                last["content"] = _parts(last_content) + _parts(msg_content)
            elif last_content and msg_content:
                last["content"] = f"{last_content}\n\n{msg_content}"
            elif msg_content:
                last["content"] = msg_content
        else:
            merged_others.append(msg)

    # 4. Ensure the first message after system prompt is user
    if merged_others and merged_others[0].get("role") == "assistant":
        merged_others.insert(
            0, {"role": "user", "content": "Let's continue the conversation."}
        )

    # 5. Build final list
    normalized = []
    if system_contents:
        normalized.append({"role": "system", "content": "\n\n".join(system_contents)})
    normalized.extend(merged_others)

    # 6. Ensure the sequence is not system-only to avoid GLM/MiniMax API errors
    if len(normalized) == 1 and normalized[0].get("role") == "system":
        normalized.append(
            {"role": "user", "content": "Hello. I am setting up the workspace session."}
        )

    return normalized


@functools.lru_cache(maxsize=None)
def _get_token_estimator(model: str = "cl100k_base"):
    """
    Returns a function to estimate tokens in text.
    Uses tiktoken if available, otherwise falls back to char/4 approximation.
    """
    if HAS_TIKTOKEN:
        try:
            encoding = tiktoken.get_encoding(model)
            # disallowed_special=(): text containing e.g. "<|endoftext|>" (read
            # from a file) must count as plain text, not raise ValueError.
            return lambda text: len(encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    # Fallback: rough approximation (1 token ≈ 4 chars)
    return lambda text: max(1, len(text) // 4)


def count_message_tokens(
    messages: List[Dict[str, Any]], model: str = "cl100k_base"
) -> int:
    """
    Count tokens in a message list using tiktoken if available.
    """
    estimator = _get_token_estimator(model)
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # ponytail: flat ~1k tokens per image instead of counting base64;
            # use the provider's per-tile formula if budgets get tight.
            texts = [str(p.get("text", "")) for p in content if isinstance(p, dict)]
            total += 1000 * sum(
                1
                for p in content
                if isinstance(p, dict) and p.get("type") == "image_url"
            )
            content = "\n".join(texts)
        content = str(content)
        tool_calls = str(msg.get("tool_calls", ""))
        total += estimator(content) + estimator(tool_calls)
        # Add overhead for message structure
        total += 4  # role, content, etc.
    return total


def _shrink_unit(
    unit: List[Dict[str, Any]], available: int, unit_tokens: int
) -> List[Dict[str, Any]]:
    """Scale tool/user contents of one unit down to ~`available` tokens.

    Uses head+tail `truncate_output` so the start (context) and the end
    (errors, stack traces) survive. Assistant tool_calls are left intact.
    """
    from nimcode.tools.base import truncate_output

    def trimmable(m: Dict[str, Any]) -> bool:
        return m.get("role") in ("tool", "user") and isinstance(m.get("content"), str)

    trim_tokens = count_message_tokens([m for m in unit if trimmable(m)])
    fixed = unit_tokens - trim_tokens
    # ponytail: char-proportional scaling with 5% slack for the truncation
    # marker / tokenizer drift; exact fit would need per-message re-counting.
    ratio = 0.95 * max(available - fixed, 0) / max(trim_tokens, 1)
    out = []
    for msg in unit:
        msg = dict(msg)
        content = msg.get("content")
        if trimmable(msg):
            msg["content"] = truncate_output(
                content, max(500, int(len(content) * ratio))
            )
        out.append(msg)
    return out


def prune_history(
    messages: List[Dict[str, Any]],
    context_budget_tokens: int = 16000,
    max_turns_retained: int = 20,
) -> List[Dict[str, Any]]:
    """
    Prunes long conversation histories to satisfy context budget constraints.
    First, it truncates older tool messages.
    If the list is still over the token budget, it drops older user/assistant turns
    while preserving the system prompt, first user prompt, and the most recent turns.
    """
    if not messages:
        return []

    # Calculate current token estimate using tiktoken if available
    estimated_tokens = count_message_tokens(messages)
    if (
        len(messages) <= max_turns_retained
        and estimated_tokens <= context_budget_tokens
    ):
        return messages

    # Step 1: Truncate older tool results
    pruned = []
    # Always keep system message and initial user prompt intact
    keep_indices = set(range(min(2, len(messages))))
    cutoff_index = max(2, len(messages) - max_turns_retained)

    for i in range(len(messages)):
        msg = messages[i]
        if i in keep_indices:
            pruned.append(dict(msg))
        else:
            msg_copy = dict(msg)
            if i < cutoff_index and msg_copy.get("role") == "tool":
                msg_copy["content"] = (
                    str(msg_copy.get("content", ""))[:150]
                    + "\n... [Older tool result truncated to conserve context]"
                )
            pruned.append(msg_copy)

    # Check if we are still over budget
    if count_message_tokens(pruned) <= context_budget_tokens:
        return pruned

    # Step 2: Over budget even after tool truncation.
    # We must drop older messages. We must keep:
    # - System message at index 0 (if present)
    # - First user query at index 1 (if present)
    # - The most recent messages
    #
    # CRITICAL: assistant messages that carry `tool_calls` must stay grouped
    # with their subsequent `tool` result messages. Dropping either half
    # violates the API contract (tool messages must be paired with the
    # assistant tool_calls that produced them). We therefore group the
    # conversation into "units" and drop whole units from the oldest end.
    system_msg = pruned[0] if pruned and pruned[0].get("role") == "system" else None
    first_user_msg = None
    start_idx = 0
    if system_msg:
        start_idx = 1
    if len(pruned) > start_idx and pruned[start_idx].get("role") == "user":
        first_user_msg = pruned[start_idx]
        start_idx += 1

    # Keep list of system and first user message
    essential = []
    if system_msg:
        essential.append(system_msg)
    if first_user_msg:
        essential.append(first_user_msg)

    essential_tokens = count_message_tokens(essential)
    budget_left = context_budget_tokens - essential_tokens

    # Build units: a standalone message, or an (assistant + tool results) block.
    units: List[List[Dict[str, Any]]] = []
    k = 0
    remaining = pruned[start_idx:]
    while k < len(remaining):
        msg = remaining[k]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = k + 1
            while j < len(remaining) and remaining[j].get("role") == "tool":
                j += 1
            units.append(remaining[k:j])
            k = j
        else:
            units.append([msg])
            k += 1

    # Collect units from the newest end until we run out of budget.
    recent_msgs: List[Dict[str, Any]] = []
    for unit in reversed(units):
        unit_tokens = count_message_tokens(unit)
        # Always keep the newest unit (the live turn), then only what fits.
        if not recent_msgs and unit_tokens > budget_left:
            # A single huge tool result / pasted prompt would overflow the
            # window on its own: head+tail trim it into what's left.
            unit = _shrink_unit(unit, budget_left, unit_tokens)
            unit_tokens = count_message_tokens(unit)
        if not recent_msgs or budget_left - unit_tokens >= 0:
            recent_msgs = unit + recent_msgs
            budget_left -= unit_tokens
        else:
            # Out of budget! Stop including older messages.
            break

    # Construct pruned list
    final_messages = essential + recent_msgs
    return final_messages


def _fallback_to_xml_tool_calling(
    config: Config, messages: List[Dict[str, Any]], reason: str
) -> None:
    """Flip an endpoint off native function calling (shared by all fallbacks).

    Sets ``disable_native_tools``, rebuilds the system prompt with XML
    instructions, and normalizes history on the next call. Never raises.
    """
    try:
        secho(
            f"\nWarning: {reason}",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        secho(
            "Falling back to prompt-based XML tool calling...\n",
            fg=typer.colors.YELLOW,
        )
        config.disable_native_tools = True
        if (
            messages
            and isinstance(messages, list)
            and messages[0].get("role") == "system"
        ):
            from nimcode.context import generate_workspace_map

            w_map = generate_workspace_map(str(config.workspace_root))
            messages[0]["content"] = build_system_prompt(
                str(config.workspace_root), w_map, disable_native_tools=True
            )
    except Exception:
        try:
            config.disable_native_tools = True
        except Exception:
            pass


def call_nim_with_retry(
    client: OpenAI,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    model: str,
    config: Config,
    max_retries: int = 3,
    timeout: Optional[float] = None,
    _context_budget: Optional[int] = None,
):
    """
    Calls the NVIDIA NIM OpenAI endpoint with exponential backoff for transient errors.
    Supports instant cancellation via ESC/Ctrl+C without freezing.
    """
    import threading

    # Prune history to satisfy context budget constraint!
    budget = _context_budget or effective_budget(config, model)
    pruned_messages = prune_history(messages, budget)

    normalized_messages = normalize_messages(
        pruned_messages, disable_native_tools=config.disable_native_tools
    )

    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": model,
                "messages": normalized_messages,
                "temperature": config.temperature_plan
                if model == config.plan_model
                else config.temperature_fast,
                "timeout": timeout if timeout is not None else float(config.timeout),
            }
            if hasattr(config, "max_tokens") and config.max_tokens is not None:
                kwargs["max_tokens"] = config.max_tokens
            else:
                kwargs["max_tokens"] = 4096

            if tools and not config.disable_native_tools:
                kwargs["tools"] = tools

            result_box = {}
            error_box = {}

            def do_call():
                try:
                    result_box["res"] = client.chat.completions.create(**kwargs)
                except Exception as ex:
                    error_box["err"] = ex

            t = threading.Thread(target=do_call, daemon=True)
            t.start()

            while t.is_alive():
                from nimcode.ui import output

                tui_instance = getattr(output, "TUI_INSTANCE", None)
                if tui_instance is not None:
                    if getattr(tui_instance, "_interrupted", False):
                        tui_instance._interrupted = False
                        raise KeyboardInterrupt("ESC_CANCELLED")
                else:
                    if (
                        sys.platform == "win32"
                        and sys.stdin is not None
                        and sys.stdin.isatty()
                    ):
                        import msvcrt

                        if msvcrt.kbhit():
                            key = msvcrt.getch()
                            if key in [b"\x1b", b"\x03"]:
                                raise KeyboardInterrupt("ESC_CANCELLED")
                time.sleep(0.05)

            if "err" in error_box:
                raise error_box["err"]

            res = result_box.get("res")
            if res is None:
                raise RuntimeError(
                    "The model API call failed without a response or error. Retrying may help."
                )
            if hasattr(res, "choices") and not getattr(res, "choices", None):
                raise RuntimeError(
                    f"The model API returned a response with an empty 'choices' list. "
                    f"This commonly occurs for models like MiniMax-M3 if the 'max_tokens' parameter is not specified or if there is a server-side error. "
                    f"Response: {res}"
                )
            return res
        except KeyboardInterrupt:
            raise
        except Exception as e:
            err_str = str(e).lower()
            status = getattr(e, "status_code", None)
            if not isinstance(status, int):
                status = None
            # NIM reports tool-choice / degraded-function / context-overflow
            # only in the message text, always as a client error. Gate the
            # text checks on those statuses so a 5xx body can't trip them.
            semantic = status is None or status in (400, 404, 422)
            is_404 = status == 404 if status is not None else "404" in err_str
            if (
                semantic
                and not config.disable_native_tools
                and "auto" in err_str
                and (
                    "tool choice" in err_str
                    or "tool-call-parser" in err_str
                    or "enable-auto-tool-choice" in err_str
                )
            ):
                _fallback_to_xml_tool_calling(
                    config,
                    messages,
                    "Native auto tool choice is not supported/configured on this server.",
                )
                # Retry immediately
                return call_nim_with_retry(
                    client,
                    messages,
                    None,
                    model,
                    config,
                    max_retries=max_retries,
                    timeout=timeout,
                    _context_budget=_context_budget,
                )

            if (
                semantic
                and not config.disable_native_tools
                and (
                    ("degraded function" in err_str)
                    or ("degraded" in err_str and "function" in err_str)
                )
            ):
                _fallback_to_xml_tool_calling(
                    config, messages, "Function calling is degraded on this server."
                )
                return call_nim_with_retry(
                    client,
                    messages,
                    None,
                    model,
                    config,
                    max_retries=max_retries,
                    timeout=timeout,
                    _context_budget=_context_budget,
                )

            fn_not_found = (
                semantic
                and "function" in err_str
                and ("not found" in err_str or "not_found" in err_str)
            )
            if (
                tools
                and not config.disable_native_tools
                and fn_not_found
                and (is_404 or "no longer" in err_str or "unknown function" in err_str)
            ):
                # NIM account-scoped function registry rejects the native call
                # (e.g. 404 "Function '<id>': Not found for account"). The
                # endpoint can't do native function calling here — go XML.
                _fallback_to_xml_tool_calling(
                    config,
                    messages,
                    "This endpoint rejected native function calling "
                    "(function not found for your account).",
                )
                return call_nim_with_retry(
                    client,
                    messages,
                    None,
                    model,
                    config,
                    max_retries=max_retries,
                    timeout=timeout,
                    _context_budget=_context_budget,
                )

            if fn_not_found and (is_404 or "unknown function" in err_str):
                # Still failing with no native tools in play: the model (or
                # the key's account) is rejected server-side. No retry
                # strategy can fix that — say what to change instead of
                # dumping the raw payload.
                raise RuntimeError(
                    f"Request rejected (404 function-not-found for your account) "
                    f"even without function calling. Model: '{model}'. This is "
                    f"an account/model issue, not a tools issue. Try: "
                    f"1) '/models-refresh' then '/model' to switch to a model "
                    f"available to your key; 2) verify NVIDIA_API_KEY belongs "
                    f"to an account entitled for '{model}'. "
                    f"Server detail: {e}"
                )

            if semantic and (
                "max context length" in err_str
                or "context length exceeded" in err_str
                or "context window" in err_str
                or "too many tokens" in err_str
            ):
                # Halve the budget on every overflow (with a floor). Previously
                # each retry re-halved config's budget -> identical payload ->
                # unbounded recursion until RecursionError.
                new_budget = budget // 2
                if new_budget < 1000:
                    raise
                secho(
                    "\nWarning: Context length exceeded. Pruning history and retrying...",
                    fg=typer.colors.YELLOW,
                    bold=True,
                )

                pruned_messages = prune_history(
                    messages,
                    context_budget_tokens=new_budget,
                    max_turns_retained=max(10, config.max_loop_iterations),
                )
                return call_nim_with_retry(
                    client,
                    pruned_messages,
                    tools,
                    model,
                    config,
                    max_retries=max_retries,
                    timeout=timeout,
                    _context_budget=new_budget,
                )

            if isinstance(e, (RateLimitError, APIConnectionError)):
                fail_fast, capacity = False, True  # incl. APITimeoutError
            elif status is not None:
                # Deterministic client errors: re-sending can only fail again.
                fail_fast = status in (400, 401, 403, 404, 422)
                capacity = status in (429, 503)
            else:
                # No status code (non-openai exception): fall back to text,
                # with word boundaries so "4000 tokens" is not a 400.
                capacity = bool(
                    re.search(
                        r"\b(429|503)\b|rate limit|resourceexhausted|"
                        r"service unavailable|overloaded",
                        err_str,
                    )
                )
                fail_fast = not capacity and bool(
                    re.search(
                        r"\b(400|401|403|404|422)\b|bad request|unauthorized|"
                        r"invalid api key|incorrect api key|forbidden|"
                        r"not found|unprocessable",
                        err_str,
                    )
                )

            if attempt == max_retries - 1 and not fail_fast:
                if capacity:
                    secho(
                        "Tip: the endpoint is rate-limiting or out of capacity "
                        "— wait ~1 min and retry, or '/model' to switch to a "
                        "model with more headroom.",
                        fg=typer.colors.YELLOW,
                    )
                raise
            if fail_fast:
                if is_404 and "model" in err_str:
                    secho(
                        "Tip: the model id may be retired — run "
                        "'nimcode refresh-models' or '/models-refresh'.",
                        fg=typer.colors.YELLOW,
                    )
                raise
            import random as _rand

            wait_time = min(30, (2**attempt) * 2 + _rand.uniform(0, 1))
            echo(
                f"\nAPI error ({e}). Retrying in {wait_time:.1f}s... (Attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(wait_time)


def list_sessions(session_dir: Path) -> List[Path]:
    """Session files (JSONL and legacy JSON lists), newest first."""
    if not session_dir.is_dir():
        return []
    files = [*session_dir.glob("session_*.json"), *session_dir.glob("session_*.jsonl")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def load_session(path: Path) -> List[Dict[str, Any]]:
    """Load a session: one JSON message per line (.jsonl) or a legacy JSON list.

    A corrupt *last* line (crash mid-append) is skipped; corruption anywhere
    else raises, since silently dropping history would mislead the model.
    """
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".json":
        data = json.loads(text) if text.strip() else []
        return data if isinstance(data, list) else []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    messages = []
    for i, line in enumerate(lines):
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return messages


COMPACT_MARKER_KEY = "nimcode_compact"


def prepare_resumed_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """History to continue from after loading a session file.

    - Starts at the last /compact marker (an empty system message that then
      serves as messages[0]), so compacted turns are not replayed.
    - Logged screenshot parts carry a file path instead of base64; they are
      flattened to text so the API never sees a non-URL image_url.
    """
    start = 0
    for i, m in enumerate(messages):
        if m.get(COMPACT_MARKER_KEY):
            start = i
    out = []
    for m in messages[start:]:
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "image_url":
                    url = str((p.get("image_url") or {}).get("url", ""))
                    if url.startswith("data:"):
                        url = "(inline image)"
                    parts.append(f"[screenshot: {url}]")
                else:
                    parts.append(str(p.get("text", "")))
            m = dict(m, content="\n".join(parts))
        out.append(m)
    return out


def open_session_path(
    session_dir: Path,
    session_file_name: Optional[str],
    existing_messages: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Path to append this session's turns to.

    `.jsonl` sessions continue in place. Anything else (a new session, or a
    legacy `.json` list that must not be appended to) gets a fresh `.jsonl`,
    seeded with `existing_messages`; the old file is left untouched.
    """
    if session_file_name and session_file_name.endswith(".jsonl"):
        return session_dir / session_file_name
    path = session_dir / f"session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    for msg in existing_messages or []:
        log_turn(msg, path)
    return path


def migrate_legacy_session(workspace_root: Path, session_dir: Path) -> Optional[Path]:
    """Move a pre-0.4 in-repo `.nimcode_session.json` into the session dir.

    Named `session_legacy_<ts>.json` so `list_sessions` sees it. Returns the
    destination, or None when there was nothing to migrate. May raise OSError.
    """
    import shutil

    legacy = Path(workspace_root) / ".nimcode_session.json"
    if not legacy.exists():
        return None
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / f"session_legacy_{int(time.time())}.json"
    shutil.move(str(legacy), str(dest))
    return dest


def log_turn(message_or_dict: Any, session_path: Path):
    """
    Appends one message as a JSON line to the session file (`.jsonl`).
    JSON-safe (default=str); a crash mid-write can only corrupt the last
    line, which `load_session` skips.
    """
    try:
        try:
            cleaned = clean_surrogates(message_or_dict)
        except Exception:
            cleaned = message_or_dict

        if isinstance(cleaned, dict):
            entry = cleaned
        else:
            entry = {
                "role": getattr(cleaned, "role", "assistant"),
                "content": getattr(cleaned, "content", ""),
            }
            if hasattr(cleaned, "tool_calls") and cleaned.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in cleaned.tool_calls
                ]

        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        with open(session_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        # Don't crash agent loop on logging issue, but don't swallow silently.
        try:
            print(f"Warning: failed to persist session turn: {e}", flush=True)
        except Exception:
            pass


def aggregate_session_stats(workspace_root: Path) -> Dict[str, Any]:
    """
    Aggregates statistics from all session files in the project's session directory.
    Returns a dictionary with overview, cost/tokens, and tool usage statistics.
    """
    from collections import Counter
    from datetime import datetime

    session_dir = get_project_nimcode_dir(workspace_root) / SESSION_DIR
    if not session_dir.exists():
        return {
            "overview": {"total_sessions": 0, "total_messages": 0, "days_active": 0},
            "tokens": {"input": 0, "output": 0, "total": 0},
            "tool_usage": {},
            "error": "No session directory found",
        }

    session_files = list_sessions(session_dir)
    if not session_files:
        return {
            "overview": {"total_sessions": 0, "total_messages": 0, "days_active": 0},
            "tokens": {"input": 0, "output": 0, "total": 0},
            "tool_usage": {},
            "error": "No session files found",
        }

    total_messages = 0
    tool_counter: Counter[str] = Counter()
    dates = set()
    total_input_tokens = 0
    total_output_tokens = 0

    for session_file in session_files:
        try:
            messages = load_session(session_file)

            # Extract date from filename if possible (session_YYYYMMDD_HHMMSS.jsonl)
            try:
                date_str = session_file.stem.split("_")[1]
                date_obj = datetime.strptime(date_str, "%Y%m%d")
                dates.add(date_obj.date())
            except Exception:
                # Fallback to file modification time
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                dates.add(mtime.date())

            total_messages += len(messages)

            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "") or ""

                # Count tokens roughly (input = user + system, output = assistant + tool)
                if role in ("user", "system"):
                    total_input_tokens += len(content) // 4
                elif role == "assistant":
                    total_output_tokens += len(content) // 4

                # Count tool calls
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        name = func.get("name", "unknown")
                        tool_counter[name] += 1

        except Exception:
            # Skip corrupt session files
            continue

    return {
        "overview": {
            "total_sessions": len(session_files),
            "total_messages": total_messages,
            "days_active": len(dates),
        },
        "tokens": {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "total": total_input_tokens + total_output_tokens,
        },
        "tool_usage": dict(tool_counter),
        "error": None,
    }


def render_stats_rich(
    stats: Dict[str, Any], console: Optional[Any] = None, tui: Optional[Any] = None
) -> None:
    """
    Renders statistics using Rich components (Panels, Tables, text histograms), or returns markdown if TUI is provided.
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group

    overview = stats.get("overview", {})
    tokens = stats.get("tokens", {})
    tool_usage = stats.get("tool_usage", {})
    error = stats.get("error")

    if tui is not None:
        if error:
            tui.add_message(
                "system",
                f"\n[yellow]No historical data found yet.[/yellow]\n*Detail: {error}*",
                markdown=True,
            )
            return None

        lines = [
            "### nimcode Usage Statistics",
            "",
            "**Overview**",
            f"- **Total Sessions:** {overview.get('total_sessions', 0):,}",
            f"- **Total Messages:** {overview.get('total_messages', 0):,}",
            f"- **Days Active:** {overview.get('days_active', 0):,}",
            "",
            "**Tokens**",
            f"- **Input Tokens:** {tokens.get('input', 0):,}",
            f"- **Output Tokens:** {tokens.get('output', 0):,}",
            f"- **Total Tokens:** {tokens.get('total', 0):,}",
            "",
        ]
        if tool_usage:
            lines.append("**Tool Usage**")
            sorted_tools = sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)
            max_count = sorted_tools[0][1] if sorted_tools else 1
            for tool, count in sorted_tools:
                pct = (count / max_count) * 100
                lines.append(f"- **{tool}:** {count} ({pct:.1f}%)")
        else:
            lines.append("No tool usage recorded.")
        tui.add_message("system", "\n".join(lines), markdown=True)
        return None

    if console is None:
        from nimcode.ui.output import console as global_console

        console = global_console

    if error:
        console.print(
            Panel(
                f"[yellow]No historical data found yet.[/yellow]\n[dim]{error}[/dim]",
                title="[bold cyan]nimcode Stats[/bold cyan]",
                border_style="yellow",
            )
        )
        return None

    # Overview Panel
    overview_table = Table(show_header=False, box=None, padding=(0, 1))
    overview_table.add_column("Metric", style="bold cyan")
    overview_table.add_column("Value", style="bold white")
    overview_table.add_row("Total Sessions", f"{overview.get('total_sessions', 0):,}")
    overview_table.add_row("Total Messages", f"{overview.get('total_messages', 0):,}")
    overview_table.add_row("Days Active", f"{overview.get('days_active', 0):,}")

    # Tokens Panel
    token_table = Table(show_header=False, box=None, padding=(0, 1))
    token_table.add_column("Type", style="bold cyan")
    token_table.add_column("Count", style="bold white")
    token_table.add_row("Input Tokens", f"{tokens.get('input', 0):,}")
    token_table.add_row("Output Tokens", f"{tokens.get('output', 0):,}")
    token_table.add_row("Total Tokens", f"{tokens.get('total', 0):,}")

    # Tool Usage Histogram
    if tool_usage:
        sorted_tools = sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)
        max_count = sorted_tools[0][1] if sorted_tools else 1
        max_bar_width = 30

        tool_lines = []
        for tool, count in sorted_tools:
            pct = (count / max_count) * 100
            bar_width = int((count / max_count) * max_bar_width)
            bar = "█" * bar_width
            tool_lines.append(
                Text.assemble(
                    (f"{tool:<20}", "bold white"),
                    (f" {count:>5} ", "bold cyan"),
                    (f" ({pct:5.1f}%) ", "dim"),
                    (bar, "green"),
                )
            )
        tool_content = (
            Group(*tool_lines)
            if tool_lines
            else Text("No tool usage recorded", style="dim")
        )
    else:
        tool_content = Text("No tool usage recorded", style="dim")

    # Combine all panels
    console.print(
        Panel(
            Group(
                Panel(
                    overview_table, title="[bold]Overview[/bold]", border_style="cyan"
                ),
                "",
                Panel(token_table, title="[bold]Tokens[/bold]", border_style="green"),
                "",
                Panel(
                    tool_content,
                    title="[bold]Tool Usage[/bold]",
                    border_style="magenta",
                ),
            ),
            title="[bold cyan]nimcode Usage Statistics[/bold cyan]",
            border_style="blue",
            padding=(1, 2),
        )
    )


def run_init_command(
    config: Config,
    client: Optional[OpenAI] = None,
    workspace_root: Optional[Path] = None,
    tui: Optional[Any] = None,
) -> str:
    """
    Initializes or updates an AGENTS.md file in the workspace root.
    Scans project tech stack markers and generates/enriches AGENTS.md.
    """
    from nimcode.ui.output import console

    root = Path(workspace_root if workspace_root else config.workspace_root).resolve()
    agents_md_path = root / "AGENTS.md"

    def _output(msg: str, **kwargs):
        if tui is not None:
            tui.add_message("system", msg)
        else:
            console.print(msg, **kwargs)

    _output(
        f"[bold cyan]Scanning workspace at {root} for initialization markers...[/bold cyan]"
    )

    # Detect stack markers
    stack_info = []
    if (root / "package.json").exists():
        stack_info.append("Node.js / JavaScript / TypeScript")
    if (
        (root / "pyproject.toml").exists()
        or (root / "setup.py").exists()
        or (root / "requirements.txt").exists()
    ):
        stack_info.append("Python")
    if (root / "Cargo.toml").exists():
        stack_info.append("Rust")
    if (root / "go.mod").exists():
        stack_info.append("Go")
    if (root / "CMakeLists.txt").exists() or (root / "Makefile").exists():
        stack_info.append("C/C++")

    stack_summary = (
        ", ".join(stack_info) if stack_info else "General Project / Unknown Stack"
    )
    _output(f"[dim]Detected Tech Stack: {stack_summary}[/dim]")

    existing_content = ""
    is_update = agents_md_path.exists()
    if is_update:
        existing_content = agents_md_path.read_text(encoding="utf-8").strip()
        _output(
            f"[yellow]Found existing AGENTS.md ({len(existing_content)} chars). Updating/Enriching...[/yellow]"
        )
    else:
        _output("[cyan]Creating new AGENTS.md...[/cyan]")

    # If we have an active client, use the AI model to generate/update
    if client is not None:
        _output(
            f"[cyan]Asking model ({config.fast_model}) to generate/enrich AGENTS.md for this repository...[/cyan]"
        )
        try:
            from nimcode.context import generate_workspace_map

            workspace_map = generate_workspace_map(str(root))

            prompt_content = f"""You are an expert AI agent architect. Your task is to generate or update an `AGENTS.md` file for this repository ({root.name}).
Detected Tech Stack: {stack_summary}

Workspace Structure Summary:
{workspace_map}

"""
            if is_update:
                prompt_content += f"""Here is the current existing `AGENTS.md` content:
```markdown
{existing_content}
```

Please output an updated, enriched, and well-structured markdown for `AGENTS.md`. Keep all existing custom rules, but improve the organization, ensure exact build/test commands are clearly documented, and add any missing architectural invariants that AI coding assistants should know."""
            else:
                prompt_content += """Please output a high-quality, authoritative `AGENTS.md` markdown file tailored for this project. Include:
1. # Project Name & Overview
2. ## Core Rules & Sandbox Boundaries (e.g. human-in-the-loop approval, strict sandbox limits)
3. ## Tech Stack & Architecture (Key directories, modules, conventions)
4. ## Build, Test & Run Commands (Exact terminal commands so future agents don't guess)
5. ## Workflow Guidelines for AI Agents"""

            req_messages = [{"role": "user", "content": prompt_content}]
            res = call_nim_with_retry(
                client, req_messages, None, config.fast_model, config
            )
            new_content = strip_reasoning_spans(
                res.choices[0].message.content or ""
            ).strip()
            # If model wrapped in ```markdown ... ``` strip the fences
            # (any info string: ```markdown, ```md, ...)
            if new_content.startswith("```"):
                new_content = new_content.partition("\n")[2].strip()
                if new_content.endswith("```"):
                    new_content = new_content[:-3].strip()

            if new_content:
                agents_md_path.write_text(new_content, encoding="utf-8")
                msg = f"AGENTS.md successfully {'updated' if is_update else 'created'} via AI at {agents_md_path}"
                _output(f"[bold green]{msg}[/bold green]")
                return msg
        except Exception as e:
            _output(
                f"[yellow]AI generation failed ({e}). Falling back to template initialization.[/yellow]"
            )

    # Fallback or offline template creation
    if not is_update:
        template = f"""# {root.name} Agent Instructions (AGENTS.md)

**Notice:** This file is intended to be read by autonomous AI coding assistants, LLMs, or other agents that are tasked with modifying, understanding, or interfacing with the `{root.name}` project.

## Core Rules & Sandbox Boundaries
1. **Never Bypass Human Gate:** Do not execute terminal commands or write files invisibly without approval or preview.
2. **Respect Directory Sandbox:** Keep all operations within `{root.name}`. Do not touch system files or credentials.
3. **Keep Explanations Clean:** Provide concise, accurate summaries and avoid unnecessary verbose output.

## Tech Stack & Architecture
- **Detected Stack:** {stack_summary}
- **Workspace Root:** `{root.name}/`

## Build, Test & Run Commands
- *(Add exact testing commands here, e.g., `pytest`, `npm test`, `cargo test`)*
- *(Add exact run commands here, e.g., `python main.py`, `npm run dev`)*

## Workflow & Coding Conventions
- Write clean, modular, and well-documented code.
- Verify changes with compilation/testing before marking tasks complete.
"""
        agents_md_path.write_text(template.strip() + "\n", encoding="utf-8")
        msg = f"AGENTS.md initialized from template at {agents_md_path}"
        _output(f"[bold green]{msg}[/bold green]")
        return msg
    else:
        msg = f"AGENTS.md already exists at {agents_md_path} and client is offline."
        _output(f"[yellow]{msg}[/yellow]")
        return msg
