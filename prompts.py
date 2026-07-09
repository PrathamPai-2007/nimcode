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


def build_system_prompt(workspace_root: str, workspace_map: str) -> str:
    agents_md_content = ""
    agents_md_path = Path(workspace_root) / "AGENTS.md"
    if not agents_md_path.exists():
        agents_md_path = Path(workspace_root) / ".nimcode" / "AGENTS.md"

    if agents_md_path.exists():
        try:
            raw_content = agents_md_path.read_text(encoding="utf-8").strip()
            if raw_content:
                agents_md_content = f'\n\n<agents_md_rules path="{agents_md_path.name}">\n{raw_content}\n</agents_md_rules>\nCRITICAL: You MUST strictly obey all architectural rules, coding standards, and commands defined above in `{agents_md_path.name}`.'
        except Exception:
            pass

    return SYSTEM_PROMPT_TEMPLATE.format(
        workspace_root=workspace_root,
        workspace_map=workspace_map,
        agents_md_section=agents_md_content,
    )


def strip_reasoning_spans(text: str) -> str:
    """
    Removes <think>...</think> spans or similar CoT reasoning artifacts from output.
    Also handles unclosed <think> blocks if truncated or still streaming.
    """
    if not text:
        return ""
    # Strip closed <think>...</think> tags (DOTALL so . matches newlines)
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip any trailing or unclosed <think> block if present
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def extract_plan_block(text: str) -> Optional[str]:
    """
    Extracts the contents inside <plan>...</plan> tags.
    Returns None if no plan block is found.
    """
    match = re.search(r"<plan>(.*?)</plan>", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None
