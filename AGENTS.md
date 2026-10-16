# Nimcode Agent Architecture & Instructions (AGENTS.md)

**Notice:** This document is live-injected into the system prompt of every agent interacting with the `nimcode` project (`src/nimcode/core/prompts.py:38`). It serves as the definitive source of truth for the project's architecture, security boundaries, tool schemas, and contribution guidelines. If you are an AI tasked with modifying `nimcode`, you **must** adhere to these immutable rules.

---

## 1. Project Layout & Core Components

`nimcode` follows a strict `src-layout`. Production code lives entirely under `src/nimcode/`. Tests live under `tests/`. All persistent runtime state lives under `~/.nimcode/<project>_<hash>/` (see §3).

```
nimcode/
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions: ruff lint/format + pytest matrix (py 3.10–3.12)
├── src/
│   └── nimcode/
│       ├── __init__.py             # __version__ = "0.6.0"
│       ├── __main__.py             # python -m nimcode → cli.app
│       ├── cli.py                  # Typer CLI: run / start / status / refresh-models / resume / init / stats (447 lines)
│       ├── config.py               # Config dataclass, load/save/validate, AVAILABLE_MODELS[10], interactive selectors, model.json persistence (1129 lines)
│       ├── config_models.py        # Live NIM model catalog (fetch/cache/filter), MODEL_CONTEXT_TOKENS, apply_model_context (342 lines)
│       ├── context.py              # is_ignored(), load_gitignore_rules(), generate_workspace_map() (261 lines)
│       ├── skills.py               # install_skill() / list_skills() / get_skill_content() → ~/.nimcode/skills/ (231 lines)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── agent.py            # call_nim_with_retry(), prune_history(), normalize_messages(), log_turn(), run_init_command(), legacy run_agent_loop() + stats (2453 lines)
│       │   ├── agent_tui.py        # run_agent_loop_tui() — async TUI loop (1185 lines), handle_command() incl. /install-skill + /<skill>, model handoff
│       │   ├── prompts.py          # SYSTEM_PROMPT_TEMPLATE (golden loop), build_system_prompt(), strip_reasoning_spans(), extract_plan_block() (175 lines)
│       │   └── state.py            # AgentState (AWAITING_PLAN_APPROVAL / EXECUTING / YOLO) + AgentMode
│       ├── tools/                  # 19 tools in AVAILABLE_TOOLS_SPEC
│       │   ├── __init__.py         # Imports fs/cmd/patch/swarm/browser/web (triggers registration); re-exports shared symbols
│       │   ├── registry.py         # @register_tool, TOOL_REGISTRY, AVAILABLE_TOOLS_SPEC, execute_local_tool() + spawn caps
│       │   ├── base.py             # resolve_and_validate_path(), is_docker_available(), is_valid_tool_call(), scrub_text/env(), backup_file(), coerce_output_cap(), truncate_output(), render_preview(), SENSITIVE_ENV_REGEX (484 lines)
│       │   ├── fs.py               # read_file, write_file, list_files, glob_files, patch_file_blocks, search_grep, search_symbols (678 lines)
│       │   ├── cmd.py              # execute_command, run_tests, execute_test_runner(), start_background_process, get_process_output, stop_process, Docker/host dispatch, BACKGROUND_PROCESSES (1124 lines)
│       │   ├── patch.py            # patch_file_ast — AST-aware symbol replacement (182 lines)
│       │   ├── swarm.py            # spawn_agent, send_message_to_agent, kill_agent, list_agents, ACTIVE_SUBAGENTS, resume_subagent_thread() with thread-local depth (437 lines)
│       │   ├── browser.py          # screenshot_page via Playwright + _is_url_allowed SSRF guard (215 lines)
│       │   └── web.py              # web_search via DuckDuckGo HTML parser (189 lines)
│       └── ui/
│           ├── __init__.py         # package marker
│           ├── console.py          # show_help() (TUI markdown vs CLI Rich table) + print_welcome() banner (111 lines)
│           ├── output.py           # Console singleton, symbol(), unicode/color detection, TUI_INSTANCE routing, safe_text() (164 lines)
│           └── rust_bridge.py      # RustTUI — loopback-socket bridge to the nim-tui sidecar (sole TUI) (566 lines)
├── tui-rs/                       # Rust sidecar source; binary bundled at src/nimcode/bin/nim-tui.exe
├── tests/                        # 61 tests (ruff + pytest green)
│   ├── test_cmd.py                 # execute_command host/timeout/truncate/blocklist
│   ├── test_config_persist.py      # Config persistence, validation & migration
│   ├── test_empty_turn.py          # Empty model-turn retry behavior
│   ├── test_missing_tools.py       # Sandbox & symbol search boundaries
│   ├── test_model_catalog.py       # Model catalog filtering & context windows
│   ├── test_normalize_messages.py  # Message normalization, system consolidation, role alternation
│   ├── test_preview.py             # Diff rendering & tool preview assertions
│   ├── test_retry_fallbacks.py     # API retry, XML fallback & context-overflow paths
│   ├── test_run_tests.py           # Test runner integration
│   ├── test_slash_commands.py      # SLASH_COMMANDS catalog vs handler coverage
│   ├── test_swarm_extra.py         # Subagent lifecycle & schema validation
│   └── test_web_search.py          # Web search schema + parser mocks
├── pyproject.toml                  # PEP 517 metadata: typer, openai, rich, prompt_toolkit, playwright
├── setup.py                        # Legacy editable install shim (find_packages where="src")
├── requirements.txt                # Full runtime deps mirror (== pyproject for deploy)
├── uv.lock                         # uv lockfile
├── AGENTS.md                       # This file — live-injected into the agent prompt
└── README.md                       # User-facing documentation (with full TOC)
```

> **Migration shims:** `get_project_nimcode_dir()` (`src/nimcode/config.py:14`) auto-migrates legacy `~/.nimcode/<name>` (unhashed) and local `.nimcode/` into the hashed global dir on first access.

---

## 2. Core Architectural Rules

1. **Never Bypass the Human Gate:** Every mutation tool (`write_file`, `patch_file_*`, `execute_command`, `run_tests`, etc.) must pass through `render_preview()` in `src/nimcode/tools/base.py:262` and await `y/n` in **Build** mode. YOLO still displays the preview before auto-approving (`src/nimcode/core/agent_tui.py:1018`). Swarm threads are the sole exception (see §4).
2. **Strict Sandbox Enforcement:**
   - All file-path arguments **must** flow through `resolve_and_validate_path()` (`src/nimcode/tools/base.py:11`) — it resolves against `workspace_root` and rejects `is_relative_to` escapes. Backups go through shared `backup_file()` (`:206`), which sanitizes `safe_rel_name` via `replace(os.sep, "_")` — never re-implement backup logic per tool.
   - Every subprocess inherits `scrub_env()` (which **drops** sensitive variables) and its command preview + output pass `scrub_text()` (`src/nimcode/tools/base.py`) to mask Bearer/`Authorization: Basic` tokens, `--password`/`--token` flags, `KEY=value` env lines, AWS/NVIDIA/OpenAI/GitHub/Slack/Google keys, JWTs, `scheme://user:pass@` URLs, and 32+ char alphanumeric keys.
3. **Consolidated Global Config:** All persistent state (chat histories `sessions/`, backups `backups/`, `process_logs/`, `model.json`, `summary.md`, optional `AGENTS.md` copy) **must** reside under `get_project_nimcode_dir(workspace_root)` → `~/.nimcode/<name>_<hash>/`. Never write these into the user's repository.
4. **Three-Mode State Machine** (`src/nimcode/core/state.py:4`):
   - `AWAITING_PLAN_APPROVAL` (Plan): `tools_arg = None` — model must emit `<plan>...</plan>` (`src/nimcode/core/prompts.py:159`); no tool calls are sent.
   - `EXECUTING` (Build): full `AVAILABLE_TOOLS_SPEC` with preview gate; cap `max_loop_iterations` (default 20).
   - `YOLO` (YOLO): same tools, auto-approve, auto-test after file mutations, cap **50**.
5. **Conceal Chain-of-Thought:** Strip `<think>` / `<thought>` / `<reasoning>` spans via `strip_reasoning_spans()` (`src/nimcode/core/prompts.py:132`) before display or logging. Handles unclosed truncated tags.
6. **Windows Terminal Compatibility:** Preserve `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top of `src/nimcode/cli.py:32` and `src/nimcode/config.py:8`. Route all prints via `src/nimcode/ui/output.py` (`symbol()`, `safe_text()`, `TUI_INSTANCE` routing at `:114`, `unicode_enabled()`/`color_enabled()` respecting `NIMCODE_ASCII`/`NIMCODE_PLAIN`/`NO_COLOR`).
7. **Session Serialization:** `log_turn()` (`src/nimcode/core/agent.py:648`) persists every turn as JSON (atomic temp+replace write, `default=str` fallback; including `tool_calls` arrays with `id`/`type`/`function.name`/`function.arguments`). Never introduce non-JSON-serializable objects into tool arguments or `messages`; session resumption (`nimcode resume` / `/resume`) depends on this.
8. **No Duplication:** Reuse `backup_file()`, `coerce_output_cap()` (`src/nimcode/tools/base.py:232`), and `load_gitignore_rules()` (`src/nimcode/context.py:150`) — grep for existing helpers before writing new ones. Fix shared functions at the root cause, not per caller.

---

## 3. Background Processes & Visual Verification

### Background Processes — `src/nimcode/tools/cmd.py:822`

Long-running servers (`npm run dev`, `python -m http.server`, `flask run`, `vite`, `uvicorn`, `ng serve`, `rails server`, `next dev`) **must** use the asynchronous tools, not `execute_command` (which rejects dev-server binaries/phrases with a prompt to use `start_background_process` — `src/nimcode/tools/cmd.py:487`):

- `start_background_process({"command": "npm run dev"})` → `bg_N` via `docker run -d` or host `Popen` → `process_logs/bg_N.log`.
- `get_process_output({"process_id": "bg_1"})` → `docker logs` or logfile tail + running/exited status.
- `stop_process({"process_id": "bg_1"})` → `docker kill/rm -f` or `taskkill /F /T` (Win) / `terminate→kill` (POSIX).

Global `BACKGROUND_PROCESSES` is terminated via `atexit` hook and `/kill` (`src/nimcode/tools/cmd.py:76`).

### Visual Verification Loop — `src/nimcode/tools/browser.py:118`

For any frontend/UI task the agent **must**:

1. `start_background_process` the dev server.
2. `screenshot_page({"url": "http://localhost:3000"})` (or `{"html_file": "dist/index.html"}` → `file://` via `Path.as_uri()`).
3. Evaluate the base64 screenshot against the checklist (unstyled defaults, consistent spacing, hierarchy, no clipping, loading/error states).
4. Fix via `patch_file_*` / `write_file`, re-screenshot, repeat.
5. `stop_process` when verified.

Playwright retries: 3 attempts, `+5s` timeout backoff, viewport `1280×720`, `domcontentloaded` + 1.5 s settle (`src/nimcode/tools/browser.py:45`). Targets pass the DNS-resolving SSRF guard (`_is_url_allowed`, `:14`); only public hosts and explicit localhost are screenshottable.

---

## 4. Subagent Swarms — `src/nimcode/tools/swarm.py:118`

- **Autonomy:** Swarm threads spawned by `spawn_agent` execute tools **without** the human gate and **without** `render_preview()`. They run the full NIM loop (`resume_subagent_thread()`) for `max_steps = 50` in YOLO else `max_loop_iterations`, via `execute_local_tool()` directly.
- **Lifecycle:** `spawn_agent_logic()` seeds `ACTIVE_SUBAGENTS[agent_id]` with `messages = [system+role, user prompt]`, then `resume_subagent_thread()` drives the model → tool → log cycle. `send_message_to_agent_logic()` appends a new `user` message and resumes. `kill_agent_logic()` sets `KILLED`. `list_agents_logic()` renders `id | role | status | snippet`.
- **Monitoring:** `/team` in the TUI lists live subagents; the sidebar polls `ACTIVE_SUBAGENTS` every turn (`src/nimcode/core/agent_tui.py:526`).
- **Thread safety:** `ACTIVE_SUBAGENTS` guarded by `_SUBAGENTS_LOCK` (`RLock`) + per-agent `threading.Lock` to prevent concurrent thread starts.
- **Depth:** each agent record carries `depth`; threads run on a `copy.copy()` of config stamped with that depth, so the depth-3 cap accumulates across generations. Never mutate the shared `config` from a swarm thread.

---

## 5. Tool Registration Flow

To add a new tool, follow this procedure **exactly**:

1. **Create/update** `src/nimcode/tools/<module>.py`:
   ```python
   from nimcode.tools.registry import register_tool

   MY_SCHEMA = {
       "type": "function",
       "function": {
           "name": "my_tool",
           "description": "One-line description.",
           "parameters": {
               "type": "object",
               "properties": {
                   "path": {"type": "string", "description": "Relative path."}
               },
               "required": ["path"]
           }
       }
   }

   @register_tool("my_tool", MY_SCHEMA)
   def my_tool_tool(args: dict, workspace_root: Path, config: Any) -> str:
       target = resolve_and_validate_path(args["path"], workspace_root)
       # ... scrub_env/scrub_text if touching env/text ...
       return "result string (will be truncate_output() capped)"
   ```

2. **Validation:** Add a branch to `is_valid_tool_call()` (`src/nimcode/tools/base.py:70`) checking required fields for `my_tool`.

3. **Preview:** Add a branch to `render_preview()` (`src/nimcode/tools/base.py:262`) returning a human-readable preview (diff, command string, or arg summary).

4. **System prompt (if needed):** Extend `SYSTEM_PROMPT_TEMPLATE` (`src/nimcode/core/prompts.py:5`) and the XML fallback examples (`src/nimcode/core/prompts.py:78`) for `disable_native_tools` mode.

5. **Export:** `from . import <module>` in `src/nimcode/tools/__init__.py:3` to trigger decorator registration at import time. Re-export any shared symbols via `__all__`.

6. **Tests:** Add coverage in `tests/test_*`.

---

## 6. Docker Execution Sandbox

See `src/nimcode/tools/cmd.py:129` / `src/nimcode/tools/base.py:43`:

- **Image:** `python:3.11-slim` overridable via `nimcode.toml:docker_image`.
- **Build:** `docker run --rm --init --cap-drop ALL --security-opt no-new-privileges:true -v <workspace>:/workspace -w /workspace [-p port:port]* [docker_extra_args]* <image> sh -c "<command>"`
- **Background:** `docker run -d --name nimcode_bg_<id>` (`src/nimcode/tools/cmd.py:866`).
- **Detection:** `is_docker_available()` → `docker info` (5s timeout).
- **Validation:** `_validate_command_safety()` **warns** (does not block) on socket mounts, `--privileged`, `--cap-add`, host namespaces, and shell injection patterns. `docker_extra_args` are filtered by `_safe_docker_extra_args()`, which **drops** privileged/mount/device/namespace/seccomp flags.
- **Fallback:** If Docker is absent or `disable_docker=true`, the command runs on the host via `Popen(shell=True)` with `scrub_env()` and output annotated `[Note: Docker unavailable, fell back to host execution]`.
- **Timeout:** `Popen.communicate(timeout=…)` → `TimeoutExpired` → `taskkill/terminate → kill` and `[timed out after Ns]`.

---

## 7. Known Security Constraints & System Limits

1. **Subagent bypass:** Swarm threads do not render previews or wait for approval — constrain their prompts carefully.
2. **Env scrubbing coverage:** `SENSITIVE_ENV_REGEX` (`src/nimcode/tools/base.py:457`) removes variables matching `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `_PW`, `_PWD`, `CREDENTIAL`, `PRIVATE_KEY`, `AWS_*`, `DATABASE_URL`, `POSTGRES_PASSWORD`, and similar. Arbitrarily named secrets without these substrings are **not** scrubbed — name credentials conventionally.
3. **Inline command secrets:** `scrub_text()` redacts `-p…`, `--password=`, `--token=`, `Bearer …`, `x-api-key: …`, `AKIA…`, `eyJ…JWT`, `postgres://…:…@…` — but the secrets still execute in the shell. Never log raw command strings outside the scrubbed preview.
4. **Output truncation:** `truncate_output()` (`src/nimcode/tools/base.py:240`) uses head+tail truncation (`[dropped N chars from the middle]`) so both initial context and final stack traces survive. Increase `output_cap` if needed via `nimcode.toml` or `/config`.
5. **Line-range reads:** For large outputs/files, use `read_file` with `start_line`/`end_line` or rely on the truncation hint.

---

## 8. Model & Token Constraints — `src/nimcode/core/agent.py:109` / `src/nimcode/core/prompts.py:132`

To satisfy NVIDIA NIM endpoints (notably **MiniMax-M3**, **GLM-5.2**) via the OpenAI-compatible API:

1. **Explicit `max_tokens`:** Always send `max_tokens` (default `4096` from `config.max_tokens`). Omitting it makes certain endpoints return `choices=[]`, which `call_nim_with_retry()` detects and raises as `empty choices` (`src/nimcode/core/agent.py:478`).
2. **System-message consolidation:** `normalize_messages()` merges all `system` roles into a single entry at `messages[0]`. Interleaved system messages are combined with `\n\n`.
3. **Alternating roles:** Consecutive same-role `user`/`assistant` messages are collapsed (`\n\n` join, `tool_calls` concatenated). If the sequence starts with `assistant`, a dummy `user: "Let's continue…"`. If the payload is `system`-only, append dummy `user: "Hello. I am setting up the workspace session."` — prevents GLM/MiniMax validation errors.
4. **XML fallback:** On `auto / tool choice` or `degraded function` errors, nimcode flips `config.disable_native_tools = true`, rebuilds the system prompt via `build_system_prompt(..., disable_native_tools=True)` (adds `<tool_call>` examples), and retries with `tools=None` so the model emits XML tool calls parsed by `parse_xml_tool_calls()` (`src/nimcode/core/agent.py:68`, fence-stripped by `_clean_json_args()`).
5. **Context-window fallback:** On `max context length / context window / too many tokens` errors, halve `context_budget_tokens` and recurse with `prune_history(..., context_budget_tokens // 2)` (`src/nimcode/core/agent.py:578`).
6. **Token counting:** `count_message_tokens()` tries `tiktoken.get_encoding("cl100k_base")`, else `len//4 + 4` overhead per message (`src/nimcode/core/agent.py:239`). `prune_history()` groups `(assistant+tool_calls, tool results)` into units so paired tool interactions are never split.

---

## 9. UI & Prompt Invariants

- **Live prompt injection:** `run_agent_loop_tui()` rebuilds `messages[0]` every turn with a fresh `generate_workspace_map()` + `build_system_prompt()` so renames, deletes, and `AGENTS.md` edits apply without restart (`src/nimcode/core/agent_tui.py:613`).
- **TUI routing:** When `TUI_INSTANCE` is set (`src/nimcode/ui/output.py:114`), `echo`/`secho`/`print_text` route to the sidecar TUI buffer; otherwise to `rich`/`typer`. It is cleared on session exit. Markdown is rendered inside the Rust sidecar (`tui-rs/src/main.rs`).
- **Mode sync:** Each turn syncs `state` with `config.mode` (`src/nimcode/core/agent_tui.py:567`) so `Shift+Tab` and `/model` side-effects are reflected immediately.
- **Cancellation:** Long NIM calls run in a daemon thread polled every 50ms for `TUI_INSTANCE._interrupted` or `msvcrt.kbhit()` → `KeyboardInterrupt("ESC_CANCELLED")` (`src/nimcode/core/agent.py:459`).

---

## 10. Testing & CI Expectations

- **Matrix:** `py 3.10 / 3.11 / 3.12` on `ubuntu-latest` (` .github/workflows/ci.yml`).
- **Steps:** `ruff check` → `ruff format --check` → smoke import `nimcode.*` + `nimcode.ui.rust_bridge`/`nimcode.ui.output` → `nimcode status` with `NVIDIA_API_KEY=dummy-key-for-ci` → `is_docker_available()` warn-only → `pytest`.
- **Before submitting:** `ruff format . && ruff check . && pytest -v` must pass. Keep tool args JSON-serializable and add `tests/test_*` for any new tool or state transition.
