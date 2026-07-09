import os
import difflib
import subprocess
import json
import time
import shutil
from pathlib import Path
from typing import Dict, Any, Tuple

AVAILABLE_TOOLS_SPEC = [
    {
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
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute a shell command inside the sandboxed workspace root directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command string to execute in the terminal.",
                    }
                },
                "required": ["command"],
            },
        },
    },
]


class ToolSandboxError(Exception):
    """Raised when a tool attempts to violate sandbox constraints or fails validation."""

    pass


def resolve_and_validate_path(path_str: str, workspace_root: Path) -> Path:
    """
    Resolves the provided path against workspace_root and ensures it does not escape
    the workspace boundaries via path traversal (e.g. '../../etc/passwd').
    """
    resolved_root = workspace_root.resolve()
    target_path = (resolved_root / path_str).resolve()

    try:
        # Python 3.9+ supports is_relative_to
        if not target_path.is_relative_to(resolved_root):
            raise ToolSandboxError(
                f"Security Error: Path '{path_str}' escapes workspace root '{resolved_root}'."
            )
    except AttributeError:
        # Fallback for earlier Python versions if needed
        if str(resolved_root) not in str(target_path):
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
        return f"\n📖 [Tool Preview: read_file] Path: {args.get('path')}\n"

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
            return f"\n✍️ [Tool Preview: write_file] Path: {path_str}\n--- Unified Diff ---\n{diff_text}\n--------------------\n"
        except Exception as e:
            return f"\n✍️ [Tool Preview: write_file] Path: {path_str} (Error computing diff: {e})\n"

    elif tool_name == "execute_command":
        cmd = args.get("command", "")
        return f"\n💻 [Tool Preview: execute_command] Command:\n$ {cmd}\n(CWD: {workspace_root.resolve()})\n"

    return f"\n❓ [Tool Preview: {tool_name}] Args: {json.dumps(args)}\n"


def is_valid_tool_call(tool_call) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validates tool name and checks if JSON arguments can be parsed cleanly.
    Returns (is_valid, tool_name, parsed_args).
    """
    try:
        tool_name = tool_call.function.name
        valid_names = {spec["function"]["name"] for spec in AVAILABLE_TOOLS_SPEC}
        if tool_name not in valid_names:
            return False, tool_name or "unknown", {}

        args = json.loads(tool_call.function.arguments)
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


def execute_local_tool(
    tool_name: str, args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    """
    Executes the validated local tool inside the sandboxed environment.
    Returns string output or error message to feed back to the model.
    """
    try:
        if tool_name == "read_file":
            path_str = args.get("path")
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

        elif tool_name == "write_file":
            path_str = args.get("path")
            content = args.get("content", "")
            target_path = resolve_and_validate_path(path_str, workspace_root)

            # Auto-backup before overwrite for easy one-command revert
            if (
                getattr(config, "auto_backup", True)
                and target_path.exists()
                and target_path.is_file()
            ):
                backup_dir = workspace_root / ".nimcode" / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                timestamp = int(time.time())
                backup_filename = f"{timestamp}_{target_path.name}"
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

        elif tool_name == "execute_command":
            cmd = args.get("command", "")
            if not cmd.strip():
                return "Error: Empty command provided."

            # Check secondary denylist backstop
            for pattern in config.denylist:
                if pattern and pattern in cmd:
                    return f"Security Error: Command contains denylisted pattern '{pattern}'."

            # Scrub environment variables (remove API key or sensitive keys)
            scrubbed_env = os.environ.copy()
            sensitive_keywords = [
                "API_KEY",
                "SECRET",
                "TOKEN",
                "PASSWORD",
                "AUTH",
                "CREDENTIAL",
                "NVIDIA",
                "OPENAI",
                "DEEPSEEK",
            ]
            for key in list(scrubbed_env.keys()):
                if any(kw in key.upper() for kw in sensitive_keywords):
                    scrubbed_env.pop(key, None)

            try:
                import sys
                import threading

                creation_flags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                )
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(workspace_root.resolve()),
                    env=scrubbed_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=creation_flags,
                )
                output_lines = []

                def read_stream(stream, prefix):
                    try:
                        for line in iter(stream.readline, ""):
                            if line:
                                output_lines.append(f"[{prefix}] {line.rstrip()}")
                    except Exception:
                        pass

                t_out = threading.Thread(
                    target=read_stream, args=(process.stdout, "stdout"), daemon=True
                )
                t_err = threading.Thread(
                    target=read_stream, args=(process.stderr, "stderr"), daemon=True
                )
                t_out.start()
                t_err.start()

                start_time = time.time()
                while process.poll() is None:
                    if sys.platform == "win32":
                        import msvcrt

                        if msvcrt.kbhit():
                            key = msvcrt.getch()
                            if key in [b"\x1b", b"\x03"]:  # ESC or Ctrl+C
                                subprocess.run(
                                    f"taskkill /F /T /PID {process.pid}",
                                    shell=True,
                                    capture_output=True,
                                )
                                raise KeyboardInterrupt("ESC_CANCELLED")

                    if time.time() - start_time > config.timeout:
                        if sys.platform == "win32":
                            subprocess.run(
                                f"taskkill /F /T /PID {process.pid}",
                                shell=True,
                                capture_output=True,
                            )
                        else:
                            try:
                                process.kill()
                            except Exception:
                                pass
                        t_out.join(timeout=0.5)
                        t_err.join(timeout=0.5)
                        full_output = (
                            "Exit code: TIMEOUT\n"
                            + "\n".join(output_lines)
                            + f"\n(Command execution timed out after {config.timeout}s and was terminated)"
                        )
                        if len(full_output) > config.output_cap:
                            return (
                                full_output[: config.output_cap]
                                + f"\n... [Output truncated at {config.output_cap} characters]\n(Command execution timed out after {config.timeout}s and was terminated)"
                            )
                        return full_output

                    time.sleep(0.05)

                t_out.join(timeout=0.5)
                t_err.join(timeout=0.5)

                if not output_lines:
                    output_lines.append("[Command completed with no output]")

                full_output = f"Exit code: {process.returncode}\n" + "\n".join(
                    output_lines
                )
                if len(full_output) > config.output_cap:
                    return (
                        full_output[: config.output_cap]
                        + f"\n... [Output truncated at {config.output_cap} characters]"
                    )
                return full_output

            except KeyboardInterrupt:
                raise

        else:
            return f"Error: Unknown tool '{tool_name}'."

    except ToolSandboxError as se:
        return str(se)
    except Exception as e:
        return f"Error executing '{tool_name}': {str(e)}"
