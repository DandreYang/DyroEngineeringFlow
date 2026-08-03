"""Local Console read-side and loopback presentation boundary.

The package deliberately exposes no browser launcher, arbitrary subprocess
execution, workspace mutation, or Core scheduling authority.  Its only child
process boundary is the fixed, read-only inspection worker.
"""

from .inspection import IsolatedOverviewService
from .launcher import launch_console
from .overview import ConsoleOverviewService
from .read_model import workspace_envelope
from .server import create_console_http_server

__all__ = [
    "ConsoleOverviewService",
    "IsolatedOverviewService",
    "create_console_http_server",
    "launch_console",
    "workspace_envelope",
]
