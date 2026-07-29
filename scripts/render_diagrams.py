#!/usr/bin/env python3
"""Optional local export: Mermaid sources → PNG via mermaid-cli.

Docs on GitHub use embedded Mermaid (English and Chinese sources).
PNGs are gitignored and not required.

  python3 scripts/render_diagrams.py            # English labels → docs/images/diagrams/*.png
  python3 scripts/render_diagrams.py --lang zh  # Chinese labels → docs/images/diagrams/zh/*.png
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs" / "images" / "diagrams"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lang",
        choices=("en", "zh"),
        default="en",
        help="en: src/*.mmd; zh: src/zh/*.mmd",
    )
    args = parser.parse_args()

    if args.lang == "zh":
        src = BASE / "src" / "zh"
        out = BASE / "zh"
    else:
        src = BASE / "src"
        out = BASE

    if not src.is_dir():
        print(f"missing {src}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.glob("*.mmd") if p.is_file())
    if not files:
        print(f"no .mmd sources in {src}", file=sys.stderr)
        return 1
    for mmd in files:
        png = out / f"{mmd.stem}.png"
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
        print("render", mmd.relative_to(ROOT), "->", png.relative_to(ROOT), "(local only, gitignored)")
        proc = subprocess.run(cmd, cwd=ROOT)
        if proc.returncode != 0:
            print(f"failed: {mmd}", file=sys.stderr)
            return proc.returncode
    print(f"ok: {len(files)} diagrams lang={args.lang} (not tracked by git)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
