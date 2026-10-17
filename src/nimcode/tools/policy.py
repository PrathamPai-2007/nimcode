"""Approval policy: which tool calls run unprompted and which need the user.

Reads are free in both modes (sensitive files excepted). Plan mode is
read-only. Build mode auto-runs ordinary writes, commands and network tools
and asks only for sensitive files and risky shell commands.

ponytail: the command rules are a regex heuristic, not a sandbox -- Docker is
the real boundary. Tune RISKY_COMMAND here; split into a parser if it needs
to understand quoting or pipelines.
"""

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, Literal

from nimcode.tools.cmd import _validate_command_safety

Decision = Literal["auto", "ask", "deny"]

READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "glob_files",
        "search_grep",
        "search_symbols",
        "get_process_output",
        "list_agents",
        "web_search",
    }
)
WRITE_TOOLS = frozenset({"write_file", "patch_file_blocks", "patch_file_ast"})
COMMAND_TOOLS = frozenset({"execute_command", "run_tests", "start_background_process"})

# Matched against the lower-cased posix path and its basename.
SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "*.tfstate",
    "credentials*",
    "secrets.*",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".git/*",
    "*/.git/*",
    ".github/workflows/*",
    "*/.github/workflows/*",
)

RISKY_COMMAND = re.compile(
    r"""(?ix)
    \brm\s+-[a-z]*r             # recursive delete
    | \bsudo\b | \bsu\s+-
    | \b(chmod|chown)\s+-[a-z]*r
    | \bgit\s+(push|reset\s+--hard|clean|checkout\s+--|rebase|filter-branch)\b
    | \b(curl|wget)\b           # network fetch from the shell
    | \b(ssh|scp|sftp|rsync|nc|ncat)\b
    | \b(pip3?|npm|yarn|pnpm|apt(-get)?|brew|gem|cargo)\s+(install|add|i)\b.*\s(-g|--global|--user)\b
    | \b(apt(-get)?|brew|yum|dnf|choco|winget)\s+(install|remove|uninstall)\b
    | \b(dd|mkfs\S*|shutdown|reboot|format)\b
    | >\s*/dev/(?!null)
    | \b(rmdir|del|rd)\s+/s
    | (?<![\w.])(/etc|/usr|/var|/root|~|[a-z]:[\\/])\S*   # absolute / home paths
    """
)


def is_sensitive_path(path: str) -> bool:
    p = str(path).replace("\\", "/").lower().removeprefix("./")
    name = p.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(name, pat)
        for pat in SENSITIVE_PATTERNS
    )


def ask_reason(tool_name: str, args: Dict[str, Any]) -> str:
    if tool_name in COMMAND_TOOLS:
        return "risky command"
    return "sensitive file"


def classify(
    tool_name: str, args: Dict[str, Any], workspace_root: Path, mode: str
) -> Decision:
    """auto = run now, ask = prompt the user, deny = refuse (Plan mode writes)."""
    if tool_name in READ_ONLY_TOOLS:
        path = args.get("path") if tool_name == "read_file" else None
        return "ask" if path and is_sensitive_path(path) else "auto"
    if mode == "plan":
        return "deny"
    if tool_name in WRITE_TOOLS:
        return "ask" if is_sensitive_path(args.get("path", "")) else "auto"
    if tool_name in COMMAND_TOOLS:
        cmd = str(args.get("command") or "")
        if tool_name == "run_tests" and not cmd:
            return "auto"
        if not _validate_command_safety(cmd)[0] or RISKY_COMMAND.search(cmd):
            return "ask"
        return "auto"
    # screenshot_page, stop_process, spawn_agent, send_message_to_agent, ...
    # Unknown tools fall through to a prompt rather than silently running.
    if tool_name in {
        "screenshot_page",
        "stop_process",
        "spawn_agent",
        "send_message_to_agent",
        "kill_agent",
    }:
        return "auto"
    return "ask"
