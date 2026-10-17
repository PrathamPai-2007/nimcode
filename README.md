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
| **Plan** | Reads your code freely, then proposes a plan. Never changes anything. |
| **Build** (default) | Edits files and runs commands on its own, runs tests after edits, and asks only for sensitive files (`.env`, keys, `.git`) and risky commands (`rm -r`, `git push`, `curl`, installs). |

Handy: `/model` switch model (`[free]` marks free endpoints that currently respond), `/help` all commands, `/restore` undo file changes, `/compact` shrink history, `nimcode resume --latest` continue a session.

Scripts and CI: `nimcode run --headless --model <id> "<task>"`.

## Good to know

- Commands run in Docker when it's available, otherwise on your machine (risky ones ask first).
- Your project stays clean: sessions, backups and logs live in `~/.nimcode/`.
- Secrets are redacted from previews and tool output.

## More

- [Developer docs](docs/README.md): configuration, CLI, architecture, security, tools, testing, benchmarks
- [Changelog](CHANGELOG.md) · [Issues](https://github.com/PrathamPai-2007/nimcode/issues)
