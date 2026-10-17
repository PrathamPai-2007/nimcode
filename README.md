# Nimcode — Terminal-First AI Coding Agent for NVIDIA NIM

> **An open-source, terminal-first autonomous coding agent that wraps a continuous Read-Eval-Print Loop (REPL) around NVIDIA NIM-hosted LLMs, giving the model programmatic “hands” to read, write, patch, execute, screenshot, and search — all from your terminal.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.6.0-cyan)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![NVIDIA NIM](https://img.shields.io/badge/powered%20by-NVIDIA%20NIM-76B900)](https://build.nvidia.com/)
[![Tests](https://img.shields.io/badge/tests-pytest-yellow)](tests/)

---

## Table of Contents

- [1. Introduction](#1-introduction)
- [2. Why Nimcode?](#2-why-nimcode)
- [3. Architecture at a Glance](#3-architecture-at-a-glance)
- [4. Project Structure](#4-project-structure)
- [5. Prerequisites](#5-prerequisites)
- [6. Installation](#6-installation)
- [7. Configuration](#7-configuration)
  - [7.1 Environment Variables & API Keys](#71-environment-variables--api-keys)
  - [7.2 `nimcode.toml` Reference](#72-nimcodetoml-reference)
  - [7.3 Runtime `~/.nimcode/<project>/model.json`](#73-runtime-nimcodeprojectmodeljson)
- [8. CLI Reference](#8-cli-reference)
- [9. TUI & Slash Commands](#9-tui--slash-commands)
- [10. Operational Modes (Plan / Build / YOLO)](#10-operational-modes-plan--build--yolo)
- [11. Tools Reference — The Agent's Hands](#11-tools-reference--the-agents-hands)
- [12. Agent Loop Internals](#12-agent-loop-internals)
- [13. Security & Sandboxing](#13-security--sandboxing)
- [14. Session Management, Backups & Recovery](#14-session-management-backups--recovery)
- [15. Model Catalog (`/model`)](#15-model-catalog-model)
- [16. Skills System](#16-skills-system)
- [17. Context Harvesting & System Prompt](#17-context-harvesting--system-prompt)
- [18. UI Subsystem (Rust TUI, Console)](#18-ui-subsystem-rust-tui-console)
- [19. Docker Sandbox Details](#19-docker-sandbox-details)
- [20. Background Processes & Visual Verification](#20-background-processes--visual-verification)
- [21. Swarm & Sub-Agents (`/team`)](#21-swarm--sub-agents-team)
- [22. Web Search & Browser Tools](#22-web-search--browser-tools)
- [23. Testing & CI](#23-testing--ci)
- [24. Development Guide](#24-development-guide)
- [25. Troubleshooting & FAQ](#25-troubleshooting--faq)
- [26. Version History](#26-version-history)
- [27. Contributing & License](#27-contributing--license)

---

## 1. Introduction

**Nimcode** (`src/nimcode/__init__.py:1`) is a **terminal-first AI coding agent** driven by **NVIDIA NIM** (default: `nvidia/nemotron-3-super-120b-a12b`). It is installed globally via `pip` and can be invoked from **any directory** on your machine. On every turn it:

1. Harvests a live **workspace map** of your project (`src/nimcode/context.py:185`).
2. Injects that map + `AGENTS.md` rules into a **system prompt** (`src/nimcode/core/prompts.py:46`).
3. Calls the NIM OpenAI-compatible endpoint with **retry, pruning, and normalization** (`src/nimcode/core/agent.py:400`).
4. Surfaces a **preview** of every filesystem or shell mutation and asks for human approval (except in YOLO).
5. Executes the approved **tool**, logs the turn to `~/.nimcode/<project>/sessions/`, and loops.

It ships with a Rust sidecar fullscreen **TUI** (`tui-rs/`, bridged by `src/nimcode/ui/rust_bridge.py`), Docker sandboxing, sub-agent swarms, visual UI verification via Playwright, and an extensible **skills** system.

---

## 2. Why Nimcode?

| Capability | Nimcode | Generic Chat Agent |
|---|---|---|
| **Live workspace awareness** | Full tree + AST import hints + `AGENTS.md` injection every turn | Static prompt |
| **Human-in-the-loop gate** | Unified diff / Docker command preview before execution | Auto-executes |
| **Three-mode state machine** | `AWAITING_PLAN_APPROVAL` / `EXECUTING` / `YOLO` (`src/nimcode/core/state.py:4`) | Single mode |
| **Session persistence** | JSON logs under `~/.nimcode/<project>/sessions/` with `nimcode resume` | In-memory |
| **Sandbox** | Docker `python:3.11-slim` by default with host fallback (`src/nimcode/tools/cmd.py:417`) | None |
| **Visual verification** | `screenshot_page` → self-critique loop (`src/nimcode/tools/browser.py:99`) | Manual |
| **Parallel delegation** | `spawn_agent` swarm (`src/nimcode/tools/swarm.py:118`) | Single thread |
| **Token resilience** | `prune_history` + `normalize_messages` + `tiktoken` counting (`src/nimcode/core/agent.py:256`, `src/nimcode/core/agent.py:109`) | OOM |

---

## 3. Architecture at a Glance

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
       │ 19 tools       │ │ RustTUI    │ │ Plan/Build/YOLO│
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

---

## 4. Project Structure

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

---

## 5. Prerequisites

- **Python 3.10+** (tested on 3.10, 3.11, 3.12, 3.13)
- **NVIDIA NIM API Key** — create one at [build.nvidia.com](https://build.nvidia.com/) (the `NVIDIA_API_KEY` env var).
- **Optional but recommended:**
  - **Docker** — for sandboxed `execute_command` / `run_tests`. If absent or `disable_docker = true`, nimcode falls back to host execution.
  - **Playwright Chromium** — for `screenshot_page` visual verification: `playwright install chromium`
  - **`tiktoken`** — for accurate token counting; otherwise falls back to `len(text)//4` (`src/nimcode/core/agent.py:239`).

---

## 6. Installation

### Quick install (prebuilt, no Rust or Python setup)

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.ps1 | iex
```

```bash
# Linux / macOS
curl -LsSf https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.sh | sh
```

Both scripts install [uv](https://docs.astral.sh/uv/) if needed, then `uv tool install` the wheel for your platform from the latest GitHub Release. Set `NIMCODE_VERSION=v0.7.0rc1` to pick a specific tag (pre-releases included). Then put `NVIDIA_API_KEY=...` in `~/.nimcode/.env` and run `nimcode`. Update by re-running the script; remove with `uv tool uninstall nimcode`.

### Install from source

```bash
# 1. Clone
git clone https://github.com/PrathamPai-2007/nimcode.git
cd nimcode

# 2. Create & activate a venv (recommended)
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# 3. Install in editable mode (reads pyproject.toml / setup.py)
pip install -e .
#    Build the TUI sidecar (source installs only; release wheels bundle it):
cargo build --release --manifest-path tui-rs/Cargo.toml

# 4. (Optional) Visual verification support
playwright install chromium

# 5. Provide your API key — pick one:
#   a) Current shell
export NVIDIA_API_KEY="nvapi-..."          # bash/zsh
$env:NVIDIA_API_KEY="nvapi-..."            # PowerShell

#   b) Local .env in your workspace
echo 'NVIDIA_API_KEY=nvapi-...' > .env

#   c) Global fallback (survives directory changes)
mkdir -p ~/.nimcode
echo 'NVIDIA_API_KEY=nvapi-...' > ~/.nimcode/.env

# 6. Verify
nimcode status
```

**Prebuilt wheels:** each `v*` release on GitHub ships a wheel per platform (Windows x64, Linux x64, macOS arm64/x86_64) with the `nim-tui` sidecar bundled, so `pip install <wheel>` needs no Rust toolchain. A source install finds the sidecar in `tui-rs/target/release/`, or set `NIM_TUI_BIN`.

`requirements.txt` vs `pyproject.toml`:
- `pyproject.toml:11` declares: `typer`, `openai`, `python-dotenv`, `tomli`, `rich`, `prompt_toolkit`, `playwright`, plus `colorama` on Windows.
- `requirements.txt` mirrors the full runtime set — `pip install -r requirements.txt` and `pip install -e .` are equivalent for deployment.

---

## 7. Configuration

### 7.1 Environment Variables & API Keys

`src/nimcode/cli.py:56` loads keys in this priority order:

1. Current process environment (`NVIDIA_API_KEY`)
2. Local `.env` in the workspace root (`load_dotenv()`)
3. Global `~/.nimcode/.env` (only if `NVIDIA_API_KEY` not already set)

If `api_base_url` is set (for local/self-hosted OpenAI-compatible endpoints) and no key is present, nimcode injects `local_dummy_key` so the OpenAI client can still initialize (`src/nimcode/cli.py:66`).

### 7.2 `nimcode.toml` Reference

Place an optional `nimcode.toml` in your **workspace root** (the directory you run `nimcode` from). All keys are optional — `load_config()` (`src/nimcode/config.py:302`) falls back to `Config` defaults and then layers `~/.nimcode/<project>/model.json` on top.

| Key | Type | Default | Description |
|---|---|---|---|
| `fast_model` | `str` | `nvidia/nemotron-3-super-120b-a12b` | Model for **Build & YOLO** execution |
| `plan_model` | `str` | `nvidia/nemotron-3-super-120b-a12b` | Model for **Plan** mode reasoning |
| `mode` | `str` | `build` | Startup mode: `plan` / `build` / `yolo` |
| `temperature_fast` | `float` | `0.2` | Sampling temperature for Build/YOLO (low = deterministic code) |
| `temperature_plan` | `float` | `0.2` | Sampling temperature for Plan |
| `max_loop_iterations` | `int` | `20` | Consecutive agent steps before pausing for human input (YOLO overrides to **50**) |
| `timeout` | `int` | `60` | Subprocess hard timeout (seconds, capped at 600) |
| `output_cap` | `int` | `100000` | Max characters from any single tool output (`src/nimcode/tools/base.py:240`) |
| `context_budget_tokens` | `int` | `128000` | Upper bound for history; the effective budget is `min(this, model window) − max_tokens − 1024` (`effective_budget()`) |
| `max_tokens` | `int` | `4096` | `max_tokens` sent to the model (required for MiniMax-M3 / GLM to avoid `choices=[]`) |
| `auto_backup` | `bool` | `true` | Snapshot files before overwriting to `backups/` |
| `disable_docker` | `bool` | `false` | Force host execution even if Docker is available (subagents then cannot run commands) |
| `docker_image` | `str` | `python:3.11-slim` | Image for sandboxed commands (`src/nimcode/tools/cmd.py:129`) |
| `docker_expose_ports` | `list[int]` | `[]` | Ports to `-p` expose in Docker (validated 1–65535) |
| `docker_extra_args` | `list[str]` | `[]` | Extra `docker run` args (privileged/mount/device/namespace/seccomp flags dropped) |
| `docker_memory` | `str` | `"2g"` | `--memory` limit (`""` disables) |
| `docker_pids_limit` | `int` | `512` | `--pids-limit` (`0` disables) |
| `docker_cpus` | `str` | `"2"` | `--cpus` limit (`""` disables) |
| `docker_network` | `str` | `"bridge"` | `bridge` or `none` (`host` rejected) |
| `disable_native_tools` | `bool` | `false` | Force prompt-based XML `<tool_call>` mode (auto-set if server rejects `auto` tool choice) |
| `subagent_timeout_s` | `int` | `900` | Wall-clock limit per subagent run (`TIMED_OUT` when exceeded) |
| `vision_screenshots` | `bool` | `false` | Attach `screenshot_page` PNGs as image parts for vision models |
| `searxng_url` | `str` | `""` | SearXNG instance used when DuckDuckGo blocks or fails (`format=json` must be enabled) |
| `api_base_url` | `str \| null` | `null` | Override OpenAI base URL (defaults to `https://integrate.api.nvidia.com/v1`) |
| `denylist` | `list[str]` | `[]` | Reserved for future denylist enforcement |

**Validation:** `_validate_config()` (`src/nimcode/config.py:191`) coerces numeric fields first (a malformed TOML warns and falls back to defaults instead of crashing startup), then clamps temperatures to `[0, 2]`, fixes non-positive `timeout`/`output_cap`/`max_tokens`, and drops invalid ports/modes with a warning.

**Inline example:**

```toml
# nimcode.toml
fast_model = "deepseek-ai/deepseek-v4-flash"
plan_model = "nvidia/nemotron-3-super-120b-a12b"
mode = "plan"
timeout = 90
max_loop_iterations = 15
output_cap = 50000
disable_docker = false
docker_image = "python:3.11-slim"
docker_expose_ports = [3000, 8000]
```

Edit at runtime with `/config` inside the TUI — changes persist via `save_config()` → `save_last_used_model()` (`src/nimcode/config.py:133`). `save_config()` writes the full scalar set (mode, models, loop cap, timeouts, caps, both temperatures, backups, docker settings, denylist) so nothing edited in-session is lost on restart.

### 7.3 Runtime `~/.nimcode/<project>/model.json`

Persisted per-workspace by `save_last_used_model()` (`src/nimcode/config.py:87`):

```json
{
  "model": "nvidia/nemotron-3-super-120b-a12b",
  "fast_model": "nvidia/nemotron-3-super-120b-a12b",
  "plan_model": "nvidia/nemotron-3-ultra-550b-a55b",
  "updated_at": 1714680000.0,
  "timestamp": 1714680000.0,
  "docker_image": "python:3.11-slim",
  "docker_expose_ports": [],
  "docker_extra_args": [],
  "disable_docker": false,
  "docker_memory": "2g",
  "docker_pids_limit": 512,
  "docker_cpus": "2",
  "docker_network": "bridge"
}
```

This file is re-loaded on every CLI boot so `/model` switches survive restarts.

---

## 8. CLI Reference

All commands are defined in `src/nimcode/cli.py:137` as `typer.Typer` subcommands. Available: `run`, `start`, `status`, `config`, `refresh-models`, `resume`, `init`, `stats`, plus `nimcode --version` / `-V`.

### `nimcode run --headless` (scripts, CI, benchmarks)

```bash
nimcode run --headless --yolo --model <model-id> --max-steps 150 --json-out run.json "fix the failing test"
```

Runs the real agent loop with no TUI: messages stream to stdout as `[role] text`, every approval is auto-accepted, and the run ends when the agent hands control back (finishes) or after `--max-steps`. Exit code `0` = finished, `2` = step limit, `1` = error. `--json-out` writes `{status, model, mode, duration_s, api_calls, prompt_tokens, completion_tokens, total_tokens, error}`, with token counts taken from the API's reported usage. No Rust sidecar is needed.

### `nimcode config`

Prints every setting with its value and where it came from: `[default]`, `[nimcode.toml]`, `[model.json]` or `[cwd]`.

### `nimcode` (no args) → `nimcode start`

Boots the interactive TUI and immediately prompts for an initial task. Equivalent to `nimcode start`.

### `nimcode run <prompt> [--mode plan|build|yolo] [--yolo]`

Start the agent loop with an initial task:

```bash
nimcode run "Create a Python script that fetches the current weather."
nimcode run --mode plan "Design a rate-limiter for the API"
nimcode run --yolo "Refactor main.py to use async/await and run the tests."
nimcode run -m yolo -y "Bulk-rename all handlers"
```

Mode resolution (`src/nimcode/cli.py:159`): `--yolo` flag wins → explicit `--mode` → `config.mode` default.

### `nimcode start`

Interactive TUI boot that shows the welcome splash (`src/nimcode/ui/console.py:81`) and waits for you to type a task.

### `nimcode status`

Prints:

- Workspace root
- API key presence
- Fast / Plan model IDs
- Active mode & loop cap
- Session log count
- Harvested file count

(`src/nimcode/cli.py:223`)

### `nimcode resume [--latest]`

Resumes a persisted session from `~/.nimcode/<project>_<hash>/sessions/`:

- Without flags → interactive `select_interactive_session()` picker (`src/nimcode/config.py:1056`).
- `--latest` / `-l` → auto-load the newest session (`session_*.jsonl`, or a legacy `session_*.json`, which is copied into a fresh `.jsonl` on resume).

Migrates legacy `<workspace>/.nimcode_session.json` automatically (`migrate_legacy_session()`).

### `nimcode init`

Generates or enriches `AGENTS.md` in the workspace root. When an `OpenAI` client is available it asks the model to produce a tailored document; otherwise falls back to a template (`src/nimcode/core/agent.py:935`).

### `nimcode stats`

Aggregates historical session analytics (`src/nimcode/core/agent.py:713`):

- Total sessions / messages / days active
- Input / output / total tokens (char/4 estimate)
- Tool usage histogram rendered with `rich` panels

Works both as CLI (`nimcode stats`) and TUI slash command (`/stats`).

### `nimcode refresh-models`

Forces a live refresh of the model catalog from the NIM API (blocking), reconciles retired saved models, re-applies the context budget, and lists the top 20 entries with context badges. Same as `/models-refresh` in-TUI.

### `python -m nimcode`

Shim via `src/nimcode/__main__.py:1` — identical to `nimcode`.

---

## 9. TUI & Slash Commands

The TUI is the Rust sidecar `nim-tui` (`tui-rs/`, bundled in release wheels, override via `NIM_TUI_BIN`), driven by `run_agent_loop_tui()` through the `RustTUI` bridge (`src/nimcode/ui/rust_bridge.py`). Python speaks NDJSON to the sidecar over a loopback socket; the sidecar owns the terminal (raw mode + alternate screen), so keystrokes are handled exactly once. If the binary is missing, `nimcode` exits with rebuild instructions instead of starting a degraded UI.

#### Design: "Nim"

*Nim* is the old game of taking matchsticks from heaps (classic start: 1-3-5-7), and the context window plays the same way. The sidebar's context gauge is that board: 16 sticks, each 1/16 of the budget; unused sticks keep a sulfur head, spent ones are struck to a dim `╎`. Under it, a plain "41k of 128k" (sulfur from 75%).

- **No boxes.** One vertical rule separates the conversation from the sidebar; one horizontal rule sits above the input.
- **Who is speaking** is a coloured left gutter `▎`: wisteria for you, pine for nimcode, sulfur for tools, ash for system notes, red only for errors. Text wraps at 100 columns.
- **Mode** colours the mode word and the `›` prompt: Plan = wisteria, Build = pine, YOLO = sulfur.
- **Tool output** is dimmed and folded past 12 lines (`Ctrl+O` expands); diffs use pine `+` and sulfur `-`.
- **Working** shows a burning fuse `━━━━╸` with the working message and elapsed seconds; when the turn ends, the reply's gutter flashes once.
- **Empty state:** the board drawn large with "Describe a task, or type / for commands."
- Hints at the bottom right change with context (typing, selector, approval, scrolled, working). Below 90 columns the sidebar hides and the status line shows `41k/128k`.
- `NO_COLOR` / `NIMCODE_PLAIN` disable all colour; `NIMCODE_ASCII` switches to ASCII glyphs. Layout is covered by TestBackend snapshots in `tui-rs/snapshots/`.

#### Keys

The list lives in `KEY_BINDINGS` (`src/nimcode/ui/console.py`) and is shown by `/help`:

| Key | Action |
|---|---|
| Enter | Send (with the `/` popup open: send the highlighted command) |
| Ctrl+J / Shift+Enter | Newline (Ctrl+J works in every terminal; Alt+Enter is taken by Windows Terminal) |
| ←/→, Home/End, Ctrl+←/→ | Move the cursor / by word |
| Ctrl+W, Ctrl+Backspace | Delete the previous word |
| Ctrl+U | Delete to the start of the line |
| ↑/↓ | Input history (moves between lines in multi-line input) |
| PgUp/PgDn, Ctrl+↑/↓, mouse wheel | Scroll the conversation ("↑ N more" shows while scrolled) |
| Esc | Back to the latest message / dismiss the popup |
| Tab | Complete a `/command` |
| Shift+Tab / Ctrl+T | Cycle Plan → Build → YOLO (applies between tool calls too) |
| Ctrl+O | Expand / fold long tool output |
| Ctrl+C | Clear the input, or interrupt the turn when it is empty |
| Ctrl+D | Exit (when the input is empty), even mid-turn |
| Shift+drag | Select text (mouse capture is on for wheel scrolling) |

Pasted text is inserted, never sent.

#### In-TUI Slash Commands

All commands are dispatched by `handle_command()` (`src/nimcode/core/agent_tui.py:179`); the authoritative catalog is `SLASH_COMMANDS` (`:43`), which also feeds sidecar autocomplete (plus dynamic `/<skill>` entries).

| Command | Aliases | Action | Handler |
|---|---|---|---|
| `/help` | `?`, `help` | Show keyboard shortcuts & command table | `src/nimcode/ui/console.py:11` |
| `/plan` | — | Switch to Plan Mode (`AWAITING_PLAN_APPROVAL`) | `src/nimcode/core/agent_tui.py:195` |
| `/build` | — | Switch to Build Mode (`EXECUTING`, human gate) | same |
| `/yolo` | — | Switch to YOLO Mode (auto-approve, cap 50) | same |
| `/model` | — | Inline model selector (`select_model()`) → context handoff via `summary.md` | `src/nimcode/ui/rust_bridge.py:390` |
| `/models-refresh` | — | Blocking catalog refresh + retired-model reconcile + context re-apply | `src/nimcode/core/agent_tui.py:263` |
| `/config` | — | Inline table + prompt to edit `output_cap`, `timeout`, `auto_backup`, etc. | `src/nimcode/ui/rust_bridge.py:545` |
| `/changes` | `/diff` | `git status -s` + recent backups | `src/nimcode/config.py:391` |
| `/restore` | — | Inline backup picker → restore to the original path recorded in `backups/manifest.jsonl` | `restore_backup()` in `src/nimcode/ui/rust_bridge.py` |
| `/test [cmd]` | — | `execute_test_runner()` auto-detects `pytest`/`npm test`/`cargo test`/`go test` | `src/nimcode/tools/cmd.py:653` |
| `/team` | — | List subagents (id/role/status) with each transcript path | `src/nimcode/core/agent_tui.py` + `swarm.py` |
| `/kill` | — | `cleanup_background_processes()` — stops all bg procs | `src/nimcode/tools/cmd.py:76` |
| `/compact` | — | Summarise the session with the fast model into `summary.md`, continue from the summary, and log a marker so `resume` starts there | `generate_summary()` in `src/nimcode/ui/rust_bridge.py` |
| `/stats` | — | `aggregate_session_stats()` + `render_stats_rich()` | `src/nimcode/core/agent.py:803` |
| `/recap` | — | Not yet implemented in TUI mode | `src/nimcode/core/agent_tui.py:359` |
| `/skills` | — | `skills.list_skills()` | `src/nimcode/skills.py:178` |
| `/install-skill <url>` | — | Download raw `.md` / `.zip` / git repo to staging, show a preview (files, size, sha256, first 40 lines), install only on confirmation | `fetch_skill()` / `commit_skill()` in `src/nimcode/skills.py` |
| `/init` | — | `run_init_command()` — (re)generate `AGENTS.md` | `src/nimcode/core/agent.py:935` |
| `/resume` | — | Inline session picker; loads the chosen session in place and keeps appending to it | `src/nimcode/core/agent_tui.py` |
| `/<skill> [args]` | — | Inject installed skill content as `<skill_context>` and continue the turn | `src/nimcode/skills.py:193` |
| `/exit`, `/quit`, `q`, `done`, `exit`, `quit` | — | End session gracefully | `src/nimcode/core/agent_tui.py:183` |

#### Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Shift+Tab` | Cycle `Plan → Build → YOLO → Plan` (updates `config.mode`) |
| `Ctrl+C` | Cancel current work: clears input, or interrupts the in-flight API call / remaining tool calls (`_interrupted` flag, `src/nimcode/ui/rust_bridge.py`, `src/nimcode/core/agent.py:395`). Press again at an empty prompt handling to exit |
| `Ctrl+D` | Exit the session cleanly (sticky `_eof_requested`, observed even mid-turn) |
| `Ctrl+T` | Cycle `Plan → Build → YOLO → Plan` (same as `Shift+Tab`) |
| `Tab` (with `/` popup) | Accept the highlighted completion inline |
| `Esc` | Dismiss the `/` popup, else stop stick-to-bottom follow mode |
| `↑` / `↓` / `PageUp` / `PageDown` / `Home` / `End` | Scroll the conversation pane |

---

## 10. Operational Modes (Plan / Build / YOLO)

Defined in `src/nimcode/core/state.py:4`:

```python
class AgentState(str, Enum):
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"
    YOLO = "YOLO"
```

| Mode | State | How to enter | Tool access | Human gate | Loop cap |
|---|---|---|---|---|---|
| **Plan** | `AWAITING_PLAN_APPROVAL` | `/plan` or `nimcode run --mode plan` or default `mode=plan` | **Disabled** — `tools_arg=None` so model must emit `<plan>...</plan>` (`src/nimcode/core/agent_tui.py:626`) | Plan must be approved `y` | `max_loop_iterations` (20) |
| **Build** | `EXECUTING` | `/build` or `mode=build` (default) | Full `AVAILABLE_TOOLS_SPEC` (19 tools) | **Every** tool shows `render_preview()` diff/command and waits for `y/n` (`src/nimcode/core/agent_tui.py:1018`) | `max_loop_iterations` (20) |
| **YOLO** | `YOLO` | `/yolo`, `--yolo`, or approving a plan with `yolo` | Full tools | **Auto-approves** (`y`) and auto-runs tests after file mutations (`src/nimcode/core/agent_tui.py:1011`, `src/nimcode/core/agent_tui.py:1053`) | **50** (hard override) |

**Plan flow** (`src/nimcode/core/agent_tui.py:777`): assistant text → `extract_plan_block()` (`src/nimcode/core/prompts.py:159`) → render plan → prompt `Approve? (y/n/yolo)` → on `y`/`yolo` inject trigger message `"Plan approved. Please proceed with step 1 using tool calls."`.

---

## 11. Tools Reference — The Agent's Hands

All tools are registered via `@register_tool` (`src/nimcode/tools/registry.py:13`) and dispatched by `execute_local_tool()` (`src/nimcode/tools/registry.py:40`), which also enforces JSON-serializable args and the subagent nesting/parallelism caps. Validation happens in `is_valid_tool_call()` (`src/nimcode/tools/base.py:70`). Total spec lives in `AVAILABLE_TOOLS_SPEC` (19 tools).

#### File System (`src/nimcode/tools/fs.py`)

| Tool | Required args | Description |
|---|---|---|
| `read_file` | `path` + optional `start_line`, `end_line` | UTF-8 read with line-range header `[File: … \| Lines X to Y of Z]`; both full and ranged reads capped at `output_cap`; rejects symlinks, >5 MB files, binary |
| `write_file` | `path`, `content` | Create-or-overwrite with auto-backup via `backup_file()` (`~/.nimcode/<project>/backups/<ts>_<pid>_<safe>`), atomic temp+replace write, CRLF preservation, 5 MB content cap |
| `list_files` | optional `path` (default `.`) | Sorted directory listing with `(File)`/`(Directory)` suffixes; `.gitignore`-aware; 2000-entry cap |
| `glob_files` | `pattern` | `Path.glob()` with `.gitignore` filtering via shared `load_gitignore_rules()`; 2000-match cap |
| `patch_file_blocks` | `path`, `edit_blocks` | Preferred edit path: Aider-style `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` multi-block patch; rejects empty/ambiguous SEARCH; dry-run diff preview |
| `search_grep` | `query` (+ optional `path`, `include` glob, `case_sensitive`) | Uses `rg --json` when ripgrep is on PATH, else a Python walk; regex with substring fallback on invalid patterns, case-insensitive by default, honours ignore rules, 1 MB file cap, 5000-match cap |
| `search_symbols` | `query` | AST parse of `.py` files for `ClassDef`/`FunctionDef` name matching; symlink/depth/IO-guarded walker; skips >1 MB files; 5000-symbol cap |

#### Code Patch (`src/nimcode/tools/patch.py:118`)

| Tool | Required args | Description |
|---|---|---|
| `patch_file_ast` | `path`, `target_symbol`, `replacement_code` | AST-aware replacement of a `def`/`class`/`async def` by `target_symbol` name (decorators included, replacement re-indented, result syntax-checked); word-boundary substring fallback with literal (backslash-safe) substitution; dry-run diff |

#### Execution (`src/nimcode/tools/cmd.py`)

| Tool | Required args | Description |
|---|---|---|
| `execute_command` | `command` (+ optional `timeout`, `use_docker`) | Docker → host dispatch (`_execute_command()`); dev-server binaries (`uvicorn`, `vite`, `gunicorn`, …) and server phrases (`npm run dev`, `http.server`, …) are redirected to `start_background_process` (`:487`) |
| `run_tests` | optional `command` (default `pytest`), `timeout` | Preferred post-edit verification; same dispatch, default timeout `config.timeout * 2` (`:556`) |
| `start_background_process` | `command` | Detached `docker run -d --name nimcode_bg_<uuid>` or host `Popen` (new session group) with `process_logs/<id>.log`; max 10 processes; returns `bg_N` id (`:822`) |
| `get_process_output` | `process_id` | `docker inspect` + `docker logs --tail 500`, or `poll()` + 200 KB logfile tail with running/exited status (`:957`) |
| `stop_process` | `process_id` | `docker kill/rm -f`, `taskkill /F /T` (Windows), or whole-process-group `SIGTERM→SIGKILL` (POSIX) (`:1084`) |

*Helper `execute_test_runner()` (`src/nimcode/tools/cmd.py:653`) auto-detects `pytest -v` / `npm test` / `cargo test` / `go test ./...` based on marker files (POSIX) and `python -m unittest discover -v` on Windows.*

#### Swarm (`src/nimcode/tools/swarm.py`)

| Tool | Required args | Description |
|---|---|---|
| `spawn_agent` | `role`, `task_prompt` | Creates `ACTIVE_SUBAGENTS[agent_id]` (`sub_<8-hex>`, with per-agent `depth`) and starts `resume_subagent_thread()` with a fresh NIM client loop; max 10 active, max nesting depth 3 |
| `send_message_to_agent` | `agent_id`, `message` | Appends user message and resumes thread |
| `kill_agent` | `agent_id` | Sets status `KILLED` (cooperative — the thread observes it) |
| `list_agents` | — | Lists active/completed/failed agents with snippets (lock-safe snapshot) |

#### Web & Browser

| Tool | Required args | Description |
|---|---|---|
| `web_search` | `query` (+ optional `max_results` default 5, clamped 1–10) | Pure-Python DuckDuckGo HTML scrape (`html.duckduckgo.com`) with 4 fallback parse strategies, redirect unwrapping (`uddg=`), 3× retry + 30 s overall timeout, 20 k output cap; falls back to SearXNG (`searxng_url`) on a bot check or error |
| `screenshot_page` | `url` **or** `html_file` (+ optional `full_page`) | Playwright Chromium 1280×720 screenshot → saved PNG under `screenshots/`, reported on a `[screenshot: <path>]` line, + capped base64 preview (attached as an image when `vision_screenshots` is on); static files go `html_file` → `file://` via sandboxed `resolve_and_validate_path` |

#### Validation & Preview (`src/nimcode/tools/base.py`)

- `is_valid_tool_call()` (`:70`) — exhaustive per-tool required-field checks.
- `render_preview()` (`:262`) — unified diff for `write_file`/`patch_*`, Docker command preview for execution tools, truncated args for search tools.
- `backup_file()` (`:206`) — timestamped (`<µs>_<pid>_[tag_]<safe>`) crash-safe snapshots shared by all mutating file tools.
- `coerce_output_cap()` (`:232`) — reads `output_cap` off config with fallback on garbage.

---

## 12. Agent Loop Internals

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
   - `initial_state` sync (`YOLO`/`AWAITING_PLAN_APPROVAL`/`EXECUTING`).
   - Model-dependent `tools_arg` selection (`:626`).
   - `pending_summary_injection` handoff (`<model_handoff_context>`).
   - Loop-limit guard (20 Build / 50 YOLO) with user prompt to resume/redirect/cancel.

`run_agent_loop_tui()` is the only agent loop (the legacy ANSI loop was removed).

### Model handoff

`select_model()` in `src/nimcode/ui/rust_bridge.py` implements the **model-switch handshake**:

1. Ask the **old** model to summarize the session into `summary.md`.
2. Persist the new `model.json`.
3. Feed `summary.md` as `<model_handoff_context>` to the **new** model for acknowledgment.

---

## 13. Security & Sandboxing

| Layer | Implementation | File |
|---|---|---|
| **Path traversal guard** | `resolve_and_validate_path()` resolves against `workspace_root` and asserts `is_relative_to` | `src/nimcode/tools/base.py:11` |
| **Backup isolation** | `backup_file()` sanitizes `safe_rel_name` via `replace(os.sep, "_")` to avoid directory creation attacks | `src/nimcode/tools/base.py:206` |
| **Zip Slip guard** | `ZipFile` members are checked with `relative_to(target_path)` before extraction | `src/nimcode/skills.py:113` |
| **Skill name sanitization** | URL basename regex-sanitized; skill names must `fullmatch(r"[a-zA-Z0-9_\-\.]+")` | `src/nimcode/skills.py:48` / `:205` |
| **Subagent confinement** | Subagents may only run commands inside Docker; without Docker they get `SUBAGENT_HOST_REFUSAL` | `src/nimcode/tools/cmd.py` |
| **Docker resource limits** | `--memory 2g --pids-limit 512 --cpus 2 --network bridge` by default (configurable) | `_docker_run_argv()` in `src/nimcode/tools/cmd.py` |
| **Docker command validation** | `_validate_command_safety()` warns on `--privileged`, docker socket mounts, `--cap-add`, host namespaces, shell injection patterns; `docker_extra_args` dangerous flags are dropped | `src/nimcode/tools/cmd.py:343` |
| **Env scrubbing** | `SENSITIVE_ENV_REGEX` (covers `API_KEY`, `TOKEN`, `_PW`, `AWS_SECRET`, DB URLs, etc.) + `scrub_env()` before every `Popen` | `src/nimcode/tools/base.py:457` |
| **Inline secret scrubbing** | `scrub_text()` masks `Bearer`, `Authorization: Basic`, `--password`/`--token`, `x-api-key`, `KEY=value` lines, `AKIA*`/`nvapi-`/`sk-`/`ghp_` keys, JWT, `scheme://user:pass@`, 32+ char keys | `src/nimcode/tools/base.py:162` |
| **Output truncation** | `truncate_output()` keeps head + tail, drops middle (`dropped N chars`) | `src/nimcode/tools/base.py:240` |
| **SSRF guard** | `_is_url_allowed()` DNS-resolves targets and rejects any non-global IP (localhost excepted for the dev-server loop); applied to every screenshot request and every skill-download redirect | `src/nimcode/tools/base.py` |
| **Skill install review** | Staged download + preview + confirmation; `.source.json` records url + sha256 | `src/nimcode/skills.py` |
| **Human gate** | All mutations require `y/n` (Build) or `yolo` (Plan); YOLO still shows preview | `src/nimcode/core/agent_tui.py:1018` |
| **Subagent caveat** | Swarm threads **bypass** the human gate — they execute tools directly (Docker-only for commands) | `src/nimcode/tools/swarm.py` |

---

## 14. Session Management, Backups & Recovery

### Central Storage (`get_project_nimcode_dir`)

`src/nimcode/config.py:14`:

```python
hash = md5(str(workspace_root.resolve()).encode()).hexdigest()[:8]
project_dir = Path.home() / ".nimcode" / f"{resolved.name}_{hash}"
```

- One-time migration from legacy `~/.nimcode/<name>` (unhashed) and local `<workspace>/.nimcode/` is performed automatically.
- `project_dir` holds: `sessions/`, `backups/`, `process_logs/`, `model.json`, `summary.md`, `AGENTS.md` (optional).

### Sessions

- **Naming:** `session_<YYYYMMDD_HHMMSS>.jsonl`.
- **Logging:** `log_turn()` appends one JSON line per message (including `tool_calls` arrays, `default=str` fallback). Nothing is rewritten, so a crash loses at most the last line, which `load_session()` skips.
- **Resume:** `nimcode resume` uses `list_sessions()` (newest first; `.jsonl` plus legacy `.json`). Legacy `.json` sessions are copied into a new `.jsonl` and the original is left untouched. History is rendered in the TUI before waiting for input.

### Backups

- Every `write_file` / `patch_file_*` with `auto_backup=true` snapshots the previous file via `backup_file()` to `backups/<microseconds>_<pid>_[blocks_|ast_]<safe_rel>` and appends `{backup, original, tag, ts}` to `backups/manifest.jsonl`.
- **View:** `/changes` shows `git status -s` + the 15 most recent backups from `list_backups()` (sorted by backup time).
- **Restore:** `/restore` shows an inline backup picker (↑/↓, Enter) and restores straight to the recorded original path. Only legacy backups without a manifest entry prompt for the target. The target must stay inside the workspace root.

### Process Logs

Host-mode background processes write to `process_logs/bg_N.log` (`src/nimcode/tools/cmd.py:906`). Cleaned up to keep last 50 logs on exit (`:76`).

---

## 15. Model Catalog (`/model`)

Static seed `AVAILABLE_MODELS` (`src/nimcode/config.py:750`, 10 entries) is only the fallback. At runtime the catalog is **fetched live** (`src/nimcode/config_models.py`):

- Source: OpenAI-compatible `GET {api_base_url}/v1/models` (`client.models.list()`), normalized to `{id, name, context_tokens}`; non-chat endpoints (embed/rerank/OCR/vision/TTS/safety) filtered out; pretty display names derived from IDs.
- Context windows: curated native windows (Nemotron-3 family 1M, DeepSeek V4 1M, Mixtral 32k); server-provided `max_model_len` wins when present (self-hosted); unknown models leave the budget untouched — never fabricated.
- Cache: global `~/.nimcode/models_cache.json`, 24h TTL (`NIMCODE_MODELS_TTL_HOURS`), atomic writes. Startup is instant (cache/static) + background daemon refresh; `NIMCODE_NO_MODEL_REFRESH=1` disables it (air-gapped). Offline startup unaffected.
- Retired models: saved `fast_model`/`plan_model` missing from the catalog auto-falls back to the default with a notice (startup, `/model`, `refresh-models`).
- Context auto-adjust: switching models sets `context_budget_tokens` to the new window (announced); a manually customized budget survives restarts and is only overridden by an explicit switch. Served caps below native windows self-correct via prune-on-overflow retry.
- Commands: `nimcode refresh-models` (blocking refresh + top-20 list), `/models-refresh` in-TUI, `nimcode status` shows catalog size + freshness.

| # | Name | Model ID | Native window |
|---|---|---|---|
| 1 | Nemotron 3 Nano 30B | `nvidia/nemotron-3-nano-30b-a3b` | 1M |
| 2 | GPT-OSS 20B | `openai/gpt-oss-20b` | unknown |
| 3 | Mixtral 8x7B Instruct | `mistralai/mixtral-8x7b-instruct-v0.1` | 32k |
| 4 | Mistral Small | `mistralai/mistral-small-4-119b-2603` | unknown |
| 5 | Nemotron 3 Super 120B | `nvidia/nemotron-3-super-120b-a12b` | 1M |
| 6 | GLM 5.2 | `z-ai/glm-5.2` | unknown |
| 7 | MiniMax M3 | `minimaxai/minimax-m3` | unknown |
| 8 | Mistral Large | `mistralai/mistral-large-3-675b-instruct-2512` | unknown |
| 9 | DeepSeek V4 Pro | `deepseek-ai/deepseek-v4-pro` | 1M |
| 10 | Nemotron 3 Ultra 550B | `nvidia/nemotron-3-ultra-550b-a55b` | 1M |

- **TUI selector** (`select_model()`, `src/nimcode/ui/rust_bridge.py:390`) shows a sidecar list (↑/↓, `Enter`) with `[Nk ctx]` badges, highlights the current model with ` (current)`, and handles the full handshake (summary generation + handoff injection).
---

## 16. Skills System

`src/nimcode/skills.py:10` — global `SKILLS_DIR = ~/.nimcode/skills`.

| Function | Description |
|---|---|
| `fetch_skill(url)` | `https/http` only, SSRF-checked including every redirect. Downloads into `SKILLS_DIR/.staging/`: `.zip` (20MB download / 50MB extracted / 2000 entries, symlink + Zip-Slip rejected), `.git`/`github.com` (`git clone --depth 1`, no redirects, 60s timeout), raw `.md` (2MB, installed as `<name>/SKILL.md`). Returns `(staging_dir, preview, sha256)`; raises `ValueError` on failure. |
| `commit_skill(staging, name, url, sha256)` / `discard_skill(staging)` | Move the staged skill into place (refusing to overwrite) and write `.source.json`, or delete it |
| `install_skill(url)` | `fetch_skill` + `commit_skill` without the confirmation step |
| `list_skills()` (`:178`) | Lists directories + `.md` stems under `SKILLS_DIR` (empty list if missing) |
| `get_skill_content(name)` (`:193`) | Strict name allowlist (`fullmatch(r"[a-zA-Z0-9_\-\.]+")`); 200KB cap; reads `SKILL.md` → `README.md` → `instructions.md`, or the raw `.md` file; returns `None` on any error |
| `init_skills_dir()` (`:35`) | Ensures `SKILLS_DIR` exists |

In the TUI, `/skills` lists `/{skill}` entries; typing `/<skill> [args]` injects its content as `<skill_context>` and continues the turn, and `/install-skill <url>` previews the skill and installs it after confirmation.

---

## 17. Context Harvesting & System Prompt

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
MODES: Plan Mode (<plan>, no tools) / Build/YOLO (execute step by step).
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

---

## 18. UI Subsystem (Rust TUI, Console)

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

---

## 19. Docker Sandbox Details

`_docker_run_argv()` (`src/nimcode/tools/cmd.py`), shared by foreground and background runs:

```bash
docker run --rm --init \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -v <workspace>:/workspace \
  -w /workspace \
  --memory 2g --pids-limit 512 --cpus 2 --network bridge \
  [-p 3000:3000 ...] \
  [extra_args...] \
  python:3.11-slim \
  sh -c "<command>"
```

- **Background containers** use `docker run -d --name nimcode_bg_<uuid>` (no `--rm`, so crash logs stay readable; removed by `stop_process`).
- Detection: `is_docker_available()` runs `docker info` with 5s timeout, cached 30 s (`src/nimcode/tools/base.py:43`).
- **Fallback** (main agent only) is automatic if Docker is absent; a real fallback is annotated `[Note: Docker unavailable, fell back to host execution]`. Subagents never fall back to the host.
- **Host cleanup** uses `taskkill /F /T` on Windows and process-group `SIGTERM→SIGKILL` on POSIX (no orphaned grandchildren) (`src/nimcode/tools/cmd.py:43`).
- **Validation:** `_validate_command_safety()` warns on socket mounts, `--privileged`, `--cap-add`, host namespaces, and shell-injection patterns; dangerous `docker_extra_args` are dropped by `_safe_docker_extra_args()`.

---

## 20. Background Processes & Visual Verification

### Lifecycle (`src/nimcode/tools/cmd.py:822`)

```
start_background_process("npm run dev")
  → bg_1 (Docker: docker run -d / Host: Popen in new session group + logfile)
get_process_output("bg_1")
  → docker inspect + docker logs --tail 500  |  poll() + 200KB logfile tail
stop_process("bg_1")
  → docker kill/rm -f  |  _kill_host_process()
cleanup_background_processes()  ← atexit hook + /kill (max 10 processes)
```

Global registry `BACKGROUND_PROCESSES: Dict[str, Dict]` (`:24`, guarded by `_BG_LOCK`) tracks `popen_or_container_id`, `logfile_path`, `is_docker`, `container_name`, `start_time`, `command`.

### Visual Verification Loop

Documented in the system prompt and executed as:

1. `start_background_process({"command": "npm run dev"})`
2. `screenshot_page({"url": "http://localhost:3000"})` — Playwright 1280×720, `domcontentloaded` + 1.5s settle, retries 3× with `+5s` timeout backoff (`src/nimcode/tools/browser.py:45`). Screenshots are saved to `~/.nimcode/<project>_<hash>/screenshots/` and only a capped base64 preview is inlined to protect context.
3. Agent evaluates the base64 screenshot against a 5-point checklist (unstyled defaults, spacing, hierarchy, clipping, loading/error states)
4. `patch_file_*` / `write_file` fixes → re-screenshot → re-evaluate
5. `stop_process({"process_id": "bg_1"})`

Also supports `screenshot_page({"html_file": "dist/index.html"})` → resolves to `file://` URI via `resolve_and_validate_path`.

---

## 21. Swarm & Sub-Agents (`/team`)

`src/nimcode/tools/swarm.py:118`:

- `spawn_agent_logic(role, task_prompt, workspace_root, config)` builds a fresh `messages = [system + role suffix, user task_prompt]` with a live `workspace_map`, seeds `ACTIVE_SUBAGENTS[agent_id]` (`id = sub_<8-hex-uuid>`, with per-agent `depth`), caps at 10 active subagents / nesting depth 3 (fork-bomb guard), and spawns `resume_subagent_thread()`.
- `resume_subagent_thread()` (`:178`) runs on a **thread-local config copy** carrying the agent's depth, so the depth cap accumulates across generations (sibling threads never race on shared state). It loops `max_steps` (50 in YOLO, else `max_loop_iterations`) calling the NIM API (same `AVAILABLE_TOOLS_SPEC` + `normalize_messages`), executing tools via `execute_local_tool()` directly (no human gate), and appending `assistant` / `tool` turns to `sub["messages"]`. Aborts after 5 consecutive invalid tool calls; empty model responses mark `ERROR` (no zombie `RUNNING`); cooperative `KILLED` stays `KILLED` (never overwritten to `ERROR`). Thread safety via `sub["lock"]` (`threading.Lock`) + `_SUBAGENTS_LOCK` (`RLock`, `:13`).
- Monitor live with `/team` (sidebar also polls `ACTIVE_SUBAGENTS` every turn).
- Each run has a wall-clock budget (`subagent_timeout_s`, default 900 s); exceeding it marks the agent `TIMED_OUT`.
- Transcripts are appended to `~/.nimcode/<project>_<hash>/subagents/<id>.jsonl`; finished, killed and timed-out agents are dropped from `ACTIVE_SUBAGENTS` 10 minutes after they end (transcripts stay).
- Subagents run shell commands only inside Docker.
- Interact with `send_message_to_agent` (injects a new `user` message and resumes the thread) or terminate with `kill_agent`.

---

## 22. Web Search & Browser Tools

### `web_search` — `src/nimcode/tools/web.py:149`

- Fetches `https://html.duckduckgo.com/html/?q=<query>` with `Mozilla/5.0 … Chrome/120` UA.
- `max_results` clamped to 1–10; output capped via `truncate_output` (20k chars).
- Retry: `_fetch_with_retry()` 3× with exponential backoff `(2^attempt)*0.5s`.
- Overall timeout: `_fetch_with_overall_timeout()` wraps in a daemon thread joined at 30s.
- Parsing: `_parse_duckduckgo_results()` tries 4 strategies (modern `results_links_deep`, `links_main`, generic `result` divs, `result__snippet` anchor split), then 2 title/href/snippet pattern sets, and unwraps `uddg=` redirect params.

### `screenshot_page` — `src/nimcode/tools/browser.py:118`

- Requires `playwright` (`sync_playwright` guard). If absent, returns install hint.
- SSRF-guarded by `_is_url_allowed()` (`:14`): `url` must be `http/https`; the host is DNS-resolved and **every** returned IP is checked — decimal/octal/hex spellings (`2130706433`, `127.1`, `0x7f.0.0.1`), wildcard-DNS tricks (`*.nip.io`), metadata endpoints, and private/link-local/reserved ranges are all blocked. Only public hosts plus explicit `localhost`/`127.0.0.1`/`::1` (which must resolve to loopback) stay allowed for the dev-server loop. Static files go through `html_file` → sandboxed `resolve_and_validate_path` → `file://` URI.
- Navigate + screenshot happen inside one Playwright context (no leaked driver/pages): `chromium.launch(headless, --no-sandbox --disable-dev-shm-usage)`, viewport 1280×720, `page.goto(url, wait_until="domcontentloaded", timeout=15000+attempt*5000)` + 1.5s settle, 3× retries.
- Full PNG is saved under `~/.nimcode/<project>_<hash>/screenshots/` (`:191`); the tool returns the path plus a capped base64 preview so the model context can't blow out.

---

## 23. Testing & CI

### Running Tests Locally

```bash
uv sync --extra dev        # or: pip install -e .[dev]  (pins ruff + pytest)
pytest -v
pytest tests/test_cmd.py -v
pytest --collect-only
```

Suite (`tests/`, ~145 tests). Main files:

| File | What it covers |
|---|---|
| `test_cmd.py` | `execute_command_tool` host execution, timeout, empty-command guard, `truncate_output`, dev-server blocklist |
| `test_config_persist.py` | `Config` persistence, validation coercion & migration |
| `test_empty_turn.py` | Empty model-turn retry behavior |
| `test_missing_tools.py` | Path sandbox escape, `search_symbols` boundaries |
| `test_model_catalog.py` | Model catalog filtering & context windows |
| `test_normalize_messages.py` | System consolidation, alternating roles, empty-check injection |
| `test_preview.py` | `render_preview` diff/command previews |
| `test_retry_fallbacks.py` | API retry, XML fallback & context-overflow paths |
| `test_run_tests.py` | `execute_test_runner` auto-detection |
| `test_slash_commands.py` | `SLASH_COMMANDS` catalog vs handler coverage |
| `test_swarm_extra.py` | `ACTIVE_SUBAGENTS` lifecycle, tool schema validation |
| `test_web_search.py` | `web_search` schema + parser mocks |
| `test_arch_*.py` | JSONL sessions, backup manifest/restore, TUI loop (mid-batch mode switch, off-thread tools, skill install) |
| `test_sec_track_b.py` | Subagent host refusal, Docker limits argv, skill SSRF/redirects/staging |
| `test_bridge_protocol.py` | Real `RustTUI` socket round-trip against `fake_sidecar.py` + `protocol_samples.json` |
| `test_ci_windows_kill.py` | Windows-only process-tree kill on timeout |
| `test_*_review_*.py` | Regression tests for the full-project review fixes |

### Continuous Integration (`.github/workflows/ci.yml`)

Workflow `CI` on `push`/`pull_request` to `main`/`master`:

- **python**: `ubuntu-latest` + `windows-latest` × py 3.10/3.11/3.12: `pip install -e .[dev]` → `ruff check` → `ruff format --check` → import smoke → `nimcode status` (dummy key) → `pytest`.
- **rust**: ubuntu + windows: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test` (includes the shared protocol-sample test).
- **wheel**: builds the sidecar and a wheel, asserts `nimcode/bin/nim-tui` is inside.

### Releases (`.github/workflows/release.yml`)

Push a `v*` tag → builds the sidecar for `x86_64-pc-windows-msvc`, `x86_64-unknown-linux-musl`, `aarch64-apple-darwin` and `x86_64-apple-darwin`, packages a platform-tagged wheel for each (smoke-tested in a fresh venv), plus an sdist, and attaches them to a GitHub Release.

### Deploy checklist

1. `pip install -e .` (or `pip install -r requirements.txt` + `pip install -e .`) — both carry the full runtime set.
2. `ruff check . && ruff format --check . && pytest -q` — must be green.
3. `nimcode status` with `NVIDIA_API_KEY` set — verifies config, workspace map, session dir, live model catalog.
4. `playwright install chromium` — only needed for `screenshot_page` visual verification.
5. Docker is optional: without a daemon, commands run on the host with a `[Note: Docker unavailable …]` annotation; set `disable_docker = true` to silence.

---

## 24. Development Guide

```bash
# Editable install + dev deps (pinned ruff/pytest)
uv sync --extra dev        # or: pip install -e .[dev]
pip install tiktoken       # optional, exact token counts
cargo build --release --manifest-path tui-rs/Cargo.toml   # TUI sidecar

# Lint & format (Ruff)
ruff check .
ruff format .
ruff check --fix .

# Type sketching (optional, not enforced in CI yet)
# mypy src/nimcode

# Smoke import test
python -c "import nimcode.cli, nimcode.config, nimcode.context, nimcode.skills, nimcode.core.agent, nimcode.core.agent_tui, nimcode.core.prompts, nimcode.core.state, nimcode.tools, nimcode.ui.console, nimcode.ui.output, nimcode.ui.rust_bridge; print('OK')"

# CLI self-check
NVIDIA_API_KEY=dummy pytest  # for offline tests
nimcode status                # with real key
```

**Adding a new tool** (see §5 of `AGENTS.md`):

1. Create / edit `src/nimcode/tools/<name>.py` with a `@register_tool("my_tool", MY_SCHEMA)` function `(args, workspace_root, config) -> str`. Reuse `backup_file()`, `coerce_output_cap()`, and `load_gitignore_rules()` instead of re-implementing them.
2. Add a branch in `render_preview()` (`src/nimcode/tools/base.py:262`).
3. Extend `is_valid_tool_call()` (`src/nimcode/tools/base.py:70`) and, if user-facing, the `SYSTEM_PROMPT_TEMPLATE` (`src/nimcode/core/prompts.py:5`).
4. Register import in `src/nimcode/tools/__init__.py:3` so the decorator runs at startup.
5. Add tests in `tests/`.

---

## 25. Troubleshooting & FAQ

**`Error: NVIDIA_API_KEY is not set.`**
Set it in your shell, local `.env`, or `~/.nimcode/.env`. See §7.1.

**`playwright is not installed.`**
`pip install playwright && playwright install chromium` (`src/nimcode/tools/browser.py:100`).

**`Command '...' matches a known long-running/blocking process pattern.`**
You used `execute_command` for a dev server (matched on the invoked binary — `uvicorn`, `vite`, `gunicorn`, … — or a server phrase like `npm run dev`). Use `start_background_process` instead (`src/nimcode/tools/cmd.py:487`). Note `grep -r vite …` is *not* blocked: only the invoked program counts.

**Tool output was truncated.**
Increase `output_cap` in `nimcode.toml` or via `/config`, or use `read_file` with `start_line`/`end_line` for targeted slices (ranged reads are capped too).

**Model returns `choices=[]` / `choices list empty`.**
Set `max_tokens = 4096` (or higher) in `nimcode.toml`. The endpoint for MiniMax-M3 / GLM requires it (`src/nimcode/core/agent.py:478`).

**`auto tool choice` / `degraded function` error → fallback.**
Nimcode auto-flips `config.disable_native_tools = true`, rebuilds the system prompt with XML instructions, and retries (`src/nimcode/core/agent.py:381`). You can also pre-set `disable_native_tools = true` in `nimcode.toml` for self-hosted endpoints.

**Docker not available.**
Nimcode falls back to host. Silence the warning with `disable_docker = true`.

**Windows `cp1252` garbled output.**
Nimcode reconfigures `sys.stdout`/`stderr` to UTF-8 at startup (`src/nimcode/cli.py:32`, `src/nimcode/config.py:8`), but respects `NIMCODE_ASCII=1` / `NIMCODE_PLAIN=1` / `NO_COLOR=1` to force ASCII/no-color.

**Session file not found / empty.**
Check `~/.nimcode/<project>_<hash>/sessions/` and re-run `nimcode status` to confirm `Workspace Root`. Use `nimcode resume --latest` to load the newest file.

**Subagents run without approval — is that safe?**
Yes, by design (§13 caveat). Swarm threads have no human gate but are scoped to `workspace_root` via `resolve_and_validate_path` and `scrub_env`.

---

## 26. Version History

| Tag | Version | Notes |
|---|---|---|
| `40ac825` | **V0.1** | Initial prototype |
| `9942bdc` | **V0.2** | Early REPL + file tools |
| `8696a90` | **V0.3** | Plan/Build modes + Docker preview |
| `4f416a2` | **V0.4** | Subagents, skills, web search |
| `8dd5438` | **V0.5** | Playwright verification, background procs |
| `a273b25` | **V0.6** | Full TUI (`agent_tui.py`, sidecar bridge, selectors), stats, token budgeting, inline selectors |
| HEAD | **0.6.0** | Current (`pyproject.toml:7`, `src/nimcode/__init__.py:1`): hardening + deployability (secret redaction, SSRF guards, Zip-Slip/symlink fixes, atomic writes + session persistence, UUID subagent IDs + fork-bomb caps, timeout validation, background-process caps, warn-only Docker CI), agent-quality pass (prescriptive system prompt, guided tool schemas, `temperature_fast` 0.2, loop cap 20), shared helpers (`backup_file`, `coerce_output_cap`, `load_gitignore_rules`), TUI `/install-skill` + `/<skill>`, and bug-fix batch (backup-timestamp crash, DNS-based SSRF, subagent depth accumulation, TOML validation, full `save_config` roundtrip) |

---

## 27. Contributing & License

- **Issues & feedback:** the [nimcode repo issue tracker](https://github.com/PrathamPai-2007/nimcode/issues).
- **Contributions:** Fork → feature branch → `ruff format . && ruff check . && pytest -q` → PR to `main`. Keep tools JSON-serializable and workspace-scoped; add `tests/test_*` for new tools or state transitions.
- **License:** MIT (add a `LICENSE` file if distributing).

---

*Built with `typer`, `openai`, `rich`, `prompt_toolkit`, `playwright`, and NVIDIA NIM. All system data lives under `~/.nimcode/` — your workspace stays pristine.*

## Benchmarks (Harbor)

`bench/harbor_nimcode.py` runs nimcode headless inside [Harbor](https://github.com/laude-institute/harbor) tasks (Terminal-Bench 2.0, Aider Polyglot, ...).

```bash
uv tool install harbor
python -m build --wheel --outdir bench/dist
export PYTHONPATH=. NVIDIA_API_KEY=nvapi-...
harbor run -d terminal-bench@2.0 -a bench.harbor_nimcode:NimcodeAgent -m <nim-model-id> -l 10
```

`NIMCODE_API_BASE_URL` points nimcode at any OpenAI-compatible endpoint. Real runs spend NVIDIA credits.
