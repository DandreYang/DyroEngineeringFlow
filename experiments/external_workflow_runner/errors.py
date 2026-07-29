"""Errors raised by the removable external workflow runner experiment."""


class Stage0ValidationError(ValueError):
    """Fail-closed validation error for an untrusted Stage 0 input."""
