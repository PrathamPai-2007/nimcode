import os
import sys
import json
import typer
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path
from typing import Optional, Any

from nimcode.ui.tui import NimcodeTUI
from nimcode.config import (
    load_config,
    get_project_nimcode_dir,
    select_interactive_session,
)
from nimcode.context import generate_workspace_map
from nimcode.core.agent import run_agent_loop, run_init_command

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys, "stderr", None) is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def patch_completions_menu_above():
    """
    Monkeypatches prompt_toolkit's FloatContainer._draw_float to force the
    autocomplete completion menus to always render above the cursor line.
    """
    try:
        from prompt_toolkit.layout.containers import FloatContainer
        from prompt_toolkit.layout.menus import (
            CompletionsMenu,
            MultiColumnCompletionsMenu,
        )
        from prompt_toolkit.layout.containers import ConditionalContainer
        from prompt_toolkit.application.current import get_app

        original_draw_float = FloatContainer._draw_float

        def patched_draw_float(
            self, fl, screen, mouse_handlers, write_position, style, erase_bg, z_index
        ):
            content = fl.content
            while isinstance(content, ConditionalContainer):
                content = content.content

            # Identify if this float represents standard or multi-column completions menus
            is_menu = (
                isinstance(content, (CompletionsMenu, MultiColumnCompletionsMenu))
                or "CompletionsMenu" in type(content).__name__
            )

            if is_menu and fl.ycursor:
                try:
                    cpos = screen.get_menu_position(
                        fl.attach_to_window or get_app().layout.current_window
                    )
                    cursor_y = cpos.y - write_position.ypos

                    # Calculate preferred width/height of the completion menu
                    fl_width = fl.get_width()
                    if fl_width is None:
                        width = fl.content.preferred_width(
                            write_position.width
                        ).preferred
                        width = min(write_position.width, width)
                    else:
                        width = fl_width

                    height = fl.content.preferred_height(
                        width, write_position.height
                    ).preferred

                    # Force the height to fit the space above the cursor
                    height = min(height, cursor_y)
                    ypos = cursor_y - height

                    # Temporarily override Float attributes to absolute coordinates above the cursor
                    orig_ycursor = fl.ycursor
                    orig_top = fl.top
                    orig_bottom = fl.bottom
                    orig_height = fl.height

                    fl.ycursor = False
                    fl.top = ypos
                    fl.bottom = None
                    fl.height = height

                    try:
                        original_draw_float(
                            self,
                            fl,
                            screen,
                            mouse_handlers,
                            write_position,
                            style,
                            erase_bg,
                            z_index,
                        )
                    finally:
                        # Restore original values to prevent side effects on subsequent renders
                        fl.ycursor = orig_ycursor
                        fl.top = orig_top
                        fl.bottom = orig_bottom
                        fl.height = orig_height
                    return
                except Exception:
                    pass

            # Fallback to standard rendering logic
            original_draw_float(
                self,
                fl,
                screen,
                mouse_handlers,
                write_position,
                style,
                erase_bg,
                z_index,
            )

        FloatContainer._draw_float = patched_draw_float
    except Exception:
        pass


# Run patch at startup
patch_completions_menu_above()

app = typer.Typer(
    name="nimcode",
    help="An open-source, terminal-first AI coding agent driven by NVIDIA NIM (DeepSeek V4).",
)


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
        timeout=float(config.timeout),
        max_retries=0,
    )

    workspace_root = Path(config.workspace_root).resolve()
    config.workspace_root = str(workspace_root)

    # Ensure any persisted model across sessions for this workspace_root is loaded
    try:
        model_file = get_project_nimcode_dir(workspace_root) / "model.json"
        if model_file.exists():
            with open(model_file, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)
                if "fast_model" in mdata and mdata["fast_model"]:
                    config.fast_model = mdata["fast_model"]
                if "plan_model" in mdata and mdata["plan_model"]:
                    config.plan_model = mdata["plan_model"]
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
    run_agent_loop(
        client,
        prompt,
        config,
        workspace_root,
        workspace_map,
        initial_state=initial_state,
    )


@app.command()
def start():
    """
    Start an interactive nimcode session by prompting for the initial task with a beautiful TUI.
    """
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    typer.clear()

    tui = NimcodeTUI(config=config)
    tui.show_welcome(workspace_root)

    prompt, mode = tui.get_prompt()

    if prompt and prompt.startswith("<RESUME_SESSION> "):
        session_file_name = prompt.split(" ", 1)[1]
        session_path = (
            get_project_nimcode_dir(workspace_root) / "sessions" / session_file_name
        )
        with open(session_path, "r", encoding="utf-8") as f:
            existing_messages = json.load(f)
        run_agent_loop(
            client,
            "",
            config,
            workspace_root,
            workspace_map,
            existing_messages=existing_messages,
            session_file_name=session_file_name,
        )
        return

    if not prompt or not prompt.strip():
        typer.echo("No prompt provided. Exiting.")
        raise typer.Exit()

    initial_state = (
        "AWAITING_PLAN_APPROVAL"
        if mode == "Plan Mode"
        else ("YOLO" if mode == "YOLO Mode" else "EXECUTING")
    )
    run_agent_loop(
        client,
        prompt.strip(),
        config,
        workspace_root,
        workspace_map,
        initial_state=initial_state,
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
    typer.echo(f"  • Fast Model:         {config.fast_model}")
    typer.echo(f"  • Plan Model:         {config.plan_model}")
    typer.echo(f"  • Active Mode:        {config.mode}")
    typer.echo(f"  • Loop Iteration Cap: {config.max_loop_iterations}")
    session_dir = get_project_nimcode_dir(workspace_root) / "sessions"
    session_count = len(list(session_dir.glob("*.json"))) if session_dir.exists() else 0
    typer.echo(
        f"  • Session Logs:       {'Active (' + str(session_count) + ' sessions)' if session_count > 0 else 'None'}"
    )

    workspace_map = generate_workspace_map(str(workspace_root))
    line_count = len(workspace_map.splitlines())
    typer.echo(
        f"  • Harvested Files:    ~{line_count - 1} entries across workspace tree"
    )
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
        session_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_session), str(session_dir / "legacy_session.json"))
        typer.secho(
            f"\nMigrated legacy session to {session_dir}/legacy_session.json",
            fg=typer.colors.CYAN,
        )

    if not session_dir.exists() or not list(session_dir.glob("*.json")):
        typer.secho(
            "No active sessions found. Use 'start' or 'run' to begin a new session.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit()

    sessions = sorted(
        list(session_dir.glob("*.json")), key=lambda x: x.stat().st_mtime, reverse=True
    )

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

    if not existing_messages:
        typer.secho(
            "Session file is empty. Starting a new session.", fg=typer.colors.YELLOW
        )
        run_agent_loop(
            client,
            "Hello, continue from where we left off.",
            config,
            workspace_root,
            workspace_map,
            session_file_name=selected_session.name,
        )
    else:
        typer.secho(
            f"Restoring session history from {selected_session.name} ({len(existing_messages)} messages loaded)...",
            fg=typer.colors.GREEN,
            bold=True,
        )
        run_agent_loop(
            client,
            "",
            config,
            workspace_root,
            workspace_map,
            existing_messages=existing_messages,
            session_file_name=selected_session.name,
        )


@app.command()
def init():
    """
    Initialize or update an AGENTS.md file in the workspace root.
    """
    client, config, workspace_root, workspace_map = _get_client_and_setup()
    run_init_command(config, client=client, workspace_root=workspace_root)


if __name__ == "__main__":
    app()
