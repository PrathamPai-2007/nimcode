"""Headless stand-in for RustTUI: runs the real agent loop with no terminal UI.

Used by `nimcode run --headless` (benchmarks, CI, scripts). Messages stream to
stdout as plain text, every approval is auto-accepted, and the session ends the
first time the agent hands control back to the user (asks for input).
"""

import json
import sys
import threading
from typing import Any, Dict, List, Optional, TextIO


class UsageMeter:
    """Wraps client.chat.completions.create to total the API's reported usage."""

    def __init__(self, client: Any):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._lock = threading.Lock()  # subagent threads share the client
        completions = client.chat.completions
        original = completions.create

        def create(*args, **kwargs):
            res = original(*args, **kwargs)
            usage = getattr(res, "usage", None)
            with self._lock:
                self.calls += 1
                self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.completion_tokens += int(
                    getattr(usage, "completion_tokens", 0) or 0
                )
            return res

        completions.create = create

    def as_dict(self) -> Dict[str, int]:
        return {
            "api_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class HeadlessTUI:
    """Implements the slice of the RustTUI API that the agent loop uses."""

    LOOP_PROMPT = "Resume loop?"

    def __init__(self, config: Any = None, out: TextIO = None, max_resumes: int = 2):
        self.config = config
        self.out = out or sys.stdout
        self.mode = {"plan": "Plan Mode", "yolo": "YOLO Mode"}.get(
            getattr(config, "mode", "build"), "Build Mode"
        )
        self._interrupted = False
        self._tui_task = None
        # Each loop-limit check-in grants another batch of steps (50 in YOLO).
        self.max_resumes = max_resumes
        self.resumes_used = 0
        self.ended_by: Optional[str] = None
        self.messages: List[Dict[str, str]] = []

    # -- lifecycle --
    async def start_async(self) -> None:
        return None

    def exit(self, result=None):
        return None

    def consume_eof(self) -> bool:
        return False

    def poll_events(self) -> None:
        return None

    # -- output --
    def _print(self, role: str, content: str) -> None:
        try:
            self.out.write(f"[{role}] {content}\n")
            self.out.flush()
        except (UnicodeEncodeError, ValueError):
            pass

    def add_message(self, role: str, content: str, markdown: bool = True):
        self.messages.append({"role": role, "content": content})
        self._print(role, content)

    def show_welcome(self, workspace_root=None):
        self._print("system", f"Working in {workspace_root}")

    def clear_messages(self):
        return None

    # sidebar/status updates have no headless meaning
    def update_tokens(self, used: int, budget: Optional[int] = None):
        return None

    def update_subagents(self, subs):
        return None

    def update_background_procs(self, procs):
        return None

    def update_model(self, model_name: str):
        return None

    def update_mode(self, mode: str):
        self.mode = mode

    def set_working(self, working: bool, message: str = ""):
        return None

    def push_completions(self):
        return None

    # -- input --
    async def get_user_input(self, prompt: str = "", _drop_typeahead: bool = False):
        # The agent handed control back: the task is over.
        if prompt:
            self._print("system", prompt)
        self.ended_by = self.ended_by or "agent_finished"
        return None

    async def get_user_confirmation(self, prompt: str) -> str:
        if prompt:
            self._print("system", prompt)
        if self.LOOP_PROMPT in (prompt or ""):
            if self.resumes_used < self.max_resumes:
                self.resumes_used += 1
                return "y"
            self.ended_by = "step_limit"
            return "n"
        return "y"

    async def select_session(self, *a, **k):
        return None

    async def select_model(self, *a, **k):
        return None

    async def restore_backup(self, *a, **k):
        return None

    async def configure(self, *a, **k):
        return None


def write_summary(path: str, summary: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
