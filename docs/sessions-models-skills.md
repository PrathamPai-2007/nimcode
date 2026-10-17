# Sessions, models and skills

## Session Management, Backups & Recovery

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

## Model Catalog (`/model`)

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

## Skills System

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
