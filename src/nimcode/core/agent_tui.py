"""
TUI-aware async agent loop - replaces the old ANSI-based run_agent_loop.
"""

import asyncio
import base64
import re
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
from nimcode.tools.policy import READ_ONLY_TOOLS, ask_reason, classify
from nimcode.core.agent import (
    call_nim_with_retry,
    prune_history,
    log_turn,
    parse_xml_tool_calls,
    MockMessage,
    SESSION_DIR,
    migrate_legacy_session,
    open_session_path,
    load_session,
    prepare_resumed_messages,
    COMPACT_MARKER_KEY,
    count_message_tokens,
    aggregate_session_stats,
    render_stats_rich,
)
from nimcode.config_models import effective_budget
from nimcode.context import generate_workspace_map
from nimcode.ui.rust_bridge import RustTUI, generate_summary
from nimcode.tools.swarm import ACTIVE_SUBAGENTS, reap_finished_subagents
from nimcode.tools.cmd import BACKGROUND_PROCESSES

# Authoritative slash-command catalog: every entry must have a branch in
# `handle_command`. Sent to the sidecar for input autocomplete (plus dynamic
# `/<skill>` entries at runtime).
SLASH_COMMANDS: List[str] = [
    "/help",
    "/plan",
    "/build",
    "/model",
    "/models-refresh",
    "/config",
    "/changes",
    "/diff",
    "/restore",
    "/test",
    "/team",
    "/kill",
    "/compact",
    "/stats",
    "/recap",
    "/skills",
    "/install-skill",
    "/init",
    "/resume",
    "/exit",
    "/quit",
]

# Empty model turns (no text and no tool calls): re-drive the model this
# many times before handing back to the user instead of going silent.
MAX_EMPTY_TURNS = 2


_SCREENSHOT_RE = re.compile(r"^\s*\[screenshot:\s*(.+?)\s*\]\s*$", re.M | re.I)


def parse_screenshot_path(result: Any) -> Optional[str]:
    """PNG path from a screenshot_page result's `[screenshot: <path>]` line."""
    m = _SCREENSHOT_RE.search(str(result or ""))
    return m.group(1).strip("`'\"") if m else None


def screenshot_messages(
    result: Any, workspace_root: Path
) -> Optional[tuple[Dict[str, Any], Dict[str, Any], Path]]:
    """(live message, log message, path) attaching a screenshot for vision.

    The live message carries the PNG as a base64 data URL; the logged copy
    carries the file path instead so session files stay small. None when the
    result names no readable PNG.
    """
    raw = parse_screenshot_path(result)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(workspace_root) / path
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    text = {"type": "text", "text": f"Screenshot from screenshot_page ({path}):"}

    def _msg(url: str) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": [text, {"type": "image_url", "image_url": {"url": url}}],
        }

    return _msg(f"data:image/png;base64,{data}"), _msg(str(path)), path


def _text_of(content: Any) -> str:
    """Display text of a message content (str, or multimodal part list)."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def render_history(tui: Any, messages: List[Dict[str, Any]]) -> None:
    """Re-display a loaded session (startup resume and in-place /resume)."""
    for msg in messages:
        role = msg.get("role")
        content = _text_of(msg.get("content"))
        if role == "user":
            tui.add_message("user", content, markdown=False)
        elif role == "assistant":
            tui.add_message("assistant", content, markdown=True)
            for tc in msg.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name", "unknown")
                tui.add_message("tool", f"Tool Call: {name}", markdown=False)
        elif role == "tool":
            snippet = content.strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:80] + "..."
            tui.add_message("tool", snippet, markdown=False)


def is_empty_turn(content: Any, tool_calls: Any) -> bool:
    """True when the model produced neither text nor tool calls."""
    return not (content or "").strip() and not tool_calls


def close_dangling_tool_calls(
    messages: List[Dict[str, Any]], session_path: Optional[Path] = None
) -> int:
    """Answer every unanswered tool_call of a trailing assistant turn.

    A turn interrupted mid-batch (exception, Ctrl+C, crash before resume)
    leaves `assistant.tool_calls` without matching `tool` replies, which
    OpenAI-compatible endpoints reject on the next request. Only the
    trailing assistant(+tool results) segment is patched. Returns #added.
    """
    i = len(messages) - 1
    while i >= 0 and messages[i].get("role") == "tool":
        i -= 1
    if i < 0 or messages[i].get("role") != "assistant":
        return 0
    calls = messages[i].get("tool_calls") or []
    answered = {m.get("tool_call_id") for m in messages[i + 1 :]}
    added = 0
    for tc in calls:
        tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tid or tid in answered:
            continue
        msg = {
            "role": "tool",
            "tool_call_id": tid,
            "content": "Execution cancelled (interrupted before this tool ran).",
        }
        messages.append(msg)
        if session_path:
            log_turn(msg, session_path)
        added += 1
    return added


def _ready_history(
    loaded: List[Dict[str, Any]], session_path: Path
) -> List[Dict[str, Any]]:
    """Loaded session messages -> live history for the loop."""
    messages = prepare_resumed_messages(loaded)
    # The system prompt is never logged, so a resumed session lacks it;
    # without messages[0] the per-turn refresh never runs and the model
    # would operate with no system prompt at all.
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": ""})
    # A crash mid tool-batch leaves unanswered tool_calls the API rejects.
    close_dangling_tool_calls(messages, session_path)
    return messages


async def run_agent_loop_tui(
    tui: RustTUI,
    client: OpenAI,
    user_prompt: str,
    config: Config,
    workspace_root: Path,
    workspace_map: str,
    existing_messages: Optional[List[Dict[str, Any]]] = None,
    initial_state: Optional[str] = None,
    session_file_name: Optional[str] = None,
):
    """
    TUI-aware version of the agent loop that uses the Rust sidecar TUI
    for all I/O instead of ANSI escape codes and Rich console.
    """
    tui_task = asyncio.create_task(tui.start_async())
    tui._tui_task = tui_task

    try:
        await _run_agent_loop_tui_internal(
            tui,
            client,
            user_prompt,
            config,
            workspace_root,
            workspace_map,
            existing_messages,
            initial_state,
            session_file_name,
        )
    finally:
        tui.exit()
        try:
            await tui_task
        except Exception:
            pass
        try:
            import nimcode.ui.output as _output

            _output.TUI_INSTANCE = None
        except Exception:
            pass
        from nimcode.tools.cmd import cleanup_background_processes

        cleanup_background_processes()


async def _run_agent_loop_tui_internal(
    tui: RustTUI,
    client: OpenAI,
    user_prompt: str,
    config: Config,
    workspace_root: Path,
    workspace_map: str,
    existing_messages: Optional[List[Dict[str, Any]]] = None,
    initial_state: Optional[str] = None,
    session_file_name: Optional[str] = None,
):
    """TUI loop internals. Slash commands are dispatched by `handle_command`;
    the authoritative command list is `SLASH_COMMANDS` (also sent to the
    sidecar for input autocomplete)."""
    import atexit
    import nimcode.ui.output as output

    output.TUI_INSTANCE = tui

    from nimcode.tools.cmd import cleanup_background_processes

    atexit.register(cleanup_background_processes)
    session_dir = get_project_nimcode_dir(workspace_root) / SESSION_DIR
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        migrated = migrate_legacy_session(workspace_root, session_dir)
        if migrated:
            tui.add_message("system", f"Migrated legacy session to {migrated}")
    except OSError as e:
        tui.add_message("system", f"Warning: could not migrate legacy session: {e}")

    session_path = open_session_path(session_dir, session_file_name, existing_messages)
    user_input: Optional[str] = user_prompt

    messages: list[dict[str, Any]] = []
    # No explicit state (e.g. `nimcode resume`): honour the configured mode
    # instead of silently forcing Plan mode.
    state = initial_state or {"plan": "AWAITING_PLAN_APPROVAL"}.get(
        getattr(config, "mode", ""), "EXECUTING"
    )
    if state == "AWAITING_PLAN_APPROVAL":
        config.mode = "plan"
    else:
        config.mode = "build"

    skip_first_api_call = False

    loop_count = 0

    def _budget() -> int:
        """Pruning budget for the model the next call will use."""
        model = (
            config.plan_model
            if state == AgentState.AWAITING_PLAN_APPROVAL
            else config.fast_model
        )
        return effective_budget(config, model)

    def _adopt_history(name: str, loaded: List[Dict[str, Any]]):
        """(session_path, messages) for continuing a loaded session file."""
        path = open_session_path(session_dir, name, loaded)
        return path, _ready_history(loaded, path)

    async def handle_command(user_input: str) -> str:
        nonlocal state, messages, session_path, loop_count, skip_first_api_call
        cmd = user_input.strip()
        cmd_lower = cmd.lower()

        if cmd_lower in ["exit", "quit", "/exit", "/quit", "q", "done"]:
            tui.add_message("system", "Task completed. Exiting nimcode session.")
            return "exit"

        # Intercept commands: starts with '/' or is exactly '?'
        if not (cmd.startswith("/") or cmd == "?"):
            return "none"

        # Handle specific commands
        if cmd_lower in ["/plan", "/build"]:
            if cmd_lower == "/plan":
                state = AgentState.AWAITING_PLAN_APPROVAL
                config.mode = "plan"
                tui.add_message(
                    "system",
                    "\nSwitched to Plan Mode (AWAITING_PLAN_APPROVAL). Read-only tools; no changes until a plan is approved.\n",
                )
                tui.update_mode("Plan Mode")
            elif cmd_lower == "/build":
                state = AgentState.EXECUTING
                config.mode = "build"
                tui.add_message(
                    "system",
                    "\nSwitched to Build Mode (EXECUTING; asks only for sensitive or risky actions).\n",
                )
                tui.update_mode("Build Mode")
            loop_count = 0
            return "command"

        elif cmd_lower in ["?", "/help", "help"]:
            from nimcode.ui.console import show_help

            show_help(tui=tui)
            loop_count = 0
            return "command"

        elif cmd_lower in ["/changes", "/diff"]:
            from nimcode.config import show_changes

            show_changes(
                Path(workspace_root).resolve()
                if workspace_root
                else Path(".").resolve(),
                tui=tui,
            )
            loop_count = 0
            return "command"

        elif cmd_lower == "/restore":
            await tui.restore_backup(
                Path(workspace_root).resolve()
                if workspace_root
                else Path(".").resolve()
            )
            loop_count = 0
            return "command"

        elif cmd_lower == "/config":
            await tui.configure(config)
            loop_count = 0
            return "command"

        elif cmd_lower == "/model":
            new_model = await tui.select_model(
                config, client, messages, session_path, workspace_root
            )
            if new_model:
                tui.add_message("system", f"\n✅ Model switched to {new_model}")
            loop_count = 0
            return "command"

        elif cmd_lower == "/models-refresh":
            from nimcode import config_models as _cm

            if config.api_base_url:
                # Custom endpoint: don't pollute the shared NVIDIA catalog cache.
                tui.add_message(
                    "system",
                    "Custom api_base_url set; model catalog refresh skipped.",
                )
                return "command"
            tui.add_message("system", "Refreshing model catalog from NIM API...")
            catalog, msg = await asyncio.to_thread(_cm.refresh_models_blocking, client)
            tui.add_message("system", msg)
            notice = _cm.reconcile_saved_model(config, catalog)
            if notice:
                tui.add_message("system", f"Note: {notice}")
            changed, ctx_msg = _cm.apply_model_context(
                config, config.fast_model, force=True
            )
            if changed:
                tui.add_message("system", f"Note: {ctx_msg}")
            try:
                tui.update_tokens(
                    count_message_tokens(messages), config.context_budget_tokens
                )
            except Exception:
                pass
            loop_count = 0
            return "command"

        elif cmd_lower.startswith("/test"):
            parts = cmd.split(" ", 1)
            test_cmd = parts[1].strip() if len(parts) > 1 else None
            from nimcode.tools import execute_test_runner

            root_path = (
                Path(workspace_root).resolve()
                if workspace_root
                else Path(".").resolve()
            )
            tui.add_message(
                "system",
                "\nRunning test suite inside validation sandbox...",
            )
            res = await asyncio.to_thread(
                execute_test_runner, root_path, config, test_cmd
            )
            tui.add_message(
                "tool",
                f"\n--- Test Runner Results ---\n{res}\n---------------------------",
                markdown=False,
            )
            loop_count = 0
            return "command"

        elif cmd_lower == "/team":
            reap_finished_subagents()
            if not ACTIVE_SUBAGENTS:
                tui.add_message(
                    "system",
                    "No subagents spawned yet. Use `spawn_agent` tool or ask nimcode to delegate tasks.",
                )
            else:
                tui.add_message("system", "Active Subagents:")
                # Snapshot: swarm threads mutate the dict under _SUBAGENTS_LOCK.
                for aid, sub in list(ACTIVE_SUBAGENTS.items()):
                    status = sub.get("status", "UNKNOWN")
                    role = sub.get("role", "Unknown")
                    output = str(sub.get("output") or "")[:100]
                    line = f"  • {aid}: {role} ({status}) — {output}"
                    if sub.get("transcript"):
                        line += f"\n    transcript: {sub['transcript']}"
                    tui.add_message("system", line, markdown=False)
            loop_count = 0
            return "command"

        elif cmd_lower == "/kill":
            from nimcode.tools.cmd import cleanup_background_processes

            cleanup_background_processes()
            tui.add_message("system", "All background processes terminated.")
            loop_count = 0
            return "command"

        elif cmd_lower == "/compact":
            if not any(m.get("role") == "user" for m in messages):
                tui.add_message("system", "Nothing to compact yet.")
                return "command"
            try:
                summary = await generate_summary(
                    tui,
                    client,
                    messages,
                    config.fast_model,
                    config,
                    workspace_root,
                    "Compacting session into summary.md...",
                )
            except (Exception, KeyboardInterrupt) as e:
                summary = ""
                tui.add_message("system", f"Compact failed: {e}")
            if summary:
                before = count_message_tokens(messages)
                head = messages[:1] if messages[0].get("role") == "system" else []
                summary_msg = {
                    "role": "user",
                    "content": "<compacted_context>\n"
                    f"{summary}\n</compacted_context>\n\n"
                    "The earlier conversation was compacted into the summary "
                    "above (also saved as summary.md). Continue from it.",
                }
                messages = head + [summary_msg]
                # Marker: resume starts here instead of replaying old turns.
                log_turn(
                    {"role": "system", "content": "", COMPACT_MARKER_KEY: True},
                    session_path,
                )
                log_turn(summary_msg, session_path)
                tui.add_message(
                    "system",
                    f"Compacted {before:,} tokens of history into summary.md "
                    f"(now ~{count_message_tokens(messages):,}).",
                )
            else:
                tui.add_message(
                    "system", "Compact skipped (no summary): history unchanged."
                )
            loop_count = 0
            return "command"

        elif cmd_lower == "/stats":
            stats = aggregate_session_stats(workspace_root)
            render_stats_rich(stats, tui=tui)
            loop_count = 0
            return "command"

        elif cmd_lower == "/recap":
            tui.add_message("system", "Recap feature not yet implemented in TUI mode.")
            loop_count = 0
            return "command"

        elif cmd_lower == "/skills":
            from nimcode import skills

            skill_list = skills.list_skills()
            if not skill_list:
                tui.add_message("system", "No skills installed yet.")
            else:
                tui.add_message("system", "Installed Skills:")
                for s in skill_list:
                    tui.add_message("system", f"  /{s}")
            loop_count = 0
            return "command"

        elif cmd_lower == "/init":
            from nimcode.core.agent import run_init_command

            await asyncio.to_thread(
                run_init_command,
                config,
                client=client,
                workspace_root=workspace_root,
                tui=tui,
            )
            loop_count = 0
            return "command"

        elif cmd_lower == "/resume":
            selected = await tui.select_session(config, workspace_root)
            if selected:
                try:
                    loaded = await asyncio.to_thread(load_session, selected)
                except Exception as e:
                    loaded = None
                    tui.add_message("system", f"Error loading {selected.name}: {e}")
                if loaded == []:
                    tui.add_message("system", f"{selected.name} is empty.")
                elif loaded:
                    session_path, messages = _adopt_history(selected.name, loaded)
                    tui.clear_messages()
                    render_history(tui, messages)
                    tui.add_message(
                        "system",
                        f"Resumed {selected.name} ({len(loaded)} messages).",
                    )
            loop_count = 0
            return "command"

        elif cmd_lower.startswith("/install-skill"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                tui.add_message("system", "Usage: /install-skill <url>")
            else:
                url = parts[1].strip()
                tui.add_message("system", f"Fetching skill from {url}...")
                from nimcode import skills as _skills

                try:
                    staging, preview, sha = await asyncio.to_thread(
                        _skills.fetch_skill, url
                    )
                except ValueError as e:
                    tui.add_message("system", f"Error: {e}")
                else:
                    # Human gate: nothing is installed until the user has
                    # seen the file list, size, sha256 and SKILL.md head.
                    tui.add_message("tool", preview, markdown=False)
                    answer = await tui.get_user_confirmation("Install this skill?")
                    if answer in ("y", "yes"):
                        result = _skills.commit_skill(
                            staging, _skills._get_skill_name_from_url(url), url, sha
                        )
                    else:
                        _skills.discard_skill(staging)
                        result = "Skill install cancelled."
                    tui.add_message("system", result)
            loop_count = 0
            return "command"

        # Unknown slash command — maybe an installed /<skill>?
        if cmd.startswith("/"):
            from nimcode import skills as _skills

            parts = cmd.split(" ", 1)
            skill_name = parts[0][1:]
            if skill_name and skill_name in _skills.list_skills():
                content = _skills.get_skill_content(skill_name)
                if content is None:
                    tui.add_message(
                        "system",
                        f"Error: skill '{skill_name}' could not be read "
                        "(missing, unreadable or too large).",
                    )
                    loop_count = 0
                    return "command"
                rest = parts[1] if len(parts) > 1 else ""
                user_msg = {
                    "role": "user",
                    "content": f"<skill_context>\n{content}\n</skill_context>\n\n{rest}",
                }
                # Initial-prompt path already queued the raw text: replace it.
                if (
                    messages
                    and messages[-1].get("role") == "user"
                    and messages[-1].get("content") == user_input
                ):
                    messages[-1] = user_msg
                else:
                    messages.append(user_msg)
                log_turn(user_msg, session_path)
                tui.add_message(
                    "system", f"Injecting context from skill: {skill_name}..."
                )
                tui.add_message("user", rest or f"/{skill_name}", markdown=False)
                loop_count = 0
                # "resume": messages updated, go straight to the API call.
                return "resume"
        tui.add_message("system", f"Unknown command: {cmd}")
        loop_count = 0
        return "command"

    if existing_messages:
        messages = _ready_history(existing_messages, session_path)
        render_history(tui, messages)
        skip_first_api_call = True
    else:
        system_prompt = build_system_prompt(
            str(workspace_root),
            workspace_map,
            disable_native_tools=config.disable_native_tools,
        )
        messages = [{"role": "system", "content": system_prompt}]
        if user_prompt:
            tui.add_message("user", user_prompt, markdown=False)
        else:
            skip_first_api_call = True

    # Handle slash commands in the initial prompt before first API call.
    # A plain prompt is only queued (and logged, so resume sees it) when it
    # isn't a command — a "/plan" must not linger in the model's history.
    if not existing_messages and user_prompt:
        cmd_res = await handle_command(user_prompt)
        if cmd_res == "exit":
            return
        elif cmd_res == "command":
            skip_first_api_call = True
        elif cmd_res == "none":
            first_msg = {"role": "user", "content": user_prompt}
            messages.append(first_msg)
            log_turn(first_msg, session_path)

    loop_count = 0
    consecutive_api_errors = 0
    consecutive_empty = 0
    # Label from the (just normalised) config.mode: tui.mode was computed at
    # construction and pushing it back would revert initial_state.
    tui.update_mode({"plan": "Plan Mode"}.get(config.mode, "Build Mode"))
    tui.update_tokens(0, config.context_budget_tokens)

    async def get_user_turn() -> Optional[str]:
        """Get user input from TUI."""
        nonlocal state, session_path, consecutive_empty
        value = await tui.get_user_input()
        if value is not None:
            consecutive_empty = 0
        return value

    def _cancel_requested() -> bool:
        try:
            return bool(getattr(tui, "_interrupted", False))
        except Exception:
            return False

    def _clear_cancel() -> None:
        try:
            tui._interrupted = False
        except Exception:
            pass

    def _sync_state() -> None:
        nonlocal state
        if config.mode == "plan":
            state = AgentState.AWAITING_PLAN_APPROVAL
        elif config.mode == "build":
            state = AgentState.EXECUTING

    def update_token_estimate():
        """Update token count in sidebar."""
        tokens = count_message_tokens(messages)
        tui.update_tokens(tokens, config.context_budget_tokens)

    # Update subagents and background procs periodically
    def update_sidebar_data():
        try:
            reap_finished_subagents()
            subs = list(ACTIVE_SUBAGENTS.items())
        except Exception:
            subs = []
        try:
            procs = list(BACKGROUND_PROCESSES.items())
        except Exception:
            procs = []
        try:
            tui.update_subagents(
                [
                    {
                        "id": aid,
                        "role": str(sub.get("role", "Unknown")),
                        "status": str(sub.get("status", "UNKNOWN")),
                        "output": str(sub.get("output") or "")[:100],
                    }
                    for aid, sub in subs
                    if isinstance(sub, dict)
                ]
            )
        except Exception:
            pass
        try:
            tui.update_background_procs(
                [
                    {
                        "pid": str(pid),
                        "command": str(proc.get("command", ""))[:50],
                        "is_docker": proc.get("is_docker", False),
                    }
                    for pid, proc in procs
                    if isinstance(proc, dict)
                ]
            )
        except Exception:
            pass

    while True:
        try:
            # Sync state with config.mode (e.g. if switched via Shift+Tab or /config)
            _sync_state()

            update_token_estimate()
            update_sidebar_data()

            # Stringent Ctrl+D: exit cleanly even mid-turn (sticky flag set by TUI).
            _consume_eof = getattr(tui, "consume_eof", None)
            if callable(_consume_eof) and _consume_eof():
                tui.add_message("system", "EOF received. Exiting nimcode session.")
                break

            if skip_first_api_call:
                skip_first_api_call = False
                user_input = await get_user_turn()
                if user_input is None:
                    break

                cmd_res = await handle_command(user_input)
                if cmd_res == "exit":
                    break
                elif cmd_res == "command":
                    skip_first_api_call = True
                    continue
                elif cmd_res == "resume":
                    loop_count = 0
                    continue

                if user_input == "__RESUME_AI_LOOP__":
                    loop_count = 0
                    continue

                user_msg = {"role": "user", "content": user_input}
                messages.append(user_msg)
                log_turn(user_msg, session_path)
                tui.add_message("user", user_input, markdown=False)
                loop_count = 0
                continue

            # Refresh system prompt with latest AGENTS.md and live workspace tree
            if messages and len(messages) > 0 and messages[0].get("role") == "system":
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
            # Plan mode gets the read-only tools; XML fallback passes none.
            if config.disable_native_tools:
                tools_arg = None
            elif state == AgentState.AWAITING_PLAN_APPROVAL:
                tools_arg = [
                    t
                    for t in AVAILABLE_TOOLS_SPEC
                    if t["function"]["name"] in READ_ONLY_TOOLS
                ]
            else:
                tools_arg = AVAILABLE_TOOLS_SPEC

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
                    # Append a new message rather than mutating an earlier user
                    # turn: log_turn is append-only, so re-logging a mutated
                    # message duplicated it in the session file. Consecutive
                    # user messages are merged by normalize_messages().
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
                    tui.add_message(
                        "system",
                        f"Injected summary.md context into prompt for {current_model}.",
                    )
                config.pending_summary_injection = False

            # Show working animation
            # Fresh cancel state: a Ctrl+C from an earlier interaction must
            # not instantly cancel this call.
            _clear_cancel()
            tui.set_working(True, "Thinking...")

            try:
                response = await asyncio.to_thread(
                    call_nim_with_retry,
                    client,
                    messages,
                    tools_arg,
                    current_model,
                    config,
                )
                consecutive_api_errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_api_errors += 1
                tui.set_working(False)
                tui.add_message(
                    "system",
                    f"API error: {e}. Type a new prompt to retry, or /exit to quit.",
                )
                # Back off so a persistently failing endpoint can't hot-loop,
                # then wait for user input instead of hammering the API.
                await asyncio.sleep(min(2 * consecutive_api_errors, 8))
                skip_first_api_call = True
                continue
            finally:
                tui.set_working(False)

            if response is None or not getattr(response, "choices", None):
                consecutive_api_errors += 1
                tui.add_message(
                    "system",
                    "API returned an empty response. Type a new prompt to retry.",
                )
                await asyncio.sleep(min(2 * consecutive_api_errors, 8))
                skip_first_api_call = True
                continue
            if _cancel_requested():
                # Cancelled while the call was in flight; discard the result
                # instead of executing tools the user no longer wants.
                _clear_cancel()
                raise KeyboardInterrupt("ESC_CANCELLED")
            raw_message = response.choices[0].message
            cleaned_content = strip_reasoning_spans(raw_message.content or "")

            if config.disable_native_tools:
                xml_calls = parse_xml_tool_calls(cleaned_content)
                raw_message = MockMessage(cleaned_content, xml_calls)

            if is_empty_turn(cleaned_content, raw_message.tool_calls):
                # Defective turn: never store or display a blank assistant
                # message — it pollutes history and shows as a silent bubble.
                consecutive_empty += 1
                if consecutive_empty <= MAX_EMPTY_TURNS:
                    tui.add_message(
                        "system",
                        f"Model returned an empty response "
                        f"(attempt {consecutive_empty}/{MAX_EMPTY_TURNS}). Retrying...",
                    )
                    skip_first_api_call = False
                    continue
                tui.add_message(
                    "system",
                    "Model keeps returning empty responses. "
                    "Type a new prompt to retry, or /exit to quit.",
                )
                skip_first_api_call = True
                continue
            consecutive_empty = 0

            # Build serializable message for history
            msg_dict: Dict[str, Any] = {"role": "assistant", "content": cleaned_content}
            # Plan mode never executes tools, so tool_calls (e.g. parsed from
            # XML fallback) would stay unanswered and break the next request.
            if raw_message.tool_calls and state != AgentState.AWAITING_PLAN_APPROVAL:
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

            # Show assistant response in TUI
            if cleaned_content:
                tui.add_message("assistant", cleaned_content, markdown=True)

            # State 1: AWAITING_PLAN_APPROVAL
            # Plan mode may read freely: tool calls go to the tool loop below,
            # and a reply without a <plan> is just an answer, not a proposal.
            plan_text = extract_plan_block(cleaned_content)
            if (
                state == AgentState.AWAITING_PLAN_APPROVAL
                and plan_text
                and not raw_message.tool_calls
            ):
                tui.add_message(
                    "system", "--- Proposed Architectural Plan (<plan>) ---"
                )
                tui.add_message("assistant", plan_text, markdown=True)
                tui.add_message(
                    "system", "--------------------------------------------"
                )

                approval = (
                    (
                        await tui.get_user_confirmation(
                            "Approve this plan to begin execution? (y/n)"
                        )
                        or ""
                    )
                    .strip()
                    .lower()
                )

                if approval in ("q", "n", ""):
                    tui.add_message(
                        "system",
                        "Plan cancelled. You can refine your request or type a new prompt.",
                    )
                    cancel_msg = {
                        "role": "user",
                        "content": "[Plan rejected/cancelled by user. Awaiting new instructions.]",
                    }
                    messages.append(cancel_msg)
                    log_turn(cancel_msg, session_path)
                    loop_count = 0
                    # Wait for the user, don't immediately re-plan.
                    skip_first_api_call = True
                    continue
                elif approval != "y":
                    feedback = await tui.get_user_input(
                        "Plan rejected. Enter feedback/revisions for the model:"
                    )
                    if feedback is None:
                        # Ctrl+C/Ctrl+D at the feedback prompt: hand back to
                        # the user instead of sending "Feedback: None".
                        skip_first_api_call = True
                        continue
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
                tui.add_message(
                    "system",
                    f"Plan approved! Switching to {state} state with fast model: {current_model}",
                )
                tui.update_mode("Build Mode")
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
                    # Already displayed above
                    pass

                user_input = await get_user_turn()
                if user_input is None:
                    break

                cmd_res = await handle_command(user_input)
                if cmd_res == "exit":
                    break
                elif cmd_res == "command":
                    skip_first_api_call = True
                    continue
                elif cmd_res == "resume":
                    loop_count = 0
                    continue

                if user_input == "__RESUME_AI_LOOP__":
                    loop_count = 0
                    continue

                user_msg = {"role": "user", "content": user_input}
                messages.append(user_msg)
                log_turn(user_msg, session_path)
                tui.add_message("user", user_input, markdown=False)
                loop_count = 0
                continue

            # Loop limit check
            loop_limit = config.max_loop_iterations
            if loop_count >= loop_limit:
                tui.add_message(
                    "system",
                    f"Warning: Max consecutive loop iterations ({loop_limit}) reached without human check-in. Stopping runaway execution.",
                )
                user_input = (
                    await tui.get_user_confirmation(
                        "Resume loop? (y to continue / feedback to redirect / n or q to cancel & stay)"
                    )
                    or ""
                ).strip()

                if user_input.lower() in ("q", "n", ""):
                    tui.add_message(
                        "system",
                        "Loop cancelled. You're still in the session — type a new prompt.",
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
                    user_input = await get_user_turn()
                    if user_input is None:
                        break

                    cmd_res = await handle_command(user_input)
                    if cmd_res == "exit":
                        break
                    elif cmd_res == "command":
                        skip_first_api_call = True
                        continue
                    elif cmd_res == "resume":
                        loop_count = 0
                        continue

                    if user_input == "__RESUME_AI_LOOP__":
                        loop_count = 0
                        continue
                    user_msg = {"role": "user", "content": user_input}
                    messages.append(user_msg)
                    log_turn(user_msg, session_path)
                    tui.add_message("user", user_input, markdown=False)
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
            cancelled_mid_tools = False
            # Screenshot image messages wait until the whole batch is answered:
            # a user message between tool results would break the pairing.
            pending_images = []
            pending_calls = list(raw_message.tool_calls)
            for _call_idx, tool_call in enumerate(pending_calls):
                # Apply Shift+Tab / Ctrl+C pressed during the previous tool:
                # switching to Plan mid-batch refuses the remaining writes.
                tui.poll_events()
                _sync_state()
                if _cancel_requested():
                    tui.add_message(
                        "system",
                        "Cancelled by user (Ctrl+C). Remaining tool calls skipped.",
                    )
                    for tc in pending_calls[_call_idx:]:
                        tool_result_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "Execution cancelled by user (Ctrl+C). Awaiting new instructions.",
                        }
                        messages.append(tool_result_msg)
                        log_turn(tool_result_msg, session_path)
                    _clear_cancel()
                    cancelled_mid_tools = True
                    break
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
                    tui.add_message("system", f"Warning: {result}")
                else:
                    try:
                        preview = render_preview(
                            tool_name, args, workspace_root, config=config
                        )
                    except Exception as e:
                        preview = f"(preview unavailable: {e})"
                    tui.add_message("tool", f"Preview:\n{preview}", markdown=False)

                    decision = classify(tool_name, args, workspace_root, config.mode)
                    if decision == "deny":
                        approval = "deny"
                    elif decision == "auto":
                        approval = "y"
                    else:
                        approval = (
                            (
                                await tui.get_user_confirmation(
                                    f"Approve {tool_name} ({ask_reason(tool_name, args)})? (y/n to skip)"
                                )
                                or ""
                            )
                            .strip()
                            .lower()
                        )

                    if approval == "q":
                        approval = "n"
                    elif approval == "deny":
                        result = (
                            f"Error: Plan mode is read-only; '{tool_name}' was not run. "
                            "Put the change in your <plan> instead."
                        )
                        tui.add_message("system", result)
                    elif approval == "y":
                        tui.add_message("system", f"Executing {tool_name}...")
                        try:
                            # ponytail: a running tool can't be interrupted;
                            # Ctrl+C takes effect before the next tool call.
                            # Per-tool cancellation is the upgrade path.
                            result = await asyncio.to_thread(
                                execute_local_tool,
                                tool_name,
                                args,
                                workspace_root,
                                config,
                            )
                        except Exception as e:
                            result = f"Error executing '{tool_name}': {e}"

                        # Show result as compact diff box
                        tui.add_message(
                            "tool",
                            f"Output ({len(result)} chars):\n{result}",
                            markdown=False,
                        )

                        if config.auto_test and tool_name in [
                            "write_file",
                            "patch_file_ast",
                            "patch_file_blocks",
                        ]:
                            from nimcode.tools import (
                                execute_test_runner,
                                no_tests_found,
                                tests_passed,
                            )

                            tui.add_message(
                                "system",
                                "Code changed. Auto-running test suite for verification...",
                            )
                            try:
                                test_res = await asyncio.to_thread(
                                    execute_test_runner, workspace_root, config
                                )
                            except Exception as e:
                                test_res = f"Error running tests: {e}"
                            tui.add_message(
                                "tool", f"Test Results:\n{test_res}", markdown=False
                            )
                            if not tests_passed(test_res) and not no_tests_found(
                                test_res
                            ):
                                result += f"\n\n[CRITICAL WARNING: Auto-run tests failed! Please fix this immediately]\n{test_res}"
                    else:
                        user_feedback = await tui.get_user_input(
                            "Execution rejected. Enter feedback/reason (or leave blank for 'Rejected by user')"
                        )
                        feedback = user_feedback.strip() if user_feedback else ""
                        result = feedback if feedback else "Rejected by user."
                        tui.add_message("system", "Execution rejected.")

                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
                messages.append(tool_result_msg)
                log_turn(tool_result_msg, session_path)
                if (
                    is_valid
                    and tool_name == "screenshot_page"
                    and getattr(config, "vision_screenshots", False)
                ):
                    shot = screenshot_messages(result, workspace_root)
                    if shot:
                        pending_images.append(shot)

            for live_msg, logged_msg, shot_path in pending_images:
                messages.append(live_msg)
                log_turn(logged_msg, session_path)
                tui.add_message(
                    "system", f"Attached screenshot {shot_path.name} for the model."
                )

            messages = prune_history(messages, _budget())
            if cancelled_mid_tools:
                # Back to the user, not straight into another API call.
                loop_count = 0
                skip_first_api_call = True
                continue

        except KeyboardInterrupt:
            tui.add_message(
                "system",
                "Stopped by ESC/Interrupt. Stopped working on current prompt. Standing by for next prompt...",
            )
            close_dangling_tool_calls(messages, session_path)
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

            user_input = await get_user_turn()
            if user_input is None:
                break

            cmd_res = await handle_command(user_input)
            if cmd_res == "exit":
                break
            elif cmd_res == "command":
                skip_first_api_call = True
                continue
            elif cmd_res == "resume":
                loop_count = 0
                continue

            if user_input == "__RESUME_AI_LOOP__":
                loop_count = 0
                continue

            user_msg = {"role": "user", "content": user_input}
            messages.append(user_msg)
            log_turn(user_msg, session_path)
            tui.add_message("user", user_input, markdown=False)
            continue
        except Exception as e:
            import traceback

            if "nim-tui terminated" in str(e):
                try:
                    tui.add_message("system", "Rust TUI sidecar died. Ending session.")
                except Exception:
                    pass
                break
            tui.add_message(
                "system",
                f"Error: {type(e).__name__}: {e}\n{traceback.format_exc()}",
            )
            close_dangling_tool_calls(messages, session_path)
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

            user_input = await get_user_turn()
            if user_input is None:
                break

            cmd_res = await handle_command(user_input)
            if cmd_res == "exit":
                break
            elif cmd_res == "command":
                skip_first_api_call = True
                continue
            elif cmd_res == "resume":
                loop_count = 0
                continue

            if user_input == "__RESUME_AI_LOOP__":
                loop_count = 0
                continue

            user_msg = {"role": "user", "content": user_input}
            messages.append(user_msg)
            log_turn(user_msg, session_path)
            tui.add_message("user", user_input, markdown=False)
            continue

    cleanup_background_processes()
    tui.add_message("system", "Session ended.")
