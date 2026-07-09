# nimcode Agent Instructions (AGENTS.md)

**Notice:** This file is intended to be read by autonomous AI coding assistants, LLMs, or other agents that are tasked with modifying, understanding, or interfacing with the `nimcode` project.

## Core Rules of the `nimcode` Architecture

If you are an AI tasked with adding features or fixing bugs in `nimcode`, you **must** adhere to these immutable rules:

1. **Never Bypass the Human Gate:** `nimcode` is built around a human-in-the-loop approval mechanism (`Build Mode` and `Plan Mode`). Do not add tools or features that execute terminal commands or write files invisibly without going through `render_preview` and `typer.prompt` approval. (Note: When the user explicitly enters `YOLO Mode` (`state == "YOLO"` or `/yolo`), `nimcode` will render the exact tool preview first and then auto-approve execution).
2. **Respect the Sandbox & Consolidated Directory Architecture:**
   * All file system operations (`read_file`, `write_file`) must pass through `resolve_and_validate_path` in `tools.py` to prevent directory traversal attacks.
   * All terminal operations (`execute_command`) must scrub the environment dictionary of sensitive tokens (e.g., `API_KEY`, `TOKEN`, `SECRET`) before calling `subprocess.run`.
   * **Consolidated `.nimcode/` Directory:** All `nimcode`-generated system garbage, session histories, backups, and handoff summaries MUST be stored cleanly inside `.nimcode/` to keep the user's workspace root pristine:
     - Session Logs: `.nimcode/sessions/session_YYYYMMDD_HHMMSS.json`
     - File Backups: `.nimcode/backups/timestamp_filename`
     - Context Handoff Briefings: `.nimcode/summary.md`
3. **The Three-Mode Architecture & State Loop:** `nimcode` relies on `AWAITING_PLAN_APPROVAL` (`Plan Mode`), `EXECUTING` (`Build Mode`), and `YOLO` (`YOLO Mode`).
   * Do not pass `tools=AVAILABLE_TOOLS_SPEC` to the API client when in `AWAITING_PLAN_APPROVAL`. The agent must output text bounded by `<plan>...</plan>` first.
4. **Conceal Chain-of-Thought (CoT):** DeepSeek/Nemotron/Mistral models may output reasoning spans inside `<think>` tags. The `prompts.py` module explicitly strips these using regex (`strip_reasoning_spans`) so the CLI user is not overwhelmed by raw reasoning text. Maintain and test this regex if you change how text is processed.
5. **Windows Terminal Compatibility:** When adding CLI outputs using `typer.secho` or `print`, rely on the UTF-8 `sys.stdout.reconfigure` hack already present at the top of the main modules (`agent.py`, `main.py`, `tui.py`). Do not remove this, or Windows users will suffer `cp1252` charmap crashes.
6. **Live `AGENTS.md` Injection:** `build_system_prompt` automatically inspects the workspace root (`AGENTS.md` or `.nimcode/AGENTS.md`) and injects its contents into the system prompt right before every turn inside `run_agent_loop`. If `AGENTS.md` is updated mid-session via `/init` or manual edits, the AI agent instantly sees the new rules on the very next prompt.

## Slash Commands & Interactive UI (TUI)

If tasked to add a new user-facing command (like `/model`, `/skills`, `/init`, `/resume`, `/yolo`, etc.):
1. **Intercept in TUI:** Add the detection logic inside `tui.py` (`get_prompt()`) so it's captured on `nimcode start` (`nimcode`). Also ensure `WordCompleter` includes the command.
2. **Intercept in REPL:** Add the identical detection logic inside `agent.py` (`get_user_turn()` inside `run_agent_loop`) to ensure mid-session commands are equally supported.
3. **Dynamic State & Context Handoff:** Do *not* restart the agent loop to apply state changes. Update the references on the active `config` object in memory. When switching models mid-session (`/model`), `nimcode` automatically checks if new prompts occurred since the last switch; if so, it prompts the old model to write `.nimcode/summary.md` and injects it into the new model for seamless context continuity.

## How to Add a New Tool

To equip `nimcode` with a new capability (e.g., `search_web` or `git_commit`), follow this strict sequence:

1. **Define Schema:** Add the OpenAI-compatible JSON schema to `AVAILABLE_TOOLS_SPEC` in `tools.py`.
2. **Render Preview:** Update `render_preview(tool_name, args, workspace_root)` in `tools.py` to return a clear, human-readable string explaining exactly what the tool is about to do.
3. **Execute Logic:** Add the implementation to `execute_local_tool(tool_name, args, workspace_root, config)` in `tools.py`. Ensure you catch exceptions and return them as strings (do not crash the agent loop).
4. **Sandbox Check:** If the tool reads or writes data, ensure it operates strictly within `workspace_root` (or `.nimcode/`) and respects `config.output_cap` so massive responses don't blow up the context window.

## Important Note on Session Persistence

`nimcode` logs every message (both user and tool) via `log_turn()` in `agent.py` to timestamped files inside `.nimcode/sessions/session_*.json`. When making changes to the message schemas or tool formats, ensure they remain JSON serializable so the `resume` (`/resume` or `nimcode resume --latest`) functionality continues to work.

## Global Installation & Environment Fallbacks

`nimcode` is designed to be installed globally via `pip install -e .`. When invoked globally, the `workspace_root` dynamically resolves to the directory from which the user ran the command (`Cwd`).
Additionally, if a local `.env` is not present, `main.py` is configured to look for a global environment file at `~/.nimcode/.env`. Do not assume that `NVIDIA_API_KEY` is completely missing just because it is not in the local workspace directory!
