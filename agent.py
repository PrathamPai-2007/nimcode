import os
import sys

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import time
import typer
from pathlib import Path
from typing import List, Dict, Any, Optional
from openai import OpenAI

from config import Config
from prompts import build_system_prompt, strip_reasoning_spans, extract_plan_block
from tools import (
    AVAILABLE_TOOLS_SPEC,
    is_valid_tool_call,
    render_preview,
    execute_local_tool,
)

SESSION_DIR = ".nimcode/sessions"


def call_nim_with_retry(
    client: OpenAI,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    model: str,
    config: Config,
    max_retries: int = 3,
):
    """
    Calls the NVIDIA NIM OpenAI endpoint with exponential backoff for transient errors.
    Supports instant cancellation via ESC/Ctrl+C without freezing.
    """
    import threading

    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": config.temperature_plan
                if model == config.plan_model
                else config.temperature_fast,
                "timeout": float(config.timeout),
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

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
            return result_box["res"]
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # Catch OpenAI API errors or timeout errors
            if attempt == max_retries - 1:
                raise
            wait_time = (2**attempt) * 2
            typer.echo(
                f"\n⚠️ API error ({e}). Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(wait_time)


def prune_history(
    messages: List[Dict[str, Any]],
    context_budget_tokens: int = 16000,
    max_turns_retained: int = 20,
) -> List[Dict[str, Any]]:
    """
    Prunes long conversation histories by keeping the initial system and user messages,
    and truncating older tool responses while preserving the most recent turns.
    Respects both token budget estimates (~4 chars/token) and turn counts.
    """
    total_chars = sum(
        len(str(m.get("content", ""))) + len(str(m.get("tool_calls", "")))
        for m in messages
    )
    estimated_tokens = total_chars // 4

    if (
        len(messages) <= max_turns_retained
        and estimated_tokens <= context_budget_tokens
    ):
        return messages

    pruned = []
    # Always keep system message and initial user prompt
    for i in range(min(2, len(messages))):
        pruned.append(messages[i])

    # For the middle messages, truncate heavy tool contents if over budget or turn limit
    cutoff_index = max(2, len(messages) - max_turns_retained)
    for i in range(2, len(messages)):
        msg = messages[i]
        if i < cutoff_index:
            # If over token budget significantly, we may skip or heavily summarize older tool output
            if msg.get("role") == "tool":
                pruned_msg = dict(msg)
                pruned_msg["content"] = (
                    str(msg.get("content", ""))[:150]
                    + "\n... [Older tool result truncated to conserve context]"
                )
                pruned.append(pruned_msg)
                continue
        pruned.append(msg)

    return pruned


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
    from rich.console import Console

    console = Console()

    root = Path(workspace_root if workspace_root else config.workspace_root).resolve()
    agents_md_path = root / "AGENTS.md"

    console.print(
        f"[bold cyan]🔍 Scanning workspace at {root} for initialization markers...[/bold cyan]"
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
            f"[yellow]⚠️ Found existing AGENTS.md ({len(existing_content)} chars). Updating/Enriching...[/yellow]"
        )
    else:
        console.print("[cyan]Creating new AGENTS.md...[/cyan]")

    # If we have an active client, use the AI model to generate/update
    if client is not None:
        console.print(
            f"[cyan]🤖 Asking model ({config.fast_model}) to generate/enrich AGENTS.md for this repository...[/cyan]"
        )
        try:
            from context import generate_workspace_map

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
                msg = f"✅ AGENTS.md successfully {'updated' if is_update else 'created'} via AI at {agents_md_path}"
                console.print(f"[bold green]{msg}[/bold green]")
                return msg
        except Exception as e:
            console.print(
                f"[yellow]⚠️ AI generation failed ({e}). Falling back to template initialization.[/yellow]"
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
        msg = f"✅ AGENTS.md initialized from template at {agents_md_path}"
        console.print(f"[bold green]{msg}[/bold green]")
        return msg
    else:
        msg = f"ℹ️ AGENTS.md already exists at {agents_md_path} and client is offline."
        console.print(f"[yellow]{msg}[/yellow]")
        return msg


def enable_persistent_prompt(state, config, workspace_root, last_input):
    import shutil
    import sys
    from config import AVAILABLE_MODELS

    term_width, term_height = shutil.get_terminal_size()
    # Scroll region: lines 1 to height-4
    sys.stdout.write(f"\033[1;{term_height - 4}r")

    if state == "AWAITING_PLAN_APPROVAL" or getattr(config, "mode", "") == "plan":
        mode_name = "PLAN MODE"
        sep_class_ansi = "\033[1;34m"  # Blue
    elif state == "YOLO" or getattr(config, "mode", "") == "yolo":
        mode_name = "YOLO MODE"
        sep_class_ansi = "\033[1;31m"  # Red
    else:
        mode_name = "BUILD MODE"
        sep_class_ansi = "\033[1;32m"  # Green

    pwd = os.getcwd()
    model_id = (
        config.fast_model if state != "AWAITING_PLAN_APPROVAL" else config.plan_model
    )
    model_name = model_id
    for m in AVAILABLE_MODELS.values():
        if m["id"] == model_id:
            model_name = m["name"]
            break

    mode_label = f"── {mode_name} "
    remaining_upper = term_width - len(mode_label)
    upper_border = mode_label + ("─" * max(0, remaining_upper))

    left_str = f"── ({pwd}) "
    right_str = f" ({model_name}) ──"
    padding = term_width - len(left_str) - len(right_str)
    if padding < 0:
        avail = term_width - len(right_str) - 6
        if avail > 5:
            left_str = f"── (..{pwd[-(avail - 2) :]}) "
        else:
            left_str = "── "
        padding = term_width - len(left_str) - len(right_str)
    lower_border = left_str + ("─" * max(0, padding)) + right_str

    sys.stdout.write(
        f"\033[{term_height - 2};1H\033[2K{sep_class_ansi}{upper_border}\033[0m"
    )

    input_str = last_input or ""
    input_str = input_str.replace("\n", " ")
    if len(input_str) > term_width - 6:
        input_str = input_str[: term_width - 9] + "..."
    sys.stdout.write(f"\033[{term_height - 1};1H\033[2K\033[1;36m❯ \033[0m{input_str}")
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
    session_dir = workspace_root / SESSION_DIR
    session_dir.mkdir(parents=True, exist_ok=True)

    legacy_session = workspace_root / ".nimcode_session.json"
    if legacy_session.exists():
        import shutil

        shutil.move(str(legacy_session), str(session_dir / "legacy_session.json"))
        typer.secho(
            f"\n📁 Migrated legacy session to {SESSION_DIR}/legacy_session.json",
            fg=typer.colors.CYAN,
        )

    if not session_file_name:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        session_file_name = f"session_{timestamp}.json"

    session_path = session_dir / session_file_name
    user_input = user_prompt

    messages = []
    state = initial_state
    if state == "YOLO":
        config.mode = "yolo"
    elif state == "AWAITING_PLAN_APPROVAL":
        config.mode = "plan"
    else:
        config.mode = "build"

    if existing_messages:
        messages = existing_messages
    else:
        system_prompt = build_system_prompt(str(workspace_root), workspace_map)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    loop_count = 0

    def prompt_user(text, **kwargs):
        disable_persistent_prompt()
        res = typer.prompt(text, **kwargs)
        enable_persistent_prompt(state, config, workspace_root, user_input)
        return res

    def get_user_turn() -> Optional[str]:
        nonlocal state
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
                "/skills",
                "/install-skill",
                "/init",
                "/quit",
                "/exit",
            ]
            try:
                import skills

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
                    if state == "AWAITING_PLAN_APPROVAL":
                        state = "EXECUTING"
                        config.mode = "build"
                    elif state == "EXECUTING":
                        state = "YOLO"
                        config.mode = "yolo"
                    else:
                        state = "AWAITING_PLAN_APPROVAL"
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
            session = PromptSession(key_bindings=bindings, style=style)

            def get_prompt_message():
                import shutil

                term_width = shutil.get_terminal_size().columns
                if (
                    state == "AWAITING_PLAN_APPROVAL"
                    or getattr(config, "mode", "") == "plan"
                ):
                    mode_name = "PLAN MODE"
                    sep_class = "class:sep-plan"
                elif state == "YOLO" or getattr(config, "mode", "") == "yolo":
                    mode_name = "YOLO MODE"
                    sep_class = "class:sep-yolo"
                else:
                    mode_name = "BUILD MODE"
                    sep_class = "class:sep-build"

                mode_label = f"── {mode_name} "
                remaining_width = term_width - len(mode_label)
                if remaining_width < 0:
                    sep = mode_label[:term_width] + "\n"
                else:
                    sep = mode_label + ("─" * remaining_width) + "\n"
                return FormattedText([(sep_class, sep), ("class:prompt", "❯ ")])

            def get_bottom_toolbar():
                import shutil
                from config import AVAILABLE_MODELS

                term_width = shutil.get_terminal_size().columns
                if (
                    state == "AWAITING_PLAN_APPROVAL"
                    or getattr(config, "mode", "") == "plan"
                ):
                    sep_class = "class:sep-plan"
                elif state == "YOLO" or getattr(config, "mode", "") == "yolo":
                    sep_class = "class:sep-yolo"
                else:
                    sep_class = "class:sep-build"
                "─" * term_width + "\n"

                pwd = os.getcwd()
                model_id = (
                    config.fast_model
                    if state != "AWAITING_PLAN_APPROVAL"
                    else config.plan_model
                )

                model_name = model_id
                for m in AVAILABLE_MODELS.values():
                    if m["id"] == model_id:
                        model_name = m["name"]
                        break

                left_str = f"── ({pwd}) "
                right_str = f" ({model_name}) ──"

                padding = term_width - len(left_str) - len(right_str)
                if padding < 0:
                    avail = term_width - len(right_str) - 6
                    if avail > 5:
                        left_str = f"── (..{pwd[-(avail - 2) :]}) "
                    else:
                        left_str = "── "
                    padding = term_width - len(left_str) - len(right_str)

                status_line = left_str + ("─" * max(0, padding)) + right_str
                return FormattedText([(sep_class, status_line)])

            disable_persistent_prompt()
            typer.secho("\nUser Input (or 'exit' to finish task)", fg=typer.colors.CYAN)
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
                typer.echo("\nPrompt cancelled. Exiting.")
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
                    state = "AWAITING_PLAN_APPROVAL"
                    config.mode = "plan"
                    typer.secho(
                        "\n📋 Switched to Plan Mode (AWAITING_PLAN_APPROVAL). Tools disabled until plan is approved.\n",
                        fg=typer.colors.CYAN,
                        bold=True,
                    )
                elif cmd == "/build":
                    state = "EXECUTING"
                    config.mode = "build"
                    typer.secho(
                        "\n🔨 Switched to Build Mode (EXECUTING with human-in-the-loop approval).\n",
                        fg=typer.colors.GREEN,
                        bold=True,
                    )
                elif cmd == "/yolo":
                    state = "YOLO"
                    config.mode = "yolo"
                    typer.secho(
                        "\n⚡ Switched to YOLO Mode (EXECUTING with auto-approval of edits and commands).\n",
                        fg=typer.colors.RED,
                        bold=True,
                    )
                if len(parts) > 1 and parts[1].strip():
                    return parts[1].strip()
                continue
            if user_input.startswith("/install-skill "):
                url = user_input.split(" ", 1)[1].strip()
                typer.secho(f"Installing skill from {url}...", fg=typer.colors.CYAN)
                import skills

                result = skills.install_skill(url)
                typer.secho(result, fg=typer.colors.GREEN, bold=True)
                continue
            elif user_input.lower() == "/model":
                from config import select_interactive_model

                select_interactive_model(
                    config,
                    client=client,
                    messages=messages,
                    workspace_root=workspace_root,
                    session_path=session_path,
                )
                continue
            elif user_input.lower() == "/skills":
                import skills

                skill_list = skills.list_skills()
                if not skill_list:
                    typer.secho("No skills installed yet.", fg=typer.colors.CYAN)
                else:
                    typer.secho("\nInstalled Skills:", fg=typer.colors.CYAN, bold=True)
                    for s in skill_list:
                        typer.echo(f"  /{s}")
                continue
            elif user_input.lower() == "/init":
                run_init_command(config, client=client, workspace_root=workspace_root)
                continue
            elif user_input.startswith("/") and user_input.lower() not in [
                "/exit",
                "/quit",
                "/help",
                "/model",
                "/skills",
                "/init",
                "?",
            ]:
                import skills

                parts = user_input.split(" ", 1)
                skill_name = parts[0][1:]
                installed = skills.list_skills()
                if skill_name in installed:
                    content = skills.get_skill_content(skill_name)
                    rest_of_prompt = parts[1] if len(parts) > 1 else ""
                    typer.secho(
                        f"Injecting context from skill: {skill_name}...",
                        fg=typer.colors.CYAN,
                    )
                    return f"<skill_context>\n{content}\n</skill_context>\n\n{rest_of_prompt}"
            return user_input

    while True:
        try:
            enable_persistent_prompt(state, config, workspace_root, user_input)
            # Refresh system prompt with latest AGENTS.md and live workspace tree
            if messages and len(messages) > 0 and messages[0].get("role") == "system":
                from context import generate_workspace_map

                workspace_map = generate_workspace_map(str(workspace_root))
                messages[0]["content"] = build_system_prompt(
                    str(workspace_root), workspace_map
                )

            current_model = (
                config.plan_model
                if state == "AWAITING_PLAN_APPROVAL"
                else config.fast_model
            )
            # During AWAITING_PLAN_APPROVAL, do not pass tools so the model focuses purely on <plan> output
            tools_arg = (
                None if state == "AWAITING_PLAN_APPROVAL" else AVAILABLE_TOOLS_SPEC
            )

            try:
                import sys
                import rich.spinner
                from rich.console import Console

                rich.spinner.SPINNERS["working_dots"] = {
                    "interval": 300,
                    "frames": ["working", "working.", "working..", "working..."],
                }

                c = Console()
                import shutil

                term_height = shutil.get_terminal_size().lines
                sys.stdout.write(f"\033[{term_height - 3};1H")
                sys.stdout.flush()
                try:
                    with c.status("", spinner="working_dots", spinner_style="cyan"):
                        response = call_nim_with_retry(
                            client, messages, tools_arg, current_model, config
                        )
                finally:
                    sys.stdout.write(f"\033[{term_height - 4};1H")
                    sys.stdout.flush()

            except Exception as e:
                typer.secho(
                    f"\n❌ Fatal API error: {e}", fg=typer.colors.RED, bold=True
                )
                break

            raw_message = response.choices[0].message
            cleaned_content = strip_reasoning_spans(raw_message.content or "")

            # Build serializable message for history
            msg_dict = {"role": "assistant", "content": cleaned_content}
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
            if state == "AWAITING_PLAN_APPROVAL":
                plan_text = extract_plan_block(cleaned_content)
                if not plan_text:
                    typer.secho(
                        "\n--- Proposed Architectural Plan ---",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                    typer.echo(cleaned_content)
                    typer.secho(
                        "-----------------------------------",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                else:
                    typer.secho(
                        "\n--- Proposed Architectural Plan (<plan>) ---",
                        fg=typer.colors.BLUE,
                        bold=True,
                    )
                    typer.echo(plan_text)
                    typer.secho(
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
                    typer.echo("Exiting nimcode session.")
                    break
                elif approval == "yolo":
                    state = "YOLO"
                    config.mode = "yolo"
                    current_model = config.fast_model
                    typer.secho(
                        f"\n⚡ Plan approved! Switching to {state} state (Auto-approval enabled) with fast model: {current_model}\n",
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

                state = "EXECUTING"
                current_model = config.fast_model
                typer.secho(
                    f"\n✅ Plan approved! Switching to {state} state with fast model: {current_model}\n",
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
                    typer.echo(f"\n{cleaned_content}")

                user_input = get_user_turn()
                if user_input is None or user_input.lower() in [
                    "exit",
                    "quit",
                    "/exit",
                    "/quit",
                    "q",
                    "done",
                ]:
                    typer.secho(
                        "\n🎉 Task completed. Exiting nimcode session.",
                        fg=typer.colors.GREEN,
                        bold=True,
                    )
                    break

                user_msg = {"role": "user", "content": user_input}
                messages.append(user_msg)
                log_turn(user_msg, session_path)
                loop_count = 0  # Reset loop counter on human input turn
                continue

            if loop_count >= config.max_loop_iterations:
                typer.secho(
                    f"\n⚠️ Max consecutive loop iterations ({config.max_loop_iterations}) reached without human check-in. Stopping runaway execution.",
                    fg=typer.colors.YELLOW,
                    bold=True,
                )
                user_input = (
                    prompt_user("Resume loop? (y to continue / q to quit)")
                    .strip()
                    .lower()
                )
                if user_input != "y":
                    break
                loop_count = 0

            loop_count += 1

            # Process tool calls
            for tool_call in raw_message.tool_calls:
                is_valid, tool_name, args = is_valid_tool_call(tool_call)
                if not is_valid:
                    result = f"Rejected: Unknown tool or malformed arguments for '{tool_name}'."
                    typer.secho(f"\n⚠️ {result}", fg=typer.colors.RED)
                else:
                    preview = render_preview(tool_name, args, workspace_root)
                    typer.echo(preview)

                    if state == "YOLO":
                        typer.secho(
                            f"⚡ [YOLO Mode] Auto-approving execution of {tool_name}...",
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
                        typer.echo("Exiting nimcode session.")
                        disable_persistent_prompt()
                        atexit.unregister(disable_persistent_prompt)
                        return
                    elif approval == "y":
                        result = execute_local_tool(
                            tool_name, args, workspace_root, config
                        )
                        typer.secho(
                            f"--- Output ({len(result)} chars) ---\n{result}\n-----------------------------",
                            fg=typer.colors.CYAN,
                        )
                    else:
                        feedback = prompt_user(
                            "Execution rejected. Enter feedback/reason (or leave blank for 'Rejected by user')"
                        ).strip()
                        result = feedback if feedback else "Rejected by user."
                        typer.secho("Execution rejected.", fg=typer.colors.YELLOW)

                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
                messages.append(tool_result_msg)
                log_turn(tool_result_msg, session_path)

            messages = prune_history(messages, config.context_budget_tokens)

        except KeyboardInterrupt:
            typer.secho(
                "\n\n🛑 [Stopped by ESC/Interrupt] Stopped working on current prompt. Standing by for next prompt...\n",
                fg=typer.colors.YELLOW,
                bold=True,
            )
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
                typer.secho(
                    "\n🎉 Task completed. Exiting nimcode session.",
                    fg=typer.colors.GREEN,
                    bold=True,
                )
                break

            user_msg = {"role": "user", "content": user_input}
            messages.append(user_msg)
            log_turn(user_msg, session_path)
            continue

    disable_persistent_prompt()
    atexit.unregister(disable_persistent_prompt)
