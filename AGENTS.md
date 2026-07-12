# Nimcode Agent Architecture & Instructions (AGENTS.md)

**Notice:** This document is live-injected into the system prompt of every agent interacting with the `nimcode` project. It serves as the definitive source of truth for the project's architecture, security boundaries, tool schemas, and contribution guidelines. If you are an AI tasked with modifying `nimcode`, you **must** adhere to these immutable rules.

## 1. Project Layout & Core Components

`nimcode` follows a strict `src-layout`. Production code lives entirely under `src/nimcode/`.

```
nimcode/
├── .github/
│   └── workflows/
│       └── ci.yml          # GitHub Actions CI workflow (ruff lint/format, pytest)
├── src/
│   └── nimcode/
│       ├── cli.py          # Typer CLI entry point — defines all global commands (status, run, resume, etc.)
│       ├── config.py       # Config management (nimcode.toml), model lists, and interactive model selection
│       ├── context.py      # Workspace map harvester (generates the directory tree for the prompt)
│       ├── skills.py       # Dynamic skill loading & execution logic
│       ├── core/
│       │   ├── agent.py    # Main agent REPL loop (run_agent_loop, call_nim_with_retry, turn persistence)
│       │   ├── prompts.py  # System prompt builder, CoT reasoning stripping (strip_reasoning_spans)
│       │   └── state.py    # AgentState and AgentMode enums (Plan, Build, YOLO)
│       ├── tools/
│       │   ├── registry.py # Tool registration (@register_tool), AVAILABLE_TOOLS_SPEC array, executor
│       │   ├── base.py     # Path sandboxing (resolve_and_validate_path) & human-in-the-loop preview logic (render_preview)
│       │   ├── fs.py       # Native file system tools (read_file, write_file, list_files, glob_files)
│       │   ├── cmd.py      # Execution tools (execute_command, background process management, run_tests)
│       │   ├── browser.py  # Playwright visual verification tool (screenshot_page)
│       │   ├── patch.py    # AST-aware precision search-and-replace (patch_file_blocks, patch_file_ast)
│       │   ├── swarm.py    # Subagent spawning and messaging (spawn_agent, send_message_to_agent)
│       │   └── web.py      # Web search tool module (web_search via DuckDuckGo HTML parser)
│       └── ui/
│           ├── tui.py      # prompt_toolkit interactive prompt screen (NimcodeTUI), live slash command handling (like /compact, /recap, etc.)
│           └── console.py  # Rich-based UI components (welcome banner, help screens)
├── tests/                  # Unit and integration test suite
│   ├── test_cmd.py         # Tests for synchronous and background command execution
│   ├── test_config_persist.py # Tests for workspace config and state migration
│   ├── test_missing_tools.py # Tests for filesystem boundaries and symbol search
│   ├── test_normalize_messages.py # Tests for message normalization, system consolidation, and role sequence rules
│   ├── test_preview.py     # Diff rendering and tool preview assertions
│   ├── test_run_tests.py   # Test suite runner integration tests
│   ├── test_swarm_extra.py # Subagent lifecycle and tool schema validation tests
│   └── test_web_search.py  # Tests for web search registration, parameter checking, and parser mock validations
├── pyproject.toml          # PEP-517 build metadata & dependency graph
├── setup.py                # Legacy editable install support
├── AGENTS.md               # This file — live-injected into the agent prompt
└── README.md               # User-facing documentation
```

## 2. Core Architectural Rules

1. **Never Bypass the Human Gate:** `nimcode` is built around a mandatory human-in-the-loop approval mechanism. Any tool that alters state (writes a file, executes a command, patches code) must first pass through `render_preview` in `src/nimcode/tools/base.py` and be presented to the user for `y/n` approval. (Exception: When `state == "YOLO"`, the engine auto-approves but still displays the preview).
2. **Strict Sandbox Enforcement:**
   * All file path arguments passed to tools must run through `resolve_and_validate_path` in `src/nimcode/tools/base.py`. This blocks directory traversal attacks (e.g., `../../../etc/shadow`).
   * Environment variables injected into subprocesses must run through `scrub_env` in `src/nimcode/tools/base.py` to mask sensitive tokens (e.g., `API_KEY`, `TOKEN`, `SECRET`).
3. **Consolidated Dotfile Architecture:** All persistent session data (chat histories, context handoff summaries, backups of overwritten files, and model selections) must reside in the global project config directory resolved by `get_project_nimcode_dir(workspace_root)` (e.g., `~/.nimcode/<project>/`). Never dump these artifacts into the user's working repository.
4. **The Three-Mode State Machine:** The agent operates under three explicit modes defined in `src/nimcode/core/state.py`:
   * `AWAITING_PLAN_APPROVAL` (Plan Mode): The agent is denied native tool access (`tools=AVAILABLE_TOOLS_SPEC` is removed from the API payload) and forced to output text wrapped in `<plan>...</plan>`.
   * `EXECUTING` (Build Mode): Standard interactive execution where each tool call is previewed and approved.
   * `YOLO` (YOLO Mode): High-speed autonomous mode that bypasses the human gate but enforces a loop limit.
5. **Conceal Chain-of-Thought (CoT):** Powerful models (like Nemotron or DeepSeek) produce raw reasoning logic in `<think>` tags. `strip_reasoning_spans` in `src/nimcode/core/prompts.py` actively strips these out to prevent UI bloat. Do not disable this mechanism.
6. **Windows Terminal Compatibility:** Ensure any direct prints or `typer.secho` calls are safe for `cp1252` encoding by preserving the `sys.stdout.reconfigure(encoding="utf-8")` hack at the top of the CLI entrypoints.
7. **Session Serialization:** `log_turn()` in `src/nimcode/core/agent.py` persists all messages (user inputs, assistant responses, and tool calls) to JSON files. Do not introduce Python-specific objects into the tool argument dictionaries; everything must remain fully JSON serializable to allow session resumption (`/resume`).

## 3. Background Processes & Visual Verification

Nimcode supports complex development workflows like starting a frontend server, visually verifying the UI, and stopping the server.

1. **Background Processes:** Synchronous commands that block forever (e.g., `npm run dev`, `python -m http.server`) must use the asynchronous background tools (`start_background_process`, `get_process_output`, `stop_process`) located in `src/nimcode/tools/cmd.py`.
2. **Visual Verification Loop:** When working on frontend/UI tasks, agents must visually verify changes using the Playwright-powered `screenshot_page` tool in `src/nimcode/tools/browser.py`. The agent must:
   - Start the server (`start_background_process`).
   - Capture a screenshot (`screenshot_page` with the local URL).
   - Evaluate the UI against a checklist (no unstyled defaults, proper hierarchy, etc.).
   - Iterate if needed, and finally stop the server (`stop_process`).
3. **Process Cleanup:** All running processes are tracked in `BACKGROUND_PROCESSES` and are forcefully terminated upon session exit via an `atexit` hook in `src/nimcode/core/agent.py` or when the `/kill` slash command is issued.

## 4. Subagent Swarms

Nimcode supports spawning multi-agent swarms for parallel task delegation.

* **Autonomy:** Background subagents (invoked via `spawn_agent` in `src/nimcode/tools/swarm.py`) run entirely autonomously in separate threads. They do not pause for user approval and do not render previews.
* **Communication:** Agents communicate via `send_message_to_agent`.
* **State Management:** Active agents are stored in the global `ACTIVE_SUBAGENTS` dictionary and can be monitored via the `/team` slash command.

## 5. Tool Registration Flow

To add a new tool to Nimcode, strictly follow this procedure:

1. **Create the Tool Module:** Create or update a file in `src/nimcode/tools/` (e.g., `git.py`).
2. **Define the Schema & Decorator:** Use the `@register_tool` decorator with the exact OpenAI-compatible JSON schema. It **must** include the `type: "function"` and `function: {...}` wrappers.
   ```python
   from nimcode.tools.registry import register_tool

   @register_tool("git_commit", schema={
       "type": "function",
       "function": {
           "name": "git_commit",
           "description": "Commits changes.",
           "parameters": { "type": "object", "properties": { ... } }
       }
   })
   def git_commit_tool(args: dict, workspace_root: Path, config: Any) -> str:
       # Must resolve paths and scrub secrets!
       ...
   ```
3. **Add Preview Logic:** Add a branch for the tool's `tool_name` inside `render_preview` in `src/nimcode/tools/base.py` to return a clear, user-friendly preview string.
4. **Update System Prompts:** If the tool requires specific behavioral instructions, update `SYSTEM_PROMPT_TEMPLATE` and the XML fallback examples in `src/nimcode/core/prompts.py`.
5. **Export the Tool:** Add an import statement (`from . import git`) to `src/nimcode/tools/__init__.py` to trigger registration at startup.

## 6. Docker Execution Sandbox

By default, synchronous `execute_command` and `run_tests` operations run inside a sandboxed Docker container:
* **Image:** `python:3.11-slim` (overridable via `nimcode.toml`).
* **Mount:** The workspace root is mounted into `/workspace`. The host filesystem is inaccessible.
* **Network:** Docker bridge is enabled (allows `pip install`, `curl`).
* **Lifecycle:** The container is destroyed immediately after execution (`--rm`).

If Docker is unavailable or explicitly disabled (`disable_docker = true` in config), execution falls back to direct, unsandboxed host subprocess execution.

## 7. Known Security Constraints & System Limits

1. **Subagent Execution:** Swarm agents execute without the human-in-the-loop preview gate.
2. **Environment Scrubbing:** The `scrub_env` logic masks keys containing explicit keywords (`API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `_PW`, `_PWD`, `CREDENTIAL`, `PRIVATE_KEY`). It does not dynamically scrub completely arbitrary or unmarked variables.
3. **Inline Command Arguments:** Secrets passed directly in the command string (e.g., `mysql -p<secret>`, `curl -H "Authorization: Bearer <secret>"`, or `--token=<secret>`) are scrubbed via regex from the command preview and execution logs to prevent visual leaks, but they still execute in the shell.
4. **Log Truncation:** To prevent token window explosions, tool outputs exceeding `config.output_cap` are truncated. This uses a smart (head + tail) truncation strategy to preserve both the initiating context and the final crash logs or summaries.

## 8. Third-Party Model Constraints & Normalization

To ensure smooth operation when calling models like MiniMax-M3 or Zhipu GLM-5.2 via OpenAI-compatible endpoints, Nimcode enforces these requirements:
1. **Explicit Token Limits (`max_tokens`):** Completion requests require `max_tokens` (defaulting to `4096`). Failing to specify this parameter causes certain endpoints (notably MiniMax-M3 on integrate.api.nvidia.com) to fail silently and return empty completions (`choices=[]`), leading to errors.
2. **Consolidated System Messages:** System messages are extracted and merged into a single system message situated strictly at index `0` of the `messages` array. Interleaved system messages are combined to prevent API rejections.
3. **Alternating Roles & Empty Checks:** Message arrays must alternate strictly between `user` and `assistant`. If the sequence starts with an `assistant` role, a dummy bridging `user` message is inserted. A dummy `user` message is also appended if the payload is system-only, preventing GLM/MiniMax validation errors.

