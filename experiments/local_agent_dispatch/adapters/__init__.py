"""Backend adapters for local agent dispatch."""

from .base import AdapterResult, BackendAdapter
from .registry import get_adapter, list_adapters, probe_backends

__all__ = [
    "AdapterResult",
    "BackendAdapter",
    "get_adapter",
    "list_adapters",
    "probe_backends",
]
