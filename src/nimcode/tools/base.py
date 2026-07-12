import json
import re
import difflib
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple
from nimcode.tools.registry import ToolSandboxError


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


def is_docker_available() -> bool:
    """
    Check if Docker daemon is available and responsive.
    Returns True if 'docker info' succeeds, False otherwise.
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def is_valid_tool_call(tool_call: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validate that a tool call has the required arguments.
    Returns (is_valid, tool_name, args).
    """
    func = getattr(tool_call, "function", None)
    if not func:
        return False, "", {}
    tool_name = getattr(func, "name", "")
    raw_args = getattr(func, "arguments", "")

    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args) if raw_args else {}
        except Exception:
            return False, tool_name, {}

    if tool_name == "read_file":
        if not args.get("path"):
            return False, tool_name, args
    elif tool_name == "write_file":
        if not args.get("path"):
            return False, tool_name, args
        if "content" not in args:
            return False, tool_name, args
    elif tool_name == "execute_command":
        if not args.get("command"):
            return False, tool_name, args
    elif tool_name == "run_tests":
        # command is optional, defaults to pytest
        pass
    elif tool_name == "list_files":
        # path is optional, defaults to "."
        pass
    elif tool_name == "glob_files":
        if not args.get("pattern"):
            return False, tool_name, args
    elif tool_name == "patch_file_ast":
        if not args.get("path"):
            return False, tool_name, args
        if not args.get("target_symbol") or not args.get("replacement_code"):
            return False, tool_name, args
    elif tool_name == "patch_file_blocks":
        if not args.get("path"):
            return False, tool_name, args
        if not args.get("edit_blocks"):
            return False, tool_name, args
    elif tool_name == "search_grep":
        if not args.get("query"):
            return False, tool_name, args
    elif tool_name == "search_symbols":
        if not args.get("query"):
            return False, tool_name, args
    elif tool_name == "spawn_agent":
        if not args.get("role"):
            return False, tool_name, args
        if not args.get("task_prompt"):
            return False, tool_name, args
    elif tool_name == "send_message_to_agent":
        if not args.get("agent_id"):
            return False, tool_name, args
        if not args.get("message"):
            return False, tool_name, args
    elif tool_name == "kill_agent":
        if not args.get("agent_id"):
            return False, tool_name, args
    elif tool_name == "list_agents":
        pass
    elif tool_name == "start_background_process":
        if not args.get("command"):
            return False, tool_name, args
    elif tool_name == "get_process_output":
        if not args.get("process_id"):
            return False, tool_name, args
    elif tool_name == "stop_process":
        if not args.get("process_id"):
            return False, tool_name, args
    elif tool_name == "screenshot_page":
        if not args.get("url") and not args.get("html_file"):
            return False, tool_name, args
    elif tool_name == "web_search":
        if not args.get("query"):
            return False, tool_name, args
    else:
        return False, tool_name, args
    return True, tool_name, args


def scrub_text(text: str) -> str:
    """
    Scrub inline secrets (like Bearer tokens, Basic auth, or CLI secret flags) from arbitrary text.
    """
    if not text:
        return text
    # Mask Bearer tokens
    text = re.sub(r"(?i)(bearer\s+)[\w\.\-]+", r"\1***REDACTED***", text)
    # Mask Basic auth strings
    text = re.sub(r"(?i)(basic\s+)[a-zA-Z0-9\+/=]+", r"\1***REDACTED***", text)
    # Mask common CLI flags e.g., -p<pass>, --password=<pass>, --token=<token>
    text = re.sub(
        r"(?i)(-(?:p|-password|-token|-secret)\s*=?\s*['\"]?)[\w\.\-\+/\=]+",
        r"\1***REDACTED***",
        text,
    )
    # Mask headers like x-api-key
    text = re.sub(
        r"(?i)((?:api-key|x-api-key)\s*:\s*['\"]?)[\w\.\-]+", r"\1***REDACTED***", text
    )
    return text


def truncate_output(text: str, max_length: int = 100000) -> str:
    """
    Smart truncation: keeps the first N and last N chars of the string, dropping the middle.
    This ensures that both the beginning (context) and the end (stack traces/errors) are preserved.
    """
    if len(text) <= max_length:
        return text

    msg = f"\n... [Output truncated: dropped {len(text) - max_length} chars from the middle] ...\n"
    budget = max_length - len(msg)

    if budget <= 0:
        return text[:max_length]

    first_half = budget // 2
    last_half = budget - first_half

    return text[:first_half] + msg + text[-last_half:]


def render_preview(
    tool_name: str, args: Dict[str, Any], workspace_root: Path, config: Any = None
) -> str:
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
                    old_content = "<binary or unreadable file>"
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path_str}",
                tofile=f"b/{path_str}",
            )
            diff_text = "".join(diff)
            if not diff_text:
                diff_text = "(no changes)"
            return f"\n[Tool Preview: write_file] Path: {path_str}\n{diff_text}\n"
        except ToolSandboxError as e:
            return f"\n[Tool Preview: write_file] Path: {path_str}\nError: {e}\n"

    elif tool_name == "execute_command":
        command = scrub_text(args.get("command", ""))
        use_docker = args.get("use_docker", None)
        if use_docker is None:
            # Will be determined at execution time based on config
            use_docker = (
                not getattr(config, "disable_docker", False) and is_docker_available()
                if config is not None
                else is_docker_available()
            )
        if use_docker:
            docker_image = args.get("docker_image", "python:3.11-slim")
            workspace = workspace_root.resolve()
            # Show a preview of the docker command
            cmd_preview = (
                f"docker run --rm --init "
                f"--cap-drop ALL "
                f"--security-opt no-new-privileges:true "
                f"-v {str(workspace)}:/workspace "
                f"-w /workspace "
                f"{docker_image} "
                f"sh -c {shlex.quote(command)}"
            )
            return f"\n[Tool Preview: execute_command (Docker)]\n{cmd_preview}\n"
        else:
            return f"\n[Tool Preview: execute_command (Host)]\n{command}\n"

    elif tool_name == "run_tests":
        command = scrub_text(args.get("command", "pytest"))
        use_docker = args.get("use_docker", None)
        if use_docker is None:
            use_docker = (
                not getattr(config, "disable_docker", False) and is_docker_available()
                if config is not None
                else is_docker_available()
            )
        if use_docker:
            docker_image = args.get("docker_image", "python:3.11-slim")
            workspace = workspace_root.resolve()
            cmd_preview = (
                f"docker run --rm --init "
                f"--cap-drop ALL "
                f"--security-opt no-new-privileges:true "
                f"-v {str(workspace)}:/workspace "
                f"-w /workspace "
                f"{docker_image} "
                f"sh -c {shlex.quote(command)}"
            )
            return f"\n[Tool Preview: run_tests (Docker)]\n{cmd_preview}\n"
        else:
            return f"\n[Tool Preview: run_tests (Host)]\n{command}\n"

    elif tool_name == "list_files":
        path = args.get("path", ".")
        return f"\n[Tool Preview: list_files] Path: {path}\n"

    elif tool_name == "glob_files":
        pattern = args.get("pattern", "**/*")
        return f"\n[Tool Preview: glob_files] Pattern: {pattern}\n"

    elif tool_name == "patch_file_ast":
        path_str = args.get("path", "")
        target_sym = args.get("target_symbol", "")
        rep_code = args.get("replacement_code", "")
        try:
            from nimcode.tools.patch import patch_file_ast_logic

            diff_text = patch_file_ast_logic(
                workspace_root, path_str, target_sym, rep_code, dry_run=True
            )
            return f"\n[Tool Preview: patch_file_ast] Path: {path_str}\n{diff_text}\n"
        except Exception as e:
            return f"\n[Tool Preview: patch_file_ast] Path: {path_str}\nError Proposed Patch: {e}\n"

    elif tool_name == "patch_file_blocks":
        path_str = args.get("path", "")
        edit_blocks = args.get("edit_blocks", "")
        try:
            from nimcode.tools.fs import patch_file_blocks_logic

            diff_text = patch_file_blocks_logic(
                workspace_root, path_str, edit_blocks, dry_run=True
            )
            return (
                f"\n[Tool Preview: patch_file_blocks] Path: {path_str}\n{diff_text}\n"
            )
        except Exception as e:
            return f"\n[Tool Preview: patch_file_blocks] Path: {path_str}\nError Proposed Patch: {e}\n"

    elif tool_name == "search_grep":
        query = args.get("query", "")
        path = args.get("path", ".")
        return f"\n[Tool Preview: search_grep] Query: '{query}', Path: {path}\n"

    elif tool_name == "search_symbols":
        query = args.get("query", "")
        return f"\n[Tool Preview: search_symbols] Query: '{query}'\n"

    elif tool_name == "spawn_agent":
        task = args.get("task_prompt", "")
        return f"\n[Tool Preview: spawn_agent] Task: {task[:100]}...\n"

    elif tool_name == "send_message_to_agent":
        agent_id = args.get("agent_id", "")
        message = args.get("message", "")
        return f"\n[Tool Preview: send_message_to_agent] Agent: {agent_id}, Message: {message[:100]}...\n"

    elif tool_name == "kill_agent":
        agent_id = args.get("agent_id", "")
        return f"\n[Tool Preview: kill_agent] Agent: {agent_id}\n"

    elif tool_name == "list_agents":
        return "\n[Tool Preview: list_agents]\n"

    elif tool_name == "start_background_process":
        command = scrub_text(args.get("command", ""))
        return f"\n[Tool Preview: start_background_process] Start background process: `{command}` (will run detached, use get_process_output to check on it)\n"

    elif tool_name == "get_process_output":
        process_id = args.get("process_id", "")
        return f"\n[Tool Preview: get_process_output] Get output/logs for background process '{process_id}'\n"

    elif tool_name == "stop_process":
        process_id = args.get("process_id", "")
        return (
            f"\n[Tool Preview: stop_process] Stop background process '{process_id}'\n"
        )

    elif tool_name == "screenshot_page":
        url = args.get("url", "")
        html_file = args.get("html_file", "")
        target = url if url else html_file
        return f"\n[Tool Preview: screenshot_page] Target: {target}\n"

    elif tool_name == "web_search":
        query = args.get("query", "")
        return f"\n[Tool Preview: web_search] Query: '{query}'\n"

    return f"\n[Tool Preview: {tool_name}] Args: {json.dumps(args, indent=2)}\n"


# Compiled regex for detecting sensitive environment variables.
# This covers common names, abbreviations (_PW, _PWD), and ignores case.
SENSITIVE_ENV_REGEX = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|_PW|_PWD|CREDENTIAL|PRIVATE_KEY|AUTH_)",
    re.IGNORECASE,
)


def scrub_env(env: Dict[str, str]) -> Dict[str, str]:
    """
    Remove sensitive environment variables before passing to subprocess.
    Uses regex to catch common credential patterns and abbreviations.
    """
    scrubbed = {}
    for k, v in env.items():
        if SENSITIVE_ENV_REGEX.search(k):
            scrubbed[k] = "***REDACTED***"
        else:
            scrubbed[k] = v
    return scrubbed
