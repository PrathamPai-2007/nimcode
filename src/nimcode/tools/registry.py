from pathlib import Path
from typing import Callable, Dict, Any, List

TOOL_REGISTRY: Dict[str, Callable] = {}
AVAILABLE_TOOLS_SPEC: List[Dict[str, Any]] = []


class ToolSandboxError(Exception):
    pass


def register_tool(name: str, schema: Dict[str, Any]):
    def decorator(func: Callable):
        if name in TOOL_REGISTRY:
            # Remove stale spec(s) for this name so the model never sees duplicates.
            AVAILABLE_TOOLS_SPEC[:] = [
                s
                for s in AVAILABLE_TOOLS_SPEC
                if s.get("function", {}).get("name") != name
            ]
        TOOL_REGISTRY[name] = func
        # Avoid duplicate schemas if re-imported
        if schema not in AVAILABLE_TOOLS_SPEC:
            AVAILABLE_TOOLS_SPEC.append(schema)
        return func

    return decorator


# Max nested subagent depth: subagents may not spawn further subagents.
MAX_SUBAGENT_DEPTH = 3


def execute_local_tool(
    tool_name: str, args: Dict[str, Any], workspace_root: Any, config: Any
) -> str:
    import json
    import logging

    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    if tool_name not in TOOL_REGISTRY:
        return f"Error: Unknown tool '{tool_name}'."
    if not isinstance(args, dict):
        return f"Error: Invalid arguments for '{tool_name}': args must be an object."
    try:
        json.dumps(args, default=str)
    except Exception:
        return f"Error: Invalid arguments for '{tool_name}': args must be JSON-serializable."
    # Fork-bomb guard: limit nesting of spawn_agent inside swarm threads.
    if tool_name == "spawn_agent":
        depth = getattr(config, "_subagent_depth", 0) if config is not None else 0
        try:
            depth = int(depth)
        except Exception:
            depth = 0
        active = 0
        try:
            from nimcode.tools import swarm as _swarm

            # Only live agents count; finished/killed records stay in the
            # registry for /team and would otherwise exhaust the cap forever.
            active = sum(
                1
                for a in list(getattr(_swarm, "ACTIVE_SUBAGENTS", {}).values())
                if isinstance(a, dict) and a.get("status") == "RUNNING"
            )
        except Exception:
            active = 0
        if depth >= MAX_SUBAGENT_DEPTH:
            return "Error: spawn_agent denied: maximum subagent nesting depth reached."
        if active >= 10:
            return "Error: spawn_agent denied: too many active subagents (max 10)."
    try:
        handler = TOOL_REGISTRY[tool_name]
        return handler(args, workspace_root, config)
    except ToolSandboxError as se:
        return str(se)
    except Exception as e:
        logging.getLogger(__name__).exception("tool %s failed", tool_name)
        return f"Error executing '{tool_name}': {e}"
