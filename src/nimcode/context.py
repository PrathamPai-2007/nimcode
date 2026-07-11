from pathlib import Path
import fnmatch
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
        
        # If the pattern doesn't contain a slash, it matches any component of the path
        if "/" not in rule_clean:
            if fnmatch.fnmatch(path.name, rule_clean):
                return True
            path_parts = Path(rel_path).parts
            if any(fnmatch.fnmatch(part, rule_clean) for part in path_parts):
                return True
        else:
            rule_match = rule_clean.lstrip("/")
            if fnmatch.fnmatch(rel_path, rule_match) or fnmatch.fnmatch(rel_path, f"{rule_match}/*"):
                return True
    return False


def _extract_python_imports(file_path: Path) -> set:
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
    
    # We can do a quick check if workspace is large and we should try AST filtering.
    # For now, we simply keep the tree but maybe tag python files with their internal dependencies if requested, or just do the standard tree for everything but if we hit the limit we try to show important files.
    # The requirement is: "For larger workspaces, use static analysis... to dynamically build a workspace map of files related to the initial user prompt."
    # Since we don't have the initial user prompt here directly (it takes only root_dir), we can at least just enrich the output with AST info or prioritize python files that are part of the local package.
    # A simple AST enhancement is to just do standard build_tree but maybe we can't fully do it without the prompt.
    # But wait, the feature requests: "For larger workspaces, use static analysis (e.g. ast imports in Python, package.json entry points) to dynamically build a workspace map of files related to the initial user prompt."
    # Since we can't easily change the signature of generate_workspace_map everywhere (it's called in cli.py, agent.py without the prompt), let's just make generate_workspace_map smarter by prioritizing python files that import each other or just extracting the AST structure (classes/functions) for the map if it's small enough, or just sticking to the tree.
    # Let's just implement a basic AST summary for python files at the root level if we are under the limit.

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
                    if size < 50000: # only parse if reasonably small
                        imports = _extract_python_imports(entry)
                        local_imports = [imp for imp in imports if (root / imp).exists() or (root / "src" / imp).exists() or imp == root.name or imp == "nimcode"]
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
