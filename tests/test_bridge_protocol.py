"""Bridge protocol drift test: real RustTUI against tests/fake_sidecar.py.

Messages the Python side sends must have the same keys as the samples in
protocol_samples.json, which tui-rs's cargo test deserializes into PyMsg.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from nimcode.ui import rust_bridge

HERE = Path(__file__).parent
SAMPLES = json.loads((HERE / "protocol_samples.json").read_text(encoding="utf-8"))


def test_ndjson_round_trip_against_fake_sidecar(tmp_path, monkeypatch):
    log = tmp_path / "received.ndjson"
    monkeypatch.setenv("NIM_TUI_BIN", sys.executable)
    monkeypatch.setenv("FAKE_SIDECAR_LOG", str(log))
    real_popen = subprocess.Popen

    def popen(argv, *a, **kw):
        return real_popen([sys.executable, str(HERE / "fake_sidecar.py")], *a, **kw)

    monkeypatch.setattr(rust_bridge.subprocess, "Popen", popen)

    async def run():
        tui = rust_bridge.RustTUI(None)
        task = asyncio.create_task(tui.start_async())
        first = await asyncio.wait_for(tui.get_user_input(), 15)
        idx = await asyncio.wait_for(tui._select("pick", ["a", "b", "ç"]), 15)
        tui.add_message("assistant", "naïve 😀 日本")
        echoed = await asyncio.wait_for(tui.get_user_input(), 15)
        tui.exit()
        await asyncio.wait_for(task, 15)
        return first, idx, echoed

    first, idx, echoed = asyncio.run(run())
    sample_input = next(s for s in SAMPLES["rust_to_py"] if s["type"] == "input")
    assert first == sample_input["text"]
    assert idx == 2
    assert echoed == "naïve 😀 日本"

    received = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    by_type = {s["type"]: s for s in SAMPLES["py_to_rust"]}
    for msg in received:
        sample = by_type.get(msg["type"])
        assert sample is not None, f"no sample for sent message {msg}"
        assert set(msg) == set(sample), msg
        if msg["type"] == "update":
            assert set(msg["sidebar"]) == set(sample["sidebar"]), msg
    types = {m["type"] for m in received}
    assert {
        "update",
        "set_completions",
        "prompt_select",
        "add_message",
        "quit",
    } <= types
