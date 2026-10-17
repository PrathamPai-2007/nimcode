import os
import sys
import tomli
from dataclasses import dataclass, field
from typing import List, Any, Optional
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
    fast_model: str = "nvidia/nemotron-3-super-120b-a12b"
    plan_model: str = "nvidia/nemotron-3-super-120b-a12b"
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
    # Container resource limits; "" / 0 disables a limit.
    docker_memory: str = "2g"
    docker_pids_limit: int = 512
    docker_cpus: str = "2"
    docker_network: str = "bridge"  # "bridge" or "none"; "host" is rejected
    api_base_url: Optional[str] = None
    # Wall-clock limit per subagent run; on expiry the agent is TIMED_OUT.
    subagent_timeout_s: int = 900
    # Attach screenshot_page PNGs to the conversation as image_url parts.
    vision_screenshots: bool = False
    # SearXNG base URL used as web_search fallback; "" disables it.
    searxng_url: str = ""


_DOCKER_LIMIT_KEYS = (
    "docker_memory",
    "docker_pids_limit",
    "docker_cpus",
    "docker_network",
)


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
            **{k: getattr(config, k) for k in _DOCKER_LIMIT_KEYS},
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
            f"docker_memory = {q(str(config.docker_memory or ''))}",
            f"docker_pids_limit = {_num(config.docker_pids_limit, 512)}",
            f"docker_cpus = {q(str(config.docker_cpus or ''))}",
            f"docker_network = {q(str(config.docker_network or ''))}",
            f"denylist = {_json.dumps(list(config.denylist or []))}",
            f"subagent_timeout_s = {_num(config.subagent_timeout_s, 900)}",
            f"vision_screenshots = {'true' if config.vision_screenshots else 'false'}",
            f"searxng_url = {q(str(config.searxng_url or ''))}",
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
    import re
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
    for name in (
        "auto_backup",
        "disable_native_tools",
        "disable_docker",
        "vision_screenshots",
    ):
        val = getattr(cfg, name)
        if isinstance(val, str):
            setattr(cfg, name, val.strip().lower() in ("1", "true", "yes", "on"))
        elif not isinstance(val, bool):
            setattr(cfg, name, bool(val))

    cfg.subagent_timeout_s = _coerce(cfg.subagent_timeout_s, int, 900)
    if isinstance(cfg.subagent_timeout_s, bool) or cfg.subagent_timeout_s <= 0:
        warnings.warn("subagent_timeout_s must be a positive integer. Using 900.")
        cfg.subagent_timeout_s = 900

    url = str(cfg.searxng_url or "").strip().rstrip("/")
    if url and not re.match(r"https?://", url):
        warnings.warn(f"searxng_url '{url}' must start with http(s)://. Disabled.")
        url = ""
    cfg.searxng_url = url

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

    # Docker resource limits: "" / 0 disables; malformed values fall back.
    mem = str(cfg.docker_memory if cfg.docker_memory is not None else "").strip()
    if mem in ("", "0"):
        mem = ""
    elif not re.fullmatch(r"\d+(\.\d+)?[bkmgBKMG]?", mem):
        warnings.warn(f"Invalid docker_memory '{mem}'. Using '2g'.")
        mem = "2g"
    cfg.docker_memory = mem
    cfg.docker_pids_limit = _coerce(cfg.docker_pids_limit, int, 512)
    if isinstance(cfg.docker_pids_limit, bool) or cfg.docker_pids_limit < 0:
        warnings.warn("docker_pids_limit must be >= 0. Using 512.")
        cfg.docker_pids_limit = 512
    cpus = str(cfg.docker_cpus if cfg.docker_cpus is not None else "").strip()
    if _coerce(cpus or "0", float, -1.0) < 0:
        warnings.warn(f"Invalid docker_cpus '{cpus}'. Using '2'.")
        cpus = "2"
    cfg.docker_cpus = "" if _coerce(cpus or "0", float, 0.0) == 0 else cpus
    net = str(cfg.docker_network or "").strip().lower()
    if net not in ("", "bridge", "none"):
        # "host" / "container:x" would give the sandbox the host's network.
        warnings.warn(
            f"docker_network '{net}' not allowed (use 'bridge' or 'none'). Using 'bridge'."
        )
        net = "bridge"
    cfg.docker_network = net


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

    # Where each non-default value came from (shown by `nimcode config`).
    sources: dict = {}
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
            sources = dict.fromkeys(filtered, "nimcode.toml")
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

                def _set(key, value):
                    setattr(cfg, key, value)
                    sources[key] = "model.json"

                if isinstance(mdata.get("model"), str) and mdata["model"]:
                    _set("fast_model", mdata["model"])
                    _set("plan_model", mdata["model"])
                for key in ("fast_model", "plan_model"):
                    if isinstance(mdata.get(key), str) and mdata[key]:
                        _set(key, mdata[key])
                for key in (
                    "docker_image",
                    "docker_expose_ports",
                    "docker_extra_args",
                    "disable_docker",
                    *_DOCKER_LIMIT_KEYS,
                ):
                    if key in mdata:
                        _set(key, mdata[key])
    except Exception:
        pass

    # Validate after the model.json merge so its values are coerced too.
    _validate_config(cfg)
    sources["workspace_root"] = "cwd"
    cfg._sources = sources
    return cfg


def config_sources(cfg: Config) -> List[tuple]:
    """(key, value, source) for every Config field; source is "default",
    "nimcode.toml", "model.json" or "cwd" (as recorded by load_config)."""
    import dataclasses

    recorded = getattr(cfg, "_sources", {})
    return [
        (f.name, getattr(cfg, f.name), recorded.get(f.name, "default"))
        for f in dataclasses.fields(cfg)
    ]


def _backup_label(b: dict) -> str:
    """Original path from the manifest, else the legacy name-parsed guess."""
    if b.get("original"):
        return b["original"]
    return f"{b['hint']} (legacy, path guessed)"


def show_changes(workspace_root: Path, tui: Optional[Any] = None):
    from rich.console import Console
    from rich.table import Table
    from nimcode.tools.base import list_backups

    root = workspace_root.resolve()
    backups = list_backups(root)[:15]

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

        if backups:
            lines.append("**Recent File Backups (.nimcode/backups/):**")
            lines.append("| Backup Timestamp / File | Original Path / Target |")
            lines.append("| --- | --- |")
            for b in backups:
                lines.append(f"| {b['when']} | {_backup_label(b)} |")
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

    if backups:
        table = Table(
            title="Recent File Backups (.nimcode/backups/)", border_style="cyan"
        )
        table.add_column("Backup Timestamp / File", style="bold green")
        table.add_column("Original Path / Target", style="cyan")
        for b in backups:
            table.add_row(b["when"], _backup_label(b))
        console.print(table)
    else:
        console.print("[dim]No backup files stored in .nimcode/backups/ yet.[/dim]")
    console.print()


AVAILABLE_MODELS = {
    "1": {"name": "Nemotron 3 Nano 30B", "id": "nvidia/nemotron-3-nano-30b-a3b"},
    "2": {"name": "GPT-OSS 20B", "id": "openai/gpt-oss-20b"},
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


def select_interactive_session(config, workspace_root):
    from rich.console import Console
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from nimcode.core.agent import list_sessions

    console = Console()
    session_dir = (
        get_project_nimcode_dir(workspace_root if workspace_root else Path.cwd())
        / "sessions"
    )
    sessions = list_sessions(session_dir)
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
