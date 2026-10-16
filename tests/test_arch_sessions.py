"""JSONL session storage (plan item 3)."""

import json
import os

from nimcode.core.agent import (
    list_sessions,
    load_session,
    log_turn,
    migrate_legacy_session,
    open_session_path,
)


def test_log_turn_appends_jsonl_and_load_skips_corrupt_tail(tmp_path):
    p = tmp_path / "session_1.jsonl"
    log_turn({"role": "user", "content": "hi"}, p)
    log_turn({"role": "assistant", "content": "yo", "x": object()}, p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"role": "user", "cont')  # crash mid-append
    msgs = load_session(p)
    assert [m["content"] for m in msgs] == ["hi", "yo"]


def test_load_legacy_json_list(tmp_path):
    p = tmp_path / "session_old.json"
    p.write_text(json.dumps([{"role": "user", "content": "a"}]), encoding="utf-8")
    assert load_session(p) == [{"role": "user", "content": "a"}]


def test_list_sessions_newest_first_both_formats(tmp_path):
    a = tmp_path / "session_a.json"
    b = tmp_path / "session_b.jsonl"
    a.write_text("[]", encoding="utf-8")
    b.write_text("", encoding="utf-8")
    (tmp_path / "other.json").write_text("[]", encoding="utf-8")
    os.utime(a, (1, 1))
    assert list_sessions(tmp_path) == [b, a]
    assert list_sessions(tmp_path / "missing") == []


def test_resuming_json_session_copies_into_new_jsonl(tmp_path):
    old = tmp_path / "session_old.json"
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    old.write_text(json.dumps(msgs), encoding="utf-8")
    path = open_session_path(tmp_path, old.name, load_session(old))
    assert path.suffix == ".jsonl"
    assert load_session(path) == msgs
    assert json.loads(old.read_text(encoding="utf-8")) == msgs  # untouched
    # A .jsonl session continues in place.
    assert open_session_path(tmp_path, path.name, msgs) == path
    assert len(load_session(path)) == 2


def test_migrate_legacy_session_is_listed(tmp_path):
    ws, sdir = tmp_path / "ws", tmp_path / "sessions"
    ws.mkdir()
    (ws / ".nimcode_session.json").write_text("[]", encoding="utf-8")
    dest = migrate_legacy_session(ws, sdir)
    assert dest in list_sessions(sdir)
    assert migrate_legacy_session(ws, sdir) is None
