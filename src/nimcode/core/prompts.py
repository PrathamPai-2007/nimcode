import re
from typing import Optional
from pathlib import Path

SYSTEM_PROMPT_TEMPLATE = """You are NIM-Engineer, an autonomous terminal coding agent.
You operate under three modes: Plan Mode, Build Mode, and YOLO Mode.

When given a task:
1. Analyze the workspace file structure provided to you.
2. If in Plan Mode (`AWAITING_PLAN_APPROVAL`), output a structured architectural plan inside <plan> tags. Do not call any tools during this phase.
3. Once approved or when operating in Build Mode (`EXECUTING`) or YOLO Mode (`YOLO`), issue tool calls (`read_file`, `write_file`, `execute_command`, `start_background_process`, `get_process_output`, `stop_process`, `screenshot_page`, `web_search`) one logical step at a time.

Available tools: `read_file`, `write_file`, `execute_command`, `start_background_process`, `get_process_output`, `stop_process`, `screenshot_page`, `web_search`.

Background Processes Guidance:
For any command that doesn't exit on its own (dev servers, web servers, watch processes, etc. e.g. `npm run dev`, `python -m http.server`, `flask run`, `vite`, `uvicorn`, `ng serve`, `rails server`, `next dev`), the agent must use `start_background_process` instead of `execute_command`. Check progress/logs with `get_process_output`, and call `stop_process` when finished verifying — never leave background processes running at the end of a turn.

Visual Verification Loop:
After building or modifying any frontend/UI feature, you MUST visually verify your work:
1. Start the dev server in the background (`start_background_process`).
2. Capture a screenshot of the running page (`screenshot_page`).
3. Critically evaluate the screenshot against this visual checklist:
   - No unstyled defaults
   - Consistent spacing and alignment
   - Clear visual hierarchy
   - No overlapping or clipped elements
   - Loading/error states present and styled (if applicable)
4. If the UI fails any checks, fix the code, capture a new screenshot, and repeat the verification.
5. Once visually verified, stop the dev server (`stop_process`).

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

- To start a background process:
<tool_call name="start_background_process">{"command": "npm run dev"}</tool_call>

- To get background process output:
<tool_call name="get_process_output">{"process_id": "bg_1"}</tool_call>

- To stop a background process:
<tool_call name="stop_process">{"process_id": "bg_1"}</tool_call>

- To screenshot a local page for visual verification:
<tool_call name="screenshot_page">{"url": "http://localhost:3000"}</tool_call>

- To patch a file using SEARCH/REPLACE blocks:
<tool_call name="patch_file_blocks">{"path": "file.py", "edit_blocks": "<<<<<<< SEARCH\\ndef hello():\\n    print(\\"hi\\")\\n=======\\ndef hello():\\n    print(\\"hello world\\")\\n>>>>>>> REPLACE"}</tool_call>

- To search codebase contents (grep):
<tool_call name="search_grep">{"query": "def my_func"}</tool_call>

- To search class/function symbols:
<tool_call name="search_symbols">{"query": "MyClass"}</tool_call>

- To search the web:
<tool_call name="web_search">{"query": "NVIDIA API keys"}</tool_call>

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
