# Nimcode Agent Architecture & Instructions (AGENTS.md)

**Notice:** This document is live-injected into the system prompt of every agent interacting with the `nimcode` project (`build_system_prompt()` in `src/nimcode/core/prompts.py`). It serves as the definitive source of truth for the project's architecture, security boundaries, tool schemas, and contribution guidelines. If you are an AI tasked with modifying `nimcode`, you **must** adhere to these immutable rules.

> References use `file` + function/symbol names rather than line numbers — grep for the symbol.

---

## 1. Project Layout & Core Components

`nimcode` follows a strict `src-layout`. Production code lives entirely under `src/nimcode/`. Tests live under `tests/`. All persistent runtime state lives under `~/.nimcode/<project>_<hash>/` (see §2.3).

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
│       ├── cli.py                  # Typer CLI: run / start / status / config / refresh-models / resume / init / stats; --version
│       ├── config.py               # Config dataclass, load/save/validate, AVAILABLE_MODELS, model.json persistence, show_changes(), select_interactive_session()
│       ├── config_models.py        # Live NIM model catalog (fetch/cache/filter), MODEL_CONTEXT_TOKENS, apply_model_context(), effective_budget()
│       ├── context.py              # is_ignored(), load_gitignore_rules() (nested .gitignore + .git/info/exclude, cached), generate_workspace_map() (git ls-files, entry points + recent first)
│       ├── skills.py               # fetch_skill() → preview → commit_skill() / discard_skill(); install_skill(), list_skills(), get_skill_content() → ~/.nimcode/skills/
│       ├── bin/                    # nim-tui sidecar binary — gitignored, built per-platform by release.yml
│       ├── core/
│       │   ├── agent.py            # call_nim_with_retry(), prune_history(), normalize_messages(), log_turn(), load_session(), list_sessions(), open_session_path(), migrate_legacy_session(), run_init_command(), session stats
│       │   ├── agent_tui.py        # run_agent_loop_tui() — the only agent loop; handle_command() incl. /install-skill + /<skill>, model handoff
│       │   ├── prompts.py          # SYSTEM_PROMPT_TEMPLATE, build_system_prompt(), strip_reasoning_spans(), extract_plan_block()
│       │   └── state.py            # AgentState (AWAITING_PLAN_APPROVAL / EXECUTING / YOLO) + AgentMode
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
│           ├── console.py          # KEY_BINDINGS + COMMAND_HELP (single source for /help), show_help(), print_welcome()
│           ├── output.py           # Console singleton, symbol(), unicode/color detection, TUI_INSTANCE routing, safe_text()
│           └── rust_bridge.py      # RustTUI — loopback-socket NDJSON bridge to the nim-tui sidecar (sole TUI); find_rust_binary(), poll_events()
├── tui-rs/                         # Rust sidecar: main.rs (I/O loop), app.rs (state, input, history), ui.rs (drawing), theme.rs (palette + glyph tables); snapshots/ = TestBackend layouts
├── tests/                          # pytest suite (~145 tests); protocol_samples.json + fake_sidecar.py pin the Python↔Rust wire format
├── pyproject.toml                  # PEP 517 metadata; [dev] extra pins ruff/pytest
├── setup.py                        # Bare setup() shim
├── requirements.txt                # Runtime deps mirror
├── uv.lock                         # uv lockfile
├── AGENTS.md                       # This file — live-injected into the agent prompt
├── README.md                       # User-facing quick start
└── docs/                           # Developer docs (config, CLI, architecture, security, tools, testing, benchmarks)
```

> **Migration shims:** `get_project_nimcode_dir()` (`config.py`) auto-migrates legacy `~/.nimcode/<name>` (unhashed) and local `.nimcode/` into the hashed global dir on first access, never overwriting existing files. `migrate_legacy_session()` (`agent.py`) converts `<workspace>/.nimcode_session.json`.

---

## 2. Core Architectural Rules

1. **Never Bypass the Human Gate:** Every mutation tool (`write_file`, `patch_file_*`, `execute_command`, `run_tests`, etc.) must pass through `render_preview()` (`tools/base.py`) and await `y/n` in **Build** mode. YOLO still displays the preview before auto-approving. Stale typed-ahead input is discarded before each approval prompt. Swarm threads are the sole exception (see §4).
2. **Strict Sandbox Enforcement:**
   - All file-path arguments **must** flow through `resolve_and_validate_path()` (`tools/base.py`) — it resolves against `workspace_root` and rejects escapes. Backups go through shared `backup_file()`, which also records the original path in `backups/manifest.jsonl` — never re-implement backup logic per tool; read backups via `list_backups()`.
   - Every subprocess inherits `scrub_env()` (which **drops** sensitive variables) and its command preview + output pass `scrub_text()` to mask Bearer/`Authorization: Basic` tokens, `--password`/`--token` flags, `KEY=value` env lines, AWS/NVIDIA/OpenAI/GitHub/Slack/Google keys, JWTs, `scheme://user:pass@` URLs, and 32+ char alphanumeric keys.
   - Any outbound fetch of a user/model-supplied URL must pass `_is_url_allowed()` (`tools/base.py`), including every redirect hop.
3. **Consolidated Global Config:** All persistent state (chat histories `sessions/`, subagent transcripts `subagents/`, backups `backups/`, `process_logs/`, `model.json`, `summary.md`, optional `AGENTS.md` copy) **must** reside under `get_project_nimcode_dir(workspace_root)` → `~/.nimcode/<name>_<hash>/`. Never write these into the user's repository.
4. **Three-Mode State Machine** (`core/state.py`):
   - `AWAITING_PLAN_APPROVAL` (Plan): `tools_arg = None` — model must emit `<plan>...</plan>`; no tool calls are sent or recorded.
   - `EXECUTING` (Build): full `AVAILABLE_TOOLS_SPEC` with preview gate; cap `max_loop_iterations` (default 20).
   - `YOLO` (YOLO): same tools, auto-approve, auto-test after file mutations (`tests_passed()` checks the final `[exit code: N]`), cap **50**.
   - Mode changes (Shift+Tab) are applied between tool calls via `poll_events()`, so leaving YOLO mid-batch restores the gate for the remaining calls.
5. **Conceal Chain-of-Thought:** Strip `<think>` / `<thought>` / `<reasoning>` spans via `strip_reasoning_spans()` (`core/prompts.py`) before display or logging. Handles unclosed truncated tags.
6. **Windows Terminal Compatibility:** Preserve `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top of `cli.py` and `config.py`. Route all prints via `ui/output.py` (`symbol()`, `safe_text()`, `TUI_INSTANCE` routing, `unicode_enabled()`/`color_enabled()` respecting `NIMCODE_ASCII`/`NIMCODE_PLAIN`/`NO_COLOR`). Never write to stdout/stderr while the TUI is up — use `logging`.
7. **Session Serialization:** `log_turn()` (`core/agent.py`) **appends** one JSON line per message to `sessions/session_<ts>.jsonl` (`default=str` fallback; `tool_calls` arrays keep `id`/`type`/`function.name`/`function.arguments`). Read sessions only via `load_session()` / `list_sessions()` (they also accept legacy `.json` list files; resuming one copies it into a fresh `.jsonl`). Never introduce non-JSON-serializable objects into tool arguments or `messages`; never mutate an already-logged message — append a new one.
8. **No Duplication:** Reuse `backup_file()`/`list_backups()`, `coerce_output_cap()`, `truncate_output()`, `_is_url_allowed()`, `load_gitignore_rules()` (`context.py`) and `effective_budget()` — grep for existing helpers before writing new ones. Fix shared functions at the root cause, not per caller.
9. **Keep the event loop free:** In `agent_tui.py`, blocking work (tool execution, test runs, NIM calls, catalog refresh) runs via `asyncio.to_thread`. A tool that is already running cannot be interrupted; cancellation applies before the next call.

---

## 3. Background Processes & Visual Verification

### Background Processes — `start_background_process` (`tools/cmd.py`)

Long-running servers (`npm run dev`, `python -m http.server`, `flask run`, `vite`, `uvicorn`, `ng serve`, `rails server`, `next dev`) **must** use the asynchronous tools, not `execute_command` (which rejects dev-server commands with a prompt to use `start_background_process`):

- `start_background_process({"command": "npm run dev"})` → `bg_N` via `docker run -d` or host `Popen` → `process_logs/bg_N.log`.
- `get_process_output({"process_id": "bg_1"})` → `docker logs` or logfile tail + running/exited status.
- `stop_process({"process_id": "bg_1"})` → `docker rm -f` or `taskkill /F /T` (Win) / process-group kill (POSIX).

Global `BACKGROUND_PROCESSES` is terminated via an `atexit` hook and `/kill`.

### Visual Verification Loop — `screenshot_page` (`tools/browser.py`)

The result includes a `[screenshot: <path>]` line; with `vision_screenshots = true` the PNG is attached to the next model call as an image part (the session log keeps only the path).

For any frontend/UI task the agent **must**:

1. `start_background_process` the dev server.
2. `screenshot_page({"url": "http://localhost:3000"})` (or `{"html_file": "dist/index.html"}` → `file://` via `Path.as_uri()`).
3. Evaluate the screenshot against the checklist (unstyled defaults, consistent spacing, hierarchy, no clipping, loading/error states).
4. Fix via `patch_file_*` / `write_file`, re-screenshot, repeat.
5. `stop_process` when verified.

Playwright retries: 3 attempts, `+5s` timeout backoff, viewport `1280×720`, `domcontentloaded` + 1.5 s settle. Every request (redirects, iframes, subresources) passes the DNS-resolving SSRF guard; only public hosts, explicit localhost, and `file:` inside the workspace are allowed. The main host is DNS-pinned.

---

## 4. Subagent Swarms — `tools/swarm.py`

- **Autonomy:** Swarm threads spawned by `spawn_agent` execute tools **without** the human gate and **without** `render_preview()`. They run the full NIM loop (`resume_subagent_thread()`) for `max_steps = 50` in YOLO else `max_loop_iterations`, via `execute_local_tool()` directly, pruning history to `effective_budget()` each step.
- **Docker-only execution:** Subagents can never run shell commands on the host. If Docker is unavailable or disabled, `execute_command` / `run_tests` / `start_background_process` return `SUBAGENT_HOST_REFUSAL`; if Docker is available, a subagent's `use_docker: false` is ignored.
- **Lifecycle:** `spawn_agent_logic()` seeds `ACTIVE_SUBAGENTS[agent_id]`, then `resume_subagent_thread()` drives the model → tool cycle. `send_message_to_agent_logic()` puts messages in the agent's `inbox`, drained at the start of each step (no lost messages). `kill_agent_logic()` sets `KILLED`. `list_agents_logic()` renders `id | role | status | snippet`.
- **Time limit & transcripts:** each run has a `subagent_timeout_s` wall-clock budget (default 900; `TIMED_OUT` when exceeded). Transcripts go to `subagents/<id>.jsonl`; ended agents are reaped from `ACTIVE_SUBAGENTS` after 10 minutes.
- **Caps:** at most 10 **RUNNING** subagents (finished/killed ones don't count), checked atomically with the insert under `_SUBAGENTS_LOCK`.
- **Monitoring:** `/team` in the TUI lists live subagents; the sidebar polls `ACTIVE_SUBAGENTS` every turn.
- **Depth:** each agent record carries `depth`; threads run on a `copy.copy()` of config stamped with `_subagent_depth`, so the depth-3 cap accumulates across generations. Never mutate the shared `config` from a swarm thread.

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

2. **Validation:** Add a branch to `is_valid_tool_call()` (`tools/base.py`) checking required fields for `my_tool`.

3. **Preview:** Add a branch to `render_preview()` (`tools/base.py`) returning a human-readable preview (diff, command string, or arg summary). The preview must show what will actually run.

4. **System prompt (if needed):** Extend `SYSTEM_PROMPT_TEMPLATE` and the XML fallback examples (`core/prompts.py`) for `disable_native_tools` mode.

5. **Export:** `from . import <module>` in `tools/__init__.py` to trigger decorator registration at import time. Re-export any shared symbols via `__all__`.

6. **Tests:** Add coverage in `tests/test_*`.

---

## 6. Docker Execution Sandbox — `_docker_run_argv()` (`tools/cmd.py`)

- **Image:** `python:3.11-slim`, overridable via `nimcode.toml:docker_image` (an image starting with `-` falls back to the default).
- **Build:** `docker run --rm --init --cap-drop ALL --security-opt no-new-privileges:true -v <workspace>:/workspace -w /workspace --memory 2g --pids-limit 512 --cpus 2 --network bridge [-p port:port]* [docker_extra_args]* <image> sh -c "<command>"`. Foreground and background runs share this builder.
- **Limits** (`nimcode.toml`): `docker_memory` (`"2g"`), `docker_pids_limit` (`512`), `docker_cpus` (`"2"`), `docker_network` (`"bridge"` or `"none"`; `"host"` is rejected). `""`/`0` disables a limit. Ports come from `docker_expose_ports`.
- **Background:** `docker run -d --name nimcode_bg_<id>` (no `--rm`, so crash logs stay readable; removed by `stop_process`/cleanup).
- **Detection:** `is_docker_available()` → `docker info` (5s timeout).
- **Validation:** `_validate_command_safety()` **warns** (does not block) on socket mounts, `--privileged`, `--cap-add`, host namespaces, and shell injection patterns. `_safe_docker_extra_args()` **drops** privileged/mount/device/namespace/seccomp flags from `docker_extra_args`, in both joined and split forms.
- **Fallback:** If Docker is absent or `disable_docker=true`, the **main agent's** command runs on the host via `Popen(shell=True)` with `scrub_env()` and `stdin=DEVNULL`; a real fallback is annotated `[Note: Docker unavailable, fell back to host execution]`. Subagents never fall back (§4).
- **Timeout:** `[timed out after Ns]`; the whole process tree is killed (`taskkill /T` on Windows, process group on POSIX).

---

## 7. Known Security Constraints & System Limits

1. **Subagent bypass:** Swarm threads do not render previews or wait for approval — constrain their prompts carefully. They are confined to Docker (§4).
2. **Env scrubbing coverage:** `SENSITIVE_ENV_REGEX` (`tools/base.py`) removes variables matching `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `_PW`, `_PWD`, `CREDENTIAL`, `PRIVATE_KEY`, `AWS_*`, `DATABASE_URL`, `POSTGRES_PASSWORD`, and similar. Arbitrarily named secrets without these substrings are **not** scrubbed — name credentials conventionally.
3. **Inline command secrets:** `scrub_text()` redacts them in previews and output, but the secrets still execute in the shell. Never log raw command strings outside the scrubbed preview.
4. **Skills are prompt content:** `/install-skill` downloads to a staging dir, shows a preview (files, size, sha256, first 40 lines) and requires confirmation before `commit_skill()` writes `.source.json`. Skill downloads check DNS but do not pin it (DNS rebinding remains possible).
5. **Output truncation:** `truncate_output()` uses head+tail truncation (`[dropped N chars from the middle]`) so both initial context and final stack traces survive. Increase `output_cap` via `nimcode.toml` or `/config`.
6. **Line-range reads:** For large outputs/files, use `read_file` with `start_line`/`end_line` or rely on the truncation hint.

---

## 8. Model & Token Constraints — `call_nim_with_retry()` / `prune_history()` (`core/agent.py`)

To satisfy NVIDIA NIM endpoints (notably **MiniMax-M3**, **GLM-5.2**) via the OpenAI-compatible API:

1. **Explicit `max_tokens`:** Always send `max_tokens` (default `4096` from `config.max_tokens`). Omitting it makes certain endpoints return `choices=[]`, which `call_nim_with_retry()` detects and raises as `empty choices`.
2. **System-message consolidation:** `normalize_messages()` merges all `system` roles into a single entry at `messages[0]`. Interleaved system messages are combined with `\n\n`.
3. **Alternating roles:** Consecutive same-role `user`/`assistant` messages are collapsed (`\n\n` join, `tool_calls` concatenated). If the sequence starts with `assistant`, a dummy `user: "Let's continue…"`. If the payload is `system`-only, append dummy `user: "Hello. I am setting up the workspace session."` — prevents GLM/MiniMax validation errors.
4. **Error classification:** by `status_code` / openai exception type — retry 429, 5xx, `RateLimitError`, `APIConnectionError`/timeouts; fail fast on 400/401/403/404/422. Message-text checks (below) run only for 400/404/422 or status-less errors.
5. **XML fallback:** On `auto / tool choice` or `degraded function` errors, nimcode flips `config.disable_native_tools = true`, rebuilds the system prompt via `build_system_prompt(..., disable_native_tools=True)` (adds `<tool_call>` examples), and retries with `tools=None` so the model emits XML tool calls parsed by `parse_xml_tool_calls()`.
6. **Context budget:** `effective_budget(config, model)` (`config_models.py`) = `min(context_budget_tokens, model window) − max_tokens − 1024`, floored at 2000. On context-overflow errors the budget halves per retry and the error is re-raised below 1000 tokens.
7. **Token counting & pruning:** `count_message_tokens()` uses tiktoken `cl100k_base` (special tokens allowed), else `len//4 + 4` per message. `prune_history()` groups `(assistant+tool_calls, tool results)` into units so paired tool interactions are never split; the newest unit is always kept, and if it alone exceeds the budget its content is head+tail trimmed.

---

## 9. UI & Prompt Invariants

- **Live prompt injection:** `run_agent_loop_tui()` rebuilds `messages[0]` every turn with a fresh `generate_workspace_map()` + `build_system_prompt()` so renames, deletes, and `AGENTS.md` edits apply without restart.
- **TUI routing:** When `TUI_INSTANCE` is set (`ui/output.py`), `echo`/`secho`/`print_text` route to the sidecar TUI buffer; otherwise to `rich`/`typer`. It is cleared on session exit. Markdown is rendered inside the Rust sidecar (`tui-rs/src/main.rs`).
- **Design ("Nim"):** no boxes; coloured speaker gutters; the context gauge is a 1-3-5-7 Nim matchstick board; mode colours the prompt (Plan wisteria, Build pine, YOLO sulfur); one fuse animation while working. Keep the palette in `tui-rs/src/theme.rs`, keep the board the only bold element, and update `tui-rs/snapshots/` (`cargo test`) when layout changes.
- **Keys:** `KEY_BINDINGS` in `ui/console.py` must match the bindings in `tui-rs/src/app.rs`.
- **Ctrl+D is sticky:** once requested, every prompt returns immediately and the interrupt flag stays set, so the loop reaches its exit check even from an approval prompt.
- **Wire protocol:** NDJSON over loopback, UTF-8 both ways (Rust emits ASCII-escaped JSON). Any new message type must be added to `tests/protocol_samples.json`, which both `cargo test` and `tests/test_bridge_protocol.py` validate.
- **Mode sync:** `config.mode` is synced into `state` at the top of each turn and between tool calls, so `Shift+Tab` and `/model` side-effects apply immediately.
- **Cancellation:** Long NIM calls run in a worker thread polled every 50ms for `TUI_INSTANCE._interrupted` → `KeyboardInterrupt("ESC_CANCELLED")`. Interrupted tool batches get cancellation results via `close_dangling_tool_calls()` so history stays valid.
- **Binary lookup:** `find_rust_binary()` checks `NIM_TUI_BIN`, then bundled `nimcode/bin/nim-tui[.exe]`, then `tui-rs/target/release`, then `PATH`.

---

- **Headless:** `nimcode run --headless` drives the same `run_agent_loop_tui()` with `HeadlessTUI` (`ui/headless.py`): stdout output, auto-approve, ends when the agent asks for input or the step budget runs out; `UsageMeter` records real API token usage. Any new TUI method the loop calls must also exist on `HeadlessTUI`.
- **Auto-tests:** pytest/unittest exit code 5 (nothing collected) is neutral (`no_tests_found()`), not a failure to report to the model.

---

## 10. Testing & CI Expectations

- **Dev setup:** `uv sync --extra dev` (or `pip install -e .[dev]`) — pins ruff and pytest.
- **CI (`ci.yml`):** Python job on `ubuntu-latest` + `windows-latest` × py 3.10/3.11/3.12 (`ruff check` → `ruff format --check` → import smoke → `nimcode status` with a dummy key → `pytest`); `rust` job (`cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`); `wheel` job (builds sidecar + wheel, asserts the binary is inside).
- **Release (`release.yml`):** push a `v*` tag → 4 platform wheels + sdist, each wheel smoke-tested, attached to a GitHub Release.
- **Before submitting:** `ruff format . && ruff check . && pytest -q` (and `cargo clippy`/`cargo test` for `tui-rs/` changes) must pass. Keep tool args JSON-serializable and add `tests/test_*` for any new tool or state transition.
