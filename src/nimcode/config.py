import os
import sys
import tomli
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def get_project_nimcode_dir(workspace_root: Any) -> Path:
    """
    Returns the centralized global project directory:
    ~/.nimcode/<project-folder-name>_<path-hash>/
    Also handles one-time automatic migration from legacy global and local .nimcode/ if present.
    """
    if not isinstance(workspace_root, Path):
        workspace_root = Path(str(workspace_root))
    resolved = workspace_root.resolve()

    import hashlib

    path_hash = hashlib.md5(
        str(resolved).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]
    global_root = (Path.home() / ".nimcode").resolve()
    project_dir = global_root / f"{resolved.name}_{path_hash}"

    # One-time migration from legacy un-hashed global directory if it exists.
    # Guard: a drive root has an empty name (legacy dir == ~/.nimcode itself)
    # and global entries like skills/ must never be swallowed by a project.
    legacy_global = global_root / resolved.name
    if (
        resolved.name
        and resolved.name not in _GLOBAL_RESERVED
        and legacy_global.is_dir()
        and legacy_global != project_dir
    ):
        _migrate_dir(legacy_global, project_dir)

    project_dir.mkdir(parents=True, exist_ok=True)

    # Migrate local .nimcode/ if it exists inside workspace_root. When the
    # workspace is $HOME, <root>/.nimcode IS the global dir — never migrate it.
    local_nimcode = resolved / ".nimcode"
    if local_nimcode.is_dir() and local_nimcode.resolve() != global_root:
        _migrate_dir(local_nimcode, project_dir)

    return project_dir


# Entries of ~/.nimcode that are global, not legacy per-project dirs.
_GLOBAL_RESERVED = {"skills", "models_cache.json", ".env"}


def _migrate_dir(src: Path, dst: Path) -> None:
    """Move src's files into dst without overwriting newer files in dst.

    Only deletes src when every file made it across, so a partial failure
    never loses data and a retry never clobbers newer state with stale copies.
    """
    import shutil

    ok = True
    for dirpath, _dirs, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        for name in files:
            target = dst / rel / name
            if target.exists():
                continue  # dst already has newer state; keep it
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(dirpath) / name, target)
            except Exception:
                ok = False
    if ok:
        shutil.rmtree(src, ignore_errors=True)


@dataclass
class Config:
    fast_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    plan_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    mode: str = "build"
    temperature_fast: float = 0.2
    temperature_plan: float = 0.2
    max_loop_iterations: int = 20
    workspace_root: str = "."
    timeout: int = 60
    output_cap: int = 100000
    context_budget_tokens: int = 128000
    max_tokens: int = 4096
    auto_backup: bool = True
    denylist: List[str] = field(default_factory=list)
    pending_summary_injection: bool = False
    pending_summary_old_model: str = ""
    pending_summary_new_model: str = ""
    disable_native_tools: bool = False
    # Docker sandbox configuration
    docker_image: str = "python:3.11-slim"
    docker_expose_ports: List[int] = field(default_factory=list)
    docker_extra_args: List[str] = field(default_factory=list)
    disable_docker: bool = False
    api_base_url: Optional[str] = None


def save_last_used_model(
    config: Config, model_id: str, workspace_root: Optional[Path] = None
):
    import json
    import time

    try:
        root = (
            workspace_root if workspace_root else Path(config.workspace_root).resolve()
        )
        nimcode_dir = get_project_nimcode_dir(root)
        model_file = nimcode_dir / "model.json"
        data = {
            "model": model_id,
            "fast_model": model_id,
            "plan_model": model_id,
            "updated_at": time.time(),
            "timestamp": time.time(),
            "docker_image": config.docker_image,
            "docker_expose_ports": config.docker_expose_ports,
            "docker_extra_args": config.docker_extra_args,
            "disable_docker": config.disable_docker,
        }
        # Atomic temp+replace so a crash mid-write can't corrupt model.json.
        tmp = model_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(model_file)
    except Exception:
        pass


def load_last_used_model(
    workspace_root: Optional[Path] = None,
) -> Optional[dict]:
    import json

    try:
        root = workspace_root if workspace_root else Path.cwd().resolve()
        nimcode_dir = get_project_nimcode_dir(root)
        model_file = nimcode_dir / "model.json"
        if model_file.exists():
            with open(model_file, "r", encoding="utf-8") as mf:
                return json.load(mf)
    except Exception:
        pass
    return None


def save_config(config: Config, workspace_root: Optional[Path] = None):
    save_last_used_model(config, config.fast_model, workspace_root)
    # Persist scalar settings to nimcode.toml so interactive /config edits survive.
    import json as _json

    def _num(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    try:
        root = (
            workspace_root
            if workspace_root is not None
            else Path(config.workspace_root).resolve()
            if getattr(config, "workspace_root", None)
            else Path.cwd()
        )
        toml_path = Path(root) / "nimcode.toml"
        try:
            ports = [int(p) for p in (config.docker_expose_ports or [])]
        except (TypeError, ValueError):
            ports = []
        # JSON string escaping is valid TOML basic-string syntax; raw f-string
        # quoting broke the whole file on any `"` or `\` (e.g. Windows paths).
        q = _json.dumps
        lines = [
            f"mode = {q(str(config.mode))}",
            f"fast_model = {q(str(config.fast_model))}",
            f"plan_model = {q(str(config.plan_model))}",
            f"max_loop_iterations = {_num(config.max_loop_iterations, 20)}",
            f"timeout = {_num(config.timeout, 60)}",
            f"output_cap = {_num(config.output_cap, 100000)}",
            f"context_budget_tokens = {_num(config.context_budget_tokens, 128000)}",
            f"max_tokens = {_num(config.max_tokens, 4096)}",
            f"temperature_fast = {_float(config.temperature_fast, 0.2)}",
            f"temperature_plan = {_float(config.temperature_plan, 0.2)}",
            f"auto_backup = {'true' if config.auto_backup else 'false'}",
            f"disable_native_tools = {'true' if config.disable_native_tools else 'false'}",
            f"disable_docker = {'true' if config.disable_docker else 'false'}",
            f"docker_image = {q(str(config.docker_image))}",
            f"docker_expose_ports = [{', '.join(str(p) for p in ports)}]",
            f"docker_extra_args = {_json.dumps(list(config.docker_extra_args or []))}",
            f"denylist = {_json.dumps(list(config.denylist or []))}",
        ]
        if getattr(config, "api_base_url", None):
            lines.append(f"api_base_url = {q(str(config.api_base_url))}")
        tmp = toml_path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(toml_path)
    except Exception:
        pass


def _validate_config(cfg: Config) -> None:
    """Validate config values and warn about issues.

    Coerces first: a malformed nimcode.toml (e.g. timeout = "60") warns and
    falls back to defaults instead of crashing startup with TypeError.
    """
    import warnings

    def _coerce(value, cast, default):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    # Validate temperature ranges
    cfg.temperature_fast = _coerce(cfg.temperature_fast, float, 0.2)
    if not 0.0 <= cfg.temperature_fast <= 2.0:
        warnings.warn(
            f"temperature_fast should be between 0.0 and 2.0, got {cfg.temperature_fast}"
        )
        cfg.temperature_fast = max(0.0, min(2.0, cfg.temperature_fast))

    cfg.temperature_plan = _coerce(cfg.temperature_plan, float, 0.2)
    if not 0.0 <= cfg.temperature_plan <= 2.0:
        warnings.warn(
            f"temperature_plan should be between 0.0 and 2.0, got {cfg.temperature_plan}"
        )
        cfg.temperature_plan = max(0.0, min(2.0, cfg.temperature_plan))

    # Validate timeout
    cfg.timeout = _coerce(cfg.timeout, int, 60)
    if cfg.timeout <= 0:
        warnings.warn(f"timeout must be positive, got {cfg.timeout}. Using default 60.")
        cfg.timeout = 60

    # Validate output_cap
    cfg.output_cap = _coerce(cfg.output_cap, int, 100000)
    if cfg.output_cap <= 0:
        warnings.warn(
            f"output_cap must be positive, got {cfg.output_cap}. Using default 100000."
        )
        cfg.output_cap = 100000

    # Validate context_budget_tokens
    cfg.context_budget_tokens = _coerce(cfg.context_budget_tokens, int, 128000)
    if cfg.context_budget_tokens <= 1000:
        warnings.warn(
            f"context_budget_tokens too small ({cfg.context_budget_tokens}), minimum 1000. Using 128000."
        )
        cfg.context_budget_tokens = 128000

    # Validate max_tokens
    cfg.max_tokens = _coerce(cfg.max_tokens, int, 4096)
    if cfg.max_tokens <= 0:
        warnings.warn(
            f"max_tokens must be positive, got {cfg.max_tokens}. Using default 4096."
        )
        cfg.max_tokens = 4096

    # Validate max_loop_iterations
    cfg.max_loop_iterations = _coerce(cfg.max_loop_iterations, int, 20)
    if cfg.max_loop_iterations <= 0:
        warnings.warn(
            f"max_loop_iterations must be positive, got {cfg.max_loop_iterations}. Using default 20."
        )
        cfg.max_loop_iterations = 20

    # Validate mode
    valid_modes = ["plan", "build", "yolo"]
    if cfg.mode not in valid_modes:
        warnings.warn(
            f"Invalid mode '{cfg.mode}', must be one of {valid_modes}. Using 'build'."
        )
        cfg.mode = "build"

    # Validate docker_expose_ports: coerce single values, filter invalid entries.
    ports = cfg.docker_expose_ports
    if isinstance(ports, bool) or ports is None:
        ports = []
    elif isinstance(ports, (str, int, float)):
        try:
            ports = [int(ports)]
        except (TypeError, ValueError):
            ports = []
    elif not isinstance(ports, (list, tuple)):
        warnings.warn("docker_expose_ports must be a list of ints. Using [].")
        ports = []
    if ports:
        valid_ports = []
        for p in ports:
            try:
                p = int(p)
            except (TypeError, ValueError):
                continue
            if isinstance(p, bool) or not 1 <= p <= 65535:
                continue
            valid_ports.append(p)
        if len(valid_ports) != len(list(ports)):
            warnings.warn(
                "Dropping invalid entries in docker_expose_ports. Must be ints 1-65535."
            )
        cfg.docker_expose_ports = valid_ports

    # Bools: a TOML/JSON string "false" is truthy, so parse it explicitly.
    for name in ("auto_backup", "disable_native_tools", "disable_docker"):
        val = getattr(cfg, name)
        if isinstance(val, str):
            setattr(cfg, name, val.strip().lower() in ("1", "true", "yes", "on"))
        elif not isinstance(val, bool):
            setattr(cfg, name, bool(val))

    # String lists: a bare string would be iterated char-by-char as docker args.
    for name in ("denylist", "docker_extra_args"):
        val = getattr(cfg, name)
        if isinstance(val, str):
            val = [val]
        elif not isinstance(val, (list, tuple)):
            val = []
        setattr(cfg, name, [str(v) for v in val if v is not None])

    # Validate docker_image
    if not cfg.docker_image or not isinstance(cfg.docker_image, str):
        warnings.warn(
            "docker_image must be a non-empty string. Using default 'python:3.11-slim'."
        )
        cfg.docker_image = "python:3.11-slim"


def load_config(path: Optional[Any] = None) -> Config:
    import dataclasses
    import json

    if path is not None:
        p = Path(path)
        if p.is_dir():
            config_path = p / "nimcode.toml"
            workspace_root = p
        else:
            config_path = p
            workspace_root = p.parent
    else:
        config_path = Path("nimcode.toml")
        workspace_root = Path.cwd()

    if not config_path.exists():
        cfg = Config()
    else:
        try:
            with open(config_path, "rb") as f:
                data = tomli.load(f)
        except Exception:
            data = {}
        # Filter to known Config fields so unknown TOML keys can't crash startup.
        try:
            known = {f.name for f in dataclasses.fields(Config)}
            filtered = {k: v for k, v in data.items() if k in known}
        except Exception:
            filtered = {}
        try:
            cfg = Config(**filtered)
        except TypeError:
            cfg = Config()

    cfg.workspace_root = str(workspace_root.resolve())

    # Check if a persisted model exists for this project in .nimcode/model.json
    # Specific fast/plan values win over the legacy generic "model" key.
    try:
        model_json_path = get_project_nimcode_dir(workspace_root) / "model.json"
        if model_json_path.exists():
            with open(model_json_path, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)
                if not isinstance(mdata, dict):
                    mdata = {}
                if isinstance(mdata.get("model"), str) and mdata["model"]:
                    cfg.fast_model = mdata["model"]
                    cfg.plan_model = mdata["model"]
                if isinstance(mdata.get("fast_model"), str) and mdata["fast_model"]:
                    cfg.fast_model = mdata["fast_model"]
                if isinstance(mdata.get("plan_model"), str) and mdata["plan_model"]:
                    cfg.plan_model = mdata["plan_model"]
                if "docker_image" in mdata:
                    cfg.docker_image = mdata["docker_image"]
                if "docker_expose_ports" in mdata:
                    cfg.docker_expose_ports = mdata["docker_expose_ports"]
                if "docker_extra_args" in mdata:
                    cfg.docker_extra_args = mdata["docker_extra_args"]
                if "disable_docker" in mdata:
                    cfg.disable_docker = mdata["disable_docker"]
    except Exception:
        pass

    # Validate after the model.json merge so its values are coerced too.
    _validate_config(cfg)
    return cfg


def _format_backup_time(backup_name: str) -> tuple:
    """Split '<ts>_<safe>' backup names into (display_time, original_hint).

    Handles both second and microsecond timestamps. Never raises.
    """
    import time

    parts = backup_name.split("_", 1)
    original = parts[1] if len(parts) > 1 else backup_name
    # backup_file() names are "<ts>_<pid>_<tag_><safe>": drop the pid segment.
    pid, sep, rest = original.partition("_")
    if sep and pid.isdigit() and rest:
        original = rest
    try:
        ts = int(parts[0])
        if ts > 9999999999:  # microsecond timestamps from backup_file()
            ts //= 1_000_000
        display = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        display = "Unknown Time"
    return display, original


def show_changes(workspace_root: Path, tui: Optional[Any] = None):
    from rich.console import Console
    from rich.table import Table

    root = workspace_root.resolve()
    nimcode_dir = get_project_nimcode_dir(root)
    backup_dir = nimcode_dir / "backups"

    git_status = ""
    try:
        import subprocess

        res = subprocess.run(
            ["git", "status", "-s"], cwd=str(root), capture_output=True, text=True
        )
        if res.returncode == 0 and res.stdout.strip():
            git_status = res.stdout.strip()
    except Exception:
        pass

    if tui is not None:
        lines = [
            f"### Workspace Change & Backup Tracker (`{root}`)",
            "",
        ]
        if git_status:
            lines.append("**Git Working Tree Changes:**")
            lines.append("```")
            lines.append(git_status)
            lines.append("```\n")
        else:
            lines.append("*No uncommitted git working tree changes detected.*\n")

        if backup_dir.exists() and any(backup_dir.iterdir()):
            lines.append("**Recent File Backups (.nimcode/backups/):**")
            lines.append("| Backup Timestamp / File | Original Path / Target |")
            lines.append("| --- | --- |")
            backups = sorted(
                [f for f in backup_dir.iterdir() if f.is_file()],
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )[:15]
            for b in backups:
                ts_str, original_name = _format_backup_time(b.name)
                lines.append(f"| {ts_str} | {original_name} |")
        else:
            lines.append("*No backup files stored in .nimcode/backups/ yet.*")

        tui.add_message("system", "\n".join(lines), markdown=True)
        return

    console = Console()
    console.print(
        f"[bold cyan]Workspace Change & Backup Tracker ([/bold cyan][dim]{root}[/dim][bold cyan]):[/bold cyan]"
    )

    if git_status:
        console.print("[bold yellow]Git Working Tree Changes:[/bold yellow]")
        console.print(f"[dim]{git_status}[/dim]\n")
    else:
        console.print("[dim]No uncommitted git working tree changes detected.[/dim]\n")

    if backup_dir.exists() and any(backup_dir.iterdir()):
        table = Table(
            title="Recent File Backups (.nimcode/backups/)", border_style="cyan"
        )
        table.add_column("Backup Timestamp / File", style="bold green")
        table.add_column("Original Path / Target", style="cyan")
        backups = sorted(
            [f for f in backup_dir.iterdir() if f.is_file()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )[:15]
        for b in backups:
            # Shared parser: microsecond timestamps crashed time.localtime().
            ts_str, original_name = _format_backup_time(b.name)
            table.add_row(f"{ts_str}", original_name)
        console.print(table)
    else:
        console.print("[dim]No backup files stored in .nimcode/backups/ yet.[/dim]")
    console.print()


def restore_interactive_backup(workspace_root: Path):
    from rich.console import Console
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    import typer
    import shutil

    console = Console()
    root = workspace_root.resolve()
    nimcode_dir = get_project_nimcode_dir(root)
    backup_dir = nimcode_dir / "backups"

    if not backup_dir.exists() or not any(backup_dir.iterdir()):
        console.print("[yellow]No backups found in .nimcode/backups/.[/yellow]\n")
        return

    backups = sorted(
        [f for f in backup_dir.iterdir() if f.is_file()],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    current_index = 0
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        nonlocal current_index
        current_index = max(0, current_index - 1)

    @kb.add("down")
    def _(event):
        nonlocal current_index
        current_index = min(len(backups) - 1, current_index + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=backups[current_index])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    def get_text():
        lines = [("bold", "Select a Backup to Restore\n\n")]
        for i, b in enumerate(backups):
            ts_str, original_name = _format_backup_time(b.name)

            if i == current_index:
                prefix = "> "
                style = "bold #00ffff"
            else:
                prefix = "  "
                style = ""
            lines.append((style, f"{prefix}[{ts_str}] {original_name}\n"))
        lines.append(("", "\nKeyboard: ↑/↓ Navigate  enter Select  esc Cancel\n"))
        return lines

    layout = Layout(Window(content=FormattedTextControl(text=get_text)))
    app: Application = Application(layout=layout, key_bindings=kb, erase_when_done=True)

    selected_backup = app.run()
    if not selected_backup:
        return

    _, guess_name = _format_backup_time(selected_backup.name)
    if guess_name.startswith("blocks_"):
        guess_name = guess_name[len("blocks_") :]
    if guess_name.startswith("ast_"):
        guess_name = guess_name[len("ast_") :]

    # Try to find the original file in workspace by matching the backup name pattern
    # The backup name has all path separators replaced with underscores
    # We try to match it against actual files in the workspace
    from nimcode.context import DEFAULT_EXCLUDES

    guess_path = None
    # Prune node_modules/.git/venvs: a bare rglob walked them all on big repos.
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDES]
        for name in files:
            rel = (Path(dirpath) / name).relative_to(root)
            if str(rel).replace(os.sep, "_").replace("/", "_") == guess_name:
                guess_path = str(rel)
                break
        if guess_path:
            break
    if guess_path is None:
        # Fallback: replace underscores with path separators (may be wrong for files with underscores)
        guess_path = guess_name.replace("_", "/")

    target_rel = typer.prompt(
        "Enter target path to restore to (relative to workspace)", default=guess_path
    )
    target_path = (root / target_rel).resolve()

    if not target_path.is_relative_to(root):
        console.print("[red]Error: Target path is outside workspace.[/red]\n")
        return

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(selected_backup, target_path)
        console.print(
            f"[bold green]Successfully restored backup to {target_rel}[/bold green]\n"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to restore backup: {e}[/bold red]\n")


def configure_interactive(config: Config):
    from rich.console import Console
    from rich.table import Table
    import typer

    console = Console()
    table = Table(title="Current Nimcode Configuration", border_style="cyan")
    table.add_column("Setting Key", style="bold cyan")
    table.add_column("Current Value", style="bold green")
    table.add_column("Description", style="dim")

    table.add_row(
        "1. output_cap",
        str(config.output_cap),
        "Max characters returned by tool outputs",
    )
    table.add_row(
        "2. timeout",
        str(config.timeout),
        "Seconds before long-running commands timeout",
    )
    table.add_row(
        "3. auto_backup",
        str(config.auto_backup),
        "Automatically backup files before overwriting",
    )
    table.add_row(
        "4. max_loop_iterations",
        str(config.max_loop_iterations),
        "Max consecutive AI loop steps without user prompt",
    )
    table.add_row(
        "5. temperature_fast",
        str(config.temperature_fast),
        "Temperature for fast/build model generation",
    )
    table.add_row(
        "6. temperature_plan",
        str(config.temperature_plan),
        "Temperature for plan model generation",
    )
    table.add_row(
        "7. disable_docker",
        str(config.disable_docker),
        "Disable Docker and always execute shell commands on host",
    )
    table.add_row(
        "8. docker_image",
        str(config.docker_image),
        "Docker image to use for command sandboxing",
    )

    console.print(table)
    choice = (
        typer.prompt(
            "Enter setting number to modify (1-8) or press Enter/q to exit",
            default="q",
        )
        .strip()
        .lower()
    )
    if choice == "q" or not choice:
        return

    if choice == "1":
        val = typer.prompt(
            "Enter new output_cap (e.g. 2000, 5000)", default=str(config.output_cap)
        )
        try:
            config.output_cap = int(val)
            console.print(f"[green]output_cap updated to {config.output_cap}[/green]\n")
        except ValueError:
            console.print("[red]Invalid integer.[/red]\n")
    elif choice == "2":
        val = typer.prompt(
            "Enter new timeout in seconds (e.g. 30, 60, 120)",
            default=str(config.timeout),
        )
        try:
            config.timeout = int(val)
            console.print(f"[green]timeout updated to {config.timeout}[/green]\n")
        except ValueError:
            console.print("[red]Invalid integer.[/red]\n")
    elif choice == "3":
        val = (
            typer.prompt(
                "Enable auto_backup? (y/n)",
                default="y" if config.auto_backup else "n",
            )
            .strip()
            .lower()
        )
        config.auto_backup = val == "y"
        console.print(f"[green]auto_backup updated to {config.auto_backup}[/green]\n")
    elif choice == "4":
        val = typer.prompt(
            "Enter max_loop_iterations (e.g. 10, 20)",
            default=str(config.max_loop_iterations),
        )
        try:
            config.max_loop_iterations = int(val)
            console.print(
                f"[green]max_loop_iterations updated to {config.max_loop_iterations}[/green]\n"
            )
        except ValueError:
            console.print("[red]Invalid integer.[/red]\n")
    elif choice == "5":
        val = typer.prompt(
            "Enter temperature_fast (0.0 to 1.0)",
            default=str(config.temperature_fast),
        )
        try:
            config.temperature_fast = float(val)
            console.print(
                f"[green]temperature_fast updated to {config.temperature_fast}[/green]\n"
            )
        except ValueError:
            console.print("[red]Invalid float.[/red]\n")
    elif choice == "6":
        val = typer.prompt(
            "Enter temperature_plan (0.0 to 1.0)",
            default=str(config.temperature_plan),
        )
        try:
            config.temperature_plan = float(val)
            console.print(
                f"[green]temperature_plan updated to {config.temperature_plan}[/green]\n"
            )
        except ValueError:
            console.print("[red]Invalid float.[/red]\n")
    elif choice == "7":
        val = (
            typer.prompt(
                "Disable Docker? (y/n)",
                default="y" if config.disable_docker else "n",
            )
            .strip()
            .lower()
        )
        config.disable_docker = val == "y"
        console.print(
            f"[green]disable_docker updated to {config.disable_docker}[/green]\n"
        )
    elif choice == "8":
        val = typer.prompt(
            "Enter Docker image name",
            default=str(config.docker_image),
        ).strip()
        if val:
            config.docker_image = val
            console.print(
                f"[green]docker_image updated to {config.docker_image}[/green]\n"
            )
        else:
            console.print("[red]Invalid empty string.[/red]\n")

    # Clamp bad input (e.g. timeout=-5) before it is persisted and used.
    _validate_config(config)
    save_config(config)


AVAILABLE_MODELS = {
    "1": {"name": "Nemotron 3 Nano 30B", "id": "nvidia/nemotron-3-nano-30b-a3b"},
    "2": {"name": "DeepSeek V4-Flash", "id": "deepseek-ai/deepseek-v4-flash"},
    "3": {
        "name": "Mixtral 8x7B Instruct",
        "id": "mistralai/mixtral-8x7b-instruct-v0.1",
    },
    "4": {"name": "Mistral Small", "id": "mistralai/mistral-small-4-119b-2603"},
    "5": {"name": "Nemotron 3 Super 120B", "id": "nvidia/nemotron-3-super-120b-a12b"},
    "6": {"name": "GLM-5.2", "id": "z-ai/glm-5.2"},
    "7": {"name": "MiniMax-M3", "id": "minimaxai/minimax-m3"},
    "8": {
        "name": "Mistral Large",
        "id": "mistralai/mistral-large-3-675b-instruct-2512",
    },
    "9": {"name": "DeepSeek V4-Pro", "id": "deepseek-ai/deepseek-v4-pro"},
    "10": {"name": "Nemotron 3 Ultra 550B", "id": "nvidia/nemotron-3-ultra-550b-a55b"},
}


def select_interactive_model(
    config: Config,
    client: Optional[Any] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    workspace_root: Optional[Path] = None,
    session_path: Optional[Path] = None,
):
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    import typer
    from rich.console import Console

    console = Console()
    try:
        from nimcode.config_models import get_model_catalog

        model_list = get_model_catalog()
    except Exception:
        model_list = list(AVAILABLE_MODELS.values())
    if not model_list:
        model_list = list(AVAILABLE_MODELS.values())
    current_model_id = config.fast_model

    current_index = 0
    for i, m in enumerate(model_list):
        if m["id"] == current_model_id:
            current_index = i
            break

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        nonlocal current_index
        current_index = max(0, current_index - 1)

    @kb.add("down")
    def _(event):
        nonlocal current_index
        current_index = min(len(model_list) - 1, current_index + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=model_list[current_index]["id"])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    def get_text():
        lines = [("bold", "Switch Model\n\n")]
        for i, m in enumerate(model_list):
            is_curr = m["id"] == current_model_id
            if i == current_index:
                prefix = "> "
                style = "bold #00ffff"
            else:
                prefix = "  "
                style = ""

            suffix = "         (current)" if is_curr else ""
            lines.append((style, f"{prefix}{m['name']}{suffix}\n"))

        lines.append(("", "\nKeyboard: ↑/↓ Navigate  enter Select  esc Go Back\n"))
        return lines

    layout = Layout(Window(content=FormattedTextControl(text=get_text)))
    app: Application = Application(layout=layout, key_bindings=kb, erase_when_done=True)

    try:
        selection_id = app.run()
    except Exception:
        selection_id = None

    if selection_id is None:
        typer.secho("\nModel selection cancelled.", fg=typer.colors.YELLOW)
        return

    selected_model = None
    for m in model_list:
        if m["id"] == selection_id:
            selected_model = m
            break

    if selected_model:
        old_model = config.fast_model
        new_model_id = selected_model["id"]
        save_last_used_model(config, new_model_id, workspace_root)
        try:
            from nimcode.config_models import apply_model_context

            changed, ctx_msg = apply_model_context(config, new_model_id, force=True)
            if changed:
                console.print(f"[cyan]Note: {ctx_msg}[/cyan]")
        except Exception:
            pass

        # If there is prior user prompt history and we have an active client, generate or reuse summary.md
        if (
            client is not None
            and messages is not None
            and len([m for m in messages if m.get("role") == "user"]) >= 1
        ):
            try:
                from nimcode.core.agent import call_nim_with_retry, log_turn
                from nimcode.core.prompts import strip_reasoning_spans

                summary_file = (
                    get_project_nimcode_dir(workspace_root) / "summary.md"
                    if workspace_root
                    else None
                )
                last_user_msg = next(
                    (m for m in reversed(messages) if m.get("role") == "user"), None
                )

                # Do not regenerate if the user hasn't prompted again since the last model switch
                should_regenerate = True
                if (
                    summary_file
                    and summary_file.exists()
                    and last_user_msg
                    and "<model_handoff_context>"
                    in str(last_user_msg.get("content", ""))
                ):
                    should_regenerate = False

                if should_regenerate:
                    console.print(
                        f"\n[bold cyan]⚡ Prior prompt detected. Asking previous model ({old_model}) to generate summary.md for context handoff to {selected_model['name']}...[/bold cyan]"
                    )
                    summary_prompt = [
                        {
                            "role": "user",
                            "content": "Please generate a high-quality, accurate summary of our conversation, progress, current state of the workspace, and any key decisions made so far. Format the output clearly in markdown suitable for a summary.md handoff document to a new AI model taking over the task.",
                        }
                    ]
                    summary_req_messages = list(messages) + summary_prompt

                    summary_res = call_nim_with_retry(
                        client,
                        summary_req_messages,
                        None,
                        old_model,
                        config,
                        timeout=300.0,
                    )
                    summary_text = strip_reasoning_spans(
                        summary_res.choices[0].message.content or ""
                    ).strip()

                    if not summary_text:
                        summary_text = "# Summary\nPrevious session context handoff."

                    if summary_file:
                        summary_file.parent.mkdir(parents=True, exist_ok=True)
                        summary_file.write_text(summary_text, encoding="utf-8")
                        console.print(
                            f"[bold green]✅ summary.md generated and saved to {summary_file} ({len(summary_text)} chars).[/bold green]"
                        )
                    else:
                        console.print(
                            f"[bold green]✅ summary.md generated ({len(summary_text)} chars).[/bold green]"
                        )
                else:
                    console.print(
                        f"\n[bold cyan]⚡ Reusing existing summary.md for context handoff to {selected_model['name']}...[/bold cyan]"
                    )
                    summary_text = (
                        summary_file.read_text(encoding="utf-8") if summary_file else ""
                    )

                # Update config models to the new model
                config.plan_model = new_model_id
                config.fast_model = new_model_id
                console.print(
                    f"[bold green]✅ Model switched to: {selected_model['name']} ({new_model_id}) for Plan, Build & YOLO modes.[/bold green]\n"
                )

                # Now feed summary.md into newly selected model for injecting context
                console.print(
                    f"[bold cyan]🤖 Feeding summary.md into {selected_model['name']} for context injection...[/bold cyan]"
                )

                if messages and messages[-1].get("role") == "tool":
                    dummy_ack = {
                        "role": "assistant",
                        "content": "Tool execution completed.",
                    }
                    messages.append(dummy_ack)
                    if session_path:
                        log_turn(dummy_ack, session_path)

                handoff_msg = {
                    "role": "user",
                    "content": f"<model_handoff_context>\nAI Model has been switched from {old_model} to {new_model_id}.\nTo ensure proper context continuity, here is the accurate summary (`summary.md`) generated by the previous model:\n\n{summary_text}\n</model_handoff_context>\n\nPlease review this context and give a concise acknowledgment indicating you are ready to continue the task.",
                }
                messages.append(handoff_msg)
                if session_path:
                    log_turn(handoff_msg, session_path)

                try:
                    ack_res = call_nim_with_retry(
                        client, messages, None, new_model_id, config
                    )
                    ack_text = strip_reasoning_spans(
                        ack_res.choices[0].message.content or ""
                    ).strip()
                    if ack_text:
                        console.print(
                            f"\n[bold green]🤖 {selected_model['name']}:[/bold green] {ack_text}\n"
                        )
                        ack_msg = {"role": "assistant", "content": ack_text}
                        messages.append(ack_msg)
                        if session_path:
                            log_turn(ack_msg, session_path)
                except Exception as ex:
                    console.print(
                        f"[yellow]Note: Could not get immediate confirmation from new model ({ex}), but context was successfully injected.[/yellow]\n"
                    )

            except Exception as e:
                console.print(
                    f"[yellow]⚠️ Could not generate summary.md ({e}). Proceeding with direct model switch.[/yellow]\n"
                )
                config.plan_model = new_model_id
                config.fast_model = new_model_id
                console.print(
                    f"[bold green]✅ Model switched to: {selected_model['name']} ({new_model_id}) for Plan, Build & YOLO modes.[/bold green]\n"
                )
        else:
            config.plan_model = new_model_id
            config.fast_model = new_model_id
            console.print(
                f"[bold green]✅ Model switched to: {selected_model['name']} ({new_model_id}) for Plan, Build & YOLO modes.[/bold green]\n"
            )
    else:
        console.print(
            "[bold red]Invalid model selection. Keeping current models.[/bold red]\n"
        )


def generate_summary_with_progress(
    client, messages, current_model, config, workspace_root
):
    from rich.console import Console
    from nimcode.core.prompts import strip_reasoning_spans
    from nimcode.core.agent import call_nim_with_retry
    from pathlib import Path

    console = Console()
    summary_file = (
        get_project_nimcode_dir(workspace_root if workspace_root else Path.cwd())
        / "summary.md"
    )
    summary_prompt = [
        {
            "role": "user",
            "content": "Please generate a high-quality, accurate summary of our conversation, progress, current state of the workspace, and any key decisions made so far. Format the output clearly in markdown suitable for a summary.md handoff document to a new AI model taking over the task.",
        }
    ]
    summary_req_messages = list(messages) + summary_prompt
    try:
        summary_res = call_nim_with_retry(
            client, summary_req_messages, None, current_model, config, timeout=300.0
        )
        summary_text = strip_reasoning_spans(
            summary_res.choices[0].message.content or ""
        ).strip()
        if not summary_text:
            summary_text = "# Summary\nPrevious session context handoff."
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(summary_text, encoding="utf-8")
        console.print(
            f"[bold green]✅ summary.md generated and saved to {summary_file} ({len(summary_text)} chars).[/bold green]"
        )
        return True
    except Exception as e:
        console.print(f"[yellow]⚠️ Could not generate summary.md ({e}).[/yellow]\n")
        return False


def select_interactive_session(config, workspace_root):
    from rich.console import Console
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    import glob
    from pathlib import Path

    console = Console()
    session_dir = (
        get_project_nimcode_dir(workspace_root if workspace_root else Path.cwd())
        / "sessions"
    )
    if not session_dir.exists():
        console.print(
            "[yellow]No saved sessions found in central project sessions directory[/yellow]\n"
        )
        return None
    sessions = sorted(
        [Path(p) for p in glob.glob(str(session_dir / "session_*.json"))],
        key=os.path.getmtime,
        reverse=True,
    )
    if not sessions:
        console.print(
            "[yellow]No saved sessions found in central project sessions directory[/yellow]\n"
        )
        return None
    current_index = 0

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        nonlocal current_index
        current_index = max(0, current_index - 1)

    @kb.add("down")
    def _(event):
        nonlocal current_index
        current_index = min(len(sessions) - 1, current_index + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        nonlocal current_index
        current_index = -1
        event.app.exit()

    def get_text():
        lines = [("bold", "Select a Session to Resume\n\n")]
        for idx, s in enumerate(sessions):
            if idx == current_index:
                prefix = "> "
                style = "bold #00ffff"
            else:
                prefix = "  "
                style = ""
            lines.append((style, f"{prefix}{s.name}\n"))
        lines.append(("", "\nKeyboard: ↑/↓ Navigate  enter Select  esc Cancel\n"))
        return lines

    layout = Layout(Window(content=FormattedTextControl(text=get_text)))
    app = Application(layout=layout, key_bindings=kb, erase_when_done=True)
    app.run()
    if current_index == -1:
        return None
    return sessions[current_index]
