import functools
from pathlib import Path
from typing import Callable, Dict, Any, List

TOOL_REGISTRY: Dict[str, Callable] = {}
AVAILABLE_TOOLS_SPEC: List[Dict[str, Any]] = []


class ToolSandboxError(Exception):
    pass


def register_tool(name: str, schema: Dict[str, Any]):
    def decorator(func: Callable):
        TOOL_REGISTRY[name] = func
        # Avoid duplicate schemas if re-imported
        if schema not in AVAILABLE_TOOLS_SPEC:
            AVAILABLE_TOOLS_SPEC.append(schema)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def execute_local_tool(
    tool_name: str, args: Dict[str, Any], workspace_root: Any, config: Any
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)
    if tool_name not in TOOL_REGISTRY:
        return f"Error: Unknown tool '{tool_name}'."
    try:
        handler = TOOL_REGISTRY[tool_name]
        return handler(args, workspace_root, config)
    except ToolSandboxError as se:
        return str(se)
    except Exception as e:
        return f"Error executing '{tool_name}': {e}"
