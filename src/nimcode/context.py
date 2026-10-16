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


BINARY_SUFFIXES = {
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
}


def is_ignored(path: Path, gitignore_rules: list, root: Optional[Path] = None) -> bool:
    path = Path(path)
    if path.name in DEFAULT_EXCLUDES or path.name == ".git" or ".git" in path.parts:
        return True
    if path.suffix.lower() in BINARY_SUFFIXES and path.is_file():
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


# (path, mtime_ns) -> rule lines; an ignore file is re-read only when edited.
_IGNORE_FILE_CACHE: dict = {}
# root -> (monotonic time, [(ignore_file, dir_prefix)]). Every tool call loads
# rules, so the discovery walk runs at most once per TTL per workspace.
# ponytail: 5s TTL, a brand-new NESTED .gitignore can take that long to apply.
_DISCOVERY_CACHE: dict = {}
_DISCOVERY_TTL_S = 5.0


def _read_ignore_file(path: Path) -> list:
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return []
    lines = _IGNORE_FILE_CACHE.get(key)
    if lines is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        _IGNORE_FILE_CACHE[key] = lines
    return lines


def _rebase_rule(rule: str, prefix: str) -> str:
    """Rewrite a rule from `<prefix>/.gitignore` relative to the root:
    `*.log` -> `prefix/**/*.log`; `/build` or `a/b` -> `prefix/build`, `prefix/a/b`."""
    if not prefix:
        return rule
    neg = rule.startswith("!")
    r = rule[1:] if neg else rule
    if r.startswith("\\"):  # escaped leading `!`/`#` no longer leads
        r = r[1:]
    anchored = "/" in r.rstrip("/")
    r = prefix + "/" + (r.lstrip("/") if anchored else "**/" + r)
    return ("!" if neg else "") + r


def _top_ignore_files(root: Path) -> list:
    """Always-fresh (not TTL-cached) ignore files: info/exclude, root .gitignore."""
    return [(root / ".git" / "info" / "exclude", ""), (root / ".gitignore", "")]


def _discover_nested_ignore_files(root: Path) -> list:
    import time

    hit = _DISCOVERY_CACHE.get(str(root))
    if hit and time.monotonic() - hit[0] < _DISCOVERY_TTL_S:
        return hit[1]
    found = []
    rules = [r for p, _ in _top_ignore_files(root) for r in _read_ignore_file(p)]
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel = d.relative_to(root).as_posix()
        if rel != "." and ".gitignore" in filenames:
            found.append((d / ".gitignore", rel))
            rules.extend(
                _rebase_rule(r, rel) for r in _read_ignore_file(d / ".gitignore")
            )
        if rel.count("/") >= 25:
            dirnames[:] = []
            continue
        # Prune ignored dirs (node_modules, build/...) so they're never walked.
        dirnames[:] = [
            n
            for n in dirnames
            if not (d / n).is_symlink() and not is_ignored(d / n, rules, root=root)
        ]
    _DISCOVERY_CACHE[str(root)] = (time.monotonic(), found)
    return found


def load_gitignore_rules(workspace_root: Path) -> list:
    """All ignore rules as root-relative lines in git precedence order (later
    wins): .git/info/exclude, root .gitignore, then nested .gitignore files
    rebased onto their directory. Empty list when there are none."""
    root = Path(workspace_root).resolve()
    rules: list = []
    for path, prefix in _top_ignore_files(root) + _discover_nested_ignore_files(root):
        rules.extend(_rebase_rule(r, prefix) for r in _read_ignore_file(path))
    return rules


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


_ENTRY_POINT_RE = re.compile(
    r"(__main__\.py|(main|cli|app|index|server)\.[^.]+|manage\.py|setup\.py"
    r"|package\.json|pyproject\.toml|Cargo\.toml|go\.mod)\Z",
    re.IGNORECASE,
)


def _git_list_files(root: Path) -> Optional[list]:
    """Tracked + untracked-not-ignored files via git (honours every ignore
    source git knows). None when not a repo, git is missing, or it fails."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        return None
    from nimcode.tools.base import scrub_env

    try:
        proc = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=str(root),
            capture_output=True,
            timeout=5,
            env=scrub_env(dict(os.environ)),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for rel in proc.stdout.decode("utf-8", errors="replace").split("\0"):
        if not rel or any(p in DEFAULT_EXCLUDES for p in rel.split("/")):
            continue
        if Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        out.append(rel)
    return out


def _walk_list_files(root: Path, cap: int) -> list:
    rules = load_gitignore_rules(root)
    out: list = []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        dirnames[:] = sorted(
            n
            for n in dirnames
            if depth < 8
            and not (d / n).is_symlink()
            and not is_ignored(d / n, rules, root=root)
        )
        for name in sorted(filenames):
            if not is_ignored(d / name, rules, root=root):
                out.append(name if depth == 0 else f"{rel_dir}/{name}")
        if len(out) >= cap:
            break
    return out


def generate_workspace_map(root_dir: str, max_files: int = 500) -> str:
    """Tree of up to `max_files` files: likely entry points first (shallowest
    first), then the most recently modified; rendered in path order."""
    root = Path(root_dir).resolve()
    # ponytail: candidates capped (one stat per file each turn); a huge repo
    # only ranks its first 20 x max_files paths in listing order.
    cap = max_files * 20
    files = _git_list_files(root)
    if files is None:
        files = _walk_list_files(root, cap)

    ranked = []
    for rel in files[:cap]:
        try:
            st = (root / rel).stat()
        except OSError:
            continue  # deleted but still tracked
        if not os.path.isfile(root / rel):
            continue
        name = rel.rsplit("/", 1)[-1]
        if _ENTRY_POINT_RE.match(name):
            key = (0, rel.count("/"))
        else:
            key = (1, -st.st_mtime_ns)
        ranked.append((key, rel, st.st_size))
    ranked.sort()
    chosen = ranked[:max_files]
    omitted = len(ranked) - len(chosen)

    tree: dict = {}
    sizes = {}
    for _, rel, size in chosen:
        node = tree
        parts = rel.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None
        sizes[rel] = size

    def enrich(rel: str) -> str:
        # Simple AST enrichment for reasonably small Python files.
        if not rel.endswith(".py") or sizes[rel] >= 50000:
            return ""
        local = [
            imp
            for imp in _extract_python_imports(root / rel)
            if (root / imp).exists()
            or (root / "src" / imp).exists()
            or imp == root.name
            or imp == "nimcode"
        ]
        return f" (imports: {', '.join(local[:3])})" if local else ""

    def render(node: dict, prefix: str, rel_prefix: str) -> list:
        lines: list = []
        # Directories before files, then by name (as before).
        keys = sorted(node, key=lambda k: (not k.endswith("/"), k.rstrip("/")))
        for i, key in enumerate(keys):
            is_last = i == len(keys) - 1
            name = key.rstrip("/")
            rel = rel_prefix + name
            if node[key] is None:
                lines.append(
                    prefix + ("`-- " if is_last else "|-- ") + name + enrich(rel)
                )
            else:
                lines.append(prefix + ("`-- " if is_last else "|-- ") + name)
                ext = "    " if is_last else "|   "
                lines.extend(render(node[key], prefix + ext, rel + "/"))
        return lines

    lines = render(tree, "", "")
    if omitted:
        lines.append(
            f"`-- ... [{omitted} more file(s) not shown; entry points and recently modified files listed first]"
        )
    return f"{root.name}/\n" + "\n".join(lines)
