# nimcode Agent Instructions (AGENTS.md)

**Notice:** This file is intended to be read by autonomous AI coding assistants, LLMs, or other agents that are tasked with modifying, understanding, or interfacing with the `nimcode` project.

## Project Layout

The project follows a standard **`src-layout`** packaging structure. All production code lives under `src/nimcode/`:

```
nimcode/
├── src/
│   └── nimcode/
│       ├── cli.py          # Typer CLI entry point — all nimcode commands defined here
│       ├── config.py       # Config dataclass, AVAILABLE_MODELS list, interactive selectors
│       ├── context.py      # Workspace map harvester (generate_workspace_map)
│       ├── skills.py       # Skills loader
│       ├── core/
│       │   ├── agent.py    # Main agent REPL loop (run_agent_loop, call_nim_with_retry)
│       │   ├── prompts.py  # System prompt builder, CoT stripper (strip_reasoning_spans)
│       │   └── state.py    # AgentState / AgentMode enums
│       ├── tools/
│       │   ├── registry.py # @register_tool decorator, AVAILABLE_TOOLS_SPEC, execute_local_tool
│       │   ├── base.py     # resolve_and_validate_path, render_preview, is_valid_tool_call
│       │   ├── fs.py       # File read/write/list/glob tools
│       │   ├── cmd.py      # Shell execution (execute_command) & test runner
│       │   ├── patch.py    # AST-aware patch / search-replace tool
│       │   └── swarm.py    # Sub-agent spawning & messaging (ACTIVE_SUBAGENTS)
│       └── ui/
│           ├── tui.py      # prompt_toolkit interactive prompt screen (NimcodeTUI)
│           └── console.py  # Welcome banner, help listings
├── pyproject.toml          # PEP-517 build metadata & dependencies
├── setup.py                # Editable install support (find_packages(where="src"))
├── AGENTS.md               # This file — live-injected into every agent turn
└── README.md
```

## Core Rules of the `nimcode` Architecture

If you are an AI tasked with adding features or fixing bugs in `nimcode`, you **must** adhere to these immutable rules:

1. **Never Bypass the Human Gate:** `nimcode` is built around a human-in-the-loop approval mechanism (`Build Mode` and `Plan Mode`). Do not add tools or features that execute terminal commands or write files invisibly without going through `render_preview` and `typer.prompt` approval. (Note: When the user explicitly enters `YOLO Mode` (`state == "YOLO"` or `/yolo`), `nimcode` will render the exact tool preview first and then auto-approve execution).
2. **Respect the Sandbox & Consolidated Directory Architecture:**
   * All file system operations (`read_file`, `write_file`) must pass through `resolve_and_validate_path` in `src/nimcode/tools/base.py` to prevent directory traversal attacks.
   * All terminal operations (`execute_command`) must scrub the environment dictionary of sensitive tokens (e.g., `API_KEY`, `TOKEN`, `SECRET`) before calling `subprocess.run`. This logic lives in `src/nimcode/tools/cmd.py`.
   * **Global `~/.nimcode/<project>/` Directory:** All `nimcode`-generated system files, session histories, backups, and handoff summaries MUST be stored in the global project directory returned by `get_project_nimcode_dir(workspace_root)` in `src/nimcode/config.py`. Never write these files into the user's workspace root:
     - Session Logs: `~/.nimcode/<project>/sessions/session_YYYYMMDD_HHMMSS.json`
     - File Backups: `~/.nimcode/<project>/backups/timestamp_filename`
     - Context Handoff Briefings: `~/.nimcode/<project>/summary.md`
     - Persisted Model Selection: `~/.nimcode/<project>/model.json`
3. **The Three-Mode Architecture & State Loop:** `nimcode` relies on `AWAITING_PLAN_APPROVAL` (`Plan Mode`), `EXECUTING` (`Build Mode`), and `YOLO` (`YOLO Mode`). These are defined as enums in `src/nimcode/core/state.py`.
   * Do not pass `tools=AVAILABLE_TOOLS_SPEC` to the API client when in `AWAITING_PLAN_APPROVAL`. The agent must output text bounded by `<plan>...</plan>` first.
4. **Conceal Chain-of-Thought (CoT):** DeepSeek/Nemotron/Mistral models may output reasoning spans inside `<think>` tags. `strip_reasoning_spans` in `src/nimcode/core/prompts.py` explicitly strips these using regex so the CLI user is not overwhelmed by raw reasoning text. Maintain and test this regex if you change how text is processed.
5. **Windows Terminal Compatibility:** When adding CLI outputs using `typer.secho` or `print`, rely on the UTF-8 `sys.stdout.reconfigure` hack already present at the top of `src/nimcode/cli.py`, `src/nimcode/core/agent.py`, and `src/nimcode/ui/tui.py`. Do not remove this, or Windows users will suffer `cp1252` charmap crashes.
6. **Live `AGENTS.md` Injection:** `build_system_prompt` in `src/nimcode/core/prompts.py` automatically inspects the workspace root for `AGENTS.md` and injects its contents into the system prompt right before every turn inside `run_agent_loop`. If `AGENTS.md` is updated mid-session via `/init` or manual edits, the AI agent instantly sees the new rules on the very next prompt.
7. **Decorator-Based Tool Registry:** All tools must be registered using the `@register_tool` decorator in `src/nimcode/tools/registry.py`. Do **not** manually add entries to `AVAILABLE_TOOLS_SPEC` — this list is assembled automatically from registered tools at import time. See the "How to Add a New Tool" section below.

## Slash Commands & Interactive UI (TUI)

If tasked to add a new user-facing command (like `/model`, `/skills`, `/init`, `/resume`, `/yolo`, etc.):
1. **Intercept in TUI:** Add the detection logic inside `src/nimcode/ui/tui.py` (`get_prompt()`) so it's captured on `nimcode start` (`nimcode`). Also ensure `WordCompleter` includes the command.
2. **Intercept in REPL:** Add the identical detection logic inside `src/nimcode/core/agent.py` (`get_user_turn()` inside `run_agent_loop`) to ensure mid-session commands are equally supported.
3. **Dynamic State & Context Handoff:** Do *not* restart the agent loop to apply state changes. Update the references on the active `config` object in memory. When switching models mid-session (`/model`), `nimcode` automatically checks if new prompts occurred since the last switch; if so, it prompts the old model to write `summary.md` and injects it into the new model for seamless context continuity. This logic lives in `src/nimcode/config.py` (`select_interactive_model`).

## How to Add a New Tool

To equip `nimcode` with a new capability (e.g., `search_web` or `git_commit`), follow this strict sequence:

1. **Create or choose a module** under `src/nimcode/tools/` (e.g., `src/nimcode/tools/git.py`).
2. **Define Schema & Register:** Use the `@register_tool` decorator from `src/nimcode/tools/registry.py`. The decorator accepts the OpenAI-compatible JSON schema and automatically adds the tool to `AVAILABLE_TOOLS_SPEC` and `TOOL_REGISTRY`.
   ```python
   from nimcode.tools.registry import register_tool

   @register_tool(schema={
       "name": "git_commit",
       "description": "Commits all staged changes with the given message.",
       "parameters": { ... }
   })
   def git_commit(args: dict, workspace_root, config) -> str:
       ...
   ```
3. **Render Preview:** Update `render_preview(tool_name, args, workspace_root)` in `src/nimcode/tools/base.py` to return a clear, human-readable string explaining exactly what the tool is about to do. This is displayed to the user in Build Mode before approval.
4. **Import in `__init__.py`:** Add `from . import git` (or equivalent) at the top of `src/nimcode/tools/__init__.py` to trigger decorator registration at startup. Also add the public symbols to `__all__`.
5. **Sandbox Check:** If the tool reads or writes data, ensure it calls `resolve_and_validate_path` and operates strictly within `workspace_root`. Respect `config.output_cap` so massive responses don't blow up the context window.

## Important Note on Session Persistence

`nimcode` logs every message (both user and tool) via `log_turn()` in `src/nimcode/core/agent.py` to timestamped files inside `~/.nimcode/<project>/sessions/session_*.json`. When making changes to the message schemas or tool formats, ensure they remain JSON serializable so the `resume` (`/resume` or `nimcode resume --latest`) functionality continues to work.

## Global Installation & Environment Fallbacks

`nimcode` is designed to be installed globally via `pip install -e .`. The package entry point is defined in `pyproject.toml` as `nimcode = "nimcode.cli:app"`. When invoked globally, the `workspace_root` dynamically resolves to the directory from which the user ran the command (`Path.cwd()`).

Additionally, if a local `.env` is not present in the workspace, `src/nimcode/cli.py` is configured to look for a global environment file at `~/.nimcode/.env`. Do not assume that `NVIDIA_API_KEY` is completely missing just because it is not in the local workspace directory!

## Default Model

The default model for both `fast_model` and `plan_model` is **Nemotron 3 Nano 30B** (`nvidia/nemotron-3-nano-30b-a3b`), configured in the `Config` dataclass in `src/nimcode/config.py`. This can be overridden per-workspace via `nimcode.toml` or persisted mid-session via `/model` (saved to `~/.nimcode/<project>/model.json`).
