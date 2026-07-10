# Tools package initialization
# Import all tool modules to trigger decorators/registrations
from . import fs
from . import cmd
from . import patch
from . import swarm

# Expose public interfaces
from .registry import (
    TOOL_REGISTRY,
    AVAILABLE_TOOLS_SPEC,
    execute_local_tool,
    ToolSandboxError,
)
from .base import (
    resolve_and_validate_path,
    render_preview,
    is_valid_tool_call,
)
from .swarm import (
    ACTIVE_SUBAGENTS,
    spawn_agent_logic,
    send_message_to_agent_logic,
)
from .cmd import (
    execute_test_runner,
)

__all__ = [
    "fs",
    "cmd",
    "patch",
    "swarm",
    "TOOL_REGISTRY",
    "AVAILABLE_TOOLS_SPEC",
    "execute_local_tool",
    "ToolSandboxError",
    "resolve_and_validate_path",
    "render_preview",
    "is_valid_tool_call",
    "ACTIVE_SUBAGENTS",
    "spawn_agent_logic",
    "send_message_to_agent_logic",
    "execute_test_runner",
]
