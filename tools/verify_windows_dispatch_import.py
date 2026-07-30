"""Windows smoke check for local-dispatch import and fail-closed execution."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from experiments.local_agent_dispatch.context_guard import read_guarded_file
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.supervisor import DispatchSupervisor


def main() -> None:
    if os.name != "nt":
        raise SystemExit("this smoke check must run on Windows")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        source = workspace / "src" / "app.py"
        source.parent.mkdir(parents=True)
        source.write_text("safe = True\n", encoding="utf-8")
        relative, content = read_guarded_file(source, workspace)
        if relative != "src/app.py" or content != "safe = True\n":
            raise SystemExit("Windows context fallback returned unexpected content")
        try:
            DispatchSupervisor(home=Path(tmp) / "state")
        except DispatchValidationError as exc:
            if "requires a POSIX host" not in str(exc):
                raise
        else:
            raise SystemExit("Windows dispatch execution did not fail closed")
    print("Windows context fallback and fail-closed supervision verified")


if __name__ == "__main__":
    main()
