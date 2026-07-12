import os
import time
import shutil
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import resolve_and_validate_path, truncate_output

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file located within the workspace root, optionally specifying a line range.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-indexed starting line number to read (inclusive).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-indexed ending line number to read (inclusive).",
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
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
}


@register_tool("read_file", READ_FILE_SCHEMA)
def read_file_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path")
    if not path_str or not isinstance(path_str, str):
        return "Error: Invalid or missing 'path' parameter for read_file."

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

    start_line = args.get("start_line")
    end_line = args.get("end_line")

    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        total_lines = len(lines)

        s_idx = max(0, int(start_line) - 1) if start_line is not None else 0
        e_idx = min(total_lines, int(end_line)) if end_line is not None else total_lines

        if s_idx >= total_lines:
            return f"[Empty read: start_line {start_line} exceeds total lines {total_lines}]"

        selected_lines = lines[s_idx:e_idx]
        content = "\n".join(selected_lines)
        header = f"[File: {path_str} | Lines {s_idx + 1} to {min(total_lines, e_idx)} of {total_lines}]\n"
        content = header + content
        return content

    output_cap = getattr(config, "output_cap", 100000) if config is not None else 100000
    if len(content) > output_cap:
        return (
            content[:output_cap]
            + f"\n... [Output truncated at {output_cap} characters. Use start_line and end_line parameters to read specific sections of the file.]"
        )
    return content


@register_tool("write_file", WRITE_FILE_SCHEMA)
def write_file_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path")
    if not path_str or not isinstance(path_str, str):
        return "Error: Invalid or missing 'path' parameter for write_file."
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    target_path = resolve_and_validate_path(path_str, workspace_root)

    # Auto-backup before overwrite for easy one-command revert
    if (
        getattr(config, "auto_backup", True)
        and target_path.exists()
        and target_path.is_file()
    ):
        from nimcode.config import get_project_nimcode_dir

        backup_dir = get_project_nimcode_dir(workspace_root) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        try:
            rel_path = target_path.relative_to(workspace_root)
            safe_rel_name = str(rel_path).replace(os.sep, "_").replace("/", "_")
        except Exception:
            safe_rel_name = target_path.name
        backup_filename = f"{timestamp}_{safe_rel_name}"
        backup_path = backup_dir / backup_filename
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            pass

    # Create parent directories
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Detect original line ending if file exists
    has_crlf = False
    if target_path.exists() and target_path.is_file():
        try:
            with open(target_path, "rb") as f_bin:
                has_crlf = b"\r\n" in f_bin.read(4096)
        except Exception:
            pass

    newline = "\r\n" if has_crlf else "\n"
    with open(target_path, "w", encoding="utf-8", newline=newline) as f:
        f.write(content)
    return f"Successfully wrote {len(content)} characters to '{path_str}'."


LIST_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List files and directories in the workspace under the specified path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to list files from. Defaults to '.' (workspace root).",
                }
            },
        },
    },
}

GLOB_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "glob_files",
        "description": "Find files in the workspace matching a glob pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to search for (e.g. '**/*.py').",
                }
            },
            "required": ["pattern"],
        },
    },
}


@register_tool("list_files", LIST_FILES_SCHEMA)
def list_files_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    from nimcode.tools.registry import ToolSandboxError

    path_str = args.get("path", ".")
    try:
        target_path = resolve_and_validate_path(path_str, workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    if not target_path.exists():
        return f"Error: Path '{path_str}' does not exist."
    if not target_path.is_dir():
        return f"Error: Path '{path_str}' is a file, not a directory."

    try:
        entries = sorted(target_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        result = []
        for entry in entries:
            rel = entry.relative_to(workspace_root)
            t = "File" if entry.is_file() else "Directory"
            result.append(f"{rel.as_posix()} ({t})")
        if not result:
            return f"Directory '{path_str}' is empty."
        output = "\n".join(result)
        output_cap = getattr(config, "output_cap", 100000)
        output = truncate_output(output, output_cap)
        return output
    except Exception as e:
        return f"Error listing directory '{path_str}': {e}"


@register_tool("glob_files", GLOB_FILES_SCHEMA)
def glob_files_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    from nimcode.tools.registry import ToolSandboxError
    from nimcode.context import is_ignored

    pattern = args.get("pattern")
    if not pattern:
        return "Error: Missing 'pattern' parameter."

    gitignore_rules = []
    gitignore_path = workspace_root / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                gitignore_rules = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            pass

    try:
        results = []
        for p in workspace_root.glob(pattern):
            try:
                resolved = resolve_and_validate_path(
                    str(p.relative_to(workspace_root)), workspace_root
                )
                if resolved.is_file():
                    if is_ignored(resolved, gitignore_rules, root=workspace_root):
                        continue
                    results.append(p.relative_to(workspace_root).as_posix())
            except (ToolSandboxError, ValueError):
                continue

        if not results:
            return f"No files matched pattern '{pattern}'."

        output = "\n".join(sorted(results))
        output_cap = getattr(config, "output_cap", 100000)
        output = truncate_output(output, output_cap)
        return output
    except Exception as e:
        return f"Error globbing pattern '{pattern}': {e}"


PATCH_FILE_BLOCKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_file_blocks",
        "description": "Apply precision multi-line search-and-replace edits to a file using Aider-style SEARCH/REPLACE blocks.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to modify.",
                },
                "edit_blocks": {
                    "type": "string",
                    "description": "One or more edit blocks formatted with <<<<<<< SEARCH, =======, and >>>>>>> REPLACE delimiters.",
                },
            },
            "required": ["path", "edit_blocks"],
        },
    },
}

SEARCH_GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_grep",
        "description": "Search codebase file contents for a specific query substring or regex pattern recursively.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The exact substring or regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative path to search under. Defaults to '.' (workspace root).",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_SYMBOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_symbols",
        "description": "Find Python class and function definitions matching a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name of the class or function to find.",
                }
            },
            "required": ["query"],
        },
    },
}


@register_tool("patch_file_blocks", PATCH_FILE_BLOCKS_SCHEMA)
def patch_file_blocks_tool(
    args: Dict[str, Any], workspace_root: Path, config: Any
) -> str:
    path_str = args.get("path", "")
    edit_blocks = args.get("edit_blocks", "")
    return patch_file_blocks_logic(
        workspace_root, path_str, edit_blocks, config=config, dry_run=False
    )


def patch_file_blocks_logic(
    workspace_root: Path,
    path_str: str,
    edit_blocks: str,
    config: Any = None,
    dry_run: bool = False,
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)

    try:
        target_path = resolve_and_validate_path(path_str, workspace_root)
    except Exception as e:
        return f"Error: {e}"

    if not target_path.exists() or not target_path.is_file():
        return f"Error: File '{path_str}' does not exist or is not a file."

    try:
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading '{path_str}': {e}"

    import re

    pattern = r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE"
    matches = list(re.finditer(pattern, edit_blocks, re.DOTALL))
    if not matches:
        return "Error: No valid edit blocks found. Ensure you use <<<<<<< SEARCH, =======, and >>>>>>> REPLACE delimiters exactly."

    new_content = content
    for idx, match in enumerate(matches):
        search_str = match.group(1)
        replace_str = match.group(2)

        count = new_content.count(search_str)
        if count == 0:
            return f"Error in block #{idx + 1}: SEARCH block not found in the file. Make sure lines match exactly including leading spaces."
        if count > 1:
            return f"Error in block #{idx + 1}: SEARCH block matches multiple locations ({count} found). Make the search block more unique."

        new_content = new_content.replace(search_str, replace_str, 1)

    if dry_run:
        import difflib

        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path_str} (current)",
                tofile=f"b/{path_str} (proposed block patch)",
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
        backup_path = backup_dir / f"{timestamp}_blocks_{safe_rel_name}"
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            pass

    # Detect original line ending
    has_crlf = False
    try:
        with open(target_path, "rb") as f_bin:
            has_crlf = b"\r\n" in f_bin.read(4096)
    except Exception:
        pass

    newline = "\r\n" if has_crlf else "\n"
    with open(target_path, "w", encoding="utf-8", newline=newline) as f:
        f.write(new_content)

    return f"Successfully applied {len(matches)} edit block(s) to '{path_str}'."


@register_tool("search_grep", SEARCH_GREP_SCHEMA)
def search_grep_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    query = args.get("query", "")
    path_str = args.get("path", ".")

    try:
        search_path = resolve_and_validate_path(path_str, workspace_root)
    except Exception as e:
        return f"Error: {e}"

    if not search_path.exists():
        return f"Error: Path '{path_str}' does not exist."

    import re
    from nimcode.context import is_ignored

    gitignore_rules = []
    gitignore_path = workspace_root / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                gitignore_rules = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            pass

    try:
        compiled_regex = re.compile(query, re.IGNORECASE)
    except Exception:
        compiled_regex = None

    matches = []

    def walk_and_search(dir_path: Path):
        for entry in sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if is_ignored(entry, gitignore_rules, root=workspace_root):
                continue
            if entry.is_dir():
                walk_and_search(entry)
            elif entry.is_file():
                file_size = entry.stat().st_size
                if file_size > 1_000_000:
                    continue
                try:
                    with open(entry, "r", encoding="utf-8", errors="replace") as f:
                        for line_num, line in enumerate(f, 1):
                            if compiled_regex:
                                if compiled_regex.search(line):
                                    matches.append(
                                        (
                                            entry.relative_to(
                                                workspace_root
                                            ).as_posix(),
                                            line_num,
                                            line.strip(),
                                        )
                                    )
                            else:
                                if query in line:
                                    matches.append(
                                        (
                                            entry.relative_to(
                                                workspace_root
                                            ).as_posix(),
                                            line_num,
                                            line.strip(),
                                        )
                                    )
                except Exception:
                    pass

    if search_path.is_file():
        try:
            with open(search_path, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if compiled_regex:
                        if compiled_regex.search(line):
                            matches.append(
                                (
                                    search_path.relative_to(workspace_root).as_posix(),
                                    line_num,
                                    line.strip(),
                                )
                            )
                    else:
                        if query in line:
                            matches.append(
                                (
                                    search_path.relative_to(workspace_root).as_posix(),
                                    line_num,
                                    line.strip(),
                                )
                            )
        except Exception as e:
            return f"Error reading file '{path_str}': {e}"
    else:
        walk_and_search(search_path)

    if not matches:
        return f"No matches found for query '{query}'."

    output_lines = [f"{m[0]}:{m[1]}: {m[2]}" for m in matches]
    output = "\n".join(output_lines)
    output_cap = getattr(config, "output_cap", 100000)
    output = truncate_output(output, output_cap)
    return output


@register_tool("search_symbols", SEARCH_SYMBOLS_SCHEMA)
def search_symbols_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    query = args.get("query", "")
    if not query:
        return "Error: Missing 'query' parameter."

    import ast
    from nimcode.context import is_ignored

    gitignore_rules = []
    gitignore_path = workspace_root / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                gitignore_rules = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            pass

    symbols = []

    def scan_file(file_path: Path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    sym_name = node.name
                    if query.lower() in sym_name.lower():
                        rel = file_path.relative_to(workspace_root).as_posix()
                        t = "class" if isinstance(node, ast.ClassDef) else "function"
                        symbols.append(f"{rel}:{node.lineno}: [{t}] {sym_name}")
        except Exception:
            pass

    def walk_and_scan(dir_path: Path):
        for entry in sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if is_ignored(entry, gitignore_rules, root=workspace_root):
                continue
            if entry.is_dir():
                walk_and_scan(entry)
            elif entry.is_file() and entry.suffix == ".py":
                scan_file(entry)

    walk_and_scan(workspace_root)

    if not symbols:
        return f"No symbols matching query '{query}' found."

    output = "\n".join(symbols)
    output_cap = getattr(config, "output_cap", 100000)
    output = truncate_output(output, output_cap)
    return output
