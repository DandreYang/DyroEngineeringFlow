from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time


pid_file = Path(sys.argv[1])
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)"
        ),
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pid_file.write_text(f"{child.pid}\n", encoding="utf-8")
time.sleep(60)
