import os
from pathlib import Path
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.completion import WordCompleter
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.table import Table

console = Console()


class NimcodeTUI:
    def __init__(self, config=None):
        self.config = config
        self.mode = "Plan Mode"
        self.bindings = KeyBindings()

        @self.bindings.add("tab")
        def _(event):
            b = event.app.current_buffer
            if b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=True)

        @self.bindings.add("s-tab")
        def _(event):
            b = event.app.current_buffer
            if b.complete_state:
                b.complete_previous()
            else:
                if self.mode == "Plan Mode":
                    self.mode = "Build Mode"
                elif self.mode == "Build Mode":
                    self.mode = "YOLO Mode"
                else:
                    self.mode = "Plan Mode"
                event.app.invalidate()

        @self.bindings.add("escape")
        def _(event):
            if event.app.current_buffer.text:
                event.app.current_buffer.reset()
            else:
                event.app.current_buffer.reset()

        @self.bindings.add("backspace")
        def _(event):
            b = event.app.current_buffer
            if b.cursor_position > 0:
                b.delete_before_cursor(count=event.arg)

        self.style = Style.from_dict(
            {
                "bottom-toolbar": "bg:default #aaaaaa",
                "separator": "#666666",
                "sep-plan": "bold #0088ff",
                "sep-build": "bold #00cc44",
                "sep-yolo": "bold #ff2200",
                "prompt": "bold #00ffff",
                "mode": "bg:#0088ff #ffffff bold",
                "mode-build": "bg:#00aa44 #ffffff bold",
                "mode-yolo": "bg:#ff2200 #ffffff bold",
            }
        )

    def show_welcome(self, workspace_root: str):
        left_text = Text()
        left_text.append(
            "       _                         _      \n", style="bold cyan"
        )
        left_text.append(
            " _ __ (_)_ __ ___   ___ ___   __| | ___ \n", style="bold cyan"
        )
        left_text.append(
            "| '_ \\| | '_ ` _ \\ / __/ _ \\ / _` |/ _ \\\n", style="bold cyan"
        )
        left_text.append(
            "| | | | | | | | | | (_| (_) | (_| |  __/\n", style="bold cyan"
        )
        left_text.append(
            "|_| |_|_|_| |_| |_|\\___\\___/ \\__,_|\\___|\n\n", style="bold cyan"
        )
        left_text.append("NVIDIA NIM Coding Agent\n", style="dim")

        right_text = Text()
        right_text.append("\n\n")
        right_text.append(
            "• Shift+Tab to instantly cycle between Plan, Build & YOLO modes\n",
            style="dim",
        )
        right_text.append("• To use commands and skills, use /\n", style="dim")
        right_text.append(
            "• Run /help or ? for a list of nimcode commands\n", style="dim"
        )

        columns = Columns(
            [Align.center(left_text), right_text], expand=True, padding=(0, 4)
        )
        console.print(columns)
        console.print()

    def show_help(self):
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
        table.add_row("/resume", "Show and access old chat sessions")
        table.add_row("/model", "Choose active AI model (Mistral, DeepSeek, etc.)")
        table.add_row("/skills", "Show installed agent skills")
        table.add_row(
            "/install-skill <link>", "Install new skill from raw .md, .zip, or git repo"
        )
        table.add_row("/exit / /quit", "Exit the nimcode application")
        table.add_row("Ctrl + C / ESC", "Cancel the current prompt / exit")

        panel = Panel(table, border_style="cyan", padding=(1, 2))
        console.print(panel)
        console.print()

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
            from config import AVAILABLE_MODELS
            from prompt_toolkit.formatted_text import FormattedText

            term_width = shutil.get_terminal_size().columns
            if self.mode == "Plan Mode":
                sep_class = "class:sep-plan"
            elif self.mode == "YOLO Mode":
                sep_class = "class:sep-yolo"
            else:
                sep_class = "class:sep-build"
            "─" * term_width + "\n"

            pwd = os.getcwd()
            model_id = (
                self.config.fast_model
                if (self.config and self.mode != "Plan Mode")
                else (self.config.plan_model if self.config else "Unknown")
            )

            model_name = model_id
            if self.config:
                for m in AVAILABLE_MODELS.values():
                    if m["id"] == model_id:
                        model_name = m["name"]
                        break

            left_str = f"── ({pwd}) "
            right_str = f" ({model_name}) ──"

            padding = term_width - len(left_str) - len(right_str)
            if padding < 0:
                avail = term_width - len(right_str) - 6
                if avail > 5:
                    left_str = f"── (..{pwd[-(avail - 2) :]}) "
                else:
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
                "/skills",
                "/install-skill",
                "/init",
                "/quit",
                "/exit",
            ]
            try:
                import skills

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
                            "[bold red]⚡ Switched to YOLO Mode (Auto-accept enabled).[/bold red]"
                        )
                    continue
                elif stripped.lower() == "/resume":
                    session_dir = (
                        Path(self.config.workspace_root).resolve()
                        / ".nimcode"
                        / "sessions"
                    )
                    if not session_dir.exists() or not list(session_dir.glob("*.json")):
                        console.print(
                            "[yellow]No old chats found in this workspace.[/yellow]"
                        )
                        continue

                    sessions = sorted(
                        list(session_dir.glob("*.json")),
                        key=lambda x: x.stat().st_mtime,
                        reverse=True,
                    )

                    table = Table(title="Old Chats", border_style="cyan")
                    table.add_column("ID", style="bold cyan")
                    table.add_column("Date", style="dim")
                    table.add_column("First Prompt", style="white")

                    for idx, s_path in enumerate(sessions):
                        import json

                        try:
                            with open(s_path, "r", encoding="utf-8") as f:
                                data = json.load(f)
                                first_prompt = next(
                                    (
                                        m["content"]
                                        for m in data
                                        if m.get("role") == "user"
                                    ),
                                    "Empty session",
                                )
                                first_prompt = (
                                    (first_prompt[:50] + "...")
                                    if len(first_prompt) > 50
                                    else first_prompt
                                )
                        except Exception:
                            first_prompt = "Error reading session"

                        import datetime

                        date_str = datetime.datetime.fromtimestamp(
                            s_path.stat().st_mtime
                        ).strftime("%Y-%m-%d %H:%M")
                        table.add_row(
                            str(idx + 1), date_str, first_prompt.replace("\n", " ")
                        )

                    console.print(Panel(table, border_style="cyan", padding=(1, 2)))
                    choice = self.session.prompt(
                        "Enter ID to resume (or 'c' to cancel): "
                    )
                    if choice.isdigit() and 1 <= int(choice) <= len(sessions):
                        selected_session = sessions[int(choice) - 1]
                        return f"<RESUME_SESSION> {selected_session.name}", self.mode
                    else:
                        console.print("[yellow]Resume cancelled.[/yellow]")
                    continue
                elif stripped.startswith("/install-skill "):
                    url = stripped.split(" ", 1)[1].strip()
                    console.print(f"[cyan]Installing skill from {url}...[/cyan]")
                    import skills

                    result = skills.install_skill(url)
                    console.print(f"[bold green]{result}[/bold green]")
                    continue
                elif stripped.lower() == "/model":
                    if self.config:
                        from config import select_interactive_model

                        select_interactive_model(self.config)
                    else:
                        console.print(
                            "[red]Config object not available for model selection.[/red]"
                        )
                    continue
                elif stripped.lower() == "/skills":
                    import skills

                    skill_list = skills.list_skills()
                    if not skill_list:
                        console.print(
                            "[dim]No skills installed yet. Use /install-skill <link> to install one.[/dim]"
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
                        from agent import run_init_command

                        run_init_command(self.config)
                    else:
                        console.print(
                            "[red]Config object not available for AGENTS.md initialization.[/red]"
                        )
                    continue
                elif stripped.startswith("/"):
                    import skills

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
                return user_input, self.mode
            except KeyboardInterrupt:
                return None, None
            except EOFError:
                return None, None
