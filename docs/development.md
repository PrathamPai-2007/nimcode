# Development, testing and releases

## Testing & CI

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

## Development Guide

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
