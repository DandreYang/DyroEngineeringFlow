"""Source-tree public process: ``python -m dyro.bridge``.

This is not a packaged console script. The default wheel still omits
``dyro.bridge``, so an installed ``dyro`` distribution must not grow
``dyro-bridge``.
"""

from __future__ import annotations

from pathlib import Path
import sys

from .transport import serve_once


def main() -> int:
    try:
        return serve_once(
            sys.stdin.buffer,
            sys.stdout.buffer,
            cwd=Path.cwd(),
            exposure="public",
        )
    except BrokenPipeError:
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
