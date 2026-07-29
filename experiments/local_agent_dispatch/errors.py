"""Errors for the local agent dispatch experiment."""

from __future__ import annotations


class DispatchValidationError(ValueError):
    """Fail-closed validation error before or after a local dispatch run."""
