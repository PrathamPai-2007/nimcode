import os
import time
import shutil
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import resolve_and_validate_path

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file located within the workspace root.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to read.",
                }
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write or overwrite a file within the workspace root with new content. Creates parent directories if needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The complete text content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}


@register_tool("read_file", READ_FILE_SCHEMA)
def read_file_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path")
    if not path_str or not isinstance(path_str, str):
        return "Error: Invalid or missing 'path' parameter for read_file."

    target_path = resolve_and_validate_path(path_str, workspace_root)

    if not target_path.exists():
        return f"Error: File '{path_str}' does not exist."
    if not target_path.is_file():
        return f"Error: Path '{path_str}' is a directory, not a file."

    # Check file size before reading full content if binary/large
    file_size = target_path.stat().st_size
    if file_size > 5_000_000:  # 5MB absolute upper limit check for binary
        return f"Error: File '{path_str}' is too large to read ({file_size} bytes)."

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"Error: File '{path_str}' appears to be binary or not UTF-8 encoded."

    if len(content) > config.output_cap:
        return (
            content[: config.output_cap]
            + f"\n... [Output truncated at {config.output_cap} characters]"
        )
    return content


@register_tool("write_file", WRITE_FILE_SCHEMA)
def write_file_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path")
    if not path_str or not isinstance(path_str, str):
        return "Error: Invalid or missing 'path' parameter for write_file."
    content = args.get("content", "")
    target_path = resolve_and_validate_path(path_str, workspace_root)

    # Auto-backup before overwrite for easy one-command revert
    if (
        getattr(config, "auto_backup", True)
        and target_path.exists()
        and target_path.is_file()
    ):
        from nimcode.config import get_project_nimcode_dir

        backup_dir = get_project_nimcode_dir(workspace_root) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        try:
            rel_path = target_path.relative_to(workspace_root)
            safe_rel_name = str(rel_path).replace(os.sep, "_").replace("/", "_")
        except Exception:
            safe_rel_name = target_path.name
        backup_filename = f"{timestamp}_{safe_rel_name}"
        backup_path = backup_dir / backup_filename
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            pass

    # Create parent directories
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully wrote {len(content)} characters to '{path_str}'."
