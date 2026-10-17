# Troubleshooting

## Troubleshooting & FAQ

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
