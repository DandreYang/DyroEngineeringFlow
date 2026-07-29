#!/usr/bin/env python3
"""Render docs/images/diagrams/src/*.mmd to PNG via mermaid-cli (npx)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "images" / "diagrams" / "src"
OUT = ROOT / "docs" / "images" / "diagrams"


def main() -> int:
    if not SRC.is_dir():
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(SRC.glob("*.mmd"))
    if not files:
        print("no .mmd sources", file=sys.stderr)
        return 1
    for mmd in files:
        png = OUT / f"{mmd.stem}.png"
        cmd = [
            "npx",
            "--yes",
            "@mermaid-js/mermaid-cli@11.4.2",
            "-i",
            str(mmd),
            "-o",
            str(png),
            "-b",
            "white",
            "-s",
            "2",
            "-w",
            "1600",
        ]
        print("render", mmd.name, "->", png.name)
        proc = subprocess.run(cmd, cwd=ROOT)
        if proc.returncode != 0:
            print(f"failed: {mmd}", file=sys.stderr)
            return proc.returncode
    print(f"ok: {len(files)} diagrams")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
