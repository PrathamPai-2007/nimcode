# Changelog

## 0.7.0

**TUI**
- New "Nim" design: no boxes, speaker gutters, a 1-3-5-7 matchstick context gauge, mode-coloured prompt, `NO_COLOR` / `NIMCODE_ASCII` fallbacks.
- Fixed input lag on Windows (~700 ms per keystroke → ~7 ms): keys are now read on a dedicated thread.
- Input editing (Ctrl+J / Shift+Enter newline, Ctrl+U, history), in-place resume, real `/compact`, sticky Ctrl+D.
- `/model` and `nimcode refresh-models` tag free-endpoint models with `[free]`.

**Agent**
- Two modes: Plan and Build. YOLO is gone. Plan can now read the whole project (read-only tools) before proposing a plan; Build runs ordinary edits, commands and tests on its own and asks only for sensitive files (`.env`, keys, `.git`, CI workflows) and risky commands (`rm -r`, `sudo`, `git push`, `curl`, installs). `mode = "yolo"` in old configs falls back to `build`; `--yolo` is removed.
- `auto_test` option; `max_loop_iterations` default is now 40.
- Headless mode: `nimcode run --headless --model <id> --json-out run.json "<task>"` with token usage.
- `NIMCODE_API_BASE_URL` points nimcode at any OpenAI-compatible endpoint.
- Sessions stored as JSONL; backup manifest; API errors classified by type; pruning budget from the model's real context window.
- "No tests collected" is no longer reported to the model as a failure.

**Security**
- Subagents never run commands on the host (Docker only).
- Docker memory / pids / cpu / network limits; split-form `docker_extra_args` are sanitised.
- Skill installs are staged, previewed (sha256) and confirmed; redirects are SSRF-checked.

**Distribution**
- Per-platform wheels (Windows, Linux, macOS arm64 + x86_64) with the sidecar bundled, published to GitHub Releases; `install.ps1` / `install.sh` one-line installers.
- CI: ruff, pytest (Ubuntu + Windows, py 3.10–3.12), Rust fmt/clippy/test, wheel-contents check; release tags must match the package version.
- `bench/harbor_nimcode.py` runs nimcode in Harbor (Terminal-Bench 2.0, Aider Polyglot).

**Fixes**
- Startup crash with typer 0.26, Ctrl+D hang at approval prompts, duplicate "(y/n)", and many bugs from the full-project review.
- Default model is now `nvidia/nemotron-3-super-120b-a12b`; retired models (HTTP 410) are hidden automatically and `[free]` marks only endpoints verified to respond.
- API timeouts fail after one attempt with a hint to switch models, instead of three silent retries.
