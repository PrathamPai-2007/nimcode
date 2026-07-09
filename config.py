import os
import tomli
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from pathlib import Path


@dataclass
class Config:
    fast_model: str = "deepseek-ai/deepseek-v4-flash"
    plan_model: str = "deepseek-ai/deepseek-v4-pro"
    mode: str = "build"
    temperature_fast: float = 0.7
    temperature_plan: float = 0.2
    max_loop_iterations: int = 10
    workspace_root: str = "."
    timeout: int = 60
    output_cap: int = 2000
    context_budget_tokens: int = 16000
    auto_backup: bool = True
    denylist: List[str] = field(default_factory=list)


def save_last_used_model(
    config: Config, model_id: str, workspace_root: Optional[Path] = None
):
    import json
    import time

    try:
        root = (
            workspace_root if workspace_root else Path(config.workspace_root).resolve()
        )
        nimcode_dir = root / ".nimcode"
        nimcode_dir.mkdir(parents=True, exist_ok=True)
        model_file = nimcode_dir / "model.json"
        data = {
            "fast_model": model_id,
            "plan_model": model_id,
            "updated_at": time.time(),
        }
        model_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_config(config_path="nimcode.toml") -> Config:
    import json

    cfg = Config()
    # 1. Check current working directory
    if os.path.exists(config_path):
        with open(config_path, "rb") as f:
            data = tomli.load(f)
        cfg = Config(**data)
    # 2. Check global home fallback
    elif (Path.home() / ".nimcode" / "nimcode.toml").exists():
        with open(Path.home() / ".nimcode" / "nimcode.toml", "rb") as f:
            data = tomli.load(f)
        cfg = Config(**data)
    # 3. Check nimcode source directory (useful for editable installs)
    elif (Path(__file__).parent / "nimcode.toml").exists():
        with open(Path(__file__).parent / "nimcode.toml", "rb") as f:
            data = tomli.load(f)
        cfg = Config(**data)

    # Check if a persisted model exists for this project in .nimcode/model.json
    try:
        model_json_path = Path(cfg.workspace_root).resolve() / ".nimcode" / "model.json"
        if model_json_path.exists():
            with open(model_json_path, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)
                if "fast_model" in mdata and mdata["fast_model"]:
                    cfg.fast_model = mdata["fast_model"]
                if "plan_model" in mdata and mdata["plan_model"]:
                    cfg.plan_model = mdata["plan_model"]
    except Exception:
        pass

    return cfg


AVAILABLE_MODELS = {
    "1": {"name": "Mistral Small", "id": "mistralai/mistral-small-4-119b-2603"},
    "2": {
        "name": "Mistral Large",
        "id": "mistralai/mistral-large-3-675b-instruct-2512",
    },
    "3": {
        "name": "Mixtral 8x7B Instruct",
        "id": "mistralai/mixtral-8x7b-instruct-v0.1",
    },
    "4": {"name": "DeepSeek V4-Pro", "id": "deepseek-ai/deepseek-v4-pro"},
    "5": {"name": "DeepSeek V4-Flash", "id": "deepseek-ai/deepseek-v4-flash"},
    "6": {"name": "Nemotron 3 Nano 30B", "id": "nvidia/nemotron-3-nano-30b-a3b"},
    "7": {"name": "Nemotron 3 Super 120B", "id": "nvidia/nemotron-3-super-120b-a12b"},
    "8": {"name": "Nemotron 3 Ultra 550B", "id": "nvidia/nemotron-3-ultra-550b-a55b"},
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
    app = Application(layout=layout, key_bindings=kb, erase_when_done=True)

    try:
        selection_id = app.run()
    except Exception:
        selection_id = None

    if selection_id is None:
        typer.secho("\nModel selection cancelled.", fg=typer.colors.YELLOW)
        return

    selected_model = None
    for m in AVAILABLE_MODELS.values():
        if m["id"] == selection_id:
            selected_model = m
            break

    if selected_model:
        old_model = config.fast_model
        new_model_id = selected_model["id"]
        save_last_used_model(config, new_model_id, workspace_root)

        # If there is prior user prompt history and we have an active client, generate or reuse summary.md
        if (
            client is not None
            and messages is not None
            and len([m for m in messages if m.get("role") == "user"]) >= 1
        ):
            try:
                from agent import call_nim_with_retry, log_turn
                from prompts import strip_reasoning_spans

                summary_file = (
                    workspace_root / ".nimcode" / "summary.md"
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
                        client, summary_req_messages, None, old_model, config
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
                    summary_text = summary_file.read_text(encoding="utf-8")

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
