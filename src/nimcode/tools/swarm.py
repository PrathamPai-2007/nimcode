import os
from pathlib import Path
from typing import Dict, Any
import threading
from nimcode.tools.registry import (
    register_tool,
    execute_local_tool,
    AVAILABLE_TOOLS_SPEC,
)
from nimcode.tools.base import is_valid_tool_call

ACTIVE_SUBAGENTS: Dict[str, Any] = {}
_SUBAGENTS_LOCK = threading.RLock()

# Ended agents (any status but RUNNING) leave ACTIVE_SUBAGENTS this long
# after they end; their JSONL transcripts stay on disk.
REAP_AFTER_S = 600


def _transcript_path(workspace_root: Path, agent_id: str) -> Path:
    from nimcode.config import get_project_nimcode_dir

    return get_project_nimcode_dir(workspace_root) / "subagents" / f"{agent_id}.jsonl"


def _log(sub: Dict[str, Any], msg: Dict[str, Any]) -> None:
    """Append one message to the agent's JSONL transcript (if it has one)."""
    path = sub.get("transcript")
    if path:
        from nimcode.core.agent import log_turn

        log_turn(msg, Path(path))


def _record(sub: Dict[str, Any], msg: Dict[str, Any]) -> None:
    # Caller holds _SUBAGENTS_LOCK.
    sub["messages"].append(msg)
    _log(sub, msg)


def reap_finished_subagents(now: float = None) -> int:
    """Drop agents that ended more than REAP_AFTER_S ago. Returns #removed."""
    import time

    now = time.time() if now is None else now
    with _SUBAGENTS_LOCK:
        stale = [
            aid
            for aid, s in ACTIVE_SUBAGENTS.items()
            if s.get("status") != "RUNNING"
            and s.get("runner") is None
            and s.get("ended_at") is not None
            and now - s["ended_at"] >= REAP_AFTER_S
        ]
        for aid in stale:
            del ACTIVE_SUBAGENTS[aid]
    return len(stale)


SPAWN_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": "Spawn an autonomous background subagent to investigate a codebase task concurrently or handle specialized research.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Role description (e.g. 'Codebase Researcher', 'Test Debugger').",
                },
                "task_prompt": {
                    "type": "string",
                    "description": "Detailed actionable task instructions for the subagent.",
                },
            },
            "required": ["role", "task_prompt"],
        },
    },
}

SEND_MESSAGE_TO_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_message_to_agent",
        "description": "Send a message or check status of a previously spawned subagent by agent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The subagent ID returned by spawn_agent.",
                },
                "message": {
                    "type": "string",
                    "description": "Instruction or question to send to the subagent.",
                },
            },
            "required": ["agent_id", "message"],
        },
    },
}


KILL_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kill_agent",
        "description": "Terminate a running background subagent by agent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The subagent ID to terminate.",
                }
            },
            "required": ["agent_id"],
        },
    },
}

LIST_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_agents",
        "description": "List all active, completed, or failed background subagents with their current statuses.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


@register_tool("spawn_agent", SPAWN_AGENT_SCHEMA)
def spawn_agent_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    role = args.get("role", "Subagent")
    task_prompt = args.get("task_prompt", "")
    return spawn_agent_logic(role, task_prompt, workspace_root, config)


@register_tool("send_message_to_agent", SEND_MESSAGE_TO_AGENT_SCHEMA)
def send_message_to_agent_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    agent_id = args.get("agent_id", "")
    message = args.get("message", "")
    return send_message_to_agent_logic(agent_id, message, workspace_root, config)


@register_tool("kill_agent", KILL_AGENT_SCHEMA)
def kill_agent_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    agent_id = args.get("agent_id", "")
    return kill_agent_logic(agent_id, workspace_root, config)


@register_tool("list_agents", LIST_AGENTS_SCHEMA)
def list_agents_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    return list_agents_logic(workspace_root, config)


def spawn_agent_logic(
    role: str, task_prompt: str, workspace_root: Path, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    import time
    import uuid

    if not isinstance(task_prompt, str) or not task_prompt.strip():
        return "Error: spawn_agent requires a non-empty 'task_prompt'."
    if not isinstance(role, str) or not role.strip():
        role = "Subagent"
    depth = getattr(config, "_subagent_depth", 0) if config is not None else 0
    try:
        depth = int(depth)
    except Exception:
        depth = 0
    if depth >= 3:
        return "Error: spawn_agent denied: maximum subagent nesting depth reached."

    agent_id = f"sub_{uuid.uuid4().hex[:8]}"

    from nimcode.context import generate_workspace_map
    from nimcode.core.prompts import build_system_prompt

    w_map = generate_workspace_map(str(workspace_root))
    disable_native = getattr(config, "disable_native_tools", False)
    sys_prompt = build_system_prompt(
        str(workspace_root),
        w_map,
        disable_native_tools=disable_native,
    )
    messages = [
        {
            "role": "system",
            "content": f"{sys_prompt}\n\nYou are a background subagent with the specialized role: {role}.",
        },
        {"role": "user", "content": task_prompt},
    ]

    reap_finished_subagents()
    with _SUBAGENTS_LOCK:
        # Check + insert under one lock hold (no check-then-act race), and
        # count only live agents: finished ones stay listed for /team.
        if _running_count() >= 10:
            return "Error: too many active subagents (max 10). Kill one first."
        sub = ACTIVE_SUBAGENTS[agent_id] = {
            "id": agent_id,
            "role": role,
            "status": "RUNNING",
            "task_prompt": task_prompt,
            "output": "Execution in progress...",
            "messages": messages,
            "created_at": time.time(),
            # Follow-up messages wait here until the worker drains them at a
            # step boundary (keeps assistant/tool ordering valid).
            "inbox": [],
            # Nesting level of this agent; threads inherit it so depth
            # accumulates across generations (fork-bomb guard).
            "depth": depth + 1,
            # JSONL transcript (task prompt onwards; the system prompt is
            # not logged, like main sessions).
            "transcript": str(_transcript_path(workspace_root, agent_id)),
        }
        _log(sub, messages[1])

    resume_subagent_thread(agent_id, workspace_root, config)

    return f"Subagent '{role}' spawned with ID: {agent_id}. Status: RUNNING in background.\nUse send_message_to_agent(agent_id='{agent_id}', message='...') or check /team to inspect results."


def _running_count() -> int:
    with _SUBAGENTS_LOCK:
        return sum(1 for s in ACTIVE_SUBAGENTS.values() if s.get("status") == "RUNNING")


def _drain_inbox(sub: Dict[str, Any]) -> None:
    # Caller holds _SUBAGENTS_LOCK.
    inbox = sub.get("inbox")
    if inbox:
        for msg in inbox:
            _record(sub, msg)
        inbox.clear()


def resume_subagent_thread(agent_id: str, workspace_root: Path, config: Any):
    token = object()
    with _SUBAGENTS_LOCK:
        sub = ACTIVE_SUBAGENTS.get(agent_id)
        if not sub or sub.get("status") == "KILLED":
            return
        # Single-runner guard decided under the global lock: if a worker is
        # live it will drain the inbox itself, so no wakeup is ever lost
        # (the old try-lock let a just-finishing worker drop new messages).
        if sub.get("runner") is not None:
            return
        sub["runner"] = token
        sub["status"] = "RUNNING"
        sub["output"] = "Execution in progress..."
        sub.pop("ended_at", None)

    import time

    # Wall-clock budget per run (a follow-up message starts a fresh one).
    try:
        timeout_s = max(1, int(getattr(config, "subagent_timeout_s", 900)))
    except (TypeError, ValueError):
        timeout_s = 900
    deadline = time.monotonic() + timeout_s

    def _timed_out() -> bool:
        return time.monotonic() >= deadline

    def _mark_timed_out() -> None:
        with _SUBAGENTS_LOCK:
            if sub["status"] != "KILLED":
                sub["status"] = "TIMED_OUT"
                sub["output"] = f"Timed out after {timeout_s}s (subagent_timeout_s)."

    def _thread_target(config):
        try:
            from openai import OpenAI
            from nimcode.context import generate_workspace_map
            from nimcode.core.prompts import (
                build_system_prompt,
                strip_reasoning_spans,
            )
            from nimcode.core.agent import (
                parse_xml_tool_calls,
                MockMessage,
                normalize_messages,
            )

            api_key = os.environ.get("NVIDIA_API_KEY")
            if not api_key:
                try:
                    from dotenv import load_dotenv

                    load_dotenv(workspace_root / ".env")
                    global_env = Path.home() / ".nimcode" / ".env"
                    if global_env.exists() and not os.environ.get("NVIDIA_API_KEY"):
                        load_dotenv(dotenv_path=global_env)
                except Exception:
                    pass
                api_key = os.environ.get("NVIDIA_API_KEY")

            if not api_key and getattr(config, "api_base_url", None):
                api_key = "local_dummy_key"
            elif not api_key:
                with _SUBAGENTS_LOCK:
                    sub["status"] = "ERROR"
                    sub["output"] = "Error: NVIDIA_API_KEY is not set."
                return

            base_url = (
                getattr(config, "api_base_url", None)
                or "https://integrate.api.nvidia.com/v1"
            )

            client = OpenAI(
                base_url=base_url,
                api_key=api_key,
            )

            # Refresh system prompt and workspace map at start of execution
            w_map = generate_workspace_map(str(workspace_root))
            disable_native = getattr(config, "disable_native_tools", False)
            sys_prompt = build_system_prompt(
                str(workspace_root),
                w_map,
                disable_native_tools=disable_native,
            )
            with _SUBAGENTS_LOCK:
                if sub["messages"] and sub["messages"][0]["role"] == "system":
                    sub["messages"][0]["content"] = (
                        f"{sys_prompt}\n\nYou are a background subagent with the specialized role: {sub['role']}."
                    )

            max_steps = getattr(config, "max_loop_iterations", 10)
            try:
                max_steps = int(max_steps)
            except Exception:
                max_steps = 10
            max_steps = max(1, max_steps)

            from nimcode.core.agent import prune_history
            from nimcode.config_models import effective_budget

            budget = effective_budget(config, getattr(config, "fast_model", ""))
            for step in range(max_steps):
                with _SUBAGENTS_LOCK:
                    status = sub["status"]
                    _drain_inbox(sub)
                    messages = list(sub["messages"])
                if status == "KILLED":
                    break
                if _timed_out():
                    _mark_timed_out()
                    break
                # Long runs would otherwise overflow the context window.
                messages = prune_history(messages, budget)
                tools_arg = None if disable_native else AVAILABLE_TOOLS_SPEC
                model_to_use = getattr(
                    config, "fast_model", "nvidia/nemotron-3-super-120b-a12b"
                )
                normalized_messages = normalize_messages(
                    messages, disable_native_tools=disable_native
                )

                kwargs = {
                    "model": model_to_use,
                    "messages": normalized_messages,
                    "temperature": 0.3,
                }
                if hasattr(config, "max_tokens") and config.max_tokens is not None:
                    kwargs["max_tokens"] = config.max_tokens
                else:
                    kwargs["max_tokens"] = 4096

                if tools_arg:
                    kwargs["tools"] = tools_arg
                # A hung request must not outlive the wall-clock budget.
                kwargs["timeout"] = max(1.0, deadline - time.monotonic())

                response = client.chat.completions.create(**kwargs)
                if not response or not response.choices:
                    with _SUBAGENTS_LOCK:
                        sub["status"] = "ERROR"
                        sub["output"] = "Error: empty model response."
                    break
                raw_msg = response.choices[0].message
                cleaned = strip_reasoning_spans(raw_msg.content or "")

                if disable_native:
                    xml_calls = parse_xml_tool_calls(cleaned)
                    raw_msg = MockMessage(cleaned, xml_calls)
                msg_dict = {"role": "assistant", "content": cleaned}
                if raw_msg.tool_calls:
                    msg_dict["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in raw_msg.tool_calls
                    ]
                with _SUBAGENTS_LOCK:
                    _record(sub, msg_dict)
                    if not raw_msg.tool_calls:
                        if sub.get("inbox") and sub["status"] != "KILLED":
                            continue  # answer follow-ups that arrived mid-turn
                        sub["output"] = cleaned
                        if sub["status"] != "KILLED":
                            sub["status"] = "COMPLETED"
                        sub["runner"] = None
                        break

                consecutive_invalid = 0
                for tc in raw_msg.tool_calls:
                    is_valid, tname, targs = is_valid_tool_call(tc)
                    if not is_valid:
                        tool_res = f"Error: Invalid arguments for '{tname}'"
                        consecutive_invalid += 1
                        if consecutive_invalid >= 5:
                            tool_res += (
                                " [aborting: too many consecutive invalid calls]"
                            )
                    else:
                        consecutive_invalid = 0
                        # Thread-local config already carries this agent's
                        # depth, so nested spawns enforce the cap directly.
                        tool_res = execute_local_tool(
                            tname, targs, workspace_root, config
                        )
                    with _SUBAGENTS_LOCK:
                        _record(
                            sub,
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": str(tool_res),
                            },
                        )
                    if consecutive_invalid >= 5:
                        break
                if consecutive_invalid >= 5:
                    with _SUBAGENTS_LOCK:
                        sub["status"] = "ERROR"
                        sub["output"] = "Aborted: too many invalid tool calls."
                    break
            else:
                with _SUBAGENTS_LOCK:
                    if sub["status"] != "KILLED":
                        sub["status"] = "COMPLETED (Reached max steps)"
                    if sub["messages"] and sub["messages"][-1].get("content"):
                        sub["output"] = sub["messages"][-1]["content"]
        except BaseException as e:
            with _SUBAGENTS_LOCK:
                # Cooperative kill / cancel still lands here; mark KILLED, not ERROR.
                if sub.get("status") == "KILLED":
                    sub["output"] = "Killed by agent tool call."
                elif _timed_out():
                    _mark_timed_out()  # e.g. the per-request timeout fired
                else:
                    sub["status"] = f"ERROR: {e}"
                    sub["output"] = f"Subagent failed: {e}"
        finally:
            with _SUBAGENTS_LOCK:
                if sub.get("runner") is token:
                    sub["runner"] = None
                if sub.get("status") != "RUNNING":
                    sub["ended_at"] = time.time()

    import copy

    # Thread-local config copy carrying this agent's nesting level, so any
    # spawn_agent call from this thread enforces depth correctly. (Mutating
    # the shared config would race across sibling threads.)
    try:
        thread_config = copy.copy(config)
    except Exception:
        thread_config = config
    try:
        thread_config._subagent_depth = int(sub.get("depth", 0))
    except Exception:
        pass

    t = threading.Thread(target=_thread_target, args=(thread_config,), daemon=True)
    t.start()


def send_message_to_agent_logic(
    agent_id: str, message: str, workspace_root: Path, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    with _SUBAGENTS_LOCK:
        sub = ACTIVE_SUBAGENTS.get(agent_id)
        if not sub:
            return f"Error: No subagent found with ID '{agent_id}'. Active IDs: {list(ACTIVE_SUBAGENTS.keys())}"
        if sub["status"] == "KILLED":
            return f"Error: Subagent '{agent_id}' has been KILLED. Cannot accept new messages."

        if not isinstance(message, str) or not message:
            return "Error: 'message' must be a non-empty string."
        sub.setdefault("inbox", []).append({"role": "user", "content": message})
        role = sub["role"]
        status = sub["status"]

    resume_subagent_thread(agent_id, workspace_root, config)
    return f"Message sent to Subagent '{agent_id}' ({role}). Current status: {status}."


def kill_agent_logic(agent_id: str, workspace_root: Path, config: Any) -> str:
    with _SUBAGENTS_LOCK:
        sub = ACTIVE_SUBAGENTS.get(agent_id)
        if not sub:
            return f"Error: No subagent found with ID '{agent_id}'."
        sub["status"] = "KILLED"
        sub["output"] = "Killed by agent tool call."
    return f"Subagent '{agent_id}' successfully KILLED."


def list_agents_logic(workspace_root: Path, config: Any) -> str:
    reap_finished_subagents()
    with _SUBAGENTS_LOCK:
        if not ACTIVE_SUBAGENTS:
            return "No subagents spawned."
        agents_copy = {}
        for aid, sub in ACTIVE_SUBAGENTS.items():
            agents_copy[aid] = {
                "role": sub["role"],
                "status": sub["status"],
                "output": sub["output"],
            }

    result = []
    for aid, sub in agents_copy.items():
        snippet = str(sub.get("output") or "").replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:80] + "..."
        result.append(
            f"  • ID: {aid} | Role: {sub['role']} | Status: {sub['status']} | Output: {snippet}"
        )
    return "\n".join(result)
