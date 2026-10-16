import os
import sys
import subprocess
import time
import uuid
import atexit
import re
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from nimcode.tools.registry import register_tool, ToolSandboxError
from nimcode.tools.base import (
    resolve_and_validate_path,
    scrub_env,
    is_docker_available,
    scrub_text,
    truncate_output,
    coerce_output_cap,
)
from nimcode.config import get_project_nimcode_dir


# In-memory registry for background processes
# Format: process_id -> { "popen_or_container_id": proc_obj_or_container_id, "logfile_path": path_or_None, "start_time": ts, "command": cmd, "is_docker": bool, "container_name": str_or_None }
BACKGROUND_PROCESSES: Dict[str, Dict[str, Any]] = {}
_process_counter = 0
_BG_LOCK = threading.RLock()
MAX_BG_PROCESSES = 10


def _coerce_timeout(raw: Any, default: int = 60) -> tuple[int, str]:
    """Validate timeout args; returns (seconds, error)."""
    if raw is None:
        return default, ""
    try:
        t = int(raw)
    except (TypeError, ValueError):
        return 0, "Error: 'timeout' must be an integer number of seconds."
    if t <= 0:
        return 0, "Error: 'timeout' must be a positive integer."
    return min(t, 600), ""


def _kill_host_process(proc: subprocess.Popen):
    """
    Cleanly or forcefully terminate a host process.
    On Windows, uses taskkill to terminate the process tree immediately and prevent orphan processes.
    On POSIX, signals the whole process group (host procs spawn detached via
    start_new_session, so bare terminate() would orphan grandchildren).
    """
    import signal

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
            try:
                proc.wait(timeout=2)  # reap the handle / exit status
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


def cleanup_background_processes():
    """
    Clean up all running background processes in the registry.
    """
    with _BG_LOCK:
        entries = list(BACKGROUND_PROCESSES.values())
        BACKGROUND_PROCESSES.clear()
    if not entries:
        return

    log_dirs = set()
    for proc_info in entries:
        if proc_info.get("is_docker"):
            _docker_rm(proc_info["popen_or_container_id"])
        else:
            _kill_host_process(proc_info["popen_or_container_id"])
            if proc_info.get("logfile_path"):
                log_dirs.add(Path(proc_info["logfile_path"]).parent)

    # Clean up old log files (keep last 50). Use the dirs the logs were
    # actually written to: Path.cwd() at exit may not be the workspace, and
    # get_project_nimcode_dir() would create/migrate a dir for it.
    for log_dir in log_dirs:
        try:
            log_files = sorted(
                log_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True
            )
            for old_log in log_files[50:]:
                try:
                    old_log.unlink()
                except Exception:
                    pass
        except Exception:
            pass


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
    return _docker_run_argv(["--rm"], command, workspace_root, config)


DEFAULT_DOCKER_IMAGE = "python:3.11-slim"
# Flags that can break out of the sandbox regardless of value.
_DOCKER_DENY_FLAGS = {
    "--privileged",
    "--cap-add",
    "--device",
    "--volume",
    "--volumes-from",
    "--mount",
    "--security-opt",
    "--userns",
    "--cgroup-parent",
    "--runtime",
}
# Namespace flags that are only dangerous with a host/container: value.
_DOCKER_NS_FLAGS = {"--network", "--net", "--pid", "--ipc", "--uts", "--cgroupns"}


def _is_host_ns(value: str) -> bool:
    v = value.strip().strip("'\"").lower()
    return v == "host" or v.startswith("container:")


def _safe_docker_extra_args(config: Any) -> List[str]:
    """Filter config.docker_extra_args, dropping host-escape flags.

    The old substring deny-list missed split forms (["--network", "host"]),
    attached short mounts ("-v/:/host", "-v=/:/h", "-itv/:/h"), --device,
    --security-opt seccomp=unconfined, --userns=host, etc.
    """
    out: List[str] = []
    pending_ns = False
    skip_value = False
    for extra in getattr(config, "docker_extra_args", None) or []:
        if not isinstance(extra, str) or not extra.strip():
            continue
        arg = extra.strip()
        if skip_value:
            # Value of a dropped split-form flag (["-v", "/:/host"]): drop it
            # too, or docker would parse it as the image name.
            skip_value = False
            continue
        if pending_ns:
            pending_ns = False
            if _is_host_ns(arg):
                out.pop()  # drop the namespace flag we already appended
                continue
        flag, sep, value = arg.lower().partition("=")
        flag = flag.split()[0]
        bare = not sep and " " not in arg
        if flag in _DOCKER_DENY_FLAGS:
            skip_value = bare and flag != "--privileged"
            continue
        # Any short-flag cluster containing 'v' may carry a bind mount.
        if flag.startswith("-") and not flag.startswith("--") and "v" in flag:
            skip_value = bare and flag.endswith("v")
            continue
        if flag in _DOCKER_NS_FLAGS:
            rest = value if sep else arg[len(flag) :]
            if rest.strip():
                if _is_host_ns(rest):
                    continue
            else:
                pending_ns = True
        out.append(arg)
    if pending_ns and out:
        out.pop()  # dangling namespace flag would swallow the image name
    return out


def _docker_run_argv(
    run_flags: List[str], command: str, workspace_root: Path, config: Any
) -> List[str]:
    """Shared `docker run` argv for foreground and background execution."""
    workspace = Path(workspace_root).resolve()
    docker_image = getattr(config, "docker_image", None) or DEFAULT_DOCKER_IMAGE
    # An "image" starting with '-' would be parsed by docker as a flag.
    if not isinstance(docker_image, str) or docker_image.strip().startswith("-"):
        docker_image = DEFAULT_DOCKER_IMAGE

    docker_cmd = [
        "docker",
        "run",
        *run_flags,
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

    # Resource limits (Config defaults when unset; "" / 0 disables one).
    for flag, key, default in (
        ("--memory", "docker_memory", "2g"),
        ("--pids-limit", "docker_pids_limit", 512),
        ("--cpus", "docker_cpus", "2"),
        ("--network", "docker_network", "bridge"),
    ):
        value = str(getattr(config, key, default) or "").strip()
        if not value or value == "0" or value.startswith("-"):
            continue
        if flag == "--network" and _is_host_ns(value):
            value = "bridge"  # never share the host's network namespace
        docker_cmd.extend([flag, value])

    # Add port exposures if configured (validated ints only)
    for port in getattr(config, "docker_expose_ports", None) or []:
        try:
            _port = int(port)
        except (TypeError, ValueError):
            continue
        if 1 <= _port <= 65535:
            docker_cmd.extend(["-p", f"{_port}:{_port}"])

    docker_cmd.extend(_safe_docker_extra_args(config))
    docker_cmd.append(docker_image.strip())
    docker_cmd.extend(["sh", "-c", command])
    return docker_cmd


def _docker_rm(container: str) -> None:
    """Force-remove (and thereby kill) a container. Never raises or hangs."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=15
        )
    except Exception:
        pass


def _subagent_depth(config: Any) -> int:
    try:
        return int(getattr(config, "_subagent_depth", 0) or 0)
    except Exception:
        return 0


SUBAGENT_HOST_REFUSAL = (
    "Error: subagents require Docker; host execution is disabled for subagents."
)


def _resolve_use_docker(raw: Any, config: Any) -> bool:
    """Decide Docker vs host. Shared by the executor and render_preview."""
    docker_ok = not getattr(config, "disable_docker", False) and is_docker_available()
    if not isinstance(raw, bool):
        # None / garbage like "false" (truthy string) -> auto-detect.
        return docker_ok
    if raw is False and docker_ok and _subagent_depth(config) > 0:
        # Swarm threads run without the human gate; they may not opt out
        # of an available sandbox.
        return True
    return raw


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
    container_name = f"nimcode_{uuid.uuid4().hex[:12]}"
    docker_cmd = _docker_run_argv(
        ["--rm", "--name", container_name], command, workspace_root, config
    )

    # Prepare environment
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env = scrub_env(env)

    try:
        result = subprocess.run(
            docker_cmd,
            stdin=subprocess.DEVNULL,
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
        _docker_rm(container_name)
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
        # e.g. FileNotFoundError when docker is missing; _docker_rm never raises.
        _docker_rm(container_name)
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

    popen_kwargs: Dict[str, Any] = {}
    if sys.platform != "win32":
        # Own process group so a timeout kills the whole tree; a bare
        # proc.kill() only killed `sh`, and grandchildren holding the pipes
        # made the follow-up communicate() hang forever.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        shell=True,
        # Never inherit the TUI's stdin: prompting commands (git commit w/o -m,
        # npx create-*, a bare `python`) would steal keystrokes or block.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(workspace_root),
        **popen_kwargs,
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
        _kill_host_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            # A detached grandchild still holds the pipes; give up on output.
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
            stdout, stderr = "", ""
        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "returncode": -1,
            "timed_out": True,
        }
    except BaseException as e:
        # Includes KeyboardInterrupt (ESC cancel): don't leave the tree running.
        _kill_host_process(proc)
        if not isinstance(e, Exception):
            raise
        return {
            "stdout": "",
            "stderr": f"Host execution error: {e}",
            "returncode": -1,
            "timed_out": False,
        }


def _validate_command_safety(command: str) -> Tuple[bool, str]:
    """
    Validate command for potential security issues when running in Docker.
    Returns (is_safe, warning_message).
    """
    # Patterns that could be dangerous in a Docker context
    dangerous_patterns = [
        # Docker socket mounting
        (r"-v\s+/var/run/docker\.sock", "Docker socket mount detected"),
        (r"--mount.*docker\.sock", "Docker socket mount detected"),
        # Privileged mode
        (r"--privileged", "Privileged mode requested"),
        # Host network/namespace
        (r"--network\s+host", "Host network mode requested"),
        (r"--pid\s+host", "Host PID namespace requested"),
        (r"--ipc\s+host", "Host IPC namespace requested"),
        # Capabilities
        (r"--cap-add", "Capability addition requested"),
        # Volume mounts that could escape
        (r"-v\s+/[^:]+:/", "Absolute host path mount detected"),
        # Shell injection patterns
        (r"\$\(", "Command substitution detected"),
        (r"`.*`", "Backtick command substitution detected"),
        (r"&&\s*rm\s+-rf", "Destructive command chain detected"),
        (r";\s*rm\s+-rf", "Destructive command chain detected"),
        (r"\|\s*sh", "Piped to shell detected"),
        (r"\|\s*bash", "Piped to bash detected"),
    ]

    for pattern, warning in dangerous_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return False, warning

    return True, ""


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
    # Only a real fallback gets the note: not when the user chose the host.
    host_requested = use_docker is False or getattr(config, "disable_docker", False)
    use_docker = _resolve_use_docker(use_docker, config)
    if not use_docker and _subagent_depth(config) > 0:
        return {"refused": SUBAGENT_HOST_REFUSAL}

    # Validate command safety for Docker execution (advisory only).
    is_safe, warning = _validate_command_safety(command)
    if not is_safe and use_docker:
        try:
            from nimcode.ui.output import secho

            # Scrubbed (never log raw commands) and routed through secho so
            # it reaches the TUI and isn't parsed as rich markup.
            secho(f"Warning: {warning} in command '{scrub_text(command)}'", fg="yellow")
        except Exception:
            pass

    if use_docker:
        result = _execute_in_docker(command, workspace_root, config, timeout, extra_env)
        return result
    else:
        result = _execute_on_host(command, workspace_root, config, timeout, extra_env)
        result["fallback"] = not host_requested
        return result


def _run_and_format(
    command: str,
    workspace_root: Path,
    config: Any,
    timeout: int,
    use_docker: Any,
    label: str,
) -> str:
    """Run via _execute_command and render the tool result string."""
    result = _execute_command(command, workspace_root, config, timeout, use_docker)
    if result.get("refused"):
        return result["refused"]
    if result["timed_out"]:
        parts = [f"Error: {label} timed out after {timeout}s and was killed"]
    elif result.get("fallback"):
        parts = ["[Note: Docker unavailable, fell back to host execution]"]
    else:
        parts = []
    if result["stdout"]:
        parts.append(f"[stdout]\n{result['stdout']}")
    if result["stderr"]:
        parts.append(f"[stderr]\n{result['stderr']}")
    parts.append(f"[exit code: {result['returncode']}]")
    # Scrub BEFORE truncating: a cut through a secret defeats the patterns.
    output = scrub_text("\n".join(parts))
    return truncate_output(output, coerce_output_cap(config))


@register_tool(
    "execute_command",
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Run a short-lived shell command that exits (build, lint, script, pytest -k). NEVER for dev servers — use start_background_process for those. Verify edits with this or run_tests.",
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
    if not command or not isinstance(command, str):
        return "Error: No command provided."

    # Redirect dev servers to start_background_process. Multi-word phrases are
    # matched as substrings; bare binary names match the invoked program only,
    # so `grep -r vite logs/` or `pytest -k test_vite` are not misclassified.
    blocking_phrases = [
        "run dev",
        "http.server",
        "flask run",
        "ng serve",
        "rails server",
        "next dev",
    ]
    blocking_bins = {
        "vite",
        "uvicorn",
        "gunicorn",
        "flask",
        "ng",
        "rails",
        "next",
        "nodemon",
    }
    command_lower = command.lower()
    try:
        import shlex

        argv = shlex.split(command, posix=(sys.platform != "win32"))
        invoked = argv[0].lower() if argv else ""
    except (ValueError, IndexError):
        invoked = command.strip().split(" ")[0].lower() if command.strip() else ""
    invoked = invoked.replace("\\", "/").split("/")[-1]
    for ext in (".exe", ".cmd", ".bat"):
        if invoked.endswith(ext):
            invoked = invoked[: -len(ext)]
            break
    if invoked in blocking_bins or any(
        phrase in command_lower for phrase in blocking_phrases
    ):
        return f"Error: Command '{command}' matches a known long-running/blocking process pattern. Please use the 'start_background_process' tool instead of 'execute_command' to run this process, so it doesn't freeze the agent."

    timeout_raw = args.get("timeout", getattr(config, "timeout", 60))
    timeout, timeout_err = _coerce_timeout(timeout_raw)
    if timeout_err:
        return timeout_err
    use_docker = args.get("use_docker", None)

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    return _run_and_format(
        command, workspace_root, config, timeout, use_docker, "Command"
    )


@register_tool(
    "run_tests",
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "PREFERRED verification after any code change: run the test suite (pytest by default). Run this before claiming work is done; fix failures and re-run.",
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
    # "" / None would run an empty shell command and report success.
    command = args.get("command") or "pytest"
    if not isinstance(command, str):
        return "Error: 'command' must be a string."
    default_timeout = getattr(config, "timeout", 60)
    try:
        default_timeout = int(default_timeout) * 2
    except (TypeError, ValueError):
        default_timeout = 120
    timeout, timeout_err = _coerce_timeout(args.get("timeout", default_timeout))
    if timeout_err:
        return timeout_err
    use_docker = args.get("use_docker", None)

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    return _run_and_format(
        command, workspace_root, config, timeout, use_docker, "Test command"
    )


def tests_passed(output: str) -> bool:
    """True iff the final ``[exit code: N]`` marker appended by the runner is 0."""
    codes = re.findall(r"\[exit code: (-?\d+)\]", output or "")
    return bool(codes) and codes[-1] == "0"


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

    try:
        default_timeout = int(getattr(config, "timeout", 60)) * 2
    except (TypeError, ValueError):
        default_timeout = 120
    timeout, timeout_err = _coerce_timeout(default_timeout)
    if timeout_err:
        return timeout_err
    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    return _run_and_format(
        test_cmd, workspace_root, config, timeout, None, "Test runner"
    )


def _build_background_docker_command(
    command: str,
    workspace_root: Path,
    config: Any,
    container_name: str,
) -> List[str]:
    """
    Build a docker run -d command list for executing the given command in a sandboxed container.
    No --rm: a dev server that crashes on startup would otherwise take its
    logs with it; stop_process / cleanup remove the container with rm -f.
    """
    return _docker_run_argv(
        ["-d", "--name", container_name], command, workspace_root, config
    )


@register_tool(
    "start_background_process",
    {
        "type": "function",
        "function": {
            "name": "start_background_process",
            "description": "Run dev servers / watch processes that never exit (npm run dev, uvicorn, flask run, etc). REQUIRED instead of execute_command for these. Follow with get_process_output, then stop_process.",
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
    if not command or not isinstance(command, str):
        return "Error: No command provided."

    # Validate workspace path
    try:
        workspace_root = resolve_and_validate_path(".", workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    use_docker = _resolve_use_docker(None, config)
    if not use_docker and _subagent_depth(config) > 0:
        return SUBAGENT_HOST_REFUSAL

    global _process_counter
    with _BG_LOCK:
        if len(BACKGROUND_PROCESSES) >= MAX_BG_PROCESSES:
            return (
                f"Error: too many background processes (max {MAX_BG_PROCESSES}). "
                "Stop one with stop_process first."
            )
        _process_counter += 1
        process_id = f"bg_{_process_counter}"

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
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(workspace_root),
                timeout=15,
            )
            if result.returncode != 0:
                _docker_rm(container_name)  # created-but-failed containers linger
                return f"Error starting background process in Docker: {scrub_text(result.stderr.strip())}"

            container_id = result.stdout.strip()
            if not container_id:
                container_id = container_name

            with _BG_LOCK:
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
            # e.g. TimeoutExpired: the container may still have started.
            _docker_rm(container_name)
            return f"Error starting background process in Docker: {e}"
    else:
        # Host execution fallback
        env = scrub_env(os.environ.copy())

        try:
            log_dir = get_project_nimcode_dir(workspace_root) / "process_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            logfile_path = log_dir / f"{process_id}.log"
            popen_kwargs = {
                "shell": True,
                # Detached servers must not read the TUI's stdin.
                "stdin": subprocess.DEVNULL,
                "stdout": None,  # set below after opening log file
                "stderr": None,
                "text": True,
                "env": env,
                "cwd": str(workspace_root),
            }
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore
            log_file = open(logfile_path, "w", encoding="utf-8")
            popen_kwargs["stdout"] = log_file
            popen_kwargs["stderr"] = log_file
            try:
                proc = subprocess.Popen(command, **popen_kwargs)  # type: ignore
            except Exception:
                try:
                    log_file.close()
                except Exception:
                    pass
                raise
            log_file.close()

            with _BG_LOCK:
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
        try:
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
        except Exception as e:
            return f"Process {process_id} Status: unknown (inspect failed: {e})"
        if inspect_res.returncode == 0:
            parts = inspect_res.stdout.strip().split()
            if len(parts) >= 2:
                is_running = parts[0] == "true"
                try:
                    exit_code = int(parts[1])
                except ValueError:
                    exit_code = -1
                status_msg = (
                    "running" if is_running else f"exited with code {exit_code}"
                )
            else:
                is_running = False
                status_msg = f"unknown (inspect: {inspect_res.stdout.strip()[:100]})"
        else:
            is_running = False
            status_msg = "exited/not found"

        # Get logs
        try:
            logs_res = subprocess.run(
                ["docker", "logs", "--tail", "500", container_id],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except Exception as e:
            return f"Process {process_id} Status: {status_msg}\nCommand: {proc_info['command']}\n\n[Output]\nError reading logs: {e}"
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
                # Tail last 200KB instead of reading GB logs into memory.
                with open(logfile_path, "rb") as lf:
                    lf.seek(0, 2)
                    size = lf.tell()
                    lf.seek(max(0, size - 200_000))
                    output = lf.read().decode("utf-8", errors="replace")
                    if size > 200_000:
                        output = (
                            f"[... truncated {size - 200_000} earlier bytes ...]\n"
                            + output
                        )
            except Exception as e:
                output = f"Error reading logfile: {e}"
        else:
            output = f"Logfile {logfile_path} not found."

    res = f"Process {process_id} Status: {status_msg}\nCommand: {proc_info['command']}\n\n[Output]\n{output}"

    # Scrub before truncating so a cut can't split a secret past the patterns.
    return truncate_output(scrub_text(res), coerce_output_cap(config))


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

    with _BG_LOCK:
        BACKGROUND_PROCESSES.pop(process_id, None)
    if proc_info["is_docker"]:
        _docker_rm(proc_info["popen_or_container_id"])
    else:
        _kill_host_process(proc_info["popen_or_container_id"])

    return f"Process {process_id} has been stopped and removed from the registry."
