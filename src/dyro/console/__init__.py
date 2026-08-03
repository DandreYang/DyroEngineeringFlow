"""Local Console presentation boundary.

The package starts with read-model composition only.  It deliberately exposes
no listener, session, browser, subprocess, or mutation capability.
"""

from .read_model import workspace_envelope

__all__ = ["workspace_envelope"]
