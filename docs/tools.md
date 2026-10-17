# Tools, background processes and web

## Tools Reference — The Agent's Hands

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

## Background Processes & Visual Verification

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

## Web Search & Browser Tools

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
