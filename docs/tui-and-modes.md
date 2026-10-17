# TUI, slash commands and modes

## TUI & Slash Commands

The TUI is the Rust sidecar `nim-tui` (`tui-rs/`, bundled in release wheels, override via `NIM_TUI_BIN`), driven by `run_agent_loop_tui()` through the `RustTUI` bridge (`src/nimcode/ui/rust_bridge.py`). Python speaks NDJSON to the sidecar over a loopback socket; the sidecar owns the terminal (raw mode + alternate screen), so keystrokes are handled exactly once. If the binary is missing, `nimcode` exits with rebuild instructions instead of starting a degraded UI.

#### Design: "Nim"

*Nim* is the old game of taking matchsticks from heaps (classic start: 1-3-5-7), and the context window plays the same way. The sidebar's context gauge is that board: 16 sticks, each 1/16 of the budget; unused sticks keep a sulfur head, spent ones are struck to a dim `╎`. Under it, a plain "41k of 128k" (sulfur from 75%).

- **No boxes.** One vertical rule separates the conversation from the sidebar; one horizontal rule sits above the input.
- **Who is speaking** is a coloured left gutter `▎`: wisteria for you, pine for nimcode, sulfur for tools, ash for system notes, red only for errors. Text wraps at 100 columns.
- **Mode** colours the mode word and the `›` prompt: Plan = wisteria, Build = pine.
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
| Shift+Tab / Ctrl+T | Cycle Plan ↔ Build (applies between tool calls too) |
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
| `/build` | — | Switch to Build Mode (`EXECUTING`, risk-based approval) | same |
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
| `Shift+Tab` | Cycle `Plan ↔ Build` (updates `config.mode`) |
| `Ctrl+C` | Cancel current work: clears input, or interrupts the in-flight API call / remaining tool calls (`_interrupted` flag, `src/nimcode/ui/rust_bridge.py`, `src/nimcode/core/agent.py:395`). Press again at an empty prompt handling to exit |
| `Ctrl+D` | Exit the session cleanly (sticky `_eof_requested`, observed even mid-turn) |
| `Ctrl+T` | Cycle `Plan ↔ Build` (same as `Shift+Tab`) |
| `Tab` (with `/` popup) | Accept the highlighted completion inline |
| `Esc` | Dismiss the `/` popup, else stop stick-to-bottom follow mode |
| `↑` / `↓` / `PageUp` / `PageDown` / `Home` / `End` | Scroll the conversation pane |

## Operational Modes (Plan / Build)

Defined in `src/nimcode/core/state.py:4`:

```python
class AgentState(str, Enum):
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"
```

| Mode | State | How to enter | Tool access | Approval | Loop cap |
|---|---|---|---|---|---|
| **Plan** | `AWAITING_PLAN_APPROVAL` | `/plan`, `nimcode run --mode plan` or `mode=plan` | Read-only tools (`read_file`, `list_files`, `glob_files`, `search_grep`, `search_symbols`, `web_search`, ...). Anything else is refused. | Reads run unprompted (sensitive files ask); the plan itself must be approved `y` | `max_loop_iterations` (40) |
| **Build** | `EXECUTING` | `/build` or `mode=build` (default) | Full `AVAILABLE_TOOLS_SPEC` | `classify()` in `src/nimcode/tools/policy.py`: ordinary writes, commands and network tools run; sensitive files and risky commands ask `y/n` | `max_loop_iterations` (40) |

Build auto-runs the test suite after file edits (`auto_test`, on by default). Tune what asks in `policy.py` (`SENSITIVE_PATTERNS`, `RISKY_COMMAND`); the command rules are a heuristic, Docker is the real boundary.

**Plan flow** (`src/nimcode/core/agent_tui.py:777`): read-only tool calls run first; then assistant text → `extract_plan_block()` (a reply with no `<plan>` is just an answer) (`src/nimcode/core/prompts.py:159`) → render plan → prompt `Approve? (y/n)` → on `y` inject trigger message `"Plan approved. Please proceed with step 1 using tool calls."`.
