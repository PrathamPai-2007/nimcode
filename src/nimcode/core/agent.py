import os
import sys
import json
import time
import functools
import typer
from pathlib import Path
from typing import List, Dict, Any, Optional
from openai import OpenAI

# Optional tiktoken for accurate token counting
try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

from nimcode.config import Config, get_project_nimcode_dir
from nimcode.core.prompts import (
    build_system_prompt,
    strip_reasoning_spans,
    extract_plan_block,
)
from nimcode.tools import (
    AVAILABLE_TOOLS_SPEC,
    is_valid_tool_call,
    render_preview,
    execute_local_tool,
)
from nimcode.core.state import AgentState
from nimcode.ui.output import echo, secho, symbol


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
            if last_content and msg_content:
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
        content = str(msg.get("content", ""))
        tool_calls = str(msg.get("tool_calls", ""))
        total += estimator(content) + estimator(tool_calls)
        # Add overhead for message structure
        total += 4  # role, content, etc.
    return total


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
    budget = _context_budget or config.context_budget_tokens
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
            if (
                not config.disable_native_tools
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

            if not config.disable_native_tools and (
                ("degraded function" in err_str)
                or ("degraded" in err_str and "function" in err_str)
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

            if (
                tools
                and not config.disable_native_tools
                and (
                    "function" in err_str
                    and ("not found" in err_str or "not_found" in err_str)
                    and (
                        "404" in err_str
                        or "no longer" in err_str
                        or "unknown function" in err_str
                    )
                )
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

            if (
                "function" in err_str
                and ("not found" in err_str or "not_found" in err_str)
                and ("404" in err_str or "unknown function" in err_str)
            ):
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

            if (
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

            if attempt == max_retries - 1:
                if any(
                    s in err_str
                    for s in (
                        "429",
                        "rate limit",
                        "resourceexhausted",
                        "503",
                        "service unavailable",
                        "overloaded",
                    )
                ):
                    secho(
                        "Tip: the endpoint is rate-limiting or out of capacity "
                        "— wait ~1 min and retry, or '/model' to switch to a "
                        "model with more headroom.",
                        fg=typer.colors.YELLOW,
                    )
                raise
            # Don't retry deterministic client errors (except rate limits):
            # re-sending the identical request can only fail identically.
            if (
                any(
                    s in err_str
                    for s in (
                        "400",
                        "bad request",
                        "401",
                        "unauthorized",
                        "invalid api key",
                        "incorrect api key",
                        "403",
                        "forbidden",
                        "404",
                        "not found",
                        "422",
                        "unprocessable",
                    )
                )
                and "429" not in err_str
                and "rate limit" not in err_str
            ):
                if "404" in err_str and "model" in err_str:
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


def log_turn(message_or_dict: Any, session_path: Path):
    """
    Logs the turn and persists session history to the specific session file.
    Atomic (temp + os.replace) and JSON-safe (default=str) so a crash or
    non-serializable tool args can never corrupt the session.
    """
    import tempfile

    try:
        try:
            cleaned = clean_surrogates(message_or_dict)
        except Exception:
            cleaned = message_or_dict
        data = []
        if session_path.exists():
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    data = loaded if isinstance(loaded, list) else []
            except Exception:
                data = []

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

        data.append(entry)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(session_path.parent), prefix=".session_tmp_"
        )
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_name, session_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            raise
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

    session_files = list(session_dir.glob("*.json"))
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
            with open(session_file, "r", encoding="utf-8") as f:
                messages = json.load(f)

            if not isinstance(messages, list):
                continue

            # Extract date from filename if possible (session_YYYYMMDD_HHMMSS.json)
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


def enable_persistent_prompt(state, config, workspace_root, last_input):
    import shutil
    import sys
    from nimcode.config_models import display_name

    term_width, term_height = shutil.get_terminal_size()
    # Scroll region: lines 1 to height-4
    sys.stdout.write(f"\033[1;{term_height - 4}r")

    if (
        state == AgentState.AWAITING_PLAN_APPROVAL
        or getattr(config, "mode", "") == "plan"
    ):
        mode_name = "PLAN MODE"
        sep_class_ansi = "\033[1;34m"  # Blue
    elif state == AgentState.YOLO or getattr(config, "mode", "") == "yolo":
        mode_name = "YOLO MODE"
        sep_class_ansi = "\033[1;31m"  # Red
    else:
        mode_name = "BUILD MODE"
        sep_class_ansi = "\033[1;32m"  # Green

    pwd = os.getcwd()
    model_id = (
        config.fast_model
        if state != AgentState.AWAITING_PLAN_APPROVAL
        else config.plan_model
    )
    model_name = display_name(model_id)

    hline = symbol("hline")
    mode_label = f"{hline}{hline} {mode_name} "
    remaining_upper = term_width - len(mode_label)
    upper_border = mode_label + (hline * max(0, remaining_upper))

    left_str = f"{hline}{hline} ({pwd}) "
    right_str = f" ({model_name}) {hline}{hline}"
    padding = term_width - len(left_str) - len(right_str)
    if padding < 0:
        avail = term_width - len(right_str) - 6
        if avail > 5:
            left_str = f"{hline}{hline} (..{pwd[-(avail - 2) :]}) "
        else:
            left_str = f"{hline}{hline} "
        padding = term_width - len(left_str) - len(right_str)
    lower_border = left_str + (hline * max(0, padding)) + right_str

    sys.stdout.write(
        f"\033[{term_height - 2};1H\033[2K{sep_class_ansi}{upper_border}\033[0m"
    )

    input_str = last_input or ""
    input_str = input_str.replace("\n", " ")
    if len(input_str) > term_width - 6:
        input_str = input_str[: term_width - 9] + "..."
    sys.stdout.write(
        f"\033[{term_height - 1};1H\033[2K\033[1;36m{symbol('prompt')} \033[0m{input_str}"
    )
    sys.stdout.write(
        f"\033[{term_height};1H\033[2K{sep_class_ansi}{lower_border}\033[0m"
    )
    sys.stdout.write(f"\033[{term_height - 3};1H\033[2K")
    sys.stdout.write(f"\033[{term_height - 4};1H")
    sys.stdout.flush()


def disable_persistent_prompt():
    import sys
    import shutil

    term_width, term_height = shutil.get_terminal_size()
    sys.stdout.write("\033[r")
    sys.stdout.write(f"\033[{term_height};1H\n")
    sys.stdout.flush()


def print_session_history(messages: List[Dict[str, Any]]):
    """
    Renders the conversation history of a resumed session in a structured, readable format.
    """
    if not messages:
        return

    secho("\n=== Resumed Session History ===", fg=typer.colors.BLUE, bold=True)
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "system":
            continue
        elif role == "user":
            secho(f"\n{symbol('prompt')} {content}", fg=typer.colors.CYAN, bold=True)
        elif role == "assistant":
            if content.strip():
                echo(f"\n{content}")
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    secho(
                        f"  {symbol('tool')} Tool Call: {name}", fg=typer.colors.YELLOW
                    )
        elif role == "tool":
            snippet = content.strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:80] + "..."
            secho(f"  {symbol('arrow')} Tool Result: {snippet}", fg=typer.colors.GREEN)
    secho("===============================\n", fg=typer.colors.BLUE, bold=True)


def run_agent_loop(
    client: OpenAI,
    user_prompt: str,
    config: Config,
    workspace_root: Path,
    workspace_map: str,
    existing_messages: Optional[List[Dict[str, Any]]] = None,
    initial_state: str = "AWAITING_PLAN_APPROVAL",
    session_file_name: Optional[str] = None,
):
    """
    The continuous REPL and state loop for nimcode.
    Enforces Plan-Before-Execution (`AWAITING_PLAN_APPROVAL` -> `EXECUTING`).
    Supports resuming from existing session history (`existing_messages`).
    """
    import atexit

    atexit.register(disable_persistent_prompt)
    session_dir = get_project_nimcode_dir(workspace_root) / SESSION_DIR
    session_dir.mkdir(parents=True, exist_ok=True)

    legacy_session = workspace_root / ".nimcode_session.json"
    if legacy_session.exists():
        import shutil

        shutil.move(str(legacy_session), str(session_dir / "legacy_session.json"))
        secho(
            f"\nMigrated legacy session to {SESSION_DIR}/legacy_session.json",
            fg=typer.colors.CYAN,
        )

    if not session_file_name:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        session_file_name = f"session_{timestamp}.json"

    session_path = session_dir / session_file_name
    user_input: Optional[str] = user_prompt

    messages = []
    state = initial_state
    if state == "YOLO":
        config.mode = "yolo"
    elif state == "AWAITING_PLAN_APPROVAL":
        config.mode = "plan"
    else:
        config.mode = "build"

    skip_first_api_call = False

    if existing_messages:
        messages = existing_messages
        print_session_history(messages)
        # Always wait for user input after loading a session — never auto-fire the agent.
        # The user should decide whether to continue, redirect, or start fresh.
        skip_first_api_call = True
    else:
        system_prompt = build_system_prompt(
            str(workspace_root),
            workspace_map,
            disable_native_tools=config.disable_native_tools,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    loop_count = 0

    def prompt_user(text, **kwargs):
        """Prompt the user for simple y/n/feedback input.

        Uses plain built-in input() to avoid prompt_toolkit deadlocks on Windows
        when called outside the normal TUI event loop (e.g. after a Rich spinner).
        """
        disable_persistent_prompt()
        prompt_text = (
            text
            if text.endswith(": ")
            else (text + " " if text.endswith(":") else text + ": ")
        )
        # Flush any pending output so the prompt appears before we block on input
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            res = input(prompt_text)
        except KeyboardInterrupt:
            echo("\n")
            res = "q"
        except EOFError:
            res = "q"
        enable_persistent_prompt(state, config, workspace_root, res)
        return res

    def get_user_turn() -> Optional[str]:
        nonlocal state, session_path
        while True:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.key_binding import KeyBindings

            commands = [
                "/help",
                "/plan",
                "/build",
                "/yolo",
                "/resume",
                "/model",
                "/config",
                "/changes",
                "/diff",
                "/test",
                "/team",
                "/compact",
                "/recap",
                "/skills",
                "/install-skill",
                "/init",
                "/quit",
                "/exit",
                "/kill",
                "/restore",
            ]
            try:
                from nimcode import skills

                commands.extend([f"/{s}" for s in skills.list_skills()])
            except Exception:
                pass
            completer = WordCompleter(commands, ignore_case=True, WORD=True)
            bindings = KeyBindings()

            @bindings.add("escape")
            def _(event):
                if event.app.current_buffer.text:
                    event.app.current_buffer.reset()
                else:
                    event.app.current_buffer.reset()

            @bindings.add("backspace")
            def _(event):
                b = event.app.current_buffer
                if b.cursor_position > 0:
                    b.delete_before_cursor(count=event.arg)

            @bindings.add("tab")
            def _(event):
                b = event.app.current_buffer
                if b.complete_state:
                    b.complete_next()
                else:
                    b.start_completion(select_first=True)

            @bindings.add("s-tab")
            def _(event):
                b = event.app.current_buffer
                if b.complete_state:
                    b.complete_previous()
                else:
                    nonlocal state
                    if state == AgentState.AWAITING_PLAN_APPROVAL:
                        state = AgentState.EXECUTING
                        config.mode = "build"
                    elif state == AgentState.EXECUTING:
                        state = AgentState.YOLO
                        config.mode = "yolo"
                    else:
                        state = AgentState.AWAITING_PLAN_APPROVAL
                        config.mode = "plan"
                    event.app.invalidate()

            from prompt_toolkit.styles import Style
            from prompt_toolkit.formatted_text import FormattedText

            style = Style.from_dict(
                {
                    "bottom-toolbar": "bg:default #aaaaaa",
                    "separator": "#666666",
                    "sep-plan": "bold #0088ff",
                    "sep-build": "bold #00cc44",
                    "sep-yolo": "bold #ff2200",
                    "prompt": "bold #00ffff",
                }
            )
            session: PromptSession = PromptSession(key_bindings=bindings, style=style)

            def get_prompt_message():
                import shutil

                term_width = shutil.get_terminal_size().columns
                if (
                    state == AgentState.AWAITING_PLAN_APPROVAL
                    or getattr(config, "mode", "") == "plan"
                ):
                    mode_name = "PLAN MODE"
                    sep_class = "class:sep-plan"
                elif state == AgentState.YOLO or getattr(config, "mode", "") == "yolo":
                    mode_name = "YOLO MODE"
                    sep_class = "class:sep-yolo"
                else:
                    mode_name = "BUILD MODE"
                    sep_class = "class:sep-build"

                hline = symbol("hline")
                mode_label = f"{hline}{hline} {mode_name} "
                remaining_width = term_width - len(mode_label)
                if remaining_width < 0:
                    sep = mode_label[:term_width] + "\n"
                else:
                    sep = mode_label + (hline * remaining_width) + "\n"
                return FormattedText(
                    [(sep_class, sep), ("class:prompt", f"{symbol('prompt')} ")]
                )

            def get_bottom_toolbar():
                import shutil
                from nimcode.config_models import display_name

                term_width = shutil.get_terminal_size().columns
                if (
                    state == AgentState.AWAITING_PLAN_APPROVAL
                    or getattr(config, "mode", "") == "plan"
                ):
                    sep_class = "class:sep-plan"
                elif state == AgentState.YOLO or getattr(config, "mode", "") == "yolo":
                    sep_class = "class:sep-yolo"
                else:
                    sep_class = "class:sep-build"

                pwd = os.getcwd()
                model_id = (
                    config.fast_model
                    if state != AgentState.AWAITING_PLAN_APPROVAL
                    else config.plan_model
                )

                model_name = display_name(model_id)

                hline = symbol("hline")
                left_str = f"{hline}{hline} ({pwd}) "
                right_str = f" ({model_name}) {hline}{hline}"

                padding = term_width - len(left_str) - len(right_str)
                if padding < 0:
                    avail = term_width - len(right_str) - 6
                    if avail > 5:
                        left_str = f"{hline}{hline} (..{pwd[-(avail - 2) :]}) "
                    else:
                        left_str = f"{hline}{hline} "
                    padding = term_width - len(left_str) - len(right_str)

                status_line = left_str + (hline * max(0, padding)) + right_str
                return FormattedText([(sep_class, status_line)])

            disable_persistent_prompt()
            secho("\nUser Input (or 'exit' to finish task)", fg=typer.colors.CYAN)
            try:
                sys.stdout.write("\033[999B\r")
                sys.stdout.flush()
            except Exception:
                pass

            try:
                user_input = session.prompt(
                    get_prompt_message,
                    completer=completer,
                    bottom_toolbar=get_bottom_toolbar,
                    reserve_space_for_menu=2,
                ).strip()
            except (KeyboardInterrupt, EOFError):
                echo("\nPrompt cancelled. Exiting.")
                return None
            if not user_input:
                continue
            if user_input.lower() in ["/plan", "/build", "/yolo"] or any(
                user_input.lower().startswith(prefix + " ")
                for prefix in ["/plan", "/build", "/yolo"]
            ):
                parts = user_input.split(" ", 1)
                cmd = parts[0].lower()
                if cmd == "/plan":
                    state = AgentState.AWAITING_PLAN_APPROVAL
                    config.mode = "plan"
                    secho(
                        "\nSwitched to Plan Mode (AWAITING_PLAN_APPROVAL). Tools disabled until plan is approved.\n",
                        fg=typer.colors.CYAN,
                        bold=True,
                    )
                elif cmd == "/build":
                    state = AgentState.EXECUTING
                    config.mode = "build"
                    secho(
                        "\nSwitched to Build Mode (EXECUTING with human-in-the-loop approval).\n",
                        fg=typer.colors.GREEN,
                        bold=True,
                    )
                elif cmd == "/yolo":
                    state = AgentState.YOLO
                    config.mode = "yolo"
                    secho(
                        "\nSwitched to YOLO Mode (EXECUTING with auto-approval of edits and commands).\n",
                        fg=typer.colors.RED,
                        bold=True,
                    )
                if len(parts) > 1 and parts[1].strip():
                    return parts[1].strip()
                continue
            if user_input.strip() in ["?", "/help", "help"]:
                from nimcode.ui.console import show_help

                show_help()
                continue
            elif user_input.strip().lower() in ["/changes", "/diff"]:
                from nimcode.config import show_changes

                show_changes(
                    Path(workspace_root).resolve()
                    if workspace_root
                    else Path(".").resolve()
                )
                continue
            elif user_input.strip().lower() == "/restore":
                from nimcode.config import restore_interactive_backup

                restore_interactive_backup(
                    Path(workspace_root).resolve()
                    if workspace_root
                    else Path(".").resolve()
                )
                continue
            elif user_input.strip().lower() == "/config":
                from nimcode.config import configure_interactive

                configure_interactive(config)
                continue
            elif user_input.strip().lower().startswith("/test"):
                parts = user_input.strip().split(" ", 1)
                test_cmd = parts[1].strip() if len(parts) > 1 else None
                from nimcode.tools import execute_test_runner

                root_path = (
                    Path(workspace_root).resolve()
                    if workspace_root
                    else Path(".").resolve()
                )
                secho(
                    "\nRunning test suite inside validation sandbox...",
                    fg=typer.colors.CYAN,
                    bold=True,
                )
                res = execute_test_runner(root_path, config, test_cmd)
                secho(
                    f"\n--- Test Runner Results ---\n{res}\n---------------------------",
                    fg=typer.colors.CYAN,
                )
                continue
            elif user_input.strip().lower() == "/team":
                from nimcode.tools import ACTIVE_SUBAGENTS

                if not ACTIVE_SUBAGENTS:
                    secho(
                        "No subagents spawned yet. Use `spawn_agent` tool or ask nimcode to delegate tasks.",
                        fg=typer.colors.YELLOW,
                    )
                else:
                    secho(
                        "\nActive Subagent Swarm Team:", fg=typer.colors.CYAN, bold=True
                    )
                    for aid, sub in ACTIVE_SUBAGENTS.items():
                        echo(f"  [{aid}] Role: {sub['role']} | Status: {sub['status']}")
                        echo(
                            f"       Summary: {sub['output'][:120]}...\n"
                            if len(sub["output"]) > 120
                            else f"       Summary: {sub['output']}\n"
                        )
                continue
            if user_input.startswith("/install-skill "):
                url = user_input.split(" ", 1)[1].strip()
                secho(f"Installing skill from {url}...", fg=typer.colors.CYAN)
                from nimcode import skills

                result = skills.install_skill(url)
                secho(result, fg=typer.colors.GREEN, bold=True)
                continue
            elif user_input.strip().lower().startswith("/kill"):
                parts = user_input.strip().split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    secho(
                        "Usage: /kill <agent_id> or /kill <process_id>",
                        fg=typer.colors.YELLOW,
                    )
                else:
                    aid = parts[1].strip()
                    from nimcode.tools.swarm import ACTIVE_SUBAGENTS
                    from nimcode.tools.cmd import (
                        BACKGROUND_PROCESSES,
                        stop_process_tool,
                    )

                    if aid in ACTIVE_SUBAGENTS:
                        ACTIVE_SUBAGENTS[aid]["status"] = "KILLED"
                        ACTIVE_SUBAGENTS[aid]["output"] = (
                            "Killed by user slash command."
                        )
                        secho(
                            f"Subagent '{aid}' has been killed.", fg=typer.colors.GREEN
                        )
                    elif aid in BACKGROUND_PROCESSES:
                        res = stop_process_tool(
                            {"process_id": aid}, workspace_root, config
                        )
                        secho(res, fg=typer.colors.GREEN)
                    else:
                        secho(
                            f"No subagent or background process found with ID '{aid}'.",
                            fg=typer.colors.RED,
                        )
                continue
            elif user_input.lower() == "/model":
                from nimcode.config import select_interactive_model

                select_interactive_model(
                    config,
                    client,
                    workspace_root=workspace_root,
                    messages=messages,
                    session_path=session_path,
                )
                continue
            elif user_input.strip().lower() == "/compact":
                from nimcode.config import generate_summary_with_progress
                from nimcode.ui.output import console

                _con = console
                user_messages = [m for m in messages if m.get("role") == "user"]
                if len(user_messages) < 2:
                    _con.print(
                        "[yellow]Not enough conversation history to compact yet. Send at least one message first.[/yellow]\n"
                    )
                else:
                    # Estimate rough token count for display
                    total_chars = sum(
                        len(str(m.get("content", "")))
                        + len(str(m.get("tool_calls", "")))
                        for m in messages
                    )
                    est_tokens = total_chars // 4
                    _con.print(
                        f"[bold cyan][compact] /compact[/bold cyan] Current history: [bold]{len(messages)}[/bold] messages (~[bold]{est_tokens:,}[/bold] tokens).\n"
                        f"This will ask the model to summarise the conversation, then [bold red]erase[/bold red] the full history\n"
                        f"and replace it with the summary to free up context space.\n"
                    )
                    try:
                        from prompt_toolkit import prompt as pt_prompt
                        from prompt_toolkit.patch_stdout import patch_stdout

                        with patch_stdout():
                            ans = (
                                pt_prompt("Proceed with compaction? (y/n) [n]: ")
                                .strip()
                                .lower()
                            )
                        confirmed = ans in ("y", "yes")
                    except (KeyboardInterrupt, EOFError):
                        _con.print("[dim]Compact cancelled.[/dim]\n")
                        continue

                    if not confirmed:
                        _con.print("[dim]Compact cancelled.[/dim]\n")
                    else:
                        current_model = (
                            config.fast_model
                            if state != AgentState.AWAITING_PLAN_APPROVAL
                            else config.plan_model
                        )
                        _con.print(
                            f"[cyan]Asking {current_model} to summarise the conversation...[/cyan]"
                        )
                        success = generate_summary_with_progress(
                            client, messages, current_model, config, workspace_root
                        )
                        if success:
                            sys_prompt = None
                            if messages and messages[0].get("role") == "system":
                                sys_prompt = messages[0]
                            messages.clear()
                            if sys_prompt:
                                messages.append(sys_prompt)

                            summary_file = (
                                get_project_nimcode_dir(workspace_root) / "summary.md"
                            )
                            if summary_file.exists():
                                summary_text = summary_file.read_text(encoding="utf-8")
                                handoff_block = (
                                    "<model_handoff_context>\n"
                                    "Context has been compacted to save tokens.\n"
                                    "Here is the accurate summary (`summary.md`) of the previous conversation:\n\n"
                                    f"{summary_text}\n"
                                    "</model_handoff_context>\n"
                                )
                                inj_msg = {"role": "user", "content": handoff_block}
                                messages.append(inj_msg)
                                if session_path:
                                    log_turn(inj_msg, session_path)

                            after_chars = sum(
                                len(str(m.get("content", ""))) for m in messages
                            )
                            after_tokens = after_chars // 4
                            _con.print(
                                f"[bold green][OK] Compacted.[/bold green] History now: [bold]{len(messages)}[/bold] messages (~[bold]{after_tokens:,}[/bold] tokens).\n"
                            )
                        else:
                            _con.print(
                                "[red]Compaction failed; history has not been modified.[/red]\n"
                            )

                continue
            elif user_input.strip().lower() == "/recap":
                from nimcode.ui.output import console
                from rich.panel import Panel

                _con = console
                user_messages = [m for m in messages if m.get("role") == "user"]
                if len(user_messages) < 1:
                    _con.print(
                        "[yellow]No conversation history yet. Send at least one message first.[/yellow]\n"
                    )
                else:
                    current_model = (
                        config.fast_model
                        if state != AgentState.AWAITING_PLAN_APPROVAL
                        else config.plan_model
                    )
                    _con.print(
                        "[cyan]Asking Mistral Small to recap the session...[/cyan]"
                    )

                    recap_prompt = [
                        {
                            "role": "user",
                            "content": "Please provide a very brief, high-level summary of what has happened in this session so far (such as tasks completed, main code files edited, and current status). The summary must be at most 4 to 5 lines long.",
                        }
                    ]
                    pruned_msgs = prune_history(messages, config.context_budget_tokens)
                    recap_req_messages = list(pruned_msgs) + recap_prompt

                    try:
                        recap_res = call_nim_with_retry(
                            client,
                            recap_req_messages,
                            None,
                            "mistralai/mistral-small-4-119b-2603",
                            config,
                            timeout=30.0,
                        )
                    except Exception:
                        try:
                            recap_res = call_nim_with_retry(
                                client,
                                recap_req_messages,
                                None,
                                current_model,
                                config,
                                timeout=30.0,
                            )
                        except Exception as e:
                            _con.print(f"[red]Error generating recap: {e}[/red]\n")
                            continue

                    recap_text = strip_reasoning_spans(
                        recap_res.choices[0].message.content or ""
                    ).strip()
                    _con.print(
                        Panel(
                            recap_text,
                            title="Session Recap",
                            border_style="green",
                            padding=(1, 2),
                        )
                    )
                    _con.print()
                continue
            elif user_input.lower() == "/skills":
                from nimcode import skills

                skill_list = skills.list_skills()
                if not skill_list:
                    secho("No skills installed yet.", fg=typer.colors.CYAN)
                else:
                    secho("\nInstalled Skills:", fg=typer.colors.CYAN, bold=True)
                    for s in skill_list:
                        echo(f"  /{s}")
                continue
            elif user_input.lower() == "/init":
                run_init_command(config, client=client, workspace_root=workspace_root)
                continue
            elif user_input.lower() == "/resume":
                from nimcode.config import select_interactive_session

                selected_session = select_interactive_session(config, workspace_root)
                if selected_session:
                    try:
                        with open(selected_session, "r", encoding="utf-8") as f:
                            existing_msgs = json.load(f)
                        messages[:] = existing_msgs
                        session_path = selected_session
                        secho(
                            f"\nResumed session: {selected_session.name}",
                            fg=typer.colors.GREEN,
                            bold=True,
                        )
                        print_session_history(messages)

                        # Patch up dangling tool calls so the history is valid
                        if messages:
                            last_msg = messages[-1]
                            if last_msg.get("role") == "assistant" and last_msg.get(
                                "tool_calls"
                            ):
                                for tc in last_msg["tool_calls"]:
                                    dummy_res = {
                                        "role": "tool",
                                        "tool_call_id": tc.get("id"),
                                        "content": "Execution aborted due to session suspension.",
                                    }
                                    messages.append(dummy_res)
                                    log_turn(dummy_res, session_path)

                        # Show a brief summary of where the session left off
                        last_non_system = next(
                            (
                                m
                                for m in reversed(messages)
                                if m.get("role") != "system"
                            ),
                            None,
                        )
                        if last_non_system:
                            role = last_non_system.get("role", "?")
                            snippet = (
                                str(last_non_system.get("content") or "")
                                .strip()
                                .replace("\n", " ")
                            )
                            if len(snippet) > 120:
                                snippet = snippet[:120] + "..."
                            secho(
                                f"\nSession last left off with a [{role}] message:",
                                fg=typer.colors.CYAN,
                            )
                            secho(f"  {snippet}", fg=typer.colors.WHITE)

                        secho(
                            "\nWhat would you like to do?",
                            fg=typer.colors.CYAN,
                            bold=True,
                        )
                        secho(
                            "  y          → Continue where it left off (agent picks up from last state)",
                            fg=typer.colors.GREEN,
                        )
                        secho(
                            "  <message>  → Give the agent new instructions",
                            fg=typer.colors.GREEN,
                        )
                        secho(
                            "  n / enter  → Stay in session but do nothing (type your own prompt)",
                            fg=typer.colors.GREEN,
                        )

                        try:
                            sys.stdout.flush()
                            resume_choice = input(
                                "\nContinue / new instructions / n: "
                            ).strip()
                        except (KeyboardInterrupt, EOFError):
                            resume_choice = ""

                        if resume_choice.lower() == "y":
                            return "__RESUME_AI_LOOP__"
                        elif resume_choice.lower() in ("n", ""):
                            # Stay in session, loop back to get_user_turn for fresh prompt
                            secho(
                                "\nSession loaded. Type your instructions below.\n",
                                fg=typer.colors.CYAN,
                            )
                            continue
                        else:
                            # Treat whatever they typed as a new user message
                            user_msg = {"role": "user", "content": resume_choice}
                            messages.append(user_msg)
                            log_turn(user_msg, session_path)
                            return "__RESUME_AI_LOOP__"

                    except Exception as e:
                        secho(f"Error loading session: {e}", fg=typer.colors.RED)
                continue
            elif (
                user_input.startswith("/")
                and user_input.strip().lower().split()[0]
                not in [
                    "/exit",
                    "/quit",
                    "/help",
                    "/model",
                    "/config",
                    "/changes",
                    "/diff",
                    "/test",
                    "/team",
                    "/compact",
                    "/recap",
                    "/skills",
                    "/init",
                    "/resume",
                    "/kill",
                    "?",
                ]
                and not user_input.strip().lower().startswith("/install-skill")
            ):
                from nimcode import skills

                parts = user_input.split(" ", 1)
                skill_name = parts[0][1:]
                installed = skills.list_skills()
                if skill_name in installed:
                    content = skills.get_skill_content(skill_name)
                    rest_of_prompt = parts[1] if len(parts) > 1 else ""
                    secho(
                        f"Injecting context from skill: {skill_name}...",
                        fg=typer.colors.CYAN,
                    )
                    return f"<skill_context>\n{content}\n</skill_context>\n\n{rest_of_prompt}"
                else:
                    secho(
                        f"Unknown command: {user_input.split()[0]}. Type /help or ? for available commands.",
                        fg=typer.colors.YELLOW,
                    )
                    continue
            return user_input

    while True:
        try:
            enable_persistent_prompt(state, config, workspace_root, user_input)

            if skip_first_api_call:
                skip_first_api_call = False
                user_input = get_user_turn()
                if user_input is None or user_input.lower() in [
                    "exit",
                    "quit",
                    "/exit",
                    "/quit",
                    "q",
                    "done",
                ]:
                    secho(
                        "\nTask completed. Exiting nimcode session.",
                        fg=typer.colors.GREEN,
                        bold=True,
                    )
                    break

                if user_input == "__RESUME_AI_LOOP__":
                    loop_count = 0
                    continue

                user_msg = {"role": "user", "content": user_input}
                messages.append(user_msg)
                log_turn(user_msg, session_path)
                loop_count = 0
                continue

            # Refresh system prompt with latest AGENTS.md and live workspace tree
            if messages and len(messages) > 0 and messages[0].get("role") == "system":
                from nimcode.context import generate_workspace_map

                workspace_map = generate_workspace_map(str(workspace_root))
                messages[0]["content"] = build_system_prompt(
                    str(workspace_root),
                    workspace_map,
                    disable_native_tools=config.disable_native_tools,
                )

            current_model = (
                config.plan_model
                if state == AgentState.AWAITING_PLAN_APPROVAL
                else config.fast_model
            )
            # During AWAITING_PLAN_APPROVAL or when native tools are disabled, do not pass tools
            tools_arg = (
                None
                if (
                    state == AgentState.AWAITING_PLAN_APPROVAL
                    or config.disable_native_tools
                )
                else AVAILABLE_TOOLS_SPEC
            )

            if config.pending_summary_injection:
                summary_file = (
                    get_project_nimcode_dir(
                        workspace_root if workspace_root else Path.cwd()
                    )
                    / "summary.md"
                )
                if summary_file.exists():
                    summary_text = summary_file.read_text(encoding="utf-8")
                    old_m = config.pending_summary_old_model or "previous model"
                    new_m = config.pending_summary_new_model or current_model
                    handoff_block = f"<model_handoff_context>\nAI Model has been switched from {old_m} to {new_m}.\nTo ensure proper context continuity, here is the accurate summary (`summary.md`) generated by the previous model:\n\n{summary_text}\n</model_handoff_context>\n\n"
                    last_user_idx = -1
                    for idx in range(len(messages) - 1, -1, -1):
                        if messages[idx].get("role") == "user":
                            last_user_idx = idx
                            break
                    if last_user_idx != -1:
                        messages[last_user_idx]["content"] = handoff_block + str(
                            messages[last_user_idx].get("content", "")
                        )
                        if session_path:
                            log_turn(messages[last_user_idx], session_path)
                    else:
                        if messages and messages[-1].get("role") == "tool":
                            dummy_ack = {
                                "role": "assistant",
                                "content": "Context handoff.",
                            }
                            messages.append(dummy_ack)
                            if session_path:
                                log_turn(dummy_ack, session_path)
                        inj_msg = {
                            "role": "user",
                            "content": handoff_block
                            + "Please review this context and proceed.",
                        }
                        messages.append(inj_msg)
                        if session_path:
                            log_turn(inj_msg, session_path)
                    secho(
                        f"\nInjected summary.md context into prompt for {current_model}.",
                        fg=typer.colors.CYAN,
                        bold=True,
                    )
                config.pending_summary_injection = False

            try:
                import sys
                import rich.spinner
                from nimcode.ui.output import console

                rich.spinner.SPINNERS["working_dots"] = {
                    "interval": 300,
                    "frames": ["working", "working.", "working..", "working..."],
                }
                c = console
                import shutil

                term_height = shutil.get_terminal_size().lines
                sys.stdout.write(f"\033[{term_height - 3};1H")
                sys.stdout.flush()
                try:
                    with c.status(
                        "[dim](Press esc to cancel)[/dim]",
                        spinner="working_dots",
                        spinner_style="cyan",
                    ):
                        response = call_nim_with_retry(
                            client, messages, tools_arg, current_model, config
                        )
                finally:
                    sys.stdout.write(f"\033[{term_height - 4};1H")
                    sys.stdout.flush()

            except Exception as e:
                secho(f"\nFatal API error: {e}", fg=typer.colors.RED, bold=True)
                break

            raw_message = response.choices[0].message
            cleaned_content = strip_reasoning_spans(raw_message.content or "")

            if config.disable_native_tools:
                xml_calls = parse_xml_tool_calls(cleaned_content)
                raw_message = MockMessage(cleaned_content, xml_calls)

            # Build serializable message for history
            msg_dict: Dict[str, Any] = {"role": "assistant", "content": cleaned_content}
            if raw_message.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in raw_message.tool_calls
                ]
            messages.append(msg_dict)
            log_turn(msg_dict, session_path)

            # State 1: AWAITING_PLAN_APPROVAL
            if state == AgentState.AWAITING_PLAN_APPROVAL:
                plan_text = extract_plan_block(cleaned_content)
                if not plan_text:
                    secho(
                        "\n--- Proposed Architectural Plan ---",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                    echo(cleaned_content)
                    secho(
                        "-----------------------------------",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                else:
                    secho(
                        "\n--- Proposed Architectural Plan (<plan>) ---",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                    echo(plan_text)
                    secho(
                        "--------------------------------------------",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )

                approval = (
                    prompt_user(
                        "Approve this plan to begin execution? (y/n/yolo for yolo mode)"
                    )
                    .strip()
                    .lower()
                )
                is_yolo_target = approval == "yolo" or (
                    approval == "y"
                    and (
                        state == AgentState.YOLO
                        or getattr(config, "mode", "") == "yolo"
                    )
                )
                if approval in ("q", "n", ""):
                    # Cancel plan — stay in session, let user redirect
                    secho(
                        "\nPlan cancelled. You can refine your request or type a new prompt.\n",
                        fg=typer.colors.YELLOW,
                        bold=True,
                    )
                    cancel_msg = {
                        "role": "user",
                        "content": "[Plan rejected/cancelled by user. Awaiting new instructions.]",
                    }
                    messages.append(cancel_msg)
                    log_turn(cancel_msg, session_path)
                    loop_count = 0
                    user_input = get_user_turn()
                    if user_input is None or user_input.lower() in [
                        "exit",
                        "quit",
                        "/exit",
                        "/quit",
                    ]:
                        secho(
                            "\nExiting nimcode session.",
                            fg=typer.colors.GREEN,
                            bold=True,
                        )
                        break
                    if user_input == "__RESUME_AI_LOOP__":
                        loop_count = 0
                        continue
                    user_msg = {"role": "user", "content": user_input}
                    messages.append(user_msg)
                    log_turn(user_msg, session_path)
                    loop_count = 0
                    continue
                elif is_yolo_target:
                    state = AgentState.YOLO
                    config.mode = "yolo"
                    current_model = config.fast_model
                    secho(
                        f"\nPlan approved! Switching to {state} state (Auto-approval enabled) with fast model: {current_model}\n",
                        fg=typer.colors.RED,
                        bold=True,
                    )
                    trigger_msg = {
                        "role": "user",
                        "content": "Plan approved. Please proceed with step 1 using tool calls.",
                    }
                    messages.append(trigger_msg)
                    log_turn(trigger_msg, session_path)
                    continue
                elif approval != "y":
                    feedback = prompt_user(
                        "Plan rejected. Enter feedback/revisions for the model"
                    )
                    user_msg = {
                        "role": "user",
                        "content": f"Plan rejected by user. Feedback: {feedback}. Please revise the <plan>.",
                    }
                    messages.append(user_msg)
                    log_turn(user_msg, session_path)
                    continue

                state = AgentState.EXECUTING
                config.mode = "build"
                current_model = config.fast_model
                secho(
                    f"\nPlan approved! Switching to {state} state with fast model: {current_model}\n",
                    fg=typer.colors.GREEN,
                    bold=True,
                )
                # Ask the model to begin executing step 1
                trigger_msg = {
                    "role": "user",
                    "content": "Plan approved. Please proceed with step 1 using tool calls.",
                }
                messages.append(trigger_msg)
                log_turn(trigger_msg, session_path)
                continue

            # State 2: EXECUTING
            if not raw_message.tool_calls:
                if cleaned_content:
                    echo(f"\n{cleaned_content}")

                user_input = get_user_turn()
                if user_input is None or user_input.lower() in [
                    "exit",
                    "quit",
                    "/exit",
                    "/quit",
                    "q",
                    "done",
                ]:
                    secho(
                        "\nTask completed. Exiting nimcode session.",
                        fg=typer.colors.GREEN,
                        bold=True,
                    )
                    break

                if user_input == "__RESUME_AI_LOOP__":
                    loop_count = 0
                    continue

                user_msg = {"role": "user", "content": user_input}
                messages.append(user_msg)
                log_turn(user_msg, session_path)
                loop_count = 0  # Reset loop counter on human input turn
                continue

            # If in YOLO mode, allow up to 50 steps before checking in, otherwise use config limit
            loop_limit = 50 if state == AgentState.YOLO else config.max_loop_iterations
            if loop_count >= loop_limit:
                secho(
                    f"\nWarning: Max consecutive loop iterations ({loop_limit}) reached without human check-in. Stopping runaway execution.",
                    fg=typer.colors.YELLOW,
                    bold=True,
                )
                user_input = prompt_user(
                    "Resume loop? (y to continue / feedback to redirect / n or q to cancel & stay)"
                ).strip()
                if user_input.lower() in ("q", "n", ""):
                    # Cancel the current loop — inject cancellation results and hand back to user
                    secho(
                        "\nLoop cancelled. You're still in the session — type a new prompt.\n",
                        fg=typer.colors.YELLOW,
                        bold=True,
                    )
                    if raw_message and raw_message.tool_calls:
                        for tc in raw_message.tool_calls:
                            cancel_msg = {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "Execution cancelled by user (loop limit reached). Awaiting new instructions.",
                            }
                            messages.append(cancel_msg)
                            log_turn(cancel_msg, session_path)
                    loop_count = 0
                    user_input = get_user_turn()
                    if user_input is None or user_input.lower() in [
                        "exit",
                        "quit",
                        "/exit",
                        "/quit",
                    ]:
                        secho(
                            "\nExiting nimcode session.",
                            fg=typer.colors.GREEN,
                            bold=True,
                        )
                        break
                    if user_input == "__RESUME_AI_LOOP__":
                        loop_count = 0
                        continue
                    user_msg = {"role": "user", "content": user_input}
                    messages.append(user_msg)
                    log_turn(user_msg, session_path)
                    loop_count = 0
                    continue
                elif user_input.lower() != "y":
                    if raw_message and raw_message.tool_calls:
                        for tc in raw_message.tool_calls:
                            cancel_msg = {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Execution redirected by user. Feedback: {user_input}",
                            }
                            messages.append(cancel_msg)
                            log_turn(cancel_msg, session_path)
                    loop_count = 0
                    continue
                loop_count = 0

            loop_count += 1

            # Process tool calls
            for tool_call in raw_message.tool_calls:
                is_valid, tool_name, args = is_valid_tool_call(tool_call)
                if not is_valid:
                    raw_args = getattr(
                        getattr(tool_call, "function", None), "arguments", ""
                    )
                    result = (
                        f"Error: Tool call '{tool_name}' was rejected (malformed or unknown). "
                        f"Raw arguments received: {raw_args!r}. "
                        "Please re-issue the tool call with valid JSON arguments matching the tool schema. "
                        "Do NOT wrap JSON in markdown code fences."
                    )
                    secho(f"\nWarning: {result}", fg=typer.colors.RED)
                else:
                    preview = render_preview(
                        tool_name, args, workspace_root, config=config
                    )
                    echo(preview)

                    if state == AgentState.YOLO:
                        secho(
                            f"[YOLO Mode] Auto-approving execution of {tool_name}...",
                            fg=typer.colors.RED,
                            bold=True,
                        )
                        approval = "y"
                    else:
                        approval = (
                            prompt_user(
                                f"Approve execution of {tool_name}? (y/n to skip)"
                            )
                            .strip()
                            .lower()
                        )
                    if approval == "q":
                        # Treat q same as n — cancel this tool call, stay in session
                        approval = "n"
                    elif approval == "y":
                        result = execute_local_tool(
                            tool_name, args, workspace_root, config
                        )
                        secho(
                            f"--- Output ({len(result)} chars) ---\n{result}\n-----------------------------",
                            fg=typer.colors.CYAN,
                        )
                        if state == AgentState.YOLO and tool_name in [
                            "write_file",
                            "patch_file_ast",
                            "patch_file_blocks",
                        ]:
                            from nimcode.tools import execute_test_runner, tests_passed

                            secho(
                                "\n[YOLO Mode] Code changed. Auto-running test suite for verification...",
                                fg=typer.colors.RED,
                                bold=True,
                            )
                            test_res = execute_test_runner(workspace_root, config)
                            secho(
                                f"--- Test Results ---\n{test_res}\n--------------------",
                                fg=typer.colors.CYAN,
                            )
                            if not tests_passed(test_res):
                                result += f"\n\n[CRITICAL WARNING: Auto-run tests failed! Please fix this immediately]\n{test_res}"
                    else:
                        feedback = prompt_user(
                            "Execution rejected. Enter feedback/reason (or leave blank for 'Rejected by user')"
                        ).strip()
                        result = feedback if feedback else "Rejected by user."
                        secho("Execution rejected.", fg=typer.colors.YELLOW)

                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
                messages.append(tool_result_msg)
                log_turn(tool_result_msg, session_path)

            messages = prune_history(messages, config.context_budget_tokens)

        except KeyboardInterrupt:
            secho(
                "\n\n[Stopped by ESC/Interrupt] Stopped working on current prompt. Standing by for next prompt...\n",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            if messages and messages[-1].get("role") == "tool":
                dummy_ack = {
                    "role": "assistant",
                    "content": "Tool execution interrupted.",
                }
                messages.append(dummy_ack)
                log_turn(dummy_ack, session_path)

            user_msg = {
                "role": "user",
                "content": "[Operation interrupted and cancelled by user via ESC/Ctrl+C. Standing by for new instructions.]",
            }
            messages.append(user_msg)
            log_turn(user_msg, session_path)
            loop_count = 0

            user_input = get_user_turn()
            if user_input is None or user_input.lower() in [
                "exit",
                "quit",
                "/exit",
                "/quit",
                "q",
                "done",
            ]:
                secho(
                    "\nTask completed. Exiting nimcode session.",
                    fg=typer.colors.GREEN,
                    bold=True,
                )
                break

            if user_input == "__RESUME_AI_LOOP__":
                loop_count = 0
                continue

            user_msg = {"role": "user", "content": user_input}
            messages.append(user_msg)
            log_turn(user_msg, session_path)
            continue

    from nimcode.tools.cmd import cleanup_background_processes

    cleanup_background_processes()
    disable_persistent_prompt()
    atexit.unregister(disable_persistent_prompt)
