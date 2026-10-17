"""Harbor installed-agent wrapper for nimcode (Terminal-Bench 2.0, Aider Polyglot, ...).

Usage (from the repo root, after `python -m build --wheel --outdir bench/dist`):

    set PYTHONPATH=.            (bash: export PYTHONPATH=.)
    set NVIDIA_API_KEY=nvapi-...
    harbor run -d terminal-bench@2.0 -a bench.harbor_nimcode:NimcodeAgent \
        -m deepseek-ai/deepseek-v4-flash -l 10

The model id is passed to nimcode verbatim (a NIM model id). nimcode runs
headless in Build mode inside the task container; with no Docker daemon there,
commands run directly in the container, which is the sandbox.
"""

import json
import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

DIST = Path(__file__).parent / "dist"
WHEEL_DIR = "/tmp/nimcode-wheel"
NIMCODE = "$HOME/.local/bin/nimcode"


def _local_wheel() -> Path:
    wheels = sorted(DIST.glob("nimcode-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise FileNotFoundError(
            f"No nimcode wheel in {DIST}. Build one: python -m build --wheel --outdir bench/dist"
        )
    return wheels[-1]


class NimcodeAgent(BaseInstalledAgent):
    """Installs the local nimcode wheel with uv and runs `nimcode run --headless`."""

    # Steps before nimcode stops on its own (Harbor also enforces the task timeout).
    MAX_STEPS = 150

    @staticmethod
    def name() -> str:
        return "nimcode"

    def get_version_command(self) -> str | None:
        return f"{NIMCODE} --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "git", "ripgrep"))
        wheel = _local_wheel()
        await self.exec_as_root(
            environment, f"mkdir -p {WHEEL_DIR} && chmod 777 {WHEEL_DIR}"
        )
        await environment.upload_file(wheel, f"{WHEEL_DIR}/{wheel.name}")
        # uv brings its own Python, so task images without python3 still work.
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null && "
                '. "$HOME/.local/bin/env" && '
                f"uv tool install --python 3.12 {WHEEL_DIR}/{wheel.name} && "
                f"{NIMCODE} --version"
            ),
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError(
                "Pass a NIM model id with -m, e.g. deepseek-ai/deepseek-v4-flash"
            )
        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise ValueError(
                "NVIDIA_API_KEY must be set in the environment running harbor"
            )
        env = {
            "NVIDIA_API_KEY": api_key,
            "NIMCODE_NO_MODEL_REFRESH": "1",
            "NIMCODE_PLAIN": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if os.environ.get("NIMCODE_API_BASE_URL"):
            env["NIMCODE_API_BASE_URL"] = os.environ["NIMCODE_API_BASE_URL"]
        logs = self.environment_logs_dir.as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"{NIMCODE} run --headless --mode build "
                f"--model {shlex.quote(self.model_name)} "
                f"--max-steps {self.MAX_STEPS} "
                f"--json-out {logs}/nimcode.json "
                f"{shlex.quote(instruction)} "
                f"2>&1 | tee {logs}/nimcode.txt"
            ),
            env=env,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        summary_path = self.logs_dir / "nimcode.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.logger.debug(f"nimcode summary unavailable: {e}")
            return
        context.n_input_tokens = summary.get("prompt_tokens") or 0
        context.n_output_tokens = summary.get("completion_tokens") or 0
