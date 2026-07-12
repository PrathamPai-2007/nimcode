import os
import ast
import time
import shutil
import difflib
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import resolve_and_validate_path

PATCH_FILE_AST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_file_ast",
        "description": "Smart AST-aware or target-symbol code block replacement for python files or structured source code without rewriting the entire file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to modify.",
                },
                "target_symbol": {
                    "type": "string",
                    "description": "The exact function name, class name, or unique target string block to replace.",
                },
                "replacement_code": {
                    "type": "string",
                    "description": "The new code content to replace the targeted symbol/block with.",
                },
            },
            "required": ["path", "target_symbol", "replacement_code"],
        },
    },
}


@register_tool("patch_file_ast", PATCH_FILE_AST_SCHEMA)
def patch_file_ast_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path", "")
    target_sym = args.get("target_symbol", "")
    rep_code = args.get("replacement_code", "")
    return patch_file_ast_logic(
        workspace_root, path_str, target_sym, rep_code, config=config, dry_run=False
    )


def patch_file_ast_logic(
    workspace_root: Path,
    path_str: str,
    target_symbol: str,
    replacement_code: str,
    config: Any = None,
    dry_run: bool = False,
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)

    target_path = resolve_and_validate_path(path_str, workspace_root)
    if not target_path.exists() or not target_path.is_file():
        return f"Error: File '{path_str}' does not exist or is not a file."

    try:
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            old_content = f.read()
    except Exception as e:
        return f"Error reading '{path_str}': {e}"

    old_lines = old_content.splitlines(keepends=True)
    new_lines = None

    if path_str.endswith(".py"):
        try:
            tree = ast.parse(old_content)
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    if getattr(node, "name", "") == target_symbol:
                        start_idx = node.lineno - 1
                        end_idx = getattr(node, "end_lineno", len(old_lines))
                        rep_lines = replacement_code.splitlines(keepends=True)
                        if rep_lines and not rep_lines[-1].endswith("\n"):
                            rep_lines[-1] += "\n"
                        new_lines = (
                            old_lines[:start_idx] + rep_lines + old_lines[end_idx:]
                        )
                        break
        except Exception:
            pass

    if new_lines is None:
        if target_symbol in old_content:
            new_content = old_content.replace(target_symbol, replacement_code, 1)
            new_lines = new_content.splitlines(keepends=True)
        else:
            return f"Error: Target symbol or block '{target_symbol}' not found in '{path_str}' via AST or substring search."

    if dry_run:
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path_str} (current)",
                tofile=f"b/{path_str} (proposed ast/symbol patch)",
            )
        )
        return "".join(diff) if diff else "No changes detected."

    if config and getattr(config, "auto_backup", True):
        from nimcode.config import get_project_nimcode_dir

        backup_dir = get_project_nimcode_dir(workspace_root) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        try:
            rel_path = target_path.relative_to(workspace_root)
            safe_rel_name = str(rel_path).replace(os.sep, "_").replace("/", "_")
        except Exception:
            safe_rel_name = target_path.name
        backup_path = backup_dir / f"{timestamp}_ast_{safe_rel_name}"
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            pass

    new_content_final = "".join(new_lines)
    has_crlf = False
    try:
        with open(target_path, "rb") as f_bin:
            has_crlf = b"\r\n" in f_bin.read(4096)
    except Exception:
        pass

    newline = "\r\n" if has_crlf else "\n"
    with open(target_path, "w", encoding="utf-8", newline=newline) as f:
        f.write(new_content_final)

    return f"Successfully patched symbol/block '{target_symbol}' in '{path_str}' ({len(new_content_final)} total chars)."
