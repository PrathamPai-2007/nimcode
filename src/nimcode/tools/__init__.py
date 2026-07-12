# Tools package initialization
# Import all tool modules to trigger decorators/registrations
from . import fs
from . import cmd
from . import patch
from . import swarm
from . import browser
from . import web

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
    is_docker_available,
)
from .swarm import (
    ACTIVE_SUBAGENTS,
    spawn_agent_logic,
    send_message_to_agent_logic,
    kill_agent_logic,
    list_agents_logic,
)
from .cmd import (
    run_tests_tool,
    execute_test_runner,
    start_background_process_tool,
    get_process_output_tool,
    stop_process_tool,
)

__all__ = [
    "fs",
    "cmd",
    "patch",
    "swarm",
    "browser",
    "web",
    "TOOL_REGISTRY",
    "AVAILABLE_TOOLS_SPEC",
    "execute_local_tool",
    "ToolSandboxError",
    "resolve_and_validate_path",
    "render_preview",
    "is_valid_tool_call",
    "is_docker_available",
    "ACTIVE_SUBAGENTS",
    "spawn_agent_logic",
    "send_message_to_agent_logic",
    "kill_agent_logic",
    "list_agents_logic",
    "run_tests_tool",
    "execute_test_runner",
    "start_background_process_tool",
    "get_process_output_tool",
    "stop_process_tool",
]
