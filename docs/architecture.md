# Architecture

## Architecture at a Glance

```
User Prompt
    │
    ▼
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  cli.py:app     │────▶│  config.py           │────▶│  context.py     │
│  Typer entry    │     │  load_config()       │     │  workspace_map  │
│  point & TUI    │     │  get_project_dir()   │     │  (+ AST hints)  │
│  boot           │     │  save_last_model()   │     │                 │
└─────────────────┘     └──────────────────────┘     └────────┬────────┘
         │                                                    │
         │            ┌──────────────────────┐                │
         └───────────▶│  prompts.py          │◀───────────────┘
                      │  build_system_prompt │
                      │  strip_reasoning_... │
                      └─────────┬────────────┘
                                │
                                ▼
                      ┌──────────────────────┐     ┌──────────────────┐
                      │  core/agent_tui.py   │────▶│  NIM API (OpenAI │
                      │  run_agent_loop_tui  │     │  compat)         │
                      │  + agent.py retry/   │◀────│  call_nim_with_  │
                      │  prune / normalize   │     │  retry()         │
                      └─────────┬────────────┘     └──────────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
      ┌────────────────┐ ┌──────────────┐ ┌────────────────┐
       │ tools/registry │ │ ui/        │ │ core/state.py  │
       │ TOOL_REGISTRY  │ │ rust_bridge│ │ AgentState     │
       │ 19 tools       │ │ RustTUI    │ │ Plan / Build  │
       │                │ │ + nim-tui  │ │                │
      └────────────────┘ └──────────────┘ └────────────────┘
              │
     ┌────────┼────────┬────────┬────────┐
     ▼        ▼        ▼        ▼        ▼
   fs.py   cmd.py  patch.py  swarm.py web.py/browser.py
                          │
                          ▼
              ┌────────────────────────┐
              │  ~/.nimcode/<project>/ │
              │  sessions/*.jsonl      │
              │  backups/* + manifest  │
              │  model.json            │
              │  summary.md            │
              │  process_logs/*.log    │
              └────────────────────────┘
```

**Key invariant:** Every file write or command execution passes through `resolve_and_validate_path` (`src/nimcode/tools/base.py:11`) and `render_preview` (`src/nimcode/tools/base.py:262`) before the human gate. Shared helpers `backup_file()` (`:206`) and `coerce_output_cap()` (`:232`) keep the tools DRY.

## Project Structure

```
nimcode/
├── .github/
│   └── workflows/
│       ├── ci.yml                  # ruff + pytest (ubuntu+windows × py3.10–3.12), rust (fmt/clippy/test), wheel-contents check
│       └── release.yml             # v* tags → per-platform wheels (win/linux-musl/macOS arm64+x86_64) + sdist → GitHub Release
├── src/
│   └── nimcode/
│       ├── __init__.py             # __version__ (single source; pyproject reads it dynamically)
│       ├── __main__.py             # python -m nimcode → cli.app
│       ├── cli.py                  # Typer CLI: run / start / status / refresh-models / resume / init / stats
│       ├── config.py               # Config dataclass, load/save/validate, AVAILABLE_MODELS, model.json persistence, show_changes(), select_interactive_session()
│       ├── config_models.py        # Live NIM model catalog (fetch/cache/filter), MODEL_CONTEXT_TOKENS, apply_model_context(), effective_budget()
│       ├── context.py              # is_ignored(), load_gitignore_rules(), generate_workspace_map()
│       ├── skills.py               # fetch_skill() → preview → commit_skill() / discard_skill(); install_skill(), list_skills(), get_skill_content() → ~/.nimcode/skills/
│       ├── bin/                    # nim-tui sidecar binary — gitignored, built per-platform by release.yml
│       ├── core/
│       │   ├── agent.py            # call_nim_with_retry(), prune_history(), normalize_messages(), log_turn(), load_session(), list_sessions(), open_session_path(), migrate_legacy_session(), run_init_command(), session stats
│       │   ├── agent_tui.py        # run_agent_loop_tui() — the only agent loop; handle_command() incl. /install-skill + /<skill>, model handoff
│       │   ├── prompts.py          # SYSTEM_PROMPT_TEMPLATE, build_system_prompt(), strip_reasoning_spans(), extract_plan_block()
│       │   └── state.py            # AgentState (AWAITING_PLAN_APPROVAL / EXECUTING) + AgentMode
│       ├── tools/                  # 19 tools in AVAILABLE_TOOLS_SPEC
│       │   ├── __init__.py         # Imports fs/cmd/patch/swarm/browser/web (triggers registration); re-exports shared symbols
│       │   ├── registry.py         # @register_tool, TOOL_REGISTRY, AVAILABLE_TOOLS_SPEC, execute_local_tool() + spawn caps
│       │   ├── base.py             # resolve_and_validate_path(), is_valid_tool_call(), render_preview(), scrub_text/env(), backup_file() + list_backups(), coerce_output_cap(), truncate_output(), _check_url()/_is_url_allowed() SSRF guard
│       │   ├── fs.py               # read_file, write_file, list_files, glob_files, patch_file_blocks, search_grep, search_symbols, atomic_write_text()
│       │   ├── cmd.py              # execute_command, run_tests, execute_test_runner(), tests_passed(), background processes, Docker/host dispatch
│       │   ├── patch.py            # patch_file_ast — AST-aware symbol replacement (supports Class.method)
│       │   ├── swarm.py            # spawn_agent, send_message_to_agent, kill_agent, list_agents, ACTIVE_SUBAGENTS, resume_subagent_thread()
│       │   ├── browser.py          # screenshot_page via Playwright + per-request SSRF route guard
│       │   └── web.py              # web_search via DuckDuckGo HTML parser
│       └── ui/
│           ├── console.py          # show_help() + print_welcome() banner
│           ├── output.py           # Console singleton, symbol(), unicode/color detection, TUI_INSTANCE routing, safe_text()
│           └── rust_bridge.py      # RustTUI — loopback-socket NDJSON bridge to the nim-tui sidecar (sole TUI); find_rust_binary(), poll_events()
├── tui-rs/                         # Rust sidecar source (ratatui/crossterm)
├── tests/                          # pytest suite (~145 tests); protocol_samples.json + fake_sidecar.py pin the Python↔Rust wire format
├── pyproject.toml                  # PEP 517 metadata; [dev] extra pins ruff/pytest
├── setup.py                        # Bare setup() shim
├── requirements.txt                # Runtime deps mirror
├── uv.lock                         # uv lockfile
├── AGENTS.md                       # This file — live-injected into the agent prompt
└── README.md                       # User-facing documentation
```

> **Note on legacy paths:** Local `.nimcode/` or `.nimcode_sessions/` directories inside a workspace are automatically migrated to the centralized `~/.nimcode/<project>_<hash>/` location on first run (`src/nimcode/config.py:14`).

## Agent Loop Internals

### `call_nim_with_retry()` — `src/nimcode/core/agent.py:400`

```
prune_history(messages, budget)
  → normalize_messages(disable_native_tools)
    → OpenAI client call (thread + 50ms poll for ESC)
      → empty-choices guard
        → fallback on "auto tool choice" / "degraded function" errors (flip disable_native_tools + rebuild prompt)
        → fallback on context-length errors (halve budget, recurse)
        → exponential backoff (2^attempt * 2s) for transient errors (4xx client errors fail fast, except 429)
```

- **Pruning** (`prune_history`, `src/nimcode/core/agent.py:256`): keeps system + first user + newest `max_turns_retained` tool groups; tool results beyond cutoff are truncated to 150 chars; then groups `(assistant+tool_calls, tool results)` into units and drops oldest units until `count_message_tokens() <= budget`. Uses `tiktoken` if available, otherwise `len//4` (`src/nimcode/core/agent.py:239`).
- **Normalization** (`normalize_messages`, `src/nimcode/core/agent.py:109`): when `disable_native_tools` is true, converts `tool_calls` → `<tool_call name="…">JSON</tool_call>` strings and `tool` → `user [Tool Response]`; merges interleaved `system` messages into one at index 0; collapses consecutive same-role `user`/`assistant` messages (never splitting assistant+tool/results pairs); bridges `assistant`-first sequences with a dummy user message; guarantees the payload is never system-only.
- **Cancellation:** A daemon thread runs the blocking `client.chat.completions.create()` call. The main loop polls `TUI_INSTANCE._interrupted` or `msvcrt.kbhit()` (Win32) every 50ms and raises `KeyboardInterrupt("ESC_CANCELLED")` for instant cancellation (`src/nimcode/core/agent.py:459`).
- **XML fallback:** When native `tools` are disabled (or endpoint rejects `auto`), the agent uses `parse_xml_tool_calls()` (`src/nimcode/core/agent.py:68`) to extract `<tool_call name="…">JSON</tool_call>` from raw content into `MockToolCall` objects with `_clean_json_args()` fence stripping.
- **Resilience:** errors are classified by HTTP status / openai exception type (retry 429/5xx/connection/timeout, fail fast on 400/401/403/404/422); empty-`choices` responses raise a descriptive error; context overflow halves the budget per retry and gives up below 1000 tokens. Empty model turns are re-driven (max 2) instead of going silent.

### `run_agent_loop_tui()` — `src/nimcode/core/agent_tui.py:78`

Async wrapper that:

1. Starts `RustTUI.start_async()` (spawns the `nim-tui` sidecar) as a concurrent task.
2. Calls `_run_agent_loop_tui_internal()` which manages:
   - Legacy session migration (`migrate_legacy_session()`) and `.jsonl` session setup (`open_session_path()`).
   - `log_turn()` persistence after every message (append-only JSONL).
   - Blocking work (tools, tests, NIM calls) via `asyncio.to_thread`; `poll_events()` between tool calls applies Shift+Tab / Ctrl+C mid-batch.
   - `handle_command()` dispatch for all slash commands (`:179`), including `/install-skill` and `/<skill>` injection (which returns `"resume"` to continue straight to the API call).
   - `initial_state` sync (`AWAITING_PLAN_APPROVAL`/`EXECUTING`).
   - Model-dependent `tools_arg` selection (`:626`).
   - `pending_summary_injection` handoff (`<model_handoff_context>`).
   - Loop-limit guard (`max_loop_iterations`, 40) with user prompt to resume/redirect/cancel.

`run_agent_loop_tui()` is the only agent loop (the legacy ANSI loop was removed).

### Model handoff

`select_model()` in `src/nimcode/ui/rust_bridge.py` implements the **model-switch handshake**:

1. Ask the **old** model to summarize the session into `summary.md`.
2. Persist the new `model.json`.
3. Feed `summary.md` as `<model_handoff_context>` to the **new** model for acknowledgment.

## Context Harvesting & System Prompt

### Workspace Map — `src/nimcode/context.py:185`

- Reads `.gitignore` once via shared `load_gitignore_rules()` (`:150`), reused by all search/list tools.
- Recursively walks the workspace up to **depth 8** or **500 files** (`max_files`), filtering with `is_ignored()` (which enforces `DEFAULT_EXCLUDES` like `.git`, `node_modules`, `__pycache__`, `.nimcode*`, binary extensions, and full `.gitignore` semantics including `!` negation, `**` recursion, and last-rule-wins).
- For Python files < 50 KB, extracts `ast` imports and annotates `(imports: …)` for local imports.
- Returns `"<root>/\n|-- …"` tree string injected as `{workspace_map}`.

### System Prompt — `src/nimcode/core/prompts.py:5`

```
You are NIM-Engineer, a senior terminal coding agent. You do real work with tools, verify it, and report concisely.
GOLDEN OPERATING LOOP (follow for every task):
1. UNDERSTAND: Never guess file contents ... Read before you edit — always.
2. REPRODUCE/CONFIRM ... 3. MINIMAL EDIT: ... Prefer patch_file_blocks ...
4. VERIFY: Re-read ... run_tests ... Never claim done without tool evidence.
5. REPORT: ... TOOL STRATEGY: one logical step per turn ...
MODES: Plan Mode (read-only tools, then <plan>) / Build Mode (execute; asks only for sensitive or risky actions).
Background Processes: start_background_process for dev servers ...
Visual Verification Loop: screenshot → checklist → fix → repeat.
COMMUNICATION: terse, no chain-of-thought dumps.
Workspace Root: {workspace_root}
Current Workspace Structure:
{workspace_map}
<agents_md_rules path="AGENTS.md">…</agents_md_rules>
CRITICAL: You MUST strictly obey …
[+ XML fallback block if disable_native_tools]
```

- `build_system_prompt()` (`src/nimcode/core/prompts.py:46`) probes three `AGENTS.md` locations: `<workspace>/AGENTS.md` → `~/.nimcode/<project>/AGENTS.md` → `<workspace>/.nimcode/AGENTS.md`, capping injected rules at 8000 chars so a huge `AGENTS.md` can't blow context.
- **Live injection:** The agent loop rebuilds this prompt **every turn** with a fresh `generate_workspace_map()` so file renames, new files, and updated `AGENTS.md` are visible immediately without restart (`src/nimcode/core/agent_tui.py:613`).
- **XML fallback appendix** (`src/nimcode/core/prompts.py:78`) teaches the model to emit `<tool_call name="…">JSON</tool_call>` when `disable_native_tools=true`, with the same read-before-editing / verify-after-editing discipline.
- **Reasoning stripping** (`strip_reasoning_spans`, `src/nimcode/core/prompts.py:132`) removes `<think>`, `<thought>`, `<reasoning>` blocks (including unclosed truncated ones) from model output before display.

## UI Subsystem (Rust TUI, Console)

### `nim-tui` sidecar — `tui-rs/src/main.rs`, `src/nimcode/ui/rust_bridge.py`

The fullscreen TUI is a Rust `ratatui`/`crossterm` sidecar, not Python:

- **Transport:** Python spawns `nim-tui` (found via `find_rust_binary()`: `NIM_TUI_BIN`, bundled `nimcode/bin/`, `tui-rs/target/release/`, then `PATH`) with `NIM_TUI_PORT`; the sidecar connects back over a loopback socket and speaks UTF-8 NDJSON (`add_message`, `update` sidebar, `set_working`, `prompt_select`, `input`/`select_result`/`interrupt`/`eof`/`mode_cycle` events). Every message type is pinned in `tests/protocol_samples.json`, checked by both `cargo test` and `tests/test_bridge_protocol.py`.
- **Terminal ownership:** the sidecar enables raw mode + alternate screen and is the sole stdin reader, so each keystroke is handled exactly once (key-release events are ignored — Windows reports press *and* release).
- **Layout:** message list + sidebar (mode, model, token bar, cwd, todos, subagents, background procs) + single-line input with inline selectors (↑/↓, Enter, Esc) for model/session/backup/config.
- **Cancel/exit:** `Ctrl+C` with text clears the line, with an empty line sends `interrupt` (`_interrupted`, cancels API calls and remaining tools); `Ctrl+D` sends `eof` (sticky `_eof_requested`, clean exit even mid-turn). `Python` side never kills the app out from under a live turn.
- **Autocomplete:** typing `/` opens a filter-as-you-type popup fed by Python's `set_completions` catalog (`SLASH_COMMANDS` + installed skills); `Tab` accepts, `↑/↓` navigate, `Enter` sends, `Esc` dismisses. Key-release events are ignored so Windows press+release pairs register once.

### Console & Output Abstraction — `src/nimcode/ui/output.py:1`, `src/nimcode/ui/console.py:1`

- `output.py` provides `unicode_enabled()` / `color_enabled()` probes (checks `NIMCODE_ASCII`, `NIMCODE_PLAIN`, `NO_COLOR`, stdout encoding) and `symbol()` / `safe_text()` shims so `•`/`❯`/`─`/`↳`/`🔧` degrade to `-`/`>`/`-`/`->`/`[tool]` on `cp1252` terminals. `TUI_INSTANCE` routes `echo`/`secho`/`print_text` to the TUI when active, otherwise to `rich`/`typer`.
- `console.py` renders the `nimcode` ASCII banner (`print_welcome()`) with `Columns[logo | hints]` and the help table / markdown help (TUI vs CLI variants).

## Swarm & Sub-Agents (`/team`)

`src/nimcode/tools/swarm.py:118`:

- `spawn_agent_logic(role, task_prompt, workspace_root, config)` builds a fresh `messages = [system + role suffix, user task_prompt]` with a live `workspace_map`, seeds `ACTIVE_SUBAGENTS[agent_id]` (`id = sub_<8-hex-uuid>`, with per-agent `depth`), caps at 10 active subagents / nesting depth 3 (fork-bomb guard), and spawns `resume_subagent_thread()`.
- `resume_subagent_thread()` (`:178`) runs on a **thread-local config copy** carrying the agent's depth, so the depth cap accumulates across generations (sibling threads never race on shared state). It loops `max_steps` (`max_loop_iterations`) calling the NIM API (same `AVAILABLE_TOOLS_SPEC` + `normalize_messages`), executing tools via `execute_local_tool()` directly (no human gate), and appending `assistant` / `tool` turns to `sub["messages"]`. Aborts after 5 consecutive invalid tool calls; empty model responses mark `ERROR` (no zombie `RUNNING`); cooperative `KILLED` stays `KILLED` (never overwritten to `ERROR`). Thread safety via `sub["lock"]` (`threading.Lock`) + `_SUBAGENTS_LOCK` (`RLock`, `:13`).
- Monitor live with `/team` (sidebar also polls `ACTIVE_SUBAGENTS` every turn).
- Each run has a wall-clock budget (`subagent_timeout_s`, default 900 s); exceeding it marks the agent `TIMED_OUT`.
- Transcripts are appended to `~/.nimcode/<project>_<hash>/subagents/<id>.jsonl`; finished, killed and timed-out agents are dropped from `ACTIVE_SUBAGENTS` 10 minutes after they end (transcripts stay).
- Subagents run shell commands only inside Docker.
- Interact with `send_message_to_agent` (injects a new `user` message and resumes the thread) or terminate with `kill_agent`.
