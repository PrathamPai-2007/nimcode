import sys
from pathlib import Path
import fnmatch
from typing import Optional

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    if path.name in DEFAULT_EXCLUDES or path.name.startswith(".git"):
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

    rel_path = (
        str(path.relative_to(root)).replace("\\", "/")
        if root and hasattr(path, "is_relative_to") and path.is_relative_to(root)
        else path.name
    )

    for rule in gitignore_rules:
        rule_clean = rule.strip().rstrip("/")
        if not rule_clean or rule_clean.startswith("#"):
            continue
        if fnmatch.fnmatch(path.name, rule_clean):
            return True
        if fnmatch.fnmatch(rel_path, rule_clean) or fnmatch.fnmatch(
            rel_path, f"*{rule_clean}*"
        ):
            return True
    return False


def generate_workspace_map(root_dir: str, max_files: int = 500) -> str:
    root = Path(root_dir).resolve()

    gitignore_rules = []
    gitignore_path = root / ".gitignore"
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

    file_counter = [0]

    def build_tree(dir_path: Path, prefix: str = "", depth: int = 0) -> list:
        lines = []
        if depth > 8 or file_counter[0] >= max_files:
            return lines

        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except (PermissionError, OSError):
            return lines

        entries = [e for e in entries if not is_ignored(e, gitignore_rules, root=root)]

        for i, entry in enumerate(entries):
            if file_counter[0] >= max_files:
                lines.append(prefix + "└── ... [Tree truncated - max files reached]")
                break

            is_last = i == (len(entries) - 1)
            pointer = "└── " if is_last else "├── "
            lines.append(prefix + pointer + entry.name)
            file_counter[0] += 1

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                lines.extend(build_tree(entry, prefix + extension, depth + 1))
        return lines

    tree = build_tree(root)
    return f"{root.name}/\n" + "\n".join(tree)
