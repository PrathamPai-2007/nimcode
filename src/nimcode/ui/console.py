from typing import Optional, Any
from rich.align import Align
from rich.columns import Columns
from rich.table import Table
from rich.text import Text

from nimcode.ui.output import console, symbol


# Single source for /help. Track the sidecar's real bindings here.
KEY_BINDINGS = [
    ("Enter", "Send (with the / popup open: send the highlighted command)"),
    ("Ctrl+J / Shift+Enter", "Insert a newline (Ctrl+J works in every terminal)"),
    ("Left/Right, Home/End", "Move the cursor"),
    ("Ctrl+Left/Right", "Move by word"),
    ("Ctrl+W / Ctrl+Backspace", "Delete the previous word"),
    ("Ctrl+U", "Delete to the start of the line"),
    ("Up/Down", "Input history (moves between lines in multi-line input)"),
    ("PgUp/PgDn, Ctrl+Up/Down, mouse wheel", "Scroll the conversation"),
    ("Esc", "Jump back to the latest message / dismiss the popup"),
    ("Tab", "Complete a /command"),
    ("Shift+Tab / Ctrl+T", "Cycle Plan / Build"),
    ("Ctrl+O", "Expand or fold long tool output"),
    ("Ctrl+C", "Clear the input, or interrupt the turn when it is empty"),
    ("Ctrl+D", "Exit (when the input is empty)"),
    ("Shift+drag", "Select text (the mouse wheel scrolls the conversation)"),
]

# Every SLASH_COMMANDS entry must appear here (tests/test_ux_help.py).
COMMAND_HELP = [
    ("? / /help", "Show this help"),
    ("/plan / /build", "Switch mode directly"),
    ("/model", "Choose the active model (hands context over via summary.md)"),
    ("/models-refresh", "Refresh the model catalog from the NIM API"),
    ("/config", "Edit runtime settings"),
    ("/changes / /diff", "Git working-tree changes and recent backups"),
    ("/restore", "Restore a file from a backup"),
    ("/test [cmd]", "Run the test suite in the sandbox"),
    ("/team", "List subagents and their transcript files"),
    ("/kill", "Stop all background processes"),
    ("/compact", "Summarise the session into summary.md and continue from it"),
    ("/stats", "Usage statistics across sessions"),
    ("/recap", "Session recap (not yet available in the TUI)"),
    ("/skills", "List installed skills"),
    ("/install-skill <url>", "Install a skill from a .md, .zip or git repo"),
    ("/<skill> [text]", "Run an installed skill"),
    ("/init", "Create or update AGENTS.md"),
    ("/resume", "Load a saved session in place"),
    ("/exit / /quit", "Exit nimcode"),
]

_DOCKER_NOTE = (
    "Shell commands and tests run in a sandboxed Docker container by default. "
    "Set 'disable_docker = true' in nimcode.toml to run them on the host."
)


def show_help(tui: Optional[Any] = None):
    if tui is not None:
        lines = ["**Commands**", "", "| Command | Action |", "| --- | --- |"]
        lines += [f"| `{c}` | {d} |" for c, d in COMMAND_HELP]
        lines += ["", "**Keys**", "", "| Key | Action |", "| --- | --- |"]
        lines += [f"| {k} | {d} |" for k, d in KEY_BINDINGS]
        lines += ["", f"*{_DOCKER_NOTE}*"]
        tui.add_message("system", "\n".join(lines), markdown=True)
        return

    for title, rows in (("Commands", COMMAND_HELP), ("Keys", KEY_BINDINGS)):
        table = Table(
            title=title,
            border_style="cyan",
            show_header=False,
        )
        table.add_column("", style="bold")
        table.add_column("", style="dim")
        for a, b in rows:
            table.add_row(a, b)
        console.print(table)
    console.print(f"[dim]{_DOCKER_NOTE}[/dim]\n")


def print_welcome():
    left_text = Text()
    left_text.append("       _                         _      \n", style="bold cyan")
    left_text.append("      (_)                       | |     \n", style="bold cyan")
    left_text.append(" _ __  _ _ __ ___   ___ ___   __| | ___ \n", style="bold cyan")
    left_text.append(
        "| '_ \\| | '_ ` _ \\ / __/ _ \\ / _` |/ _ \\\n", style="bold cyan"
    )
    left_text.append("| | | | | | | | | | (_| (_) | (_| |  __/\n", style="bold cyan")
    left_text.append(
        "|_| |_|_|_| |_| |_|\\___\\___/ \\__,_|\\___|\n\n", style="bold cyan"
    )
    left_text.append("NVIDIA NIM Coding Agent\n", style="dim")

    bullet = symbol("bullet")
    right_text = Text()
    right_text.append("\n\n\n")
    right_text.append(
        f"{bullet} Shift+Tab to cycle between Plan & Build modes\n",
        style="dim",
    )
    right_text.append(f"{bullet} To use commands and skills, use /\n", style="dim")
    right_text.append(
        f"{bullet} Run /help or ? for a list of nimcode commands\n", style="dim"
    )

    columns = Columns(
        [Align.center(left_text), right_text], expand=True, padding=(0, 4)
    )
    console.print(columns)
    console.print()
