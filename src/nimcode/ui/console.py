import sys
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.table import Table

console = Console()

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def show_help():
    table = Table(
        title="Keyboard Shortcuts & Commands",
        border_style="cyan",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Shortcut / Command", style="bold")
    table.add_column("Action", style="dim")
    table.add_row(
        "Shift + Tab",
        "Cycle between [ Plan Mode ], [ Build Mode ], and [ YOLO Mode ]",
    )
    table.add_row("?", "Show this help and keyboard shortcuts menu")
    table.add_row("/help", "Show this help menu")
    table.add_row(
        "/plan / /build / /yolo",
        "Switch active mode directly (or cycle with Shift+Tab)",
    )
    table.add_row("/test [cmd]", "Run test suite inside validation sandbox")
    table.add_row("/team", "Monitor or inspect active background subagents")
    table.add_row("/resume", "Show and access old chat sessions")
    table.add_row("/model", "Choose active AI model (Mistral, DeepSeek, etc.)")
    table.add_row("/config", "View or modify nimcode runtime configuration settings")
    table.add_row(
        "/changes / /diff", "Check git working tree changes and recent file backups"
    )
    table.add_row("/compact", "Compact conversation history into summary.md")
    table.add_row("/skills", "Show installed agent skills")
    table.add_row(
        "/install-skill <link>", "Install new skill from raw .md, .zip, or git repo"
    )
    table.add_row("/exit / /quit", "Exit the nimcode application")
    table.add_row("Ctrl + C / ESC", "Cancel the current prompt / exit")

    panel = Panel(table, border_style="cyan", padding=(1, 2))
    console.print(panel)
    console.print()


def print_welcome():
    left_text = Text()
    left_text.append("       _                         _\n", style="bold cyan")
    left_text.append(" _ __ (_) _ __ ___   ___  ___   __| | ___\n", style="bold cyan")
    left_text.append(
        "| '_ \\| | '_ ` _ \\ / __|/ _ \\ / _` |/ _ \\\n", style="bold cyan"
    )
    left_text.append("| | | | | | | | | | (_| (_) | (_| |  __/\n", style="bold cyan")
    left_text.append(
        "|_| |_|_|_| |_| |_|\\___\\___/ \\__,_|\\___|\n\n", style="bold cyan"
    )
    left_text.append("NVIDIA NIM Coding Agent\n", style="dim")

    right_text = Text()
    right_text.append("\n\n")
    right_text.append(
        "• Shift+Tab to cycle between Plan, Build & YOLO modes\n",
        style="dim",
    )
    right_text.append("• To use commands and skills, use /\n", style="dim")
    right_text.append("• Run /help or ? for a list of nimcode commands\n", style="dim")

    columns = Columns(
        [Align.center(left_text), right_text], expand=True, padding=(0, 4)
    )
    console.print(columns)
    console.print()
