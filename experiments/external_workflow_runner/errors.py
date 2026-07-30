"""Errors raised by the removable external workflow runner experiment."""

from __future__ import annotations

from collections.abc import Sequence


class Stage0ValidationError(ValueError):
    """Fail-closed validation error for an untrusted Stage 0 input."""


def report_cleanup_failures(
    stage: str,
    errors: Sequence[str],
    *,
    primary_error: BaseException | None,
) -> None:
    """Raise cleanup-only failures without replacing an active primary error."""
    if not errors:
        return
    message = (
        f"{stage} cleanup/invariant verification failed: "
        + "; ".join(errors)
    )
    if primary_error is not None:
        primary_error.add_note(message)
        return
    raise Stage0ValidationError(message)
