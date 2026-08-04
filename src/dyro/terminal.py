"""Small, dependency-free terminal presentation helpers.

The CLI is often consumed by scripts, redirected into evidence files, and run
in terminals with widely different capabilities.  Keep color opt-in by
terminal capability, while allowing users to force or disable it with the
common ``NO_COLOR`` convention or ``DYRO_COLOR=always|never|auto``.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


_RESET = "\033[0m"
_STYLES = {
    "title": "1;35",
    "value": "1;36",
    "success": "1;32",
    "warning": "1;33",
    "danger": "1;31",
    "muted": "2;37",
}


def color_enabled(stream: TextIO | None = None) -> bool:
    """Return whether ANSI color is appropriate for this output stream."""
    preference = os.environ.get("DYRO_COLOR", "auto").strip().lower()
    if preference in {"never", "no", "false", "0"}:
        return False
    if "NO_COLOR" in os.environ:
        return False
    if preference in {"always", "yes", "true", "1"}:
        return True
    target = stream or sys.stdout
    return (
        bool(getattr(target, "isatty", lambda: False)())
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def style(text: object, role: str, *, stream: TextIO | None = None) -> str:
    """Apply one semantic presentation role when color is enabled."""
    value = str(text)
    if not color_enabled(stream) or role not in _STYLES:
        return value
    return f"\033[{_STYLES[role]}m{value}{_RESET}"


def title(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "title", stream=stream)


def value(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "value", stream=stream)


def success(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "success", stream=stream)


def warning(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "warning", stream=stream)


def danger(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "danger", stream=stream)


def muted(text: object, *, stream: TextIO | None = None) -> str:
    return style(text, "muted", stream=stream)
