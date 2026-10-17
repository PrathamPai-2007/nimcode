# Nimcode

A terminal AI coding agent for [NVIDIA NIM](https://build.nvidia.com/). It reads your project, proposes changes, and **asks before it touches anything**.

## Install

```powershell
# Windows
irm https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.ps1 | iex
```
```bash
# Linux / macOS
curl -LsSf https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.sh | sh
```

The script installs [uv](https://docs.astral.sh/uv/) if needed, then the build for your platform. Re-run it to update; `uv tool uninstall nimcode` removes it. Pin a version with `NIMCODE_VERSION=v0.7.0`.

## Use

1. Get a free key at [build.nvidia.com](https://build.nvidia.com/) and set it: `NVIDIA_API_KEY=nvapi-...` (environment variable, or a `.env` file in your project or `~/.nimcode/.env`).
2. Run `nimcode` in any project folder and describe a task.

| Mode (Shift+Tab) | What happens |
|---|---|
| **Plan** | The model writes a plan; nothing runs. |
| **Build** (default) | Every file edit or command shows a preview and waits for `y`/`n`. |
| **YOLO** | Auto-approves and runs tests after edits. |

Handy: `/model` switch model (`[free]` marks free endpoints that currently respond), `/help` all commands, `/restore` undo file changes, `/compact` shrink history, `nimcode resume --latest` continue a session.

Scripts and CI: `nimcode run --headless --yolo --model <id> "<task>"`.

## Good to know

- Commands run in Docker when it's available, otherwise on your machine (after your approval).
- Your project stays clean: sessions, backups and logs live in `~/.nimcode/`.
- Secrets are redacted from previews and tool output.

## More

- [Developer docs](docs/README.md): configuration, CLI, architecture, security, tools, testing, benchmarks
- [Changelog](CHANGELOG.md) · [Issues](https://github.com/PrathamPai-2007/nimcode/issues)
