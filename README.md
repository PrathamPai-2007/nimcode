# Nimcode

**Nimcode** is an open-source, terminal-first AI coding agent driven by NVIDIA NIM, utilizing the powerful Nemotron-3 models. It wraps a continuous Read-Eval-Print Loop (REPL) around a dual-model LLM architecture, giving the AI programmatic "hands" (sandboxed tools) to inspect and alter your workspace.

Crucially, **nimcode prioritizes human control.** Every irreversible action—from modifying files to running shell commands—passes through a human approval gate before it touches your disk or the shell.

## Key Features

*   **Beautiful Terminal Interface (TUI):** A gorgeous, responsive terminal interface powered by `prompt_toolkit` and `rich`. Enjoy inline interactive model selection, mode-colored UI frames, seamless autocomplete for commands, and an unobtrusive status bar.
*   **Three Operational Modes (Plan, Build & YOLO):**
    *   **Plan Mode (`/plan`):** Enforces a strict "Plan-Before-Execution" protocol. The agent must present a structured architectural `<plan>` for approval before executing any tools.
    *   **Build Mode (`/build`):** Human-in-the-loop execution. Every tool call displays a unified diff or command preview before prompting for `y/n` approval.
    *   **YOLO Mode (`/yolo` / `--yolo`):** High-speed autonomous execution. Displays full terminal previews for every action but automatically accepts tool calls without pausing for human input.
*   **Instant Mode & Context Switching:** Press `Shift+Tab` to cycle between Plan, Build, and YOLO modes directly from the prompt. Type `/model` to instantly swap NVIDIA NIM models mid-conversation—Nimcode automatically handles the context handoff via `.nimcode/summary.md`.
*   **Consolidated Workspace Architecture:** All system garbage, session histories, backups, and handoff summaries are stored securely inside a hidden `.nimcode/` directory, keeping your workspace root pristine.
*   **Live Agent Rules:** Use the `/init` command to automatically construct a robust `AGENTS.md` file. Nimcode injects these rules live on every turn, meaning updates to the rules apply immediately.
*   **Auto-Backups & Session Recovery:** Overwritten files are automatically snapshotted to `.nimcode/backups/`. Use `/resume` or `nimcode resume` to instantly reload and continue past conversations.

## Prerequisites

*   Python 3.9+
*   An [NVIDIA NIM API Key](https://build.nvidia.com/) (set as `NVIDIA_API_KEY`).

## Installation

1.  Clone the repository and navigate into the directory:
    ```bash
    git clone https://github.com/yourusername/nimcode.git
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

`nimcode` generates or reads from a `nimcode.toml` file in your current workspace root. You can adjust:
*   `fast_model` / `plan_model`: Which models to route to via the NVIDIA NIM endpoint.
*   `workspace_root`: The sandbox boundary for the agent. Defaults to the directory you run the command from.
*   `timeout`: Hard timeout (in seconds) for subprocess execution.
*   `max_loop_iterations`: Safety cap to prevent infinite AI execution loops.
*   `auto_backup`: Set to `true` to save file snapshots before overwriting.

## Usage

Because `nimcode` is installed globally, you can run it from **any directory** on your machine.

Check your environment and configuration:
```bash
nimcode status
```

Start an interactive REPL session:
```bash
nimcode start
```

Run nimcode with a direct task instruction:
```bash
nimcode run "Create a python script that fetches the current weather."
```

Run nimcode directly in YOLO Mode (auto-approves edits and commands):
```bash
nimcode run --yolo "Create a python script that fetches the current weather and run it."
```

Resume an interrupted session:
```bash
nimcode resume
```
