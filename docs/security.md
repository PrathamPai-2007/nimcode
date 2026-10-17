# Security and Docker sandbox

## Security & Sandboxing

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

## Docker Sandbox Details

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
