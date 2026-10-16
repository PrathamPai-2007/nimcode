"""Stand-in for nim-tui used by test_bridge_protocol.py.

Connects to 127.0.0.1:$NIM_TUI_PORT like the real sidecar, logs every message
it receives to $FAKE_SIDECAR_LOG (one JSON object per line), and answers:
  on connect      -> the "input" sample from protocol_samples.json
  prompt_select   -> select_result with the last index
  add_message     -> input echoing the content back (non-ASCII round trip)
  quit            -> exit
Emits with ensure_ascii, like the Rust ascii_json().
"""

import json
import os
import socket
from pathlib import Path

SAMPLES = json.loads(
    (Path(__file__).with_name("protocol_samples.json")).read_text(encoding="utf-8")
)


def main() -> None:
    sock = socket.create_connection(("127.0.0.1", int(os.environ["NIM_TUI_PORT"])))
    rfile = sock.makefile("r", encoding="utf-8")
    wfile = sock.makefile("w", encoding="utf-8")
    log = open(os.environ["FAKE_SIDECAR_LOG"], "a", encoding="utf-8")

    def emit(obj: dict) -> None:
        wfile.write(json.dumps(obj, ensure_ascii=True) + "\n")
        wfile.flush()

    emit(next(s for s in SAMPLES["rust_to_py"] if s["type"] == "input"))
    for line in rfile:
        msg = json.loads(line)
        log.write(json.dumps(msg) + "\n")
        log.flush()
        t = msg.get("type")
        if t == "prompt_select":
            emit({"type": "select_result", "index": len(msg["items"]) - 1})
        elif t == "add_message":
            emit({"type": "input", "text": msg["content"]})
        elif t == "quit":
            break


if __name__ == "__main__":
    main()
