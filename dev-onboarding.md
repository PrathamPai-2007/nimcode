# nimcode Developer Onboarding

Welcome to the `nimcode` codebase! This document outlines the architecture, code structure, and design philosophy to help you contribute effectively.

## Architecture & Design Philosophy

`nimcode` is built around a **State Machine REPL**. The core philosophy is safety and transparency over full autonomy. The agent cannot act without your permission, and it cannot sneak reasoning tokens into the terminal output.

### State 1: `AWAITING_PLAN_APPROVAL`
- **Model:** `config.plan_model` (e.g., `deepseek-v4-pro`)
- **Tools:** `None`
- **Behavior:** The agent evaluates the workspace map and user prompt. It is forced to output a structured `<plan>` without making any tool calls. The user must approve this plan.

### State 2: `EXECUTING`
- **Model:** `config.fast_model` (e.g., `deepseek-v4-flash`)
- **Tools:** `AVAILABLE_TOOLS_SPEC` (`read_file`, `write_file`, `execute_command`)
- **Behavior:** The agent iteratively calls tools. Each tool call is intercepted, previewed to the user (via unified diffs or command strings), and requires a `y/n` prompt. Output is fed back into the loop.

## Codebase Structure

*   **`setup.py`**: Configures the project for global installation via `pip install -e .` and registers the `nimcode` entry point.
*   **`main.py`**: The `typer` CLI entry point (`nimcode`). Handles `start`, `run`, `status`, and `resume` commands, dynamically targets the current working directory, provides fallback loading for `~/.nimcode/.env`, and initializes the OpenAI client pointing to NVIDIA NIM.
*   **`agent.py`**: The heart of the REPL (`run_agent_loop`). Manages state transitions, invokes `call_nim_with_retry` (handles rate-limit backoffs), parses tool calls, and handles `prune_history` to prevent context bloat.
*   **`tools.py`**: Contains the sandboxed tool implementations (`execute_local_tool`), the JSON schema definitions (`AVAILABLE_TOOLS_SPEC`), and safety measures:
    *   `resolve_and_validate_path`: Prevents `../` traversal escapes.
    *   Environment scrubbing: Strips `API_KEY`, `SECRET`, `TOKEN` before running `subprocess`.
    *   Auto-backup: Uses `shutil.copy2` to snapshot files before `write_file` overwrites them.
*   **`prompts.py`**: Generates the system prompt, handles `<plan>` extraction, and robustly strips `<think>...</think>` tags to hide chain-of-thought artifacts from the user UI.
*   **`context.py`**: Recursively maps the working directory, automatically respecting `.gitignore` and hardcoded exclusions (`DEFAULT_EXCLUDES` like `.env` and `.nimcode/sessions/`).
*   **`config.py`**: Parses `nimcode.toml` into a Python Dataclass and manages interactive `/model` switching logic via `AVAILABLE_MODELS`.
*   **`tui.py`**: Drives the beautiful `prompt_toolkit`-powered CLI for `nimcode start`, featuring mode switching (`Shift+Tab`) and slash-command interception.
*   **`skills.py`**: Handles external `Claude Code`-style skill installations via ZIP extraction, Git cloning, or raw file downloads (`/install-skill`).

## High-Impact Edge Cases Handled

1.  **Terminal Encoding Issues:** Windows `cp1252` encoding crashes on emojis/diffs are mitigated by `sys.stdout.reconfigure(encoding="utf-8")` at the top of the main modules.
2.  **Context Window Bloat:** Handled dynamically in `agent.py` (`prune_history`) by tracking a `context_budget_tokens` limit and summarizing older, heavy tool outputs (`>150` characters).
3.  **Runaway Execution:** Bound by `config.max_loop_iterations`. The agent must pause and ask the user to resume if it hits this limit without human text input.
4.  **Process Interruption:** `log_turn()` writes to `.nimcode/sessions/session_*.json` after every API call or tool result, allowing stateful recovery via `python main.py resume`.

## Contributing

When adding new features, ensure they respect the core sandboxing principles. Never print raw API responses without passing them through `strip_reasoning_spans`, and ensure any new tools are strictly path-bounded and require user approval.
