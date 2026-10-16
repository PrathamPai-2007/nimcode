"""Rust TUI sidecar bridge — the nimcode TUI. Speaks NDJSON to nim-tui."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


BINARY_NAME = "nim-tui.exe" if os.name == "nt" else "nim-tui"

BINARY_MISSING_MSG = (
    f"nim-tui sidecar binary ({BINARY_NAME}) not found. Fix one of:\n"
    "  1) install the nimcode wheel for your platform (it bundles the binary);\n"
    "  2) build it: cd tui-rs && cargo build --release;\n"
    "  3) set NIM_TUI_BIN to the path of an existing nim-tui binary."
)


def find_rust_binary() -> Optional[Path]:
    """Order: NIM_TUI_BIN, bundled nimcode/bin, tui-rs/target/release, PATH."""
    env = os.environ.get("NIM_TUI_BIN")
    if env and Path(env).exists():
        return Path(env)
    here = Path(__file__).resolve()
    cands: List[Path] = [
        # bundled with the platform wheel — works out of the box
        here.parents[1] / "bin" / BINARY_NAME,
        # source checkout: local cargo build
        here.parents[3] / "tui-rs" / "target" / "release" / BINARY_NAME,
    ]
    which = shutil.which("nim-tui")
    if which:
        cands.append(Path(which))
    for cand in cands:
        if cand.exists():
            return cand
    return None


def make_tui(config=None):
    """Create the nimcode TUI (Rust sidecar). Requires the bundled binary."""
    binary = find_rust_binary()
    if binary is None:
        raise RuntimeError(BINARY_MISSING_MSG)
    return RustTUI(config)


class RustTUI:
    """The nimcode TUI, backed by the Rust sidecar."""

    def __init__(self, config=None):
        self.config = config
        self.mode = "Build Mode"
        if config:
            self.mode = {"plan": "Plan Mode", "yolo": "YOLO Mode"}.get(
                config.mode, "Build Mode"
            )
        self._bin = find_rust_binary()
        if not self._bin:
            raise RuntimeError(BINARY_MISSING_MSG)
        self._proc: Optional[subprocess.Popen] = None
        self._sock = None
        self._wfile = None
        self._pending: List[dict] = []
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._tui_task = None
        self._interrupted = False
        self._eof_requested = False
        # Serializes NDJSON writes: add_message is reachable from worker
        # threads (output.echo inside to_thread calls / swarm threads), and
        # interleaved writes would corrupt lines on the wire.
        self._send_lock = threading.RLock()
        # Set by exit(): stops a late start_async from spawning/keeping a
        # sidecar and stops _send from buffering forever.
        self._closed = False
        self._reader_error: Optional[str] = None
        self._sidebar: Dict[str, Any] = {
            "mode": self.mode,
            "model": self._model_name(
                getattr(config, "fast_model", "unknown") if config else "unknown"
            ),
            "tokens_used": 0,
            "tokens_budget": getattr(config, "context_budget_tokens", 128000)
            if config
            else 128000,
            "cwd": str(Path(getattr(config, "workspace_root", ".")).resolve())
            if config
            else "~",
            "is_working": False,
            "working_message": "",
            "todos": [],
            "subagents": [],
            "procs": [],
        }

    def _model_name(self, model_id: str) -> str:
        try:
            from nimcode.config_models import display_name

            return display_name(model_id) or "unknown"
        except Exception:
            return model_id or "unknown"

    # -- lifecycle --
    async def start_async(self) -> None:
        import asyncio
        import socket

        # Loopback socket carries NDJSON; the sidecar inherits our stdio so
        # crossterm renders to the real terminal (pipes would swallow the UI).
        if self._closed:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        srv.settimeout(15)
        env = dict(os.environ, NIM_TUI_PORT=str(port))
        try:
            self._proc = subprocess.Popen([str(self._bin)], env=env)
            conn, _ = await asyncio.to_thread(srv.accept)
        except Exception:
            # Don't orphan a sidecar that never connected, and wake any
            # _next_event waiter instead of letting it poll forever.
            proc, self._proc = self._proc, None
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._events.put({"type": "sidecar_dead"})
            raise RuntimeError(
                "nim-tui sidecar did not connect back; "
                "rebuild with: cargo build --release --manifest-path tui-rs/Cargo.toml"
            )
        finally:
            srv.close()
        if self._closed or self._proc is None:
            # exit() ran while we were waiting for the connection.
            try:
                conn.close()
            except Exception:
                pass
            return
        self._sock = conn
        # Explicit UTF-8: the locale default (cp1252 on Windows) would raise on
        # non-ASCII text and kill the reader thread / drop writes.
        with self._send_lock:
            self._wfile = conn.makefile("w", encoding="utf-8", errors="replace")
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(conn.makefile("r", encoding="utf-8", errors="replace"),),
            daemon=True,
        )
        self._reader.start()
        pending, self._pending = self._pending, []
        for obj in pending:
            self._send(obj)
        self._send({"type": "update", "sidebar": self._sidebar})
        self.push_completions()

        self._tui_task = asyncio.current_task()
        proc = self._proc
        if proc is not None:
            await asyncio.to_thread(proc.wait)

    def _read_loop(self, rfile):
        try:
            for line in rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    # Never print here: stdout is the sidecar's live screen.
                    # debug, not warning: logging's lastResort handler would
                    # write warnings to stderr = the same terminal.
                    _log.debug("nim-tui: malformed event: %s", line[:120])
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "interrupt":
                    self._interrupted = True
                elif ev.get("type") == "eof":
                    # Stringent exit: eof must also cancel in-flight work even
                    # when nobody is awaiting input right now.
                    self._interrupted = True
                    self._eof_requested = True
                self._events.put(ev)
        except Exception as e:
            # Surfaced in the "terminated unexpectedly" error by _next_event.
            self._reader_error = f"{type(e).__name__}: {e}"
            _log.debug("nim-tui reader stopped", exc_info=True)
        finally:
            try:
                rfile.close()
            except Exception:
                pass
            self._events.put({"type": "sidecar_dead"})

    def _send(self, obj: dict) -> None:
        with self._send_lock:
            wfile = self._wfile
            if wfile is not None:
                try:
                    wfile.write(json.dumps(obj) + "\n")
                    wfile.flush()
                    return
                except (BrokenPipeError, ValueError, OSError):
                    try:
                        wfile.close()
                    except Exception:
                        pass
                    self._wfile = None
                    # The pipe is gone for good; buffering would only leak.
                    self._closed = True
            if not self._closed:
                self._pending.append(obj)

    def exit(self, result=None):
        proc, self._proc = self._proc, None
        if proc is None:
            self._closed = True
            return
        try:
            self._send({"type": "quit"})
        except Exception:
            pass
        with self._send_lock:
            self._closed = True
            self._pending = []
            for attr in ("_wfile", "_sock"):
                fh = getattr(self, attr, None)
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._events.put({"type": "sidecar_dead"})

    def _dead_msg(self) -> str:
        err = self._reader_error
        return "nim-tui terminated unexpectedly" + (f" ({err})" if err else "")

    async def _next_event(self) -> dict:
        import asyncio

        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                ev = None
            if ev is not None:
                if ev.get("type") == "sidecar_dead":
                    # Sticky: every later waiter must see the death too.
                    self._events.put(ev)
                    raise RuntimeError(self._dead_msg())
                return ev
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(self._dead_msg())
            await asyncio.sleep(0.05)

    def _drain_stale(self, drop_inputs: bool = False) -> None:
        """Discard events queued before a new prompt was shown.

        A Ctrl+C pressed while the model was working is queued as an
        "interrupt" event; left in the queue it would answer the *next*
        prompt and end the session. For approval prompts (drop_inputs) also
        discard type-ahead text so a stale "y" can't pass the human gate.
        eof / sidecar_dead are kept; mode_cycle is applied.
        """
        keep = []
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t in ("interrupt", "select_result") or (t == "input" and drop_inputs):
                continue
            if t == "mode_cycle":
                if self.config:
                    self.update_mode(ev.get("mode", self.mode))
                continue
            keep.append(ev)
        for ev in keep:
            self._events.put(ev)

    def poll_events(self) -> None:
        """Non-blocking: apply queued mode_cycle / interrupt events now.

        Called between tool calls so Shift+Tab out of YOLO takes effect for
        the rest of the batch. Other events (input, eof, ...) stay queued.
        """
        keep = []
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t == "mode_cycle":
                if self.config:
                    self.update_mode(ev.get("mode", self.mode))
            elif t == "interrupt":
                self._interrupted = True
            else:
                keep.append(ev)
        for ev in keep:
            self._events.put(ev)

    # -- TUI API used by the agent loop --
    def consume_eof(self) -> bool:
        """Return-and-clear the sticky Ctrl+D request (polled by the agent loop)."""
        if self._eof_requested:
            self._eof_requested = False
            return True
        return False

    def show_welcome(self, workspace_root=None):
        self.add_message(
            "system",
            "## nimcode\nNVIDIA NIM Coding Agent\n\n"
            f"Workspace: `{workspace_root}`\n\n"
            "- `Shift+Tab` cycles Plan / Build / YOLO\n"
            "- `Ctrl+C` cancels, `Ctrl+D` exits\n",
        )

    def add_message(self, role: str, content: str, markdown: bool = True):
        if len(content) > 60000:
            content = (
                content[:60000]
                + f"\n\n[... truncated {len(content) - 60000} chars for display ...]"
            )
        self._send({"type": "add_message", "role": role, "content": content})

    def _push_sidebar(self):
        self._send({"type": "update", "sidebar": self._sidebar})

    def update_tokens(self, used: int, budget: Optional[int] = None):
        self._sidebar["tokens_used"] = used
        if budget:
            self._sidebar["tokens_budget"] = budget
        self._push_sidebar()

    def update_todos(self, todos: List[Dict[str, Any]]):
        self._sidebar["todos"] = [
            {"text": str(t.get("text", "")), "done": bool(t.get("done"))} for t in todos
        ]
        self._push_sidebar()

    def update_subagents(self, subs: List[Dict[str, Any]]):
        self._sidebar["subagents"] = [
            {"role": str(s.get("role", "?")), "status": str(s.get("status", ""))}
            for s in subs
        ]
        self._push_sidebar()

    def update_background_procs(self, procs: List[Dict[str, Any]]):
        self._sidebar["procs"] = [
            {"command": str(p.get("command", "")), "pid": str(p.get("pid", ""))}
            for p in procs
        ]
        self._push_sidebar()

    _MODES = {"Plan Mode": "plan", "Build Mode": "build", "YOLO Mode": "yolo"}

    def update_mode(self, mode: str):
        self.mode = mode
        self._sidebar["mode"] = mode
        if self.config:
            self.config.mode = self._MODES.get(mode, self.config.mode)
        self._push_sidebar()

    def update_model(self, model_name: str):
        self._sidebar["model"] = model_name
        self._push_sidebar()

    def update_cwd(self, cwd: str):
        self._sidebar["cwd"] = cwd
        self._push_sidebar()

    def set_working(self, working: bool, message: str = ""):
        # Keep the cached sidebar in sync so a later full "update" push
        # doesn't revert the spinner state.
        self._sidebar["is_working"] = working
        self._sidebar["working_message"] = message
        self._send({"type": "set_working", "working": working, "message": message})

    def clear_messages(self):
        self._send({"type": "clear"})

    def push_completions(self) -> None:
        """Send the slash-command catalog to the sidecar for input autocomplete.

        Base list from the agent loop's SLASH_COMMANDS plus dynamic /<skill>
        entries. Idempotent and tiny; refreshed on every input wait so newly
        installed skills appear without restart.
        """
        try:
            from nimcode.core.agent_tui import SLASH_COMMANDS
        except Exception:
            SLASH_COMMANDS = ["/help", "/exit"]
        items = list(SLASH_COMMANDS)
        try:
            from nimcode import skills

            for s in skills.list_skills():
                entry = f"/{s}"
                if entry not in items:
                    items.append(entry)
        except Exception:
            pass
        self._send({"type": "set_completions", "items": items})

    async def get_user_input(
        self, prompt: str = "", _drop_typeahead: bool = False
    ) -> Optional[str]:
        self._drain_stale(drop_inputs=_drop_typeahead)
        if prompt:
            self.add_message("system", prompt)
        self._interrupted = False
        self.push_completions()
        while True:
            ev = await self._next_event()
            t = ev.get("type")
            if t == "input":
                return ev.get("text")
            elif t in ("interrupt", "eof"):
                return None
            elif t == "mode_cycle" and self.config:
                self.update_mode(ev.get("mode", self.mode))

    async def get_user_confirmation(self, prompt: str) -> str:
        if prompt:
            self.add_message("system", prompt + " (y/n)")
        res = await self.get_user_input(_drop_typeahead=True)
        return (res or "").strip().lower()

    async def _select(self, title: str, items: List[str]) -> Optional[int]:
        self._drain_stale(drop_inputs=True)
        self._send({"type": "prompt_select", "title": title, "items": items})
        while True:
            ev = await self._next_event()
            t = ev.get("type")
            if t == "select_result":
                idx = ev.get("index")
                if isinstance(idx, int) and 0 <= idx < len(items):
                    return idx
                return None
            elif t == "input":
                self.add_message("system", "Finish selection first (Esc to cancel).")
            elif t == "mode_cycle" and self.config:
                self.update_mode(ev.get("mode", self.mode))
            elif t in ("interrupt", "eof"):
                return None

    # Selectors: list-driven; model handoff summarizes via old model, injects into new.
    async def select_session(self, config, workspace_root: Path) -> Optional[Path]:
        import os
        import time
        from nimcode.config import get_project_nimcode_dir
        from nimcode.core.agent import list_sessions

        sessions = list_sessions(get_project_nimcode_dir(workspace_root) / "sessions")
        if not sessions:
            self.add_message("system", "No saved sessions found.")
            return None
        labels = [
            f"{Path(s).name} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(s)))})"
            for s in sessions
        ]
        idx = await self._select("SESSION SELECTOR", labels)
        return Path(sessions[idx]) if idx is not None else None

    async def select_model(
        self, config, client, messages, session_path, workspace_root
    ) -> Optional[str]:
        from nimcode.config import get_project_nimcode_dir, save_last_used_model
        from nimcode.config_models import get_model_catalog, apply_model_context
        from nimcode.core.agent import call_nim_with_retry, log_turn
        from nimcode.core.prompts import strip_reasoning_spans

        models = get_model_catalog()
        if not models:
            self.add_message("system", "No models available.")
            return None
        labels = [
            m["name"]
            + (
                f"  [{m['context_tokens'] // 1000}k ctx]"
                if m.get("context_tokens")
                else ""
            )
            + ("  (current)" if m["id"] == config.fast_model else "")
            for m in models
        ]
        idx = await self._select("MODEL SELECTOR", labels)
        if idx is None:
            self.add_message("system", "Model selection cancelled.")
            return None
        new_id = models[idx]["id"]
        old_model = config.fast_model

        save_last_used_model(config, new_id, workspace_root)
        config.plan_model = new_id
        config.fast_model = new_id
        self.update_model(models[idx]["name"])
        try:
            changed, ctx_msg = apply_model_context(config, new_id, force=True)
            if changed:
                self.add_message("system", f"Note: {ctx_msg}")
        except Exception:
            pass

        # Context handoff mirrors tui.py: summarize via old model, inject into new.
        try:
            if (
                client is not None
                and len([m for m in messages if m.get("role") == "user"]) >= 1
            ):
                summary_file = get_project_nimcode_dir(workspace_root) / "summary.md"
                self.set_working(True, "Generating summary.md for model handoff...")
                try:
                    summary_res = await asyncio.to_thread(
                        call_nim_with_retry,
                        client,
                        list(messages)
                        + [
                            {
                                "role": "user",
                                "content": "Summarize our conversation, workspace state, and key decisions so far as markdown for a summary.md handoff to a new model.",
                            }
                        ],
                        None,
                        old_model,
                        config,
                        timeout=300.0,
                    )
                finally:
                    self.set_working(False)
                summary_text = (
                    strip_reasoning_spans(
                        summary_res.choices[0].message.content or ""
                    ).strip()
                    or "# Summary\nPrevious session context handoff."
                )
                summary_file.parent.mkdir(parents=True, exist_ok=True)
                summary_file.write_text(summary_text, encoding="utf-8")
                handoff = {
                    "role": "user",
                    "content": f"<model_handoff_context>\nModel switched from {old_model} to {new_id}. Summary:\n\n{summary_text}\n</model_handoff_context>\n\nAcknowledge briefly you are ready to continue.",
                }
                messages.append(handoff)
                if session_path:
                    log_turn(handoff, session_path)
                try:
                    self.set_working(True, "Injecting context into new model...")
                    ack_res = await asyncio.to_thread(
                        call_nim_with_retry, client, messages, None, new_id, config
                    )
                finally:
                    self.set_working(False)
                ack_text = strip_reasoning_spans(
                    ack_res.choices[0].message.content or ""
                ).strip()
                if ack_text:
                    self.add_message("system", f"\n{models[idx]['name']}: {ack_text}\n")
                    ack_msg = {"role": "assistant", "content": ack_text}
                    messages.append(ack_msg)
                    if session_path:
                        log_turn(ack_msg, session_path)
        except (Exception, KeyboardInterrupt) as e:
            # Ctrl+C during the summary call raises KeyboardInterrupt; it must
            # not escape /model and tear down the agent loop.
            self.add_message("system", f"Model switched, but handoff failed: {e}")
        self.add_message(
            "system", f"Model switched to {models[idx]['name']} ({new_id})."
        )
        return new_id

    async def restore_backup(self, workspace_root: Path) -> None:
        import shutil
        from nimcode.tools.base import list_backups

        backups = list_backups(workspace_root)
        if not backups:
            self.add_message("system", "No backups found.")
            return
        labels = [
            f"[{b['when']}] {b['original'] or b['hint'] + ' (legacy)'}" for b in backups
        ]
        idx = await self._select("SELECT BACKUP TO RESTORE", labels)
        if idx is None:
            return
        chosen = backups[idx]
        rel = chosen["original"]
        if not rel:
            # Legacy backup (no manifest entry): the flattened name can't be
            # mapped back reliably, so ask with a best-effort default.
            guess_path = chosen["hint"].replace("_", "/")
            target = await self.get_user_input(
                f"Restore to (relative path) [default: {guess_path}]:"
            )
            if target is None:
                return
            rel = target.strip() or guess_path
        dest = (workspace_root / rel).resolve()
        try:
            if not dest.is_relative_to(workspace_root.resolve()):
                self.add_message("system", "Error: target outside workspace.")
                return
        except Exception:
            self.add_message("system", "Error: target outside workspace.")
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(chosen["path"], dest)
        except OSError as e:
            self.add_message("system", f"Error: restore failed: {e}")
            return
        self.add_message("system", f"Restored backup to {rel}")

    async def configure(self, config) -> None:
        from nimcode.config import save_config

        items = [
            f"output_cap = {config.output_cap}",
            f"timeout = {config.timeout}",
            f"max_loop_iterations = {config.max_loop_iterations}",
            f"context_budget_tokens = {config.context_budget_tokens}",
        ]
        idx = await self._select("CONFIG (pick key to edit)", items + ["Cancel"])
        if idx is None or idx >= len(items):
            return
        key = items[idx].split(" = ")[0]
        val = await self.get_user_input(f"New value for {key}:")
        if val is None:
            return
        try:
            num = int(val.strip())
        except ValueError:
            self.add_message("system", "Invalid integer.")
            return
        if num <= 0:
            self.add_message("system", "Value must be a positive integer.")
            return
        setattr(config, key, num)
        save_config(config)
        self.add_message("system", f"{key} updated.")
