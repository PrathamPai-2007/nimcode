# CLI reference

## CLI Reference

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
