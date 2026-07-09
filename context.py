from pathlib import Path

DEFAULT_EXCLUDES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
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
}


def is_ignored(path: Path, gitignore_rules: list) -> bool:
    if path.name in DEFAULT_EXCLUDES:
        return True
    if path.is_file() and path.suffix in {
        ".exe",
        ".dll",
        ".so",
        ".pyc",
        ".bin",
        ".pdf",
        ".png",
        ".jpg",
    }:
        return True
    # Basic gitignore rule matching (simple substring for now)
    for rule in gitignore_rules:
        if rule in str(path):
            return True
    return False


def generate_workspace_map(root_dir: str) -> str:
    root = Path(root_dir).resolve()

    gitignore_rules = []
    gitignore_path = root / ".gitignore"
    if gitignore_path.exists():
        with open(gitignore_path, "r", encoding="utf-8") as f:
            gitignore_rules = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]

    def build_tree(dir_path: Path, prefix: str = "") -> list:
        lines = []
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return lines

        entries = [e for e in entries if not is_ignored(e, gitignore_rules)]

        for i, entry in enumerate(entries):
            is_last = i == (len(entries) - 1)
            pointer = "└── " if is_last else "├── "
            lines.append(prefix + pointer + entry.name)

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                lines.extend(build_tree(entry, prefix + extension))
        return lines

    tree = build_tree(root)
    return f"{root.name}/\n" + "\n".join(tree)
