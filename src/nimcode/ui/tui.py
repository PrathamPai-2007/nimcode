import sys
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.completion import WordCompleter
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from nimcode.ui.console import show_help, print_welcome

console = Console()


# Build a fast id->name lookup from AVAILABLE_MODELS (which is keyed by number)
def _model_display_name(model_id: str) -> str:
    from nimcode.config import AVAILABLE_MODELS

    for entry in AVAILABLE_MODELS.values():
        if entry["id"] == model_id:
            return entry["name"]
    return model_id


class NimcodeTUI:
    def __init__(self, config=None):
        self.config = config
        self.mode = "Plan Mode"
        if config:
            if config.mode == "plan":
                self.mode = "Plan Mode"
            elif config.mode == "yolo":
                self.mode = "YOLO Mode"
            else:
                self.mode = "Build Mode"

        self.bindings = KeyBindings()

        @self.bindings.add("s-tab")
        def _(event):
            if self.mode == "Plan Mode":
                self.mode = "Build Mode"
            elif self.mode == "Build Mode":
                self.mode = "YOLO Mode"
            else:
                self.mode = "Plan Mode"
            event.app.invalidate()

        self.style = Style.from_dict(
            {
                "prompt": "bold cyan",
                "sep-plan": "bold cyan",
                "sep-build": "bold green",
                "sep-yolo": "bold red",
            }
        )

    def print_welcome(self):
        print_welcome()

    def show_welcome(self, workspace_root: str = None):
        self.print_welcome()

    def show_help(self):
        show_help()

    def get_prompt(self):
        if not hasattr(self, "session"):
            self.session = PromptSession(key_bindings=self.bindings, style=self.style)

        def get_prompt_message():
            import shutil
            from prompt_toolkit.formatted_text import FormattedText

            term_width = shutil.get_terminal_size().columns
            if self.mode == "Plan Mode":
                mode_name = "PLAN MODE"
                sep_class = "class:sep-plan"
            elif self.mode == "YOLO Mode":
                mode_name = "YOLO MODE"
                sep_class = "class:sep-yolo"
            else:
                mode_name = "BUILD MODE"
                sep_class = "class:sep-build"

            mode_label = f"── {mode_name} "
            remaining_width = term_width - len(mode_label)
            if remaining_width < 0:
                sep = mode_label[:term_width] + "\n"
            else:
                sep = mode_label + ("─" * remaining_width) + "\n"
            return FormattedText([(sep_class, sep), ("class:prompt", "❯ ")])

        def get_bottom_toolbar():
            import shutil
            from prompt_toolkit.formatted_text import FormattedText

            term_width = shutil.get_terminal_size().columns
            if self.mode == "Plan Mode":
                sep_class = "class:sep-plan"
            elif self.mode == "YOLO Mode":
                sep_class = "class:sep-yolo"
            else:
                sep_class = "class:sep-build"

            model_name = "unknown"
            if self.config:
                current_model = (
                    self.config.fast_model
                    if self.mode != "Plan Mode"
                    else self.config.plan_model
                )
                model_name = _model_display_name(current_model)

            right_str = f" [ Shift+Tab: cycle mode | ?: help ] Model: {model_name} ──"
            left_str = "── "
            padding = term_width - len(left_str) - len(right_str)

            status_line = left_str + ("─" * max(0, padding)) + right_str
            return FormattedText([(sep_class, status_line)])

        while True:
            # Build dynamic completer list
            commands = [
                "/help",
                "/plan",
                "/build",
                "/yolo",
                "/resume",
                "/model",
                "/config",
                "/changes",
                "/diff",
                "/test",
                "/team",
                "/compact",
                "/skills",
                "/install-skill",
                "/init",
                "/quit",
                "/exit",
            ]
            try:
                from nimcode import skills

                commands.extend([f"/{s}" for s in skills.list_skills()])
            except Exception:
                pass
            completer = WordCompleter(commands, ignore_case=True, WORD=True)

            try:
                user_input = self.session.prompt(
                    get_prompt_message,
                    bottom_toolbar=get_bottom_toolbar,
                    completer=completer,
                    reserve_space_for_menu=2,
                )
                stripped = user_input.strip()

                if stripped in ["?", "/help", "help"]:
                    self.show_help()
                    continue

                elif stripped.lower() in ["/quit", "/exit"]:
                    console.print("[bold red]Exiting nimcode.[/bold red]")
                    return None, None

                elif stripped.lower() in ["/plan", "/build", "/yolo"]:
                    if stripped.lower() == "/plan":
                        self.mode = "Plan Mode"
                        console.print("[bold cyan]Switched to Plan Mode.[/bold cyan]")
                    elif stripped.lower() == "/build":
                        self.mode = "Build Mode"
                        console.print(
                            "[bold green]Switched to Build Mode.[/bold green]"
                        )
                    elif stripped.lower() == "/yolo":
                        self.mode = "YOLO Mode"
                        console.print(
                            "[bold red]Switched to YOLO Mode (Auto-accept enabled).[/bold red]"
                        )
                    continue

                elif stripped.lower().startswith("/test"):
                    parts = stripped.split(" ", 1)
                    test_cmd = parts[1].strip() if len(parts) > 1 else None
                    from nimcode.tools import execute_test_runner

                    root_path = (
                        Path(self.config.workspace_root).resolve()
                        if self.config
                        else Path(".").resolve()
                    )
                    console.print(
                        "[bold cyan]Running test suite inside validation sandbox...[/bold cyan]"
                    )
                    res = execute_test_runner(
                        root_path, self.config or object(), test_cmd
                    )
                    console.print(
                        Panel(res, title="Test Runner Results", border_style="cyan")
                    )
                    continue

                elif stripped.lower() == "/team":
                    from nimcode.tools import ACTIVE_SUBAGENTS

                    if not ACTIVE_SUBAGENTS:
                        console.print(
                            "[dim yellow]No subagents spawned yet. Use the `spawn_agent` tool "
                            "or ask nimcode to delegate tasks.[/dim yellow]"
                        )
                    else:
                        t = Table(
                            title="Active Subagent Swarm Team",
                            border_style="cyan",
                            show_header=True,
                        )
                        t.add_column("Agent ID", style="bold cyan")
                        t.add_column("Role", style="bold")
                        t.add_column("Status", style="bold green")
                        t.add_column("Output Summary", style="dim")
                        for aid, sub in ACTIVE_SUBAGENTS.items():
                            snippet = sub["output"]
                            if len(snippet) > 100:
                                snippet = snippet[:100] + "..."
                            t.add_row(aid, sub["role"], sub["status"], snippet)
                        console.print(Panel(t, border_style="cyan", padding=(1, 2)))
                    continue

                elif stripped.lower() in ["/changes", "/diff"]:
                    from nimcode.config import show_changes

                    root_path = (
                        Path(self.config.workspace_root).resolve()
                        if self.config
                        else Path(".").resolve()
                    )
                    show_changes(root_path)
                    continue

                elif stripped.lower() == "/config":
                    if self.config:
                        from nimcode.config import configure_interactive

                        configure_interactive(self.config)
                    else:
                        console.print("[red]Config object not available.[/red]")
                    continue

                elif stripped.lower() == "/resume":
                    if not self.config:
                        console.print("[red]Config not available.[/red]")
                        continue
                    from nimcode.config import select_interactive_session

                    selected_session = select_interactive_session(
                        self.config, Path(self.config.workspace_root).resolve()
                    )
                    if selected_session:
                        return f"<RESUME_SESSION> {selected_session.name}", self.mode
                    continue

                elif stripped.lower().startswith("/install-skill"):
                    parts = stripped.split(" ", 1)
                    if len(parts) < 2 or not parts[1].strip():
                        console.print("[yellow]Usage: /install-skill <url>[/yellow]")
                        continue
                    url = parts[1].strip()
                    console.print(f"[cyan]Installing skill from {url}...[/cyan]")
                    from nimcode import skills

                    result = skills.install_skill(url)
                    console.print(f"[bold green]{result}[/bold green]")
                    continue

                elif stripped.lower() == "/model":
                    if self.config:
                        from nimcode.config import select_interactive_model

                        # Note: no client in TUI context — model switch won't generate summary
                        select_interactive_model(self.config)
                    else:
                        console.print(
                            "[red]Config object not available for model selection.[/red]"
                        )
                    continue

                elif stripped.lower() == "/compact":
                    console.print(
                        "[yellow]/compact is available once a session is active. "
                        "Start a session first, then use /compact mid-conversation "
                        "to free up context.[/yellow]"
                    )
                    continue

                elif stripped.lower() == "/skills":
                    from nimcode import skills

                    skill_list = skills.list_skills()
                    if not skill_list:
                        console.print(
                            "[dim]No skills installed yet. Use /install-skill <url> to install one.[/dim]"
                        )
                    else:
                        table = Table(
                            title="Installed Skills",
                            border_style="cyan",
                            show_header=True,
                            header_style="bold cyan",
                        )
                        table.add_column("Skill Name", style="bold")
                        for s in skill_list:
                            table.add_row(f"/{s}")
                        console.print(Panel(table, border_style="cyan", padding=(1, 2)))
                    continue

                elif stripped.lower() == "/init":
                    if self.config:
                        from nimcode.core.agent import run_init_command

                        # client not available in TUI context — falls back to template
                        run_init_command(self.config)
                    else:
                        console.print(
                            "[red]Config object not available for AGENTS.md initialization.[/red]"
                        )
                    continue

                elif stripped.startswith("/"):
                    # Try skill lookup first
                    from nimcode import skills

                    parts = stripped.split(" ", 1)
                    skill_name = parts[0][1:]
                    installed = skills.list_skills()
                    if skill_name in installed:
                        content = skills.get_skill_content(skill_name)
                        rest_of_prompt = parts[1] if len(parts) > 1 else ""
                        console.print(
                            f"[dim]Injecting context from skill: {skill_name}...[/dim]"
                        )
                        user_input = f"<skill_context>\n{content}\n</skill_context>\n\n{rest_of_prompt}"
                        return user_input, self.mode
                    else:
                        console.print(
                            f"[yellow]Unknown command: {stripped}. "
                            f"Type /help or ? for a list of available commands.[/yellow]"
                        )
                        continue

                return user_input, self.mode

            except KeyboardInterrupt:
                return None, None
            except EOFError:
                return None, None
