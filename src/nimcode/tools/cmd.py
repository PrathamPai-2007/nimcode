import os
import sys
import subprocess
import shlex
from pathlib import Path
from typing import Dict, Any, List, Optional
from nimcode.tools.registry import register_tool, ToolSandboxError
from nimcode.tools.base import resolve_and_validate_path, scrub_env, is_docker_available


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
        f"{shlex.quote(str(workspace))}:/workspace",
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
        subprocess.run(
            ["docker", "stop", "-t", "1", container_name], capture_output=True
        )
        return {
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "returncode": -1,
            "timed_out": True,
        }
    except Exception as e:
        subprocess.run(
            ["docker", "stop", "-t", "1", container_name], capture_output=True
        )
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
        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
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

    timeout = args.get("timeout", getattr(config, "timeout", 60))
    use_docker = args.get("use_docker", None)

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    result = _execute_command(command, workspace_root, config, timeout, use_docker)

    output_parts = []
    if result.get("fallback"):
        output_parts.append("[Note: Docker unavailable, fell back to host execution]")

    if result["stdout"]:
        output_parts.append(f"[stdout]\n{result['stdout']}")
    if result["stderr"]:
        output_parts.append(f"[stderr]\n{result['stderr']}")

    output_parts.append(f"[exit code: {result['returncode']}]")
    if result["timed_out"]:
        output_parts.append(f"[timed out after {timeout}s]")

    output = "\n".join(output_parts)

    # Apply output cap
    output_cap = getattr(config, "output_cap", 100000)
    if len(output) > output_cap:
        output = output[:output_cap] + f"\n... [output truncated at {output_cap} chars]"

    return output


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

    result = _execute_command(command, workspace_root, config, timeout, use_docker)

    output_parts = []
    if result.get("fallback"):
        output_parts.append("[Note: Docker unavailable, fell back to host execution]")

    if result["stdout"]:
        output_parts.append(f"[stdout]\n{result['stdout']}")
    if result["stderr"]:
        output_parts.append(f"[stderr]\n{result['stderr']}")

    output_parts.append(f"[exit code: {result['returncode']}]")
    if result["timed_out"]:
        output_parts.append(f"[timed out after {timeout}s]")

    output = "\n".join(output_parts)

    # Apply output cap
    output_cap = getattr(config, "output_cap", 100000)
    if len(output) > output_cap:
        output = output[:output_cap] + f"\n... [output truncated at {output_cap} chars]"

    return output


def execute_test_runner(workspace_root: Path, config: Any, test_cmd: Optional[str] = None) -> str:
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

    result = _execute_command(test_cmd, workspace_root, config, timeout, None)

    output_parts = []
    if result.get("fallback"):
        output_parts.append("[Note: Docker unavailable, fell back to host execution]")

    if result["stdout"]:
        output_parts.append(f"[stdout]\n{result['stdout']}")
    if result["stderr"]:
        output_parts.append(f"[stderr]\n{result['stderr']}")

    output_parts.append(f"[exit code: {result['returncode']}]")
    if result["timed_out"]:
        output_parts.append(f"[timed out after {timeout}s]")

    output = "\n".join(output_parts)

    output_cap = getattr(config, "output_cap", 100000)
    if len(output) > output_cap:
        output = output[:output_cap] + f"\n... [output truncated at {output_cap} chars]"

    return output
