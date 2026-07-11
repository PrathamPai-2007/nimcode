import re
from typing import Optional
from pathlib import Path

SYSTEM_PROMPT_TEMPLATE = """You are NIM-Engineer, an autonomous terminal coding agent.
You operate under three modes: Plan Mode, Build Mode, and YOLO Mode.

When given a task:
1. Analyze the workspace file structure provided to you.
2. If in Plan Mode (`AWAITING_PLAN_APPROVAL`), output a structured architectural plan inside <plan> tags. Do not call any tools during this phase.
3. Once approved or when operating in Build Mode (`EXECUTING`) or YOLO Mode (`YOLO`), issue tool calls (`read_file`, `write_file`, `execute_command`) one logical step at a time.

Available tools: `read_file`, `write_file`, `execute_command`.

Workspace Root: {workspace_root}

Current Workspace Structure:
{workspace_map}{agents_md_section}
"""


def build_system_prompt(
    workspace_root: str, workspace_map: str, disable_native_tools: bool = False
) -> str:
    agents_md_content = ""
    agents_md_path = Path(workspace_root) / "AGENTS.md"
    if not agents_md_path.exists():
        from nimcode.config import get_project_nimcode_dir

        agents_md_path = get_project_nimcode_dir(workspace_root) / "AGENTS.md"
    if not agents_md_path.exists():
        agents_md_path = Path(workspace_root) / ".nimcode" / "AGENTS.md"

    if agents_md_path.exists():
        try:
            raw_content = agents_md_path.read_text(encoding="utf-8").strip()
            if raw_content:
                agents_md_content = f'\n\n<agents_md_rules path="{agents_md_path.name}">\n{raw_content}\n</agents_md_rules>\nCRITICAL: You MUST strictly obey all architectural rules, coding standards, and commands defined above in `{agents_md_path.name}`.'
        except Exception:
            pass

    base_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        workspace_root=workspace_root,
        workspace_map=workspace_map,
        agents_md_section=agents_md_content,
    )

    if disable_native_tools:
        base_prompt += """

CRITICAL: NATIVE TOOL CALLING IS DISABLED/NOT SUPPORTED BY THE ENDPOINT.
To execute tools, you MUST format your tool calls in XML format inside your response text.
For example:
- To read a file:
<tool_call name="read_file">{"path": "file.txt"}</tool_call>

- To write a file:
<tool_call name="write_file">{"path": "file.txt", "content": "hello world"}</tool_call>

- To execute a command:
<tool_call name="execute_command">{"command": "python script.py"}</tool_call>

- To patch a file using SEARCH/REPLACE blocks:
<tool_call name="patch_file_blocks">{"path": "file.py", "edit_blocks": "<<<<<<< SEARCH\\ndef hello():\\n    print(\\"hi\\")\\n=======\\ndef hello():\\n    print(\\"hello world\\")\\n>>>>>>> REPLACE"}</tool_call>

- To search codebase contents (grep):
<tool_call name="search_grep">{"query": "def my_func"}</tool_call>

- To search class/function symbols:
<tool_call name="search_symbols">{"query": "MyClass"}</tool_call>

Do NOT output any native OpenAI function calls. Only output the XML <tool_call> block as shown above. Only call one tool at a time.
"""
    return base_prompt


def strip_reasoning_spans(text: str) -> str:
    """
    Removes <think>...</think>, <thought>...</thought>, and <reasoning>...</reasoning>
    spans or similar CoT reasoning artifacts from output.
    Also handles unclosed blocks if truncated or still streaming.
    """
    if not text:
        return ""
    cleaned = text
    for tag in ["think", "thought", "reasoning"]:
        cleaned = re.sub(
            f"<{tag}>.*?</{tag}>", "", cleaned, flags=re.DOTALL | re.IGNORECASE
        )
        cleaned = re.sub(f"<{tag}>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def extract_plan_block(text: str) -> Optional[str]:
    """
    Extracts the contents inside <plan>...</plan> tags.
    Returns None if no plan block is found. Also handles unclosed <plan> tag if truncated.
    """
    match = re.search(r"<plan>(.*?)</plan>", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Check for unclosed <plan> block if output was truncated
    match_unclosed = re.search(r"<plan>(.*)$", text, flags=re.DOTALL | re.IGNORECASE)
    if match_unclosed:
        return match_unclosed.group(1).strip()
    return None
