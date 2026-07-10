import os
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import (
    register_tool,
    execute_local_tool,
    AVAILABLE_TOOLS_SPEC,
)
from nimcode.tools.base import is_valid_tool_call

ACTIVE_SUBAGENTS = {}

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


def spawn_agent_logic(
    role: str, task_prompt: str, workspace_root: Path, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    import threading
    import time
    from openai import OpenAI

    agent_id = (
        f"sub_{int(time.time() * 1000) % 100000}_{role.lower().replace(' ', '_')[:10]}"
    )
    ACTIVE_SUBAGENTS[agent_id] = {
        "id": agent_id,
        "role": role,
        "status": "RUNNING",
        "task_prompt": task_prompt,
        "output": "Execution in progress...",
        "messages": [],
        "created_at": time.time(),
    }

    def _run_subagent():
        try:
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
                api_key = os.environ.get("NVIDIA_API_KEY", "missing_nvidia_api_key")

            client = OpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=api_key,
            )
            from nimcode.core.prompts import build_system_prompt, strip_reasoning_spans

            sys_prompt = build_system_prompt("EXECUTING", workspace_root)
            messages = [
                {
                    "role": "system",
                    "content": f"{sys_prompt}\n\nYou are a background subagent with the specialized role: {role}.",
                },
                {"role": "user", "content": task_prompt},
            ]
            ACTIVE_SUBAGENTS[agent_id]["messages"] = messages

            max_steps = getattr(config, "max_loop_iterations", 5)
            for step in range(max_steps):
                if ACTIVE_SUBAGENTS[agent_id]["status"] == "KILLED":
                    break
                tools_arg = AVAILABLE_TOOLS_SPEC
                model_to_use = getattr(
                    config, "fast_model", "deepseek-ai/deepseek-v4-flash"
                )
                response = client.chat.completions.create(
                    model=model_to_use,
                    messages=messages,
                    tools=tools_arg,
                    temperature=0.3,
                )
                if not response or not response.choices:
                    break
                raw_msg = response.choices[0].message
                cleaned = strip_reasoning_spans(raw_msg.content or "")
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
                messages.append(msg_dict)
                if not raw_msg.tool_calls:
                    ACTIVE_SUBAGENTS[agent_id]["output"] = cleaned
                    ACTIVE_SUBAGENTS[agent_id]["status"] = "COMPLETED"
                    break

                for tc in raw_msg.tool_calls:
                    is_valid, tname, targs = is_valid_tool_call(tc)
                    if not is_valid:
                        tool_res = f"Error: Invalid arguments for '{tname}'"
                    else:
                        tool_res = execute_local_tool(
                            tname, targs, workspace_root, config
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tname,
                            "content": str(tool_res),
                        }
                    )
            else:
                ACTIVE_SUBAGENTS[agent_id]["status"] = "COMPLETED (Reached max steps)"
                if messages and messages[-1].get("content"):
                    ACTIVE_SUBAGENTS[agent_id]["output"] = messages[-1]["content"]
        except Exception as e:
            ACTIVE_SUBAGENTS[agent_id]["status"] = f"ERROR: {e}"
            ACTIVE_SUBAGENTS[agent_id]["output"] = f"Subagent failed: {e}"

    t = threading.Thread(target=_run_subagent, daemon=True)
    t.start()
    return f"Subagent '{role}' spawned with ID: {agent_id}. Status: RUNNING in background.\nUse send_message_to_agent(agent_id='{agent_id}', message='...') or check /team to inspect results."


def send_message_to_agent_logic(
    agent_id: str, message: str, workspace_root: Path, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    sub = ACTIVE_SUBAGENTS.get(agent_id)
    if not sub:
        return f"Error: No subagent found with ID '{agent_id}'. Active IDs: {list(ACTIVE_SUBAGENTS.keys())}"
    if sub["status"] != "RUNNING":
        return f"Subagent '{agent_id}' ({sub['role']}) is currently {sub['status']}.\nLast Output:\n{sub['output']}"
    sub["messages"].append({"role": "user", "content": message})
    return f"Message queued for Subagent '{agent_id}' ({sub['role']}). Current status: {sub['status']}."
