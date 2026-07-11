import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.tools.swarm import (
    ACTIVE_SUBAGENTS,
    kill_agent_logic,
    list_agents_logic,
    send_message_to_agent_logic,
)
from nimcode.tools.base import is_valid_tool_call, render_preview
from nimcode.config import Config
from nimcode.core.agent import MockToolCall, MockFunction
import threading


def test_subagent_kill_and_list():
    # Setup dummy agent in ACTIVE_SUBAGENTS
    ACTIVE_SUBAGENTS["sub_dummy"] = {
        "id": "sub_dummy",
        "role": "Test Agent",
        "status": "RUNNING",
        "task_prompt": "hello",
        "output": "Running dummy...",
        "messages": [],
        "lock": threading.Lock(),
    }

    # Test list_agents
    list_res = list_agents_logic(Path.cwd(), Config())
    assert "sub_dummy" in list_res
    assert "Test Agent" in list_res
    assert "RUNNING" in list_res

    # Test kill_agent
    kill_res = kill_agent_logic("sub_dummy", Path.cwd(), Config())
    assert "successfully KILLED" in kill_res
    assert ACTIVE_SUBAGENTS["sub_dummy"]["status"] == "KILLED"

    # Test send_message on killed agent raises error
    msg_res = send_message_to_agent_logic("sub_dummy", "ping", Path.cwd(), Config())
    assert "KILLED" in msg_res
    assert "Error" in msg_res


def test_tool_call_validation_extra():
    # kill_agent validation
    func_kill = MockFunction("kill_agent", '{"agent_id": "sub_123"}')
    tc_kill = MockToolCall("1", "function", func_kill)
    is_valid, name, args = is_valid_tool_call(tc_kill)
    assert is_valid is True
    assert name == "kill_agent"
    assert args == {"agent_id": "sub_123"}

    # list_agents validation
    func_list = MockFunction("list_agents", "{}")
    tc_list = MockToolCall("2", "function", func_list)
    is_valid, name, args = is_valid_tool_call(tc_list)
    assert is_valid is True
    assert name == "list_agents"


def test_render_preview_extra():
    # Test kill_agent preview
    preview_kill = render_preview("kill_agent", {"agent_id": "sub_123"}, Path.cwd())
    assert "[Tool Preview: kill_agent]" in preview_kill
    assert "sub_123" in preview_kill

    # Test list_agents preview
    preview_list = render_preview("list_agents", {}, Path.cwd())
    assert "[Tool Preview: list_agents]" in preview_list


def test_clean_surrogates():
    from nimcode.core.agent import clean_surrogates

    # Text with surrogate pair (stitchable)
    text_pair = "hello \ud83d\ude00 world"
    cleaned_pair = clean_surrogates(text_pair)
    assert cleaned_pair == "hello 😀 world"

    # Text with lone surrogate (should be stripped)
    text_lone = "hello \ud800 world"
    cleaned_lone = clean_surrogates(text_lone)
    assert cleaned_lone == "hello  world"


def test_all_schemas_have_type_and_function():
    from nimcode.tools.registry import AVAILABLE_TOOLS_SPEC

    for schema in AVAILABLE_TOOLS_SPEC:
        assert isinstance(schema, dict)
        assert "type" in schema, f"Schema missing 'type' field: {schema}"
        assert schema["type"] == "function"
        assert "function" in schema, f"Schema missing 'function' field: {schema}"
        assert isinstance(schema["function"], dict)
        assert "name" in schema["function"]
        assert "description" in schema["function"]
