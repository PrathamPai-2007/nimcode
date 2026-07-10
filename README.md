# Nimcode

**Nimcode** is an open-source, CLI-based AI coding agent driven by NVIDIA NIM.

It wraps a continuous Read-Eval-Print Loop (REPL) around a multi-model LLM architecture, giving the AI programmatic "hands" (sandboxed tools) to inspect and alter your workspace. Crucially, **nimcode prioritizes human control.** Every irreversible action—from modifying files to running shell commands—passes through a human approval gate before it touches your disk or the shell.

## Key Features

*   **Terminal Interface (TUI):** A gorgeous, responsive terminal interface powered by `prompt_toolkit` and `rich`. Enjoy inline interactive model selection, mode-colored UI frames, seamless autocomplete for slash commands, and an unobtrusive status bar.
*   **Three Operational Modes (Plan, Build & YOLO):**
    *   **Plan Mode (`/plan`):** Enforces a strict "Plan-Before-Execution" protocol. The agent must present a structured architectural `<plan>` for approval before executing any tools.
    *   **Build Mode (`/build`):** Human-in-the-loop execution. Every tool call displays a unified diff or command preview before prompting for `y/n` approval.
    *   **YOLO Mode (`/yolo` / `--yolo`):** High-speed autonomous execution. Displays full terminal previews for every action but automatically accepts tool calls without pausing for human input.
*   **Instant Mode & Model Switching:** Press `Shift+Tab` to cycle between Plan, Build, and YOLO modes directly from the prompt. Type `/model` to instantly swap NVIDIA NIM models mid-conversation—Nimcode automatically handles the context handoff via `summary.md`.
*   **Consolidated Workspace Architecture:** All system files, session histories, backups, and handoff summaries are stored securely inside `~/.nimcode/<project-name>/`, keeping your workspace root pristine.
*   **Live Agent Rules:** Use the `/init` command to automatically construct a robust `AGENTS.md` file. Nimcode injects these rules live on every turn, meaning updates apply immediately without restarting your session.
*   **Auto-Backups & Session Recovery:** Overwritten files are automatically snapshotted to `~/.nimcode/<project-name>/backups/`. Use `/resume` or `nimcode resume` to instantly reload and continue past conversations.
*   **Decorator-Based Tool Registry:** Tools are registered via `@register_tool` decorators in `src/nimcode/tools/`, making it trivial to add new capabilities without touching core loop logic.

## Prerequisites

*   Python 3.10+
*   An [NVIDIA NIM API Key](https://build.nvidia.com/) (set as `NVIDIA_API_KEY`).

## Installation

1.  Clone the repository and navigate into the directory:
    ```bash
    git clone https://github.com/PrathamPai-2007/nimcode.git
    cd nimcode
    ```
2.  Install `nimcode` globally in editable mode:
    ```bash
    pip install -e .
    ```
3.  Set your NVIDIA NIM API key. You can export it to your environment, create a `.env` file in your current project directory, or create a global fallback at `~/.nimcode/.env`:
    ```env
    NVIDIA_API_KEY=nvapi-...
    ```

## Configuration (`nimcode.toml`)

`nimcode` reads from an optional `nimcode.toml` file in your **current workspace root** (the directory you run the command from). All fields are optional—sensible defaults are used when the file is absent.

| Key                    | Default                         | Description                                          |
|------------------------|---------------------------------|------------------------------------------------------|
| `fast_model`           | `nvidia/nemotron-3-nano-30b-a3b` | Model used for Build & YOLO mode execution           |
| `plan_model`           | `nvidia/nemotron-3-nano-30b-a3b` | Model used for Plan mode reasoning                   |
| `mode`                 | `build`                         | Default startup mode (`plan`, `build`, or `yolo`)    |
| `timeout`              | `60`                            | Hard timeout (seconds) for subprocess execution      |
| `max_loop_iterations`  | `10`                            | Safety cap on consecutive AI steps per turn          |
| `output_cap`           | `2000`                          | Max characters returned from any single tool output  |
| `auto_backup`          | `true`                          | Snapshot files before overwriting them               |

You can switch models at any time mid-session with the `/model` command — no need to edit the config file.

## Usage

Because `nimcode` is installed globally, you can run it from **any directory** on your machine.

Check your environment and configuration:
```bash
nimcode status
```

Start a new interactive session:
```bash
nimcode
```

Run nimcode with a direct task instruction:
```bash
nimcode run "Create a Python script that fetches the current weather."
```

Run directly in YOLO Mode (auto-approves all edits and commands):
```bash
nimcode run --yolo "Refactor main.py to use async/await and run the tests."
```

Resume an interrupted session interactively:
```bash
nimcode resume
```

Resume the most recent session automatically:
```bash
nimcode resume --latest
```

Initialize or update the `AGENTS.md` rules file for the current workspace:
```bash
nimcode init
```

## Project Structure

```
nimcode/
├── src/
│   └── nimcode/
│       ├── cli.py          # Typer CLI entry point (all commands)
│       ├── config.py       # Config dataclass, model list, interactive selectors
│       ├── context.py      # Workspace map harvester
│       ├── skills.py       # Skills loader
│       ├── core/
│       │   ├── agent.py    # Main agent REPL loop
│       │   ├── prompts.py  # System prompt builder, CoT stripper
│       │   └── state.py    # AgentState / AgentMode enums
│       ├── tools/
│       │   ├── registry.py # @register_tool decorator & dispatcher
│       │   ├── base.py     # Path sandbox, render_preview
│       │   ├── fs.py       # File read/write/list tools
│       │   ├── cmd.py      # Shell execution & test runner
│       │   ├── patch.py    # AST-aware patch tool
│       │   └── swarm.py    # Sub-agent spawning & messaging
│       └── ui/
│           ├── tui.py      # prompt_toolkit TUI prompt screen
│           └── console.py  # Welcome banner, help listings
├── pyproject.toml          # PEP-517 build metadata
├── setup.py                # Editable install support
├── AGENTS.md               # Live agent rules (auto-injected each turn)
└── README.md
```

## Available Models

The `/model` command lets you switch between any of these NVIDIA NIM-hosted models mid-session:

| # | Name                    | Model ID                                       |
|---|-------------------------|------------------------------------------------|
| 1 | Mistral Small           | `mistralai/mistral-small-4-119b-2603`          |
| 2 | Mistral Large           | `mistralai/mistral-large-3-675b-instruct-2512` |
| 3 | Mixtral 8x7B Instruct   | `mistralai/mixtral-8x7b-instruct-v0.1`         |
| 4 | DeepSeek V4-Pro         | `deepseek-ai/deepseek-v4-pro`                  |
| 5 | DeepSeek V4-Flash       | `deepseek-ai/deepseek-v4-flash`                |
| 6 | Nemotron 3 Nano 30B     | `nvidia/nemotron-3-nano-30b-a3b` *(default)*   |
| 7 | Nemotron 3 Super 120B   | `nvidia/nemotron-3-super-120b-a12b`            |
| 8 | Nemotron 3 Ultra 550B   | `nvidia/nemotron-3-ultra-550b-a55b`            |
