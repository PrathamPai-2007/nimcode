import sys
import json
import difflib
from pathlib import Path
from typing import Dict, Any, Tuple
from nimcode.tools.registry import ToolSandboxError

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def resolve_and_validate_path(path_str: str, workspace_root: Path) -> Path:
    """
    Resolves the provided path against workspace_root and ensures it does not escape
    the workspace boundaries via path traversal (e.g. '../../etc/passwd').
    """
    if not path_str or not isinstance(path_str, str):
        raise ToolSandboxError("Security Error: Invalid or missing file path.")

    resolved_root = workspace_root.resolve()
    target_path = (resolved_root / path_str).resolve()

    try:
        if hasattr(target_path, "is_relative_to"):
            if not target_path.is_relative_to(resolved_root):
                raise ToolSandboxError(
                    f"Security Error: Path '{path_str}' escapes workspace root '{resolved_root}'."
                )
        else:
            target_path.relative_to(resolved_root)
    except (ValueError, AttributeError):
        raise ToolSandboxError(
            f"Security Error: Path '{path_str}' escapes workspace root '{resolved_root}'."
        )

    return target_path


def render_preview(tool_name: str, args: Dict[str, Any], workspace_root: Path) -> str:
    """
    Renders a human-friendly preview of the tool call before requesting approval.
    For write_file, generates a unified diff. For execute_command, shows the exact command.
    """
    if tool_name == "read_file":
        return f"\n[Tool Preview: read_file] Path: {args.get('path')}\n"

    elif tool_name == "write_file":
        path_str = args.get("path", "")
        new_content = args.get("content", "")
        try:
            target_path = resolve_and_validate_path(path_str, workspace_root)
            old_content = ""
            if target_path.exists() and target_path.is_file():
                try:
                    with open(
                        target_path, "r", encoding="utf-8", errors="replace"
                    ) as f:
                        old_content = f.read()
                except Exception:
                    old_content = "<binary or unreadable content>\n"

            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            diff = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{path_str} (current)",
                    tofile=f"b/{path_str} (proposed)",
                )
            )
            diff_text = "".join(diff) if diff else "No changes detected."
            return f"\n[Tool Preview: write_file] Path: {path_str}\n--- Unified Diff ---\n{diff_text}\n--------------------\n"
        except Exception as e:
            return f"\n[Tool Preview: write_file] Path: {path_str} (Error computing diff: {e})\n"

    elif tool_name == "execute_command":
        cmd = args.get("command", "")
        return f"\n[Tool Preview: execute_command] Command:\n$ {cmd}\n(CWD: {workspace_root.resolve()})\n"

    elif tool_name == "run_tests":
        test_cmd = (
            args.get("test_command", "")
            or "auto-detect (pytest / npm test / cargo test / go test)"
        )
        return f"\n[Tool Preview: run_tests] Test Command:\n$ {test_cmd}\n(CWD: {workspace_root.resolve()})\n"

    elif tool_name == "patch_file_ast":
        path_str = args.get("path", "")
        target_sym = args.get("target_symbol", "")
        rep_code = args.get("replacement_code", "")
        try:
            from nimcode.tools.patch import patch_file_ast_logic

            diff_text = patch_file_ast_logic(
                workspace_root, path_str, target_sym, rep_code, dry_run=True
            )
            return f"\n[Tool Preview: patch_file_ast] Path: {path_str} | Target: '{target_sym}'\n--- Unified Diff ---\n{diff_text}\n--------------------\n"
        except Exception as e:
            return f"\n[Tool Preview: patch_file_ast] Path: {path_str} (Error computing diff: {e})\n"

    elif tool_name == "spawn_agent":
        role = args.get("role", "Subagent")
        prompt = args.get("task_prompt", "")
        return f"\n[Tool Preview: spawn_agent] Role: {role}\nTask Prompt: {prompt[:150]}...\n"

    elif tool_name == "send_message_to_agent":
        aid = args.get("agent_id", "")
        msg = args.get("message", "")
        return f"\n[Tool Preview: send_message_to_agent] Target ID: {aid}\nMessage: {msg[:150]}...\n"

    return f"\n[Tool Preview: {tool_name}] Args: {json.dumps(args)}\n"


def is_valid_tool_call(tool_call) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validates tool name and checks if JSON arguments can be parsed cleanly.
    Returns (is_valid, tool_name, parsed_args).
    """
    import re
    from nimcode.tools.registry import AVAILABLE_TOOLS_SPEC

    try:
        tool_name = tool_call.function.name
        valid_names = {spec["function"]["name"] for spec in AVAILABLE_TOOLS_SPEC}
        if tool_name not in valid_names:
            return False, tool_name or "unknown", {}

        raw_args = tool_call.function.arguments or "{}"
        # Strip markdown code fences that some models wrap around JSON
        raw_args = re.sub(r"```(?:json)?\s*", "", raw_args, flags=re.IGNORECASE)
        raw_args = re.sub(r"```", "", raw_args)
        raw_args = raw_args.strip()
        if not raw_args:
            raw_args = "{}"

        args = json.loads(raw_args)
        if not isinstance(args, dict):
            return False, tool_name, {}

        # Check required fields
        for spec in AVAILABLE_TOOLS_SPEC:
            if spec["function"]["name"] == tool_name:
                required = spec["function"].get("required", [])
                for req_field in required:
                    if req_field not in args:
                        return False, tool_name, args
        return True, tool_name, args
    except Exception:
        return False, "malformed_tool_call", {}
