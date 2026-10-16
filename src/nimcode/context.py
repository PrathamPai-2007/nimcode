from pathlib import Path
import functools
import os
import re
from typing import Optional


DEFAULT_EXCLUDES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".nimcode",
    ".nimcode_backups",
    ".nimcode_sessions",
    ".nimcode_session.json",
    ".pytest_cache",
    ".mypy_cache",
}


def is_ignored(path: Path, gitignore_rules: list, root: Optional[Path] = None) -> bool:
    path = Path(path)
    if path.name in DEFAULT_EXCLUDES or path.name == ".git" or ".git" in path.parts:
        return True
    if path.is_file() and path.suffix.lower() in {
        ".exe",
        ".dll",
        ".so",
        ".pyc",
        ".pyo",
        ".bin",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".whl",
    }:
        return True

    rel_path = path.name
    if root:
        root = Path(root)
        # Lexical fast path (no syscalls) — build_tree passes entries already
        # under the resolved root; resolve() per entry was slow on big repos.
        try:
            rel = path.relative_to(root)
            if ".." in rel.parts:
                raise ValueError
        except ValueError:
            try:
                rel = path.resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                rel = None
        if rel is not None and rel.parts:
            rel_path = rel.as_posix()

    # gitignore semantics: last matching rule wins; `!` re-includes; and a
    # file inside an ignored directory can never be re-included, so each
    # ancestor is evaluated first.
    parts = rel_path.split("/")
    compiled = [c for c in map(_compile_rule, gitignore_rules) if c]
    if not compiled:
        return False
    path_is_dir: Optional[bool] = None
    for depth in range(1, len(parts) + 1):
        sub = "/".join(parts[:depth])
        ignored = False
        for negate, dir_only, rx in compiled:
            if dir_only and depth == len(parts):
                if path_is_dir is None:
                    path_is_dir = path.is_dir()
                if not path_is_dir:
                    continue
            if rx.match(sub):
                ignored = not negate
        if ignored:
            return True
    return False


@functools.lru_cache(maxsize=2048)
def _compile_rule(rule: str):
    """Translate one .gitignore line to (negate, dir_only, regex) or None.

    Unlike fnmatch, `*` and `?` never cross `/`, `**` spans directories, a
    pattern with a slash is anchored to the root, and a trailing `/` matches
    directories only. (`build/**` used to match the entire workspace.)
    """
    r = rule.strip()
    if not r or r.startswith("#"):
        return None
    negate = r.startswith("!")
    if negate:
        r = r[1:]
    if r.startswith("\\"):  # escaped leading `!` or `#`
        r = r[1:]
    dir_only = r.endswith("/")
    r = r.rstrip("/")
    if not r:
        return None
    anchored = "/" in r
    r = r.lstrip("/")
    out = []
    i = 0
    while i < len(r):
        if r.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif r.startswith("**", i):
            out.append(".*")
            i += 2
        elif r[i] == "*":
            out.append("[^/]*")
            i += 1
        elif r[i] == "?":
            out.append("[^/]")
            i += 1
        elif r[i] == "[" and "]" in r[i + 2 :]:
            j = r.index("]", i + 2)
            body = r[i + 1 : j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(r[i]))
            i += 1
    pattern = "".join(out) + r"\Z"
    if not anchored:
        pattern = "(?:.*/)?" + pattern
    # Windows/macOS filesystems are case-insensitive (fnmatch behaved so too).
    flags = re.IGNORECASE if os.name == "nt" else 0
    try:
        return negate, dir_only, re.compile(pattern, flags)
    except re.error:
        return None


def load_gitignore_rules(workspace_root: Path) -> list:
    """Read .gitignore into rule lines. Empty list when missing/unreadable."""
    gitignore_path = Path(workspace_root) / ".gitignore"
    if not gitignore_path.exists():
        return []
    try:
        with open(gitignore_path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except Exception:
        return []


# (path, mtime_ns, size) -> imports. The map is rebuilt every agent turn, so
# re-parsing up to 500 files each turn was pure waste.
# ponytail: unbounded dict, bounded in practice by max_files per workspace.
_IMPORTS_CACHE: dict = {}


def _extract_python_imports(file_path: Path) -> set:
    try:
        st = file_path.stat()
        key = (str(file_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return set()
    cached = _IMPORTS_CACHE.get(key)
    if cached is None:
        cached = _IMPORTS_CACHE[key] = _parse_python_imports(file_path)
    return cached


def _parse_python_imports(file_path: Path) -> set:
    import ast

    imports = set()
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    imports.add(name.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
    except Exception:
        pass
    return imports


def generate_workspace_map(root_dir: str, max_files: int = 500) -> str:
    root = Path(root_dir).resolve()

    gitignore_rules = load_gitignore_rules(root)
    file_counter = [0]

    def build_tree(dir_path: Path, prefix: str = "", depth: int = 0) -> list:
        lines: list[str] = []
        if depth > 8 or file_counter[0] >= max_files:
            return lines

        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except (PermissionError, OSError):
            return lines

        entries = [e for e in entries if not is_ignored(e, gitignore_rules, root=root)]

        for i, entry in enumerate(entries):
            if file_counter[0] >= max_files:
                lines.append(prefix + "`-- ... [Tree truncated - max files reached]")
                break

            is_last = i == (len(entries) - 1)
            pointer = "`-- " if is_last else "|-- "

            # Simple AST enrichment for Python files
            extra_info = ""
            if entry.is_file() and entry.suffix == ".py":
                try:
                    size = entry.stat().st_size
                    if size < 50000:  # only parse if reasonably small
                        imports = _extract_python_imports(entry)
                        local_imports = [
                            imp
                            for imp in imports
                            if (root / imp).exists()
                            or (root / "src" / imp).exists()
                            or imp == root.name
                            or imp == "nimcode"
                        ]
                        if local_imports:
                            extra_info = f" (imports: {', '.join(local_imports[:3])})"
                except Exception:
                    pass

            lines.append(prefix + pointer + entry.name + extra_info)
            file_counter[0] += 1

            if entry.is_dir():
                extension = "    " if is_last else "|   "
                lines.extend(build_tree(entry, prefix + extension, depth + 1))
        return lines

    tree = build_tree(root)
    return f"{root.name}/\n" + "\n".join(tree)
