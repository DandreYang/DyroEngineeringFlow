"""Local Console read-side and loopback presentation boundary.

The package deliberately exposes no browser launcher, subprocess execution,
workspace mutation, or Core scheduling authority.
"""

from .overview import ConsoleOverviewService
from .read_model import workspace_envelope
from .server import create_console_http_server

__all__ = ["ConsoleOverviewService", "create_console_http_server", "workspace_envelope"]
