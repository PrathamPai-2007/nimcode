import os
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import (
    register_tool,
    execute_local_tool,
    AVAILABLE_TOOLS_SPEC,
)
from nimcode.tools.base import is_valid_tool_call
from typing import Dict, Any

ACTIVE_SUBAGENTS: Dict[str, Any] = {}

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
    import threading
    import time

    agent_id = (
        f"sub_{int(time.time() * 1000) % 100000}_{role.lower().replace(' ', '_')[:10]}"
    )

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

    ACTIVE_SUBAGENTS[agent_id] = {
        "id": agent_id,
        "role": role,
        "status": "RUNNING",
        "task_prompt": task_prompt,
        "output": "Execution in progress...",
        "messages": messages,
        "created_at": time.time(),
        "lock": threading.Lock(),
    }

    resume_subagent_thread(agent_id, workspace_root, config)

    return f"Subagent '{role}' spawned with ID: {agent_id}. Status: RUNNING in background.\nUse send_message_to_agent(agent_id='{agent_id}', message='...') or check /team to inspect results."


def resume_subagent_thread(agent_id: str, workspace_root: Path, config: Any):
    sub = ACTIVE_SUBAGENTS.get(agent_id)
    if not sub:
        return

    def _thread_target():
        # Prevent multiple threads from running simultaneously on this subagent
        if not sub["lock"].acquire(blocking=False):
            return
        try:
            sub["status"] = "RUNNING"
            sub["output"] = "Execution in progress..."

            from openai import OpenAI
            from nimcode.context import generate_workspace_map
            from nimcode.core.prompts import build_system_prompt, strip_reasoning_spans
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
                api_key = "missing_nvidia_api_key"

            base_url = getattr(config, "api_base_url", None) or "https://integrate.api.nvidia.com/v1"

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
            if sub["messages"] and sub["messages"][0]["role"] == "system":
                sub["messages"][0]["content"] = (
                    f"{sys_prompt}\n\nYou are a background subagent with the specialized role: {sub['role']}."
                )

            max_steps = (
                50
                if getattr(config, "mode", "") == "yolo"
                else getattr(config, "max_loop_iterations", 5)
            )

            for step in range(max_steps):
                if sub["status"] == "KILLED":
                    break
                tools_arg = None if disable_native else AVAILABLE_TOOLS_SPEC
                model_to_use = getattr(
                    config, "fast_model", "deepseek-ai/deepseek-v4-flash"
                )
                normalized_messages = normalize_messages(
                    sub["messages"], disable_native_tools=disable_native
                )

                kwargs = {
                    "model": model_to_use,
                    "messages": normalized_messages,
                    "temperature": 0.3,
                }
                if tools_arg:
                    kwargs["tools"] = tools_arg

                response = client.chat.completions.create(**kwargs)
                if not response or not response.choices:
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
                sub["messages"].append(msg_dict)
                if not raw_msg.tool_calls:
                    sub["output"] = cleaned
                    sub["status"] = "COMPLETED"
                    break

                for tc in raw_msg.tool_calls:
                    is_valid, tname, targs = is_valid_tool_call(tc)
                    if not is_valid:
                        tool_res = f"Error: Invalid arguments for '{tname}'"
                    else:
                        tool_res = execute_local_tool(
                            tname, targs, workspace_root, config
                        )
                    sub["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(tool_res),
                        }
                    )
            else:
                sub["status"] = "COMPLETED (Reached max steps)"
                if sub["messages"] and sub["messages"][-1].get("content"):
                    sub["output"] = sub["messages"][-1]["content"]
        except Exception as e:
            sub["status"] = f"ERROR: {e}"
            sub["output"] = f"Subagent failed: {e}"
        finally:
            sub["lock"].release()

    import threading

    t = threading.Thread(target=_thread_target, daemon=True)
    t.start()


def send_message_to_agent_logic(
    agent_id: str, message: str, workspace_root: Path, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    sub = ACTIVE_SUBAGENTS.get(agent_id)
    if not sub:
        return f"Error: No subagent found with ID '{agent_id}'. Active IDs: {list(ACTIVE_SUBAGENTS.keys())}"
    if sub["status"] == "KILLED":
        return (
            f"Error: Subagent '{agent_id}' has been KILLED. Cannot accept new messages."
        )

    if sub["messages"] and sub["messages"][-1].get("role") == "tool":
        sub["messages"].append({"role": "assistant", "content": "Acknowledged."})

    sub["messages"].append({"role": "user", "content": message})
    resume_subagent_thread(agent_id, workspace_root, config)
    return f"Message sent to Subagent '{agent_id}' ({sub['role']}). Current status: {sub['status']}."


def kill_agent_logic(agent_id: str, workspace_root: Path, config: Any) -> str:
    sub = ACTIVE_SUBAGENTS.get(agent_id)
    if not sub:
        return f"Error: No subagent found with ID '{agent_id}'."
    sub["status"] = "KILLED"
    sub["output"] = "Killed by agent tool call."
    return f"Subagent '{agent_id}' successfully KILLED."


def list_agents_logic(workspace_root: Path, config: Any) -> str:
    if not ACTIVE_SUBAGENTS:
        return "No subagents spawned."
    result = []
    for aid, sub in ACTIVE_SUBAGENTS.items():
        snippet = sub["output"].replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:80] + "..."
        result.append(
            f"  • ID: {aid} | Role: {sub['role']} | Status: {sub['status']} | Output: {snippet}"
        )
    return "\n".join(result)
