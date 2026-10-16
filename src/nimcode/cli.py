import os
import sys
import json
import asyncio
import typer
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path
from typing import Optional, Any

from nimcode.config import (
    load_config,
    get_project_nimcode_dir,
    select_interactive_session,
)
from nimcode.context import generate_workspace_map
from nimcode.core.agent import run_init_command
from nimcode.core.agent_tui import run_agent_loop_tui


def _make_tui(config=None):
    """Create the nimcode TUI (Rust sidecar). Exits cleanly if the binary is missing."""
    from nimcode.ui.rust_bridge import make_tui as _factory

    try:
        return _factory(config=config)
    except RuntimeError as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)


if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys, "stderr", None) is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


app = typer.Typer(
    name="nimcode",
    help="An open-source, terminal-first AI coding agent driven by NVIDIA NIM (DeepSeek V4).",
)


def _safe_timeout(config) -> float:
    try:
        t = float(getattr(config, "timeout", 60))
    except (TypeError, ValueError):
        t = 60.0
    if t <= 0:
        t = 60.0
    return min(t, 600.0)


def _get_client_and_setup() -> tuple[OpenAI, Any, Path, str]:
    """Helper to load environment, config, client, and harvested workspace map."""
    load_dotenv()

    config = load_config()

    # Global fallback for API key if not in local .env
    global_env = Path.home() / ".nimcode" / ".env"
    if global_env.exists() and not os.environ.get("NVIDIA_API_KEY"):
        load_dotenv(dotenv_path=global_env)

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key and config.api_base_url:
        api_key = "local_dummy_key"

    if not api_key:
        typer.secho("Error: NVIDIA_API_KEY is not set.", fg=typer.colors.RED, bold=True)
        typer.echo(
            "Please set NVIDIA_API_KEY in your current environment, .env file, or globally at ~/.nimcode/.env"
        )
        raise typer.Exit(code=1)

    base_url = (
        config.api_base_url
        if config.api_base_url
        else "https://integrate.api.nvidia.com/v1"
    )

    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=_safe_timeout(config),
        max_retries=0,
    )

    workspace_root = Path(config.workspace_root).resolve()
    config.workspace_root = str(workspace_root)
    # model.json is already merged (and validated) by load_config().

    # Live model catalog: instant start from cache/static, then refresh in the
    # background. Reconciles retired saved models and fits the context budget
    # to the active model's window (unless the user customized it).
    # The cache holds the NVIDIA catalog; a custom endpoint must neither
    # overwrite it nor have its models "retired" against it.
    try:
        from nimcode import config_models as _cm

        if not config.api_base_url:
            _catalog = _cm.get_model_catalog()
            _notice = _cm.reconcile_saved_model(config, _catalog)
            if _notice:
                typer.secho(f"Note: {_notice}", fg=typer.colors.YELLOW)
        _changed, _msg = _cm.apply_model_context(config, config.fast_model)
        if _changed:
            typer.secho(f"Note: {_msg}", fg=typer.colors.CYAN)
        if not config.api_base_url:
            _cm.refresh_models_background(client)
    except Exception:
        pass

    workspace_map = generate_workspace_map(str(workspace_root))

    return client, config, workspace_root, workspace_map


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """
    An open-source, terminal-first AI coding agent driven by NVIDIA NIM (DeepSeek V4).
    Run without arguments (`nimcode`) to boot into the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        start()


@app.command()
def run(
    prompt: str = typer.Argument(
        ..., help="The initial coding task or instruction for NIM-Engineer."
    ),
    mode: Optional[str] = typer.Option(
        None, "--mode", "-m", help="Mode to run in: 'plan', 'build', or 'yolo'."
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        "-y",
        help="Run directly in YOLO mode (auto-accept edits and commands).",
    ),
):
    """
    Start the NIM-Engineer agent loop with a specific task prompt.
    """
    # Validate before setup/clear so a typo doesn't wipe the screen first.
    if mode is not None and mode.lower() not in ("plan", "build", "yolo"):
        raise typer.BadParameter("Mode must be one of: plan, build, yolo.")
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    typer.clear()
    if yolo or (mode and mode.lower() == "yolo"):
        config.mode = "yolo"
        initial_state = "YOLO"
    elif mode and mode.lower() == "plan":
        config.mode = "plan"
        initial_state = "AWAITING_PLAN_APPROVAL"
    elif mode and mode.lower() == "build":
        config.mode = "build"
        initial_state = "EXECUTING"
    else:
        initial_state = (
            "YOLO"
            if config.mode == "yolo"
            else ("AWAITING_PLAN_APPROVAL" if config.mode == "plan" else "EXECUTING")
        )

    # Create TUI and run async loop
    tui = _make_tui(config=config)
    tui.show_welcome(str(workspace_root))
    asyncio.run(
        run_agent_loop_tui(
            tui,
            client,
            prompt,
            config,
            workspace_root,
            workspace_map,
            initial_state=initial_state,
        )
    )


@app.command()
def start():
    """
    Start an interactive nimcode session by prompting for the initial task with a beautiful TUI.
    """
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    typer.clear()

    tui = _make_tui(config=config)
    tui.show_welcome(str(workspace_root))

    # Now run the full agent loop with the initial prompt empty, which will automatically prompt the user in TUI
    if config.mode == "yolo":
        initial_state = "YOLO"
    elif config.mode == "plan":
        initial_state = "AWAITING_PLAN_APPROVAL"
    else:
        initial_state = "EXECUTING"
    asyncio.run(
        run_agent_loop_tui(
            tui,
            client,
            "",
            config,
            workspace_root,
            workspace_map,
            initial_state=initial_state,
        )
    )


@app.command()
def status():
    """
    Check the current configuration, API key readiness, and workspace map.
    """
    load_dotenv()
    global_env = Path.home() / ".nimcode" / ".env"
    if global_env.exists() and not os.environ.get("NVIDIA_API_KEY"):
        load_dotenv(dotenv_path=global_env)

    config = load_config()
    workspace_root = Path(config.workspace_root).resolve()
    api_key = os.environ.get("NVIDIA_API_KEY")

    typer.secho("\nnimcode Status Check", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  • Workspace Root:     {workspace_root}")
    typer.echo(f"  • API Key Set:        {'Yes' if api_key else 'No'}")
    _base = config.api_base_url or "https://integrate.api.nvidia.com/v1"
    typer.echo(
        f"  • Endpoint:           {_base}"
        + ("" if config.api_base_url else " (default)")
    )
    typer.echo(f"  • Fast Model:         {config.fast_model}")
    typer.echo(f"  • Plan Model:         {config.plan_model}")
    typer.echo(f"  • Active Mode:        {config.mode}")
    typer.echo(f"  • Loop Iteration Cap: {config.max_loop_iterations}")
    try:
        from nimcode import config_models as _cm

        _catalog = _cm.get_model_catalog()
        _, _age = _cm.load_models_cache()
        _fresh = (
            "live cache"
            if _age < _cm._ttl_seconds()
            else ("stale cache" if _age != float("inf") else "static table")
        )
        typer.echo(f"  • Model Catalog:      {len(_catalog)} models ({_fresh})")
    except Exception:
        pass
    session_dir = get_project_nimcode_dir(workspace_root) / "sessions"
    session_count = len(list(session_dir.glob("*.json"))) if session_dir.exists() else 0
    typer.echo(
        f"  • Session Logs:       {'Active (' + str(session_count) + ' sessions)' if session_count > 0 else 'None'}"
    )

    try:
        workspace_map = generate_workspace_map(str(workspace_root))
    except Exception as e:
        typer.echo(f"  • Harvested Files:    (unavailable: {e})")
        typer.echo("")
        return
    line_count = len(workspace_map.splitlines())
    typer.echo(
        f"  • Harvested Files:    ~{max(0, line_count - 1)} entries across workspace tree"
    )
    typer.echo("")


@app.command(name="refresh-models")
def refresh_models():
    """
    Force a live refresh of the model catalog from the NIM API.
    """
    client, config, workspace_root, _ = _get_client_and_setup()
    from nimcode import config_models as _cm

    if config.api_base_url:
        # Custom endpoint: list live, but don't clobber the shared NVIDIA cache.
        catalog = _cm.fetch_models(client)
        msg = f"Endpoint lists {len(catalog)} models (not cached)."
    else:
        catalog, msg = _cm.refresh_models_blocking(client)
    typer.secho(f"\n{msg}", fg=typer.colors.CYAN, bold=True)
    notice = None if config.api_base_url else _cm.reconcile_saved_model(config, catalog)
    if notice:
        typer.secho(f"Note: {notice}", fg=typer.colors.YELLOW)
    changed, ctx_msg = _cm.apply_model_context(config, config.fast_model)
    if changed:
        typer.secho(f"Note: {ctx_msg}", fg=typer.colors.CYAN)
    for m in catalog[:20]:
        ctx = m.get("context_tokens")
        ctx_str = f"{ctx:,} tokens" if ctx else "unknown window"
        typer.echo(f"  • {m.get('name')} ({m.get('id')}) — {ctx_str}")
    if len(catalog) > 20:
        typer.echo(f"  … and {len(catalog) - 20} more")
    typer.echo("")


@app.command()
def resume(
    latest: bool = typer.Option(
        False, "--latest", "-l", help="Resume the most recent session automatically."
    ),
):
    """
    Resume an interrupted nimcode session.
    """
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    session_dir = get_project_nimcode_dir(workspace_root) / "sessions"

    legacy_session = workspace_root / ".nimcode_session.json"
    if legacy_session.exists():
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            import time as _t

            # Must match the picker's "session_*.json" glob or it is invisible.
            dest = session_dir / f"session_legacy_{int(_t.time())}.json"
            shutil.move(str(legacy_session), str(dest))
            typer.secho(
                f"\nMigrated legacy session to {dest}",
                fg=typer.colors.CYAN,
            )
        except Exception as e:
            typer.secho(
                f"Warning: could not migrate legacy session: {e}",
                fg=typer.colors.YELLOW,
            )

    if not session_dir.exists() or not list(session_dir.glob("*.json")):
        typer.secho(
            "No active sessions found. Use 'start' or 'run' to begin a new session.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit()

    try:
        sessions = sorted(
            list(session_dir.glob("*.json")),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        typer.secho(
            "No active sessions found. Use 'start' or 'run' to begin a new session.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit()
    except OSError as e:
        typer.secho(f"Error listing sessions: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if latest:
        selected_session = sessions[0]
    else:
        selected_session = select_interactive_session(config, workspace_root)
        if not selected_session:
            raise typer.Exit()

    try:
        with open(selected_session, "r", encoding="utf-8") as f:
            existing_messages = json.load(f)
    except Exception as e:
        typer.secho(f"Error loading session log: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if not isinstance(existing_messages, list):
        typer.secho(
            "Session file is corrupt (not a message list). Starting a new session.",
            fg=typer.colors.YELLOW,
        )
        existing_messages = []

    if not existing_messages:
        typer.secho(
            "Session file is empty. Starting a new session.", fg=typer.colors.YELLOW
        )
        asyncio.run(
            run_agent_loop_tui(
                _make_tui(config=config),
                client,
                "Hello, continue from where we left off.",
                config,
                workspace_root,
                workspace_map,
                session_file_name=selected_session.name,
            )
        )
    else:
        typer.secho(
            f"Restoring session history from {selected_session.name} ({len(existing_messages)} messages loaded)...",
            fg=typer.colors.GREEN,
            bold=True,
        )
        asyncio.run(
            run_agent_loop_tui(
                _make_tui(config=config),
                client,
                "",
                config,
                workspace_root,
                workspace_map,
                existing_messages=existing_messages,
                session_file_name=selected_session.name,
            )
        )


@app.command()
def init():
    """
    Initialize or update an AGENTS.md file in the workspace root.
    """
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    run_init_command(config, client=client, workspace_root=workspace_root)


@app.command()
def stats():
    """
    Show usage statistics and analytics from historical sessions.
    """
    load_dotenv()
    global_env = Path.home() / ".nimcode" / ".env"
    if global_env.exists() and not os.environ.get("NVIDIA_API_KEY"):
        load_dotenv(dotenv_path=global_env)

    config = load_config()
    workspace_root = Path(config.workspace_root).resolve()
    config.workspace_root = str(workspace_root)

    from nimcode.ui.output import console
    from nimcode.core.agent import aggregate_session_stats, render_stats_rich

    stats = aggregate_session_stats(workspace_root)
    render_stats_rich(stats, console)


if __name__ == "__main__":
    app()
