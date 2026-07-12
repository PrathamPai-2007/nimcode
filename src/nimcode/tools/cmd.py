import os
import sys
import subprocess
import time
import uuid
import atexit
from pathlib import Path
from typing import Dict, Any, List, Optional
from nimcode.tools.registry import register_tool, ToolSandboxError
from nimcode.tools.base import (
    resolve_and_validate_path,
    scrub_env,
    is_docker_available,
    scrub_text,
    truncate_output,
)
from nimcode.config import get_project_nimcode_dir


# In-memory registry for background processes
# Format: process_id -> { "popen_or_container_id": proc_obj_or_container_id, "logfile_path": path_or_None, "start_time": ts, "command": cmd, "is_docker": bool, "container_name": str_or_None }
BACKGROUND_PROCESSES: Dict[str, Dict[str, Any]] = {}
_process_counter = 0


def _kill_host_process(proc: subprocess.Popen):
    """
    Cleanly or forcefully terminate a host process.
    On Windows, uses taskkill to terminate the process tree immediately and prevent orphan processes.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


def cleanup_background_processes():
    """
    Clean up all running background processes in the registry.
    """
    pids = list(BACKGROUND_PROCESSES.keys())
    if not pids:
        return

    for pid in pids:
        proc_info = BACKGROUND_PROCESSES.get(pid)
        if not proc_info:
            continue

        if proc_info.get("is_docker"):
            container_id = proc_info["popen_or_container_id"]
            try:
                subprocess.run(
                    ["docker", "kill", container_id], capture_output=True, timeout=5
                )
                subprocess.run(
                    ["docker", "rm", "-f", container_id], capture_output=True, timeout=5
                )
            except Exception:
                pass
        else:
            proc = proc_info["popen_or_container_id"]
            _kill_host_process(proc)

    BACKGROUND_PROCESSES.clear()


# Register the cleanup handler to execute on interpreter shutdown
atexit.register(cleanup_background_processes)


def _build_docker_command(
    command: str,
    workspace_root: Path,
    config: Any,
    extra_env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Build a docker run command list for executing the given command in a sandboxed container.
    """
    workspace = workspace_root.resolve()
    docker_image = getattr(config, "docker_image", "python:3.11-slim")
    docker_extra_args = getattr(config, "docker_extra_args", [])
    docker_expose_ports = getattr(config, "docker_expose_ports", [])

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        f"{str(workspace)}:/workspace",
        "-w",
        "/workspace",
    ]

    # Add port exposures if configured
    for port in docker_expose_ports:
        docker_cmd.extend(["-p", f"{port}:{port}"])

    # Add extra docker args from config
    docker_cmd.extend(docker_extra_args)

    # Add the image
    docker_cmd.append(docker_image)

    # Add the command to run
    docker_cmd.extend(["sh", "-c", command])

    return docker_cmd


def _execute_in_docker(
    command: str,
    workspace_root: Path,
    config: Any,
    timeout: int,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Execute a command inside a Docker container.
    Returns dict with stdout, stderr, returncode, and timed_out.
    """
    import uuid

    container_name = f"nimcode_{uuid.uuid4().hex[:12]}"
    docker_cmd = _build_docker_command(command, workspace_root, config)
    if len(docker_cmd) > 2 and docker_cmd[1] == "run":
        docker_cmd.insert(2, "--name")
        docker_cmd.insert(3, container_name)

    # Prepare environment
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env = scrub_env(env)

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=str(workspace_root),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        stdout_str = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr_str = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        return {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "returncode": -1,
            "timed_out": True,
        }
    except Exception as e:
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        return {
            "stdout": "",
            "stderr": f"Docker execution error: {e}",
            "returncode": -1,
            "timed_out": False,
        }


def _execute_on_host(
    command: str,
    workspace_root: Path,
    config: Any,
    timeout: int,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Execute a command directly on the host (fallback path).
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env = scrub_env(env)

    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(workspace_root),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            proc.kill()
        stdout, stderr = proc.communicate()
        stdout_str = (
            stdout.decode("utf-8", errors="replace")
            if isinstance(stdout, bytes)
            else (stdout or "")
        )
        stderr_str = (
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes)
            else (stderr or "")
        )
        return {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "returncode": -1,
            "timed_out": True,
        }
    except Exception as e:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            proc.kill()
        return {
            "stdout": "",
            "stderr": f"Host execution error: {e}",
            "returncode": -1,
            "timed_out": False,
        }


def _execute_command(
    command: str,
    workspace_root: Path,
    config: Any,
    timeout: int,
    use_docker: Optional[bool] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Execute a command, using Docker if available and not disabled, otherwise falling back to host.
    """
    # Determine whether to use Docker
    if use_docker is None:
        use_docker = (
            not getattr(config, "disable_docker", False) and is_docker_available()
        )

    if use_docker:
        result = _execute_in_docker(command, workspace_root, config, timeout, extra_env)
        # If Docker fails (e.g., image not found, permission denied), fall back to host
        if result["returncode"] == -1 and "Docker execution error" in result["stderr"]:
            result = _execute_on_host(
                command, workspace_root, config, timeout, extra_env
            )
            result["fallback"] = True
        return result
    else:
        result = _execute_on_host(command, workspace_root, config, timeout, extra_env)
        return result


@register_tool(
    "execute_command",
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute a shell command in the workspace. Runs in a Docker sandbox if available, otherwise on host.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: config.timeout).",
                    },
                    "use_docker": {
                        "type": "boolean",
                        "description": "Force Docker usage (true) or host execution (false). Default: auto-detect.",
                    },
                },
                "required": ["command"],
            },
        },
    },
)
def execute_command_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    """
    Execute a shell command with Docker sandbox fallback to host execution.
    """
    command = args.get("command", "")
    if not command:
        return "Error: No command provided."

    # Check for blocking patterns
    blocking_patterns = [
        "run dev",
        "http.server",
        "flask run",
        "vite",
        "uvicorn",
        "ng serve",
        "rails server",
        "next dev",
    ]
    if any(pattern in command for pattern in blocking_patterns):
        return f"Error: Command '{command}' matches a known long-running/blocking process pattern. Please use the 'start_background_process' tool instead of 'execute_command' to run this process, so it doesn't freeze the agent."

    timeout = args.get("timeout", getattr(config, "timeout", 60))
    use_docker = args.get("use_docker", None)

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    try:
        result = _execute_command(command, workspace_root, config, timeout, use_docker)
    except subprocess.TimeoutExpired as e:
        stdout_str = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr_str = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        result = {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "returncode": -1,
            "timed_out": True,
        }

    if result["timed_out"]:
        output_parts = [f"Error: Command timed out after {timeout}s and was killed"]
        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")
        output = "\n".join(output_parts)
    else:
        output_parts = []
        if result.get("fallback"):
            output_parts.append(
                "[Note: Docker unavailable, fell back to host execution]"
            )

        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")

        output_parts.append(f"[exit code: {result['returncode']}]")
        output = "\n".join(output_parts)

    # Apply output cap
    output_cap = getattr(config, "output_cap", 100000)
    output = truncate_output(output, output_cap)

    return scrub_text(output)


@register_tool(
    "run_tests",
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the project's test suite (pytest by default) in a Docker sandbox if available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Test command to run (default: pytest).",
                        "default": "pytest",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: config.timeout * 2).",
                    },
                    "use_docker": {
                        "type": "boolean",
                        "description": "Force Docker usage (true) or host execution (false). Default: auto-detect.",
                    },
                },
                "required": [],
            },
        },
    },
)
def run_tests_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    """
    Run the project's test suite with Docker sandbox fallback to host execution.
    """
    command = args.get("command", "pytest")
    timeout = args.get("timeout", getattr(config, "timeout", 60) * 2)
    use_docker = args.get("use_docker", None)

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    try:
        result = _execute_command(command, workspace_root, config, timeout, use_docker)
    except subprocess.TimeoutExpired as e:
        stdout_str = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr_str = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        result = {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "returncode": -1,
            "timed_out": True,
        }

    if result["timed_out"]:
        output_parts = [
            f"Error: Test command timed out after {timeout}s and was killed"
        ]
        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")
        output = "\n".join(output_parts)
    else:
        output_parts = []
        if result.get("fallback"):
            output_parts.append(
                "[Note: Docker unavailable, fell back to host execution]"
            )

        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")

        output_parts.append(f"[exit code: {result['returncode']}]")
        output = "\n".join(output_parts)

    # Apply output cap
    output_cap = getattr(config, "output_cap", 100000)
    output = truncate_output(output, output_cap)

    return scrub_text(output)


def execute_test_runner(
    workspace_root: Path, config: Any, test_cmd: Optional[str] = None
) -> str:
    """
    Auto-detects and runs the project's test suite inside the Docker sandbox if available, otherwise on host.
    """
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

    timeout = getattr(config, "timeout", 60) * 2
    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    try:
        result = _execute_command(test_cmd, workspace_root, config, timeout, None)
    except subprocess.TimeoutExpired as e:
        stdout_str = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr_str = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        result = {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "returncode": -1,
            "timed_out": True,
        }

    if result["timed_out"]:
        output_parts = [f"Error: Test runner timed out after {timeout}s and was killed"]
        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")
        output = "\n".join(output_parts)
    else:
        output_parts = []
        if result.get("fallback"):
            output_parts.append(
                "[Note: Docker unavailable, fell back to host execution]"
            )

        if result["stdout"]:
            output_parts.append(f"[stdout]\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr']}")

        output_parts.append(f"[exit code: {result['returncode']}]")
        output = "\n".join(output_parts)

    output_cap = getattr(config, "output_cap", 100000)
    output = truncate_output(output, output_cap)

    return scrub_text(output)


def _build_background_docker_command(
    command: str,
    workspace_root: Path,
    config: Any,
    container_name: str,
) -> List[str]:
    """
    Build a docker run -d command list for executing the given command in a sandboxed container.
    """
    workspace = workspace_root.resolve()
    docker_image = getattr(config, "docker_image", "python:3.11-slim")
    docker_extra_args = getattr(config, "docker_extra_args", [])
    docker_expose_ports = getattr(config, "docker_expose_ports", [])

    docker_cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "--init",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        f"{str(workspace)}:/workspace",
        "-w",
        "/workspace",
    ]

    # Add port exposures if configured
    for port in docker_expose_ports:
        docker_cmd.extend(["-p", f"{port}:{port}"])

    # Add extra docker args from config
    docker_cmd.extend(docker_extra_args)

    # Add the image
    docker_cmd.append(docker_image)

    # Add the command to run
    docker_cmd.extend(["sh", "-c", command])

    return docker_cmd


@register_tool(
    "start_background_process",
    {
        "type": "function",
        "function": {
            "name": "start_background_process",
            "description": "Start a long-running command in the background (detached). Use for dev servers, web servers, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute in the background.",
                    }
                },
                "required": ["command"],
            },
        },
    },
)
def start_background_process_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    """
    Launches a command in the background (detached) and returns a process ID.
    """
    command = args.get("command", "")
    if not command:
        return "Error: No command provided."

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    global _process_counter
    _process_counter += 1
    process_id = f"bg_{_process_counter}"

    use_docker = not getattr(config, "disable_docker", False) and is_docker_available()

    if use_docker:
        container_name = f"nimcode_bg_{uuid.uuid4().hex[:12]}"
        docker_cmd = _build_background_docker_command(
            command, workspace_root, config, container_name
        )

        env = os.environ.copy()
        env = scrub_env(env)

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(workspace_root),
                timeout=15,
            )
            if result.returncode != 0:
                return f"Error starting background process in Docker: {result.stderr.strip()}"

            container_id = result.stdout.strip()
            if not container_id:
                container_id = container_name

            BACKGROUND_PROCESSES[process_id] = {
                "popen_or_container_id": container_id,
                "logfile_path": None,
                "start_time": time.time(),
                "command": command,
                "is_docker": True,
                "container_name": container_name,
            }
            return f"Background process started in Docker sandbox. Process ID: {process_id}\nUse get_process_output to view logs."
        except Exception as e:
            return f"Error starting background process in Docker: {e}"
    else:
        # Host execution fallback
        nimcode_dir = get_project_nimcode_dir(workspace_root)
        log_dir = nimcode_dir / "process_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logfile_path = log_dir / f"{process_id}.log"

        env = os.environ.copy()
        env = scrub_env(env)

        try:
            log_file = open(logfile_path, "w", encoding="utf-8")

            popen_kwargs = {
                "shell": True,
                "stdout": log_file,
                "stderr": log_file,
                "text": True,
                "env": env,
                "cwd": str(workspace_root),
            }
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            proc = subprocess.Popen(command, **popen_kwargs)
            log_file.close()

            BACKGROUND_PROCESSES[process_id] = {
                "popen_or_container_id": proc,
                "logfile_path": str(logfile_path),
                "start_time": time.time(),
                "command": command,
                "is_docker": False,
            }
            return f"Background process started on host. Process ID: {process_id}\nLogfile: {logfile_path.name}\nUse get_process_output to view logs."
        except Exception as e:
            return f"Error starting background process on host: {e}"


@register_tool(
    "get_process_output",
    {
        "type": "function",
        "function": {
            "name": "get_process_output",
            "description": "Get stdout/stderr logs of a running background process.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The process handle/id returned by start_background_process.",
                    }
                },
                "required": ["process_id"],
            },
        },
    },
)
def get_process_output_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    """
    Returns stdout/stderr tail of a background process.
    """
    process_id = args.get("process_id", "")
    if not process_id:
        return "Error: No process_id provided."

    proc_info = BACKGROUND_PROCESSES.get(process_id)
    if not proc_info:
        return f"Error: Process '{process_id}' not found in registry."

    is_running = False
    exit_code = None
    status_msg = "Unknown"
    output = ""

    if proc_info["is_docker"]:
        container_id = proc_info["popen_or_container_id"]
        # Check running state
        inspect_res = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}} {{.State.ExitCode}}",
                container_id,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if inspect_res.returncode == 0:
            parts = inspect_res.stdout.strip().split()
            if len(parts) >= 2:
                is_running = parts[0] == "true"
                exit_code = int(parts[1])
                status_msg = (
                    "running" if is_running else f"exited with code {exit_code}"
                )
        else:
            is_running = False
            status_msg = "exited/not found"

        # Get logs
        logs_res = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        output = logs_res.stdout + logs_res.stderr
    else:
        proc = proc_info["popen_or_container_id"]
        poll_val = proc.poll()
        if poll_val is None:
            is_running = True
            status_msg = "running"
        else:
            is_running = False
            exit_code = poll_val
            status_msg = f"exited with code {exit_code}"

        logfile_path = Path(proc_info["logfile_path"])
        if logfile_path.exists():
            try:
                with open(logfile_path, "r", encoding="utf-8", errors="replace") as lf:
                    output = lf.read()
            except Exception as e:
                output = f"Error reading logfile: {e}"
        else:
            output = f"Logfile {logfile_path} not found."

    res = f"Process {process_id} Status: {status_msg}\nCommand: {proc_info['command']}\n\n[Output]\n{output}"

    output_cap = getattr(config, "output_cap", 100000)
    res = truncate_output(res, output_cap)
    return scrub_text(res)


@register_tool(
    "stop_process",
    {
        "type": "function",
        "function": {
            "name": "stop_process",
            "description": "Stop a running background process cleanly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The process handle/id returned by start_background_process.",
                    }
                },
                "required": ["process_id"],
            },
        },
    },
)
def stop_process_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    """
    Terminates the specified background process.
    """
    process_id = args.get("process_id", "")
    if not process_id:
        return "Error: No process_id provided."

    proc_info = BACKGROUND_PROCESSES.get(process_id)
    if not proc_info:
        return f"Error: Process '{process_id}' not found in registry."

    if proc_info["is_docker"]:
        container_id = proc_info["popen_or_container_id"]
        subprocess.run(
            ["docker", "kill", container_id], capture_output=True, timeout=10
        )
        subprocess.run(
            ["docker", "rm", "-f", container_id], capture_output=True, timeout=10
        )
    else:
        proc = proc_info["popen_or_container_id"]
        _kill_host_process(proc)

    BACKGROUND_PROCESSES.pop(process_id, None)
    return f"Process {process_id} has been stopped and removed from the registry."
