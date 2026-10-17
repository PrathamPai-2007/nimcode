# Configuration

## Configuration

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
