import os
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import (
    resolve_and_validate_path,
    truncate_output,
    backup_file,
    coerce_output_cap,
)


def _has_crlf(path: Path) -> bool:
    try:
        with open(path, "rb") as f_bin:
            return b"\r\n" in f_bin.read(4096)
    except OSError:
        return False


def atomic_write_text(target_path: Path, text: str, crlf: bool) -> None:
    """Crash-safe write (temp file + os.replace) shared by write/patch tools.

    `text` is normalized to LF first so CRLF input never becomes "\\r\\r\\n" when
    translated back, and the original file mode is preserved (mkstemp is 0600).
    """
    import stat
    import tempfile

    text = text.replace("\r\n", "\n")
    try:
        mode = stat.S_IMODE(target_path.stat().st_mode)
    except OSError:
        # ponytail: fixed 0644 for new files; os.umask() probing races across swarm threads.
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=str(target_path.parent), prefix=".nimcode_tmp_")
    try:
        with open(fd, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
            f.write(text)
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, target_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file. ALWAYS call this before editing any existing file so your SEARCH blocks match exactly. Use start_line/end_line for large files.",
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
        "description": "Create a NEW file or fully rewrite one. For edits to existing files prefer patch_file_blocks to avoid clobbering unrelated code. Creates parent directories if needed.",
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
    if not target_path.is_file() or target_path.is_symlink():
        if target_path.is_symlink():
            return f"Error: Path '{path_str}' is a symlink; refusing to follow."
        return f"Error: Path '{path_str}' is a directory, not a file."

    # Check file size before reading full content if binary/large
    try:
        file_size = target_path.stat().st_size
    except OSError as e:
        return f"Error reading '{path_str}': {e}"
    if file_size > 5_000_000:  # 5MB absolute upper limit check for binary
        return f"Error: File '{path_str}' is too large to read ({file_size} bytes)."

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read(5_000_000 + 1)
    except UnicodeDecodeError:
        return f"Error: File '{path_str}' appears to be binary or not UTF-8 encoded."
    except OSError as e:
        return f"Error reading '{path_str}': {e}"

    start_line = args.get("start_line")
    end_line = args.get("end_line")

    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        total_lines = len(lines)

        try:
            s = int(start_line) if start_line is not None else 1
            e = int(end_line) if end_line is not None else total_lines
        except (TypeError, ValueError):
            return "Error: Invalid line number(s). start_line and end_line must be positive integers."
        if s < 1 or e < 1 or s > e:
            return "Error: Invalid line range. Require 1 <= start_line <= end_line."
        s_idx = s - 1
        e_idx = min(total_lines, e)

        if s_idx >= total_lines:
            return f"[Empty read: start_line {start_line} exceeds total lines {total_lines}]"

        selected_lines = lines[s_idx:e_idx]
        content = "\n".join(selected_lines)
        header = f"[File: {path_str} | Lines {s_idx + 1} to {min(total_lines, e_idx)} of {total_lines}]\n"
        content = header + content
        return truncate_output(content, coerce_output_cap(config))

    output_cap = coerce_output_cap(config)
    if len(content) > output_cap:
        return truncate_output(content, output_cap)
    return content


@register_tool("write_file", WRITE_FILE_SCHEMA)
def write_file_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path")
    if not path_str or not isinstance(path_str, str):
        return "Error: Invalid or missing 'path' parameter for write_file."
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    if len(content) > 5_000_000:
        return "Error: content too large to write (5MB limit)."
    target_path = resolve_and_validate_path(path_str, workspace_root)

    # Auto-backup before overwrite for easy one-command revert
    if getattr(config, "auto_backup", True):
        backup_file(target_path, workspace_root)

    # Create parent directories
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"Error: cannot create parent directories for '{path_str}': {e}"

    if target_path.is_dir():
        return f"Error: Path '{path_str}' is a directory."
    # Keep existing CRLF files CRLF; new files follow the content's own style.
    if target_path.is_file():
        crlf = _has_crlf(target_path)
    else:
        crlf = "\r\n" in content
    try:
        atomic_write_text(target_path, content, crlf)
    except OSError as e:
        return f"Error writing '{path_str}': {e}"
    return f"Successfully wrote {len(content)} characters to '{path_str}'."


LIST_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List files/directories under a path. Use for exploring unknown directories before reading files.",
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
        "description": "Find files by glob pattern. Use first to discover relevant files (e.g. '**/*.py', 'src/**/*.ts') before reading them.",
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
    from nimcode.context import is_ignored, load_gitignore_rules
    from nimcode.tools.registry import ToolSandboxError

    path_str = args.get("path", ".")
    if not isinstance(path_str, str):
        return "Error: Invalid 'path' parameter for list_files."
    try:
        target_path = resolve_and_validate_path(path_str, workspace_root)
    except ToolSandboxError as e:
        return f"Error: {e}"

    if not target_path.exists():
        return f"Error: Path '{path_str}' does not exist."
    if not target_path.is_dir():
        return f"Error: Path '{path_str}' is a file, not a directory."

    try:
        gitignore_rules = load_gitignore_rules(workspace_root)
        entries = sorted(target_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        result = []
        for entry in entries[:2000]:
            try:
                if is_ignored(entry, gitignore_rules, root=workspace_root):
                    continue
                rel = entry.relative_to(workspace_root.resolve())
            except Exception:
                continue
            t = "File" if entry.is_file() else "Directory"
            result.append(f"{rel.as_posix()} ({t})")
        if not result:
            return f"Directory '{path_str}' is empty."
        if len(entries) > 2000:
            result.append(f"... [truncated: {len(entries) - 2000} more entries]")
        output = "\n".join(result)
        output = truncate_output(output, coerce_output_cap(config))
        return output
    except Exception as e:
        return f"Error listing directory '{path_str}': {e}"


def _glob_to_regex(parts: list):
    """Path.glob-style segments ('**' = any depth) -> compiled regex on posix rel paths."""
    import re

    out = ""
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        if seg == "**":
            out += ".*" if last else "(?:[^/]+/)*"
            continue
        j = 0
        while j < len(seg):
            c = seg[j]
            if c == "*":
                out += "[^/]*"
            elif c == "?":
                out += "[^/]"
            elif c == "[" and "]" in seg[j + 2 :]:
                k = seg.index("]", j + 2)
                body = seg[j + 1 : k]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out += "[" + body.replace("\\", "\\\\") + "]"
                j = k
            else:
                out += re.escape(c)
            j += 1
        if not last:
            out += "/"
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(out + r"\Z", flags)


@register_tool("glob_files", GLOB_FILES_SCHEMA)
def glob_files_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    from nimcode.tools.registry import ToolSandboxError
    from nimcode.context import is_ignored, load_gitignore_rules

    pattern = args.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return "Error: Missing 'pattern' parameter."
    pattern = pattern.replace("\\", "/").strip()
    parts = [s for s in pattern.split("/") if s not in ("", ".")]
    if pattern.startswith("/") or Path(pattern).is_absolute() or ".." in parts:
        return "Error: glob pattern must be relative to the workspace (no '..')."
    if not parts:
        return "Error: Missing 'pattern' parameter."

    gitignore_rules = load_gitignore_rules(workspace_root)
    root = workspace_root.resolve()
    # Walk from the literal (wildcard-free) prefix only, pruning ignored dirs
    # (node_modules, .venv, ...) instead of letting Path.glob('**') visit them.
    prefix = []
    for seg in parts[:-1]:
        if any(c in seg for c in "*?["):
            break
        prefix.append(seg)
    regex = _glob_to_regex(parts)

    try:
        try:
            start = resolve_and_validate_path("/".join(prefix) or ".", workspace_root)
        except ToolSandboxError as e:
            return f"Error: {e}"
        results = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(start):
            d = Path(dirpath)
            dirnames[:] = sorted(
                n
                for n in dirnames
                if not (d / n).is_symlink()
                and not is_ignored(d / n, gitignore_rules, root=root)
            )
            for name in sorted(filenames):
                p = d / name
                rel = p.relative_to(root).as_posix()
                if not regex.match(rel) or p.is_symlink():
                    continue
                if is_ignored(p, gitignore_rules, root=root):
                    continue
                results.append(rel)
                if len(results) >= 2000:
                    truncated = True
                    break
            if truncated:
                break

        if not results:
            return f"No files matched pattern '{pattern}'."

        output = "\n".join(sorted(results))
        if truncated:
            output += "\n... [truncated: showing first 2000 matches]"
        output = truncate_output(output, coerce_output_cap(config))
        return output
    except Exception as e:
        return f"Error globbing pattern '{pattern}': {e}"


PATCH_FILE_BLOCKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_file_blocks",
        "description": "PREFERRED way to edit existing files: precise SEARCH/REPLACE blocks. Read the file first so SEARCH matches exactly including whitespace. Keep blocks small and unique.",
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
        "description": "Search file contents recursively. Use to find usages, call-sites, and error strings before reading specific files.",
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
        "description": "Find Python class/function definitions by name. Use to locate where a symbol is defined before reading it.",
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
        # Strict decode: errors="replace" would silently write U+FFFD over
        # every non-UTF-8 byte of the file on save.
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"Error: File '{path_str}' is not valid UTF-8; refusing to patch (would corrupt it)."
    except Exception as e:
        return f"Error reading '{path_str}': {e}"

    import re

    if not isinstance(edit_blocks, str):
        return "Error: Invalid 'edit_blocks' parameter: must be a string."
    # File content is newline-normalized by text-mode read; do the same to the blocks.
    edit_blocks = edit_blocks.replace("\r\n", "\n")
    # Line-anchored delimiters. Each group keeps its trailing "\n" (stripped
    # below), so an empty REPLACE (= deletion) matches instead of spilling
    # into the next block.
    pattern = (
        r"^<<<<<<<[ \t]*SEARCH[^\n]*\n(.*?)^=======[^\n]*\n(.*?)"
        r"^>>>>>>>[^\n]*REPLACE[^\n]*$"
    )
    matches = list(re.finditer(pattern, edit_blocks, re.DOTALL | re.MULTILINE))
    if not matches:
        return "Error: No valid edit blocks found. Ensure you use <<<<<<< SEARCH, =======, and >>>>>>> REPLACE delimiters exactly."

    new_content = content
    for idx, match in enumerate(matches):
        search_str = match.group(1).removesuffix("\n")
        replace_str = match.group(2).removesuffix("\n")

        if not search_str:
            return f"Error in block #{idx + 1}: empty SEARCH block."
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
        backup_file(target_path, workspace_root, tag="blocks")

    try:
        atomic_write_text(target_path, new_content, _has_crlf(target_path))
    except OSError as e:
        return f"Error writing '{path_str}': {e}"

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
    from nimcode.context import is_ignored, load_gitignore_rules

    if not isinstance(query, str) or not query:
        return "Error: Missing 'query' parameter."

    gitignore_rules = load_gitignore_rules(workspace_root)

    regex_note = ""
    try:
        compiled_regex = re.compile(query, re.IGNORECASE)
    except re.error:
        compiled_regex = None
        regex_note = (
            "[Note: invalid regex, falling back to case-insensitive substring match]\n"
        )

    matches = []
    skipped_large = 0
    visited: set[tuple[int, int]] = set()
    # search_path is resolved; relative_to() against an unresolved root
    # (relative path, symlinked/8.3 dir) raised and silently dropped every match.
    root = workspace_root.resolve()
    query_lower = query.lower()

    def grep_file(file_path: Path) -> None:
        with open(file_path, "rb") as fb:
            data = fb.read()
        if b"\0" in data[:8192]:
            return  # binary
        rel = file_path.relative_to(root).as_posix()
        for line_num, line in enumerate(
            data.decode("utf-8", errors="replace").splitlines(), 1
        ):
            if compiled_regex:
                hit = compiled_regex.search(line)
            else:
                hit = query_lower in line.lower()
            if hit:
                matches.append((rel, line_num, line.strip()[:500]))
                if len(matches) > 5000:
                    return

    def walk_and_search(dir_path: Path, depth: int = 0):
        nonlocal skipped_large
        if depth > 25 or len(matches) > 5000:
            return
        try:
            st = dir_path.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited:
                return
            visited.add(key)
        except OSError:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except OSError:
            return
        for entry in entries:
            if len(matches) > 5000:
                break
            try:
                if entry.is_symlink():
                    continue
                if is_ignored(entry, gitignore_rules, root=root):
                    continue
            except OSError:
                continue
            if entry.is_dir():
                walk_and_search(entry, depth + 1)
            elif entry.is_file():
                try:
                    file_size = entry.stat().st_size
                except OSError:
                    continue
                if file_size > 1_000_000:
                    skipped_large += 1
                    continue
                try:
                    grep_file(entry)
                except Exception:
                    pass

    if search_path.is_file():
        try:
            if search_path.stat().st_size > 5_000_000:
                return f"Error: File '{path_str}' is too large to search."
            grep_file(search_path)
        except Exception as e:
            return f"Error reading file '{path_str}': {e}"
    else:
        walk_and_search(search_path)

    if not matches:
        return f"No matches found for query '{query}'."

    output_lines = [f"{m[0]}:{m[1]}: {m[2]}" for m in matches[:5000]]
    output = regex_note + "\n".join(output_lines)
    if skipped_large:
        output += f"\n[skipped {skipped_large} large file(s) >1MB]"
    return truncate_output(output, coerce_output_cap(config))


@register_tool("search_symbols", SEARCH_SYMBOLS_SCHEMA)
def search_symbols_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    query = args.get("query", "")
    if not query or not isinstance(query, str):
        return "Error: Missing 'query' parameter."

    import ast
    import warnings
    from nimcode.context import is_ignored, load_gitignore_rules

    gitignore_rules = load_gitignore_rules(workspace_root)

    symbols = []
    visited: set = set()

    def scan_file(file_path: Path):
        try:
            if file_path.stat().st_size > 1_000_000:
                return
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # SyntaxWarnings (e.g. invalid escapes) would spew to stderr and garble the TUI.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
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

    def walk_and_scan(dir_path: Path, depth: int = 0):
        if depth > 25 or len(symbols) > 5000:
            return
        try:
            st = dir_path.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited:
                return
            visited.add(key)
        except OSError:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except OSError:
            return
        for entry in entries:
            if len(symbols) > 5000:
                break
            try:
                if entry.is_symlink():
                    continue
                if is_ignored(entry, gitignore_rules, root=workspace_root):
                    continue
            except OSError:
                continue
            if entry.is_dir():
                walk_and_scan(entry, depth + 1)
            elif entry.is_file() and entry.suffix == ".py":
                scan_file(entry)

    walk_and_scan(workspace_root)

    if not symbols:
        return f"No symbols matching query '{query}' found."

    output = "\n".join(symbols[:5000])
    output = truncate_output(output, coerce_output_cap(config))
    return output
