"""Process boundary for exercising implemented-testable Bridge services.

This runner is test evidence, not a production availability override.  It uses
the same reviewed per-platform catalog as ``dyro-bridge`` while injecting only
the fixture root and explicit process boundary needed by the audit.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys

from dyro.bridge.transport import TransportContext, run


def main() -> int:
    root = os.environ.get("DYRO_BRIDGE_FIXTURE_ROOT", "")
    if not root or "\x00" in root:
        return 64
    context = TransportContext(
        cwd=Path(root).resolve(),
        allow_test_services=False,
        event_id_factory=lambda: f"evt_{secrets.token_hex(12)}",
    )
    return run(sys.stdin.buffer, sys.stdout.buffer, context)


if __name__ == "__main__":
    raise SystemExit(main())
