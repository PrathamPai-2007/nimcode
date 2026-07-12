import os
import sys
import json
import time
import typer
from pathlib import Path
from typing import List, Dict, Any, Optional
from openai import OpenAI

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

    # Remove ```json ... ``` or ``` ... ``` fences
    raw = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```", "", raw)
    return raw.strip()


def parse_xml_tool_calls(text: str) -> List[MockToolCall]:
    import re

    tool_calls = []
    # Match <tool_call name="...">...</tool_call> with quoted or unquoted name
    matches = re.finditer(
        r"<tool_call\s+name=[\"\']?(.*?)[\"\']?\s*>(.*?)</tool_call>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    for idx, match in enumerate(matches):
        name = match.group(1).strip().strip("'\"")
        args_str = _clean_json_args(match.group(2))
        tc_id = f"call_xml_{idx}_{int(time.time())}"
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
        return {k: clean_surrogates(v) for k, v in val.items()}
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
    merged_others: List[Dict[str, Any]] = []
    for msg in other_messages:
        if (
            not msg.get("content")
            and not msg.get("tool_calls")
            and msg.get("role") != "assistant"
        ):
            # Skip empty content if it's not assistant (which could have tool_calls)
            continue
        if not merged_others:
            merged_others.append(msg)
            continue

        last = merged_others[-1]
        if last.get("role") == msg.get("role") and last.get("role") in (
            "user",
            "assistant",
        ):
            last_content = last.get("content") or ""
            msg_content = msg.get("content") or ""
            if last_content and msg_content:
                last["content"] = f"{last_content}\n\n{msg_content}"
            elif msg_content:
                last["content"] = msg_content

            if "tool_calls" in msg:
                last["tool_calls"] = last.get("tool_calls", []) + msg["tool_calls"]
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


def call_nim_with_retry(
    client: OpenAI,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    model: str,
    config: Config,
    max_retries: int = 3,
    timeout: Optional[float] = None,
):
    """
    Calls the NVIDIA NIM OpenAI endpoint with exponential backoff for transient errors.
    Supports instant cancellation via ESC/Ctrl+C without freezing.
    """
    import threading

    # Prune history to satisfy context budget constraint!
    pruned_messages = prune_history(messages, config.context_budget_tokens)

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
                if sys.platform == "win32":
                    import msvcrt

                    if msvcrt.kbhit():
                        key = msvcrt.getch()
                        if key in [b"\x1b", b"\x03"]:
                            raise KeyboardInterrupt("ESC_CANCELLED")
                time.sleep(0.05)

            if "err" in error_box:
                raise error_box["err"]

            res = result_box.get("res")
            if res and hasattr(res, "choices") and not res.choices:
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
                secho(
                    "\nWarning: Native auto tool choice is not supported/configured on this server.",
                    fg=typer.colors.YELLOW,
                    bold=True,
                )
                secho(
                    "Falling back to prompt-based XML tool calling...\n",
                    fg=typer.colors.YELLOW,
                )
                config.disable_native_tools = True
                # Re-build system prompt for messages to include XML instructions
                if messages and messages[0].get("role") == "system":
                    from nimcode.context import generate_workspace_map

                    w_map = generate_workspace_map(str(config.workspace_root))
                    messages[0]["content"] = build_system_prompt(
                        str(config.workspace_root), w_map, disable_native_tools=True
                    )
                # Retry immediately
                return call_nim_with_retry(
                    client, messages, None, model, config, max_retries=max_retries
                )

            # Catch OpenAI API errors or timeout errors
            if attempt == max_retries - 1:
                raise
            wait_time = (2**attempt) * 2
            echo(
                f"\nAPI error ({e}). Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(wait_time)


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

    # Calculate current token estimate
    def get_tokens(msg_list):
        return sum(
            (len(str(m.get("content", ""))) + len(str(m.get("tool_calls", "")))) // 4
            for m in msg_list
        )

    estimated_tokens = get_tokens(messages)
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
    if get_tokens(pruned) <= context_budget_tokens:
        return pruned

    # Step 2: Over budget even after tool truncation.
    # We must drop older messages. We must keep:
    # - System message at index 0 (if present)
    # - First user query at index 1 (if present)
    # - The most recent messages
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

    essential_tokens = get_tokens(essential)
    budget_left = context_budget_tokens - essential_tokens

    # Collect remaining messages from the end of the list until we run out of budget
    recent_msgs = []
    for msg in reversed(pruned[start_idx:]):
        msg_tokens = get_tokens([msg])
        if budget_left - msg_tokens >= 0 or not recent_msgs:
            recent_msgs.append(msg)
            budget_left -= msg_tokens
        else:
            # Out of budget! Stop including older messages.
            break

    recent_msgs.reverse()

    # Construct pruned list
    final_messages = essential + recent_msgs
    return final_messages


def log_turn(message_or_dict: Any, session_path: Path):
    """
    Logs the turn and persists session history to the specific session file.
    """
    try:
        data = []
        if session_path.exists():
            with open(session_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        if isinstance(message_or_dict, dict):
            entry = message_or_dict
        else:
            entry = {
                "role": getattr(message_or_dict, "role", "assistant"),
                "content": getattr(message_or_dict, "content", ""),
            }
            if hasattr(message_or_dict, "tool_calls") and message_or_dict.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message_or_dict.tool_calls
                ]

        data.append(entry)
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        # Don't crash agent loop on logging issue
        pass


def run_init_command(
    config: Config,
    client: Optional[OpenAI] = None,
    workspace_root: Optional[Path] = None,
) -> str:
    """
    Initializes or updates an AGENTS.md file in the workspace root.
    Scans project tech stack markers and generates/enriches AGENTS.md.
    """
    from nimcode.ui.output import console

    root = Path(workspace_root if workspace_root else config.workspace_root).resolve()
    agents_md_path = root / "AGENTS.md"

    console.print(
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
    console.print(f"[dim]Detected Tech Stack: {stack_summary}[/dim]")

    existing_content = ""
    is_update = agents_md_path.exists()
    if is_update:
        existing_content = agents_md_path.read_text(encoding="utf-8").strip()
        console.print(
            f"[yellow]Found existing AGENTS.md ({len(existing_content)} chars). Updating/Enriching...[/yellow]"
        )
    else:
        console.print("[cyan]Creating new AGENTS.md...[/cyan]")

    # If we have an active client, use the AI model to generate/update
    if client is not None:
        console.print(
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
            if new_content.startswith("```markdown"):
                new_content = new_content[len("```markdown") :].strip()
                if new_content.endswith("```"):
                    new_content = new_content[:-3].strip()
            elif new_content.startswith("```"):
                new_content = new_content[3:].strip()
                if new_content.endswith("```"):
                    new_content = new_content[:-3].strip()

            if new_content:
                agents_md_path.write_text(new_content, encoding="utf-8")
                msg = f"AGENTS.md successfully {'updated' if is_update else 'created'} via AI at {agents_md_path}"
                console.print(f"[bold green]{msg}[/bold green]")
                return msg
        except Exception as e:
            console.print(
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
        console.print(f"[bold green]{msg}[/bold green]")
        return msg
    else:
        msg = f"AGENTS.md already exists at {agents_md_path} and client is offline."
        console.print(f"[yellow]{msg}[/yellow]")
        return msg


def enable_persistent_prompt(state, config, workspace_root, last_input):
    import shutil
    import sys
    from nimcode.config import AVAILABLE_MODELS

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
    model_name = model_id
    for m in AVAILABLE_MODELS.values():
        if m["id"] == model_id:
            model_name = m["name"]
            break

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
        if messages:
            last_msg = messages[-1]
            if last_msg.get("role") == "assistant" and not last_msg.get("tool_calls"):
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
        disable_persistent_prompt()
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.patch_stdout import patch_stdout

        prompt_text = (
            text
            if text.endswith(": ")
            else (text + " " if text.endswith(":") else text + ": ")
        )
        try:
            with patch_stdout():
                res = pt_prompt(prompt_text)
        except (KeyboardInterrupt, EOFError):
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
                from nimcode.config import AVAILABLE_MODELS

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

                model_name = model_id
                for m in AVAILABLE_MODELS.values():
                    if m["id"] == model_id:
                        model_name = m["name"]
                        break

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
                                return "__RESUME_AI_LOOP__"
                            elif last_msg.get("role") in ("user", "tool"):
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
                        "Approve this plan to begin execution? (y/n/yolo for yolo mode/q to quit)"
                    )
                    .strip()
                    .lower()
                )
                if approval == "q":
                    echo("Exiting nimcode session.")
                    break
                elif approval == "yolo":
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
                    "Resume loop? (y to continue / enter feedback to redirect / q to quit)"
                ).strip()
                if user_input.lower() == "q":
                    break
                elif user_input.lower() != "y":
                    for tc in raw_message.tool_calls:
                        cancel_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"Execution cancelled by user. Feedback: {user_input}",
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
                                f"Approve execution of {tool_name}? (y/n/q to quit)"
                            )
                            .strip()
                            .lower()
                        )
                    if approval == "q":
                        echo("Exiting nimcode session.")
                        disable_persistent_prompt()
                        atexit.unregister(disable_persistent_prompt)
                        return
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
                            from nimcode.tools import execute_test_runner

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
                            if (
                                "[exit code: 0]" not in test_res
                                and "exit code: 0" not in test_res
                            ):
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
