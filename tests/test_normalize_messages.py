from nimcode.core.agent import normalize_messages


def test_normalize_messages_basic():
    messages = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
    res = normalize_messages(messages)
    assert res == messages


def test_normalize_messages_system_consolidation():
    messages = [
        {"role": "system", "content": "Sys 1"},
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "Sys 2"},
        {"role": "assistant", "content": "Hi"},
    ]
    res = normalize_messages(messages)
    assert len(res) == 3
    assert res[0] == {"role": "system", "content": "Sys 1\n\nSys 2"}
    assert res[1] == {"role": "user", "content": "Hello"}
    assert res[2] == {"role": "assistant", "content": "Hi"}


def test_normalize_messages_starts_with_assistant():
    messages = [
        {"role": "system", "content": "Sys 1"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Hello"},
    ]
    res = normalize_messages(messages)
    assert len(res) == 4
    assert res[0] == {"role": "system", "content": "Sys 1"}
    assert res[1] == {"role": "user", "content": "Let's continue the conversation."}
    assert res[2] == {"role": "assistant", "content": "Hi"}
    assert res[3] == {"role": "user", "content": "Hello"}


def test_normalize_messages_system_only():
    messages = [{"role": "system", "content": "Sys 1"}]
    res = normalize_messages(messages)
    assert len(res) == 2
    assert res[0] == {"role": "system", "content": "Sys 1"}
    assert res[1]["role"] == "user"


def test_normalize_messages_consecutive_merges():
    messages = [
        {"role": "user", "content": "Hello 1"},
        {"role": "user", "content": "Hello 2"},
        {"role": "assistant", "content": "Response"},
    ]
    res = normalize_messages(messages)
    assert len(res) == 2
    assert res[0] == {"role": "user", "content": "Hello 1\n\nHello 2"}
    assert res[1] == {"role": "assistant", "content": "Response"}


def test_prune_history_no_pruning_needed():
    from nimcode.core.agent import prune_history

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Query"},
        {"role": "assistant", "content": "Reply"},
    ]
    # budget is large enough, so it returns messages directly
    res = prune_history(messages, context_budget_tokens=1000, max_turns_retained=5)
    assert res == messages


def test_prune_history_truncates_tool_results():
    from nimcode.core.agent import prune_history

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "First User Query"},
        {"role": "assistant", "content": "Here is a tool call"},
        {"role": "tool", "content": "a" * 800},  # ~200 tokens
        {"role": "assistant", "content": "Final Reply"},
    ]
    # budget is small (e.g. 100 tokens), should truncate the tool content (since it's older than max_retained if we set max_retained=1)
    res = prune_history(messages, context_budget_tokens=100, max_turns_retained=1)
    assert len(res) == 5
    tool_msg = res[3]
    assert tool_msg["role"] == "tool"
    assert len(tool_msg["content"]) < 300
    assert "truncated" in tool_msg["content"]


def test_prune_history_drops_older_turns():
    from nimcode.core.agent import prune_history

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "First User Query"},
        {"role": "assistant", "content": "Older intermediate response 1"},
        {"role": "user", "content": "Older intermediate query 2"},
        {"role": "assistant", "content": "Recent response 3"},
        {"role": "user", "content": "Recent query 4"},
        {"role": "assistant", "content": "Recent response 5"},
    ]
    # budget is very small, say 30 tokens (approx 120 characters).
    # It must drop intermediate messages but keep system, first user query, and the most recent ones that fit.
    res = prune_history(messages, context_budget_tokens=30, max_turns_retained=2)
    # The essential parts are messages[0] and messages[1]
    assert res[0] == messages[0]
    assert res[1] == messages[1]
    # The intermediate messages (messages[2] and messages[3]) should have been dropped because they exceeded budget
    # Check that the most recent ones are retained at the end of the list
    assert res[-1] == messages[-1]
