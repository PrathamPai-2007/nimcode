import ast
import difflib
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import resolve_and_validate_path, backup_file
from nimcode.tools.fs import atomic_write_text, _has_crlf

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_defs(node, prefix=""):
    """Yield (qualified_name, def_node), e.g. ('Foo.bar', <FunctionDef bar>)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEF_TYPES):
            qual = prefix + child.name
            yield qual, child
            yield from _iter_defs(child, qual + ".")
        else:
            yield from _iter_defs(child, prefix)


PATCH_FILE_AST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_file_ast",
        "description": "Replace one Python function/class by name without rewriting the file. Read the file first, then pass the exact target_symbol and full replacement_code.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to modify.",
                },
                "target_symbol": {
                    "type": "string",
                    "description": "The exact function name, class name, or unique target string block to replace.",
                },
                "replacement_code": {
                    "type": "string",
                    "description": "The new code content to replace the targeted symbol/block with.",
                },
            },
            "required": ["path", "target_symbol", "replacement_code"],
        },
    },
}


@register_tool("patch_file_ast", PATCH_FILE_AST_SCHEMA)
def patch_file_ast_tool(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    path_str = args.get("path", "")
    target_sym = args.get("target_symbol", "")
    rep_code = args.get("replacement_code", "")
    return patch_file_ast_logic(
        workspace_root, path_str, target_sym, rep_code, config=config, dry_run=False
    )


def patch_file_ast_logic(
    workspace_root: Path,
    path_str: str,
    target_symbol: str,
    replacement_code: str,
    config: Any = None,
    dry_run: bool = False,
) -> str:
    if isinstance(workspace_root, str):
        workspace_root = Path(workspace_root)

    if not isinstance(path_str, str) or not path_str:
        return "Error: Invalid 'path' parameter for patch_file_ast."
    if not isinstance(target_symbol, str) or not target_symbol.strip():
        return "Error: Invalid or empty 'target_symbol' parameter for patch_file_ast."
    if not isinstance(replacement_code, str):
        return "Error: Invalid 'replacement_code' parameter: must be a string."
    target_symbol = target_symbol.strip()
    # Text-mode reads normalize the file to LF; match that so CRLF input
    # doesn't end up written as "\r\r\n".
    replacement_code = replacement_code.replace("\r\n", "\n")
    try:
        target_path = resolve_and_validate_path(path_str, workspace_root)
    except Exception as e:
        return f"Error: {e}"
    if not target_path.exists() or not target_path.is_file():
        return f"Error: File '{path_str}' does not exist or is not a file."

    try:
        # Strict decode: errors="replace" would corrupt non-UTF-8 bytes on save.
        with open(target_path, "r", encoding="utf-8") as f:
            old_content = f.read()
    except UnicodeDecodeError:
        return f"Error: File '{path_str}' is not valid UTF-8; refusing to patch (would corrupt it)."
    except Exception as e:
        return f"Error reading '{path_str}': {e}"

    old_lines = old_content.splitlines(keepends=True)
    new_lines = None

    if path_str.lower().endswith((".py", ".pyw")):
        try:
            tree = ast.parse(old_content)
        except SyntaxError as e:
            return f"Error: cannot parse '{path_str}' as Python: {e}"
        # 'Class.method' matches the qualified name; a bare name matches any def.
        key = 0 if "." in target_symbol else 1
        found_defs = [
            (qual, node)
            for qual, node in _iter_defs(tree)
            if (qual, node.name)[key] == target_symbol
        ]
        if len(found_defs) > 1:
            names = ", ".join(sorted({q for q, _ in found_defs}))
            return (
                f"Error: symbol '{target_symbol}' matches {len(found_defs)} definitions "
                f"in '{path_str}' ({names}); use a qualified name like 'Class.method'."
            )
        if found_defs:
            node = found_defs[0][1]
            import textwrap as _tw

            start_idx = node.lineno - 1
            # Include decorators with the definition, not just the def line.
            try:
                deco_lines = getattr(node, "decorator_list", []) or []
                if deco_lines:
                    start_idx = min([start_idx] + [d.lineno - 1 for d in deco_lines])
            except Exception:
                pass
            end_idx = getattr(node, "end_lineno", len(old_lines))
            rep_lines = replacement_code.splitlines(keepends=True)

            if rep_lines:
                base_indent = old_lines[start_idx][
                    : len(old_lines[start_idx]) - len(old_lines[start_idx].lstrip())
                ]
                dedented = _tw.dedent("".join(rep_lines)).splitlines(keepends=True)
                rep_lines = [
                    (base_indent + ln) if ln.strip() else "\n" for ln in dedented
                ]

                if not rep_lines[-1].endswith("\n"):
                    rep_lines[-1] += "\n"

            new_lines = old_lines[:start_idx] + rep_lines + old_lines[end_idx:]

    if new_lines is None:
        import re as _re

        # Word-boundary substring fallback only (no blind in-identifier replace).
        pat = _re.compile(r"(?<![\w])" + _re.escape(target_symbol) + r"(?![\w])")
        found = pat.search(old_content)
        if not found:
            return f"Error: Target symbol or block '{target_symbol}' not found in '{path_str}' via AST or substring search."
        if len(pat.findall(old_content)) > 1:
            return (
                f"Error: Target '{target_symbol}' matches multiple locations "
                f"in '{path_str}'; make it unique."
            )
        # lambda: replacement is literal text (backslashes like C:\new stay intact).
        new_content = pat.sub(lambda _m: replacement_code, old_content, count=1)
        new_lines = new_content.splitlines(keepends=True)

    if dry_run:
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path_str} (current)",
                tofile=f"b/{path_str} (proposed ast/symbol patch)",
            )
        )
        return "".join(diff) if diff else "No changes detected."

    new_content_final = "".join(new_lines)
    if path_str.lower().endswith((".py", ".pyw")):
        try:
            ast.parse(new_content_final)
        except SyntaxError as e:
            return f"Error: patched result is invalid Python: {e}. No changes written."

    if config and getattr(config, "auto_backup", True):
        backup_file(target_path, workspace_root, tag="ast")

    try:
        atomic_write_text(target_path, new_content_final, _has_crlf(target_path))
    except OSError as e:
        return f"Error writing '{path_str}': {e}"

    return f"Successfully patched symbol/block '{target_symbol}' in '{path_str}' ({len(new_content_final)} total chars)."
