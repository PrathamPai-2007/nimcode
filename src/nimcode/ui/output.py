import os
import sys
from functools import lru_cache
from typing import Any, Optional

import typer
from rich.console import Console


ASCII_SYMBOLS = {
    "bullet": "-",
    "prompt": ">",
    "hline": "-",
    "arrow": "->",
    "tool": "[tool]",
    "warn": "!",
}

UNICODE_SYMBOLS = {
    "bullet": "•",
    "prompt": "❯",
    "hline": "─",
    "arrow": "↳",
    "tool": "🔧",
    "warn": "⚠",
}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def configure_terminal() -> None:
    """Best-effort terminal setup that never becomes a startup dependency."""
    try:
        if sys.platform == "win32":
            try:
                from colorama import just_fix_windows_console

                just_fix_windows_console()
            except Exception:
                pass

        if _truthy_env("NIMCODE_FORCE_UTF8"):
            for stream_name in ("stdout", "stderr"):
                stream = getattr(sys, stream_name, None)
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _stream_encoding() -> str:
    stream = getattr(sys, "stdout", None)
    return (getattr(stream, "encoding", None) or "").lower()


def unicode_enabled() -> bool:
    if _truthy_env("NIMCODE_ASCII") or _truthy_env("NIMCODE_PLAIN"):
        return False
    encoding = _stream_encoding()
    return "utf" in encoding or "65001" in encoding


def color_enabled() -> bool:
    if _truthy_env("NIMCODE_NO_COLOR") or _truthy_env("NIMCODE_PLAIN"):
        return False
    if _truthy_env("NO_COLOR"):
        return False
    return True


def symbol(name: str) -> str:
    table = UNICODE_SYMBOLS if unicode_enabled() else ASCII_SYMBOLS
    return table.get(name, name)


def safe_text(value: Any) -> str:
    text = str(value)
    if unicode_enabled():
        return text
    replacements = {
        "•": "-",
        "❯": ">",
        "─": "-",
        "↳": "->",
        "🔧": "[tool]",
        "⚠️": "!",
        "⚠": "!",
    }
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    return text


@lru_cache(maxsize=1)
def get_console() -> Console:
    configure_terminal()
    return Console(
        color_system="auto" if color_enabled() else None,
        force_terminal=None,
        legacy_windows=False,
        safe_box=not unicode_enabled(),
        soft_wrap=True,
        highlight=False,
    )


console = get_console()


def print_text(
    message: Any = "", *, style: Optional[str] = None, **kwargs: Any
) -> None:
    get_console().print(safe_text(message), style=style, **kwargs)


def echo(message: Any = "", **kwargs: Any) -> None:
    typer.echo(safe_text(message), **kwargs)


def secho(
    message: Any = "",
    *,
    fg: Optional[str] = None,
    bg: Optional[str] = None,
    bold: Optional[bool] = None,
    **kwargs: Any,
) -> None:
    if color_enabled():
        typer.secho(safe_text(message), fg=fg, bg=bg, bold=bold, **kwargs)
    else:
        typer.echo(safe_text(message), **kwargs)


def terminal_diagnostics() -> dict[str, Any]:
    return {
        "platform": sys.platform,
        "stdout_encoding": getattr(sys.stdout, "encoding", None),
        "stderr_encoding": getattr(sys.stderr, "encoding", None),
        "stdout_isatty": bool(getattr(sys.stdout, "isatty", lambda: False)()),
        "unicode_enabled": unicode_enabled(),
        "color_enabled": color_enabled(),
        "term": os.environ.get("TERM"),
        "term_program": os.environ.get("TERM_PROGRAM"),
        "windows_terminal": bool(os.environ.get("WT_SESSION")),
    }


configure_terminal()
