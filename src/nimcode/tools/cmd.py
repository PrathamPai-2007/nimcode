import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool

EXECUTE_COMMAND_SCHEMA = {
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
}

RUN_TESTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Execute the project's test suite or a specific test command inside the sandboxed workspace to verify code correctness.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_command": {
                    "type": "string",
                    "description": "Optional specific test command (e.g. 'pytest tests/test_agent.py'). If omitted or empty, auto-detects and runs the full test suite.",
                }
            },
            "required": [],
        },
    },
}


@register_tool("execute_command", EXECUTE_COMMAND_SCHEMA)
def execute_command_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
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
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
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
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
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

        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        if not output_lines:
            output_lines.append("[Command completed with no output]")

        full_output = f"Exit code: {process.returncode}\n" + "\n".join(output_lines)
        if len(full_output) > config.output_cap:
            return (
                full_output[: config.output_cap]
                + f"\n... [Output truncated at {config.output_cap} characters]"
            )
        return full_output

    except KeyboardInterrupt:
        raise


@register_tool("run_tests", RUN_TESTS_SCHEMA)
def run_tests_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    test_cmd = args.get("test_command")
    return execute_test_runner(workspace_root, config, test_cmd)


def execute_test_runner(workspace_root: Path, config: Any, test_cmd: str = None) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)

    if not test_cmd or not test_cmd.strip():
        root = workspace_root.resolve()
        if (
            (root / "pytest.ini").exists()
            or (root / "pyproject.toml").exists()
            or (root / "tests").exists()
            or any(root.glob("test_*.py"))
        ):
            test_cmd = "pytest -v"
        elif (root / "package.json").exists():
            test_cmd = "npm test"
        elif (root / "Cargo.toml").exists():
            test_cmd = "cargo test"
        elif (root / "go.mod").exists():
            test_cmd = "go test ./..."
        else:
            test_cmd = (
                "pytest -v"
                if sys.platform != "win32"
                else "python -m unittest discover -v"
            )

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
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        process = subprocess.Popen(
            test_cmd,
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
        timeout_val = getattr(config, "timeout", 60)
        while process.poll() is None:
            if sys.platform == "win32":
                import msvcrt

                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key in [b"\x1b", b"\x03"]:
                        subprocess.run(
                            f"taskkill /F /T /PID {process.pid}",
                            shell=True,
                            capture_output=True,
                        )
                        raise KeyboardInterrupt("ESC_CANCELLED")
            if time.time() - start_time > timeout_val:
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
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                full_output = (
                    "Exit code: TIMEOUT\n"
                    + "\n".join(output_lines)
                    + f"\n(Test execution timed out after {timeout_val}s)"
                )
                if len(full_output) > getattr(config, "output_cap", 2000):
                    return (
                        full_output[: getattr(config, "output_cap", 2000)]
                        + f"\n... [Output truncated at {getattr(config, 'output_cap', 2000)} characters]"
                    )
                return full_output
            time.sleep(0.05)

        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        if not output_lines:
            output_lines.append("[Test command completed with no output]")

        full_output = (
            f"Test Command: $ {test_cmd}\nExit code: {process.returncode}\n"
            + "\n".join(output_lines)
        )
        if len(full_output) > getattr(config, "output_cap", 2000):
            return (
                full_output[: getattr(config, "output_cap", 2000)]
                + f"\n... [Output truncated at {getattr(config, 'output_cap', 2000)} characters]"
            )
        return full_output
    except KeyboardInterrupt:
        raise
