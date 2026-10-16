import json
import re
import difflib
import shlex
import subprocess
import threading
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

    if Path(path_str).is_absolute():
        raise ToolSandboxError(
            f"Security Error: Path '{path_str}' must be relative to workspace root."
        )

    if "\x00" in path_str:
        raise ToolSandboxError("Security Error: Path contains a NUL byte.")

    resolved_root = Path(workspace_root).resolve()
    try:
        target_path = (resolved_root / path_str).resolve()
    except (OSError, ValueError, RuntimeError) as e:
        raise ToolSandboxError(f"Security Error: Cannot resolve path '{path_str}': {e}")

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
    Result is cached for 30s to avoid blocking the TUI on every preview.
    """
    import time

    now = time.monotonic()
    cached = getattr(is_docker_available, "_cache", None)
    if cached is not None:
        result, ts = cached
        if now - ts < 30:
            return result
    try:
        result_proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        available = result_proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        available = False
    is_docker_available._cache = (available, now)  # type: ignore[attr-defined]
    return available


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
        except json.JSONDecodeError as e:
            return False, tool_name, {"_parse_error": f"Invalid JSON arguments: {e}"}
        except Exception:
            return False, tool_name, {}
    if not isinstance(args, dict):
        # e.g. arguments='[1,2]' or '"x"' -- .get() below would crash.
        return False, tool_name, {"_parse_error": "Arguments must be a JSON object."}

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
        if args.get("include") is not None and not isinstance(args["include"], str):
            return False, tool_name, args
        if args.get("case_sensitive") is not None and not isinstance(
            args["case_sensitive"], (bool, str)
        ):
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
    # Mask Bearer tokens (base64/url-safe chars incl. + / ~ and = padding)
    text = re.sub(r"(?i)(\bbearer\s+)[\w.~+/\-]+=*", r"\1***REDACTED***", text)
    # Mask Basic auth only in an Authorization header context; a bare
    # "basic <word>" rule mangled ordinary prose ("basic usage").
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*['\"]?basic\s+)[A-Za-z0-9+/]+=*",
        r"\1***REDACTED***",
        text,
    )
    # Secret CLI flags: --password=x / --token x / --secret x / --api-key x.
    # Lookbehind stops matches inside hyphenated words.
    text = re.sub(
        r"(?i)((?<![\w-])--(?:password|passwd|token|secret|api-key|apikey)"
        r"(?:\s*=\s*|\s+)['\"]?)[^\s'\"]+",
        r"\1***REDACTED***",
        text,
    )
    # mysql-style attached password (mysql -uroot -psecret). Scoped to the
    # mysql family: a generic "-p" rule redacted `mkdir -p x`, `find -print`,
    # `docker -p 3000:3000` and every hyphenated word ("pre-processing").
    text = re.sub(
        r"(?i)(\b(?:mysql\w*|mariadb\w*)\b[^\n]*?(?<![\w-])-p)(?=[^\s-])\S+",
        r"\1***REDACTED***",
        text,
    )
    # Mask headers like x-api-key
    text = re.sub(
        r"(?i)((?:api-key|x-api-key)\s*:\s*['\"]?)[\w\.\-]+", r"\1***REDACTED***", text
    )
    # Well-known vendor key prefixes (NVIDIA, OpenAI/Anthropic, GitHub, GitLab,
    # Slack, Google) -- these contain '-'/'_' so the 32+ alnum rule misses them.
    text = re.sub(
        r"\b(?:nvapi-|sk-|ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|xox[abprs]-|AIza)"
        r"[\w\-]{16,}",
        "***REDACTED***",
        text,
    )
    # .env-style assignments: FOO_API_KEY=..., DB_PASSWORD=...
    text = re.sub(
        r"(\b[A-Z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*"
        r"\s*=\s*['\"]?)[^\s'\"]+",
        r"\1***REDACTED***",
        text,
    )
    # Mask AWS keys (AKIA...)
    text = re.sub(r"(?i)(AKIA[0-9A-Z]{16})", r"***REDACTED***", text)
    # Mask AWS secret keys (40 chars, preceded by context like aws_secret_access_key=)
    text = re.sub(
        r"(?i)(aws_secret_access_key\s*=\s*['\"]?)([A-Za-z0-9/+=]{40})",
        r"\1***REDACTED***",
        text,
    )
    # Mask database URLs (postgres://, mysql://, mongodb://, redis://)
    # NOTE: replace the whole URL, not just append — group ref would leak creds.
    # Also covers driver suffixes (postgresql+psycopg2://, mongodb+srv://).
    text = re.sub(
        r"(?i)(?:postgres|postgresql|mysql|mongodb|redis|rediss|amqp)(?:\+[\w.]+)?"
        r"://[^:\s/@]+:[^@\s]+@[^/\s]+",
        "***REDACTED_DB_URL***",
        text,
    )
    # Any other scheme://user:password@host -- mask the password part.
    text = re.sub(
        r"(?i)(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+@", r"\1***REDACTED***@", text
    )
    # Mask JWT tokens (3 base64 parts separated by dots)
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*", "***REDACTED***", text
    )
    # Mask generic long alphanumeric strings that look like keys (32+ chars)
    text = re.sub(r"(?i)([a-z0-9]{32,})", r"***REDACTED***", text)
    return text


BACKUP_MANIFEST = "manifest.jsonl"
_BACKUP_LOCK = threading.Lock()


def backup_file(target_path: Path, workspace_root: Path, tag: str = "") -> None:
    """Copy target_path into the project's backups dir. Never raises."""
    import os
    import shutil
    import time

    try:
        if not target_path.exists() or not target_path.is_file():
            return
        from nimcode.config import get_project_nimcode_dir

        backup_dir = get_project_nimcode_dir(workspace_root) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1_000_000)
        try:
            rel_path = target_path.resolve().relative_to(workspace_root.resolve())
            safe_rel_name = str(rel_path).replace(os.sep, "_").replace("/", "_")
        except Exception:
            rel_path = None
            safe_rel_name = target_path.name
        prefix = f"{tag}_" if tag else ""
        backup_path = backup_dir / f"{timestamp}_{os.getpid()}_{prefix}{safe_rel_name}"
        shutil.copy2(target_path, backup_path)
        entry = {
            "backup": backup_path.name,
            "original": rel_path.as_posix() if rel_path is not None else None,
            "tag": tag,
            "ts": time.time(),
        }
        # Subagent threads back up files concurrently: serialize appends.
        with _BACKUP_LOCK:
            with open(backup_dir / BACKUP_MANIFEST, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _parse_backup_name(name: str) -> Tuple[Any, str, str]:
    """Legacy (manifest-less) backup name -> (ts seconds | None, hint, tag).

    Names are '<ts>_<pid>_<tag_><safe_rel>' (older ones lack the pid, and
    use second rather than microsecond timestamps). `hint` is the
    separator-flattened relative path, so it is only a guess.
    """
    ts_part, _, rest = name.partition("_")
    hint = rest or name
    pid, sep, after = hint.partition("_")
    if sep and pid.isdigit() and after:
        hint = after
    try:
        ts = int(ts_part)
        if ts > 9999999999:  # microsecond timestamps
            ts /= 1_000_000
    except ValueError:
        ts = None
    tag = ""
    for t in ("blocks", "ast"):
        if hint.startswith(t + "_"):
            tag, hint = t, hint[len(t) + 1 :]
            break
    return ts, hint, tag


def list_backups(workspace_root: Path) -> list:
    """Backups newest first: dicts with backup, path, original (relative posix
    path, or None for legacy backups without a manifest entry), hint, tag,
    ts and a display time `when`."""
    import time

    from nimcode.config import get_project_nimcode_dir

    backup_dir = get_project_nimcode_dir(workspace_root) / "backups"
    if not backup_dir.is_dir():
        return []
    manifest: Dict[str, Dict[str, Any]] = {}
    try:
        lines = (backup_dir / BACKUP_MANIFEST).read_text(encoding="utf-8")
    except OSError:
        lines = ""
    for line in lines.splitlines():
        try:
            entry = json.loads(line)
            manifest[entry["backup"]] = entry
        except (ValueError, KeyError, TypeError):
            continue
    out = []
    for f in backup_dir.iterdir():
        if not f.is_file() or f.name == BACKUP_MANIFEST:
            continue
        entry = manifest.get(f.name)
        if entry:
            item = dict(entry, hint=entry.get("original") or f.name)
        else:
            ts, hint, tag = _parse_backup_name(f.name)
            item = {"backup": f.name, "original": None, "hint": hint, "tag": tag}
            item["ts"] = ts
        if not isinstance(item.get("ts"), (int, float)):
            item["ts"] = f.stat().st_mtime
        item["path"] = f
        item["when"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["ts"]))
        out.append(item)
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out


def coerce_output_cap(config: Any, default: int = 100000) -> int:
    """Read output_cap off config; fall back to default on garbage."""
    try:
        return int(getattr(config, "output_cap", default))
    except Exception:
        return default


def truncate_output(text: str, max_length: int = 100000) -> str:
    """
    Smart truncation: keeps the first N and last N chars of the string, dropping the middle.
    This ensures that both the beginning (context) and the end (stack traces/errors) are preserved.
    """
    if not isinstance(text, str):
        text = str(text)
    try:
        max_length = max(0, int(max_length))
    except (TypeError, ValueError):
        max_length = 100000
    if len(text) <= max_length:
        return text

    # Report chars actually dropped (len - kept), not len - cap.
    msg = f"\n... [Output truncated: dropped {len(text) - max_length} chars from the middle] ...\n"
    msg = f"\n... [Output truncated: dropped {len(text) - (max_length - len(msg))} chars from the middle] ...\n"
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
        if not isinstance(new_content, str):
            new_content = str(new_content)
        try:
            target_path = resolve_and_validate_path(path_str, workspace_root)
            old_content = ""
            if target_path.exists() and target_path.is_file():
                try:
                    # Cap preview reads to avoid OOM on huge files.
                    if target_path.stat().st_size > 200_000:
                        old_content = "<file too large for preview>"
                    else:
                        with open(
                            target_path, "r", encoding="utf-8", errors="replace"
                        ) as f:
                            old_content = f.read(200_000)
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
        except Exception as e:
            return f"\n[Tool Preview: write_file] Path: {path_str}\nError: {e}\n"

    elif tool_name in ("execute_command", "run_tests"):
        default_cmd = "pytest" if tool_name == "run_tests" else ""
        command = scrub_text(str(args.get("command") or default_cmd))
        from nimcode.tools.cmd import _build_docker_command, _resolve_use_docker

        # Same decision + argv the executor uses, so the approved preview
        # matches what runs (image/ports/extra args come from config).
        if _resolve_use_docker(args.get("use_docker"), config):
            argv = _build_docker_command(command, workspace_root, config)
            cmd_preview = " ".join(shlex.quote(a) for a in argv)
            return f"\n[Tool Preview: {tool_name} (Docker)]\n{cmd_preview}\n"
        return f"\n[Tool Preview: {tool_name} (Host)]\n{command}\n"

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
        extra = ""
        if args.get("include"):
            extra += f", Include: {args['include']}"
        if args.get("case_sensitive") not in (None, False, "", "false"):
            extra += ", Case-sensitive"
        return f"\n[Tool Preview: search_grep] Query: '{query}', Path: {path}{extra}\n"

    elif tool_name == "search_symbols":
        query = args.get("query", "")
        return f"\n[Tool Preview: search_symbols] Query: '{query}'\n"

    elif tool_name == "spawn_agent":
        task = args.get("task_prompt", "")
        task = str(task) if not isinstance(task, str) else task
        suffix = "..." if len(task) > 100 else ""
        return f"\n[Tool Preview: spawn_agent] Task: {task[:100]}{suffix}\n"

    elif tool_name == "send_message_to_agent":
        agent_id = args.get("agent_id", "")
        message = args.get("message", "")
        message = str(message) if not isinstance(message, str) else message
        suffix = "..." if len(message) > 100 else ""
        return f"\n[Tool Preview: send_message_to_agent] Agent: {agent_id}, Message: {message[:100]}{suffix}\n"

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

    try:
        return f"\n[Tool Preview: {tool_name}] Args: {json.dumps(args, indent=2, default=str)}\n"
    except Exception:
        return f"\n[Tool Preview: {tool_name}] (args not displayable)\n"


# Compiled regex for detecting sensitive environment variables.
# This covers common names, abbreviations (_PW, _PWD), and ignores case.
SENSITIVE_ENV_REGEX = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|_PW|_PWD|CREDENTIAL|PRIVATE_KEY|AUTH_|"
    r"AWS_KEY|AWS_SECRET|AWS_ACCESS|AWS_SECRET_ACCESS|DATABASE_URL|DB_PASSWORD|"
    r"DB_PASS|DB_USER|DB_NAME|POSTGRES_PASSWORD|MYSQL_PASSWORD|MONGO_URI|"
    r"REDIS_URL|REDIS_PASSWORD|JWT_SECRET|SESSION_SECRET|ENCRYPTION_KEY|"
    r"SSH_KEY|SSH_PRIVATE_KEY|GPG_KEY|SIGNING_KEY|WEBHOOK_SECRET|WEBHOOK_TOKEN|"
    r"SLACK_TOKEN|DISCORD_TOKEN|TELEGRAM_TOKEN|STRIPE_KEY|STRIPE_SECRET|"
    r"PAYPAL_CLIENT_ID|PAYPAL_SECRET|SENDGRID_API_KEY|MAILGUN_API_KEY|"
    r"SENTRY_DSN|NEW_RELIC_KEY|DATADOG_API_KEY|HEROKU_API_KEY|VERCEL_TOKEN|"
    r"NETLIFY_AUTH_TOKEN|CLOUDFLARE_API_KEY|DIGITALOCEAN_TOKEN|GITHUB_TOKEN|"
    r"GITLAB_TOKEN|BITBUCKET_TOKEN|NPM_TOKEN|PYPI_TOKEN|DOCKER_PASSWORD|"
    r"DOCKER_USERNAME|KUBECONFIG|KUBE_TOKEN|VAULT_TOKEN|CONSUL_TOKEN)",
    re.IGNORECASE,
)


def scrub_env(env: Dict[str, str]) -> Dict[str, str]:
    """
    Remove sensitive environment variables before passing to subprocess.
    Uses regex to catch common credential patterns and abbreviations.
    Matching keys are dropped, not set to a placeholder: GITHUB_TOKEN=***REDACTED***
    makes tools attempt (and fail) auth instead of running anonymously, and
    breaks unrelated vars such as TOKENIZERS_PARALLELISM.
    """
    return {k: v for k, v in env.items() if not SENSITIVE_ENV_REGEX.search(k)}


# --- SSRF guard (shared by tools/browser.py and skills.py) ---
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _ip_is_internal(ip) -> bool:
    # is_global (not just is_private) also covers CGNAT 100.64/10, 0.0.0.0,
    # documentation/benchmark ranges, etc. Embedded IPv4 in IPv6 (mapped,
    # 6to4, Teredo) is checked too.
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo or (None, None))[1]
        if embedded is not None and _ip_is_internal(embedded):
            return True
    return not ip.is_global or ip.is_multicast


def _check_url(url: str) -> tuple[bool, str, list]:
    """Return (ok, reason, vetted_ips). See _is_url_allowed."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not isinstance(url, str) or any(c in "\\ " or ord(c) < 0x20 for c in url):
        # Backslashes/whitespace parse differently in urllib vs Chromium
        # (http://10.0.0.5\@example.com is example.com to urllib, 10.0.0.5 to Chromium).
        return False, "URL contains disallowed characters", []
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        parsed.port  # raises ValueError on garbage ports
    except Exception:
        return False, "unparseable URL", []
    if scheme not in ("http", "https"):
        return False, f"URL scheme '{scheme}' not allowed (http/https only)", []
    if not host:
        return False, "URL has no host", []
    if host in ("169.254.169.254", "metadata.google.internal"):
        return False, "URL target is blocked", []

    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [
                ipaddress.ip_address(info[4][0].split("%")[0])
                for info in socket.getaddrinfo(host, None)
            ]
        except Exception:
            return False, "URL host does not resolve", []
        if not ips:
            return False, "URL host does not resolve", []
    for ip in ips:
        if _ip_is_internal(ip):
            # Explicit localhost stays allowed for the dev-server verify loop,
            # but only when it resolves to loopback (never to a LAN address).
            if host in _LOCAL_HOSTS and ip.is_loopback:
                continue
            return False, "URL resolves to a private/internal address", []
    return True, "", ips


def _is_url_allowed(url: str) -> tuple[bool, str]:
    """SSRF guard: allow public hosts + explicit localhost; block everything else.

    Hostnames are DNS-resolved and *every* returned IP is checked, so decimal/
    octal/hex IP spellings (2130706433, 127.1, 0x7f.0.0.1) and wildcard-DNS
    tricks (127.0.0.1.nip.io) cannot smuggle loopback/private targets past us.
    """
    ok, reason, _ = _check_url(url)
    return ok, reason
