"""`nimcode` / `nimcode run` must reach the TUI without touching removed typer APIs."""

import pytest
from typer.testing import CliRunner

from nimcode import cli
from nimcode.config import Config


class _Reached(Exception):
    pass


@pytest.mark.parametrize("argv", [[], ["start"], ["run", "hello"]])
def test_start_paths_reach_tui(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(
        cli,
        "_get_client_and_setup",
        lambda *a, **k: (object(), Config(), tmp_path, ""),
    )

    def fake_make_tui(*a, **k):
        raise _Reached()

    monkeypatch.setattr(cli, "_make_tui", fake_make_tui)
    result = CliRunner().invoke(cli.app, argv)
    assert isinstance(result.exception, _Reached), result.output
