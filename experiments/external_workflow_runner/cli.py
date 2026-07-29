"""CLI surface for the external semantic runtime experiment (optional).

Shipped with the ``dyro`` wheel as ``dyro runtime …``. Production remains
NOT_READY (Stage5); this CLI does not run merge/push/signoff or Core import.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def cmd_status(_args: argparse.Namespace) -> int:
    from .stage5.production_gate import evaluate_production_readiness

    report = evaluate_production_readiness()
    _print_json(
        {
            "schema_version": 1,
            "kind": "external-semantic-runtime-status",
            "module": "experiments.external_workflow_runner",
            "shipped_with_dyro_wheel": True,
            "production": report,
            "entry_points": {
                "cli": "dyro runtime status|production-gate|help",
                "import": "experiments.external_workflow_runner",
            },
            "notes": [
                "Local PoC Stage0–5 may run under experiment supervisors.",
                "Production deployment is NOT_READY until PROD blockers clear.",
                "Never merge/push/signoff from this surface.",
            ],
        }
    )
    return 0


def cmd_production_gate(_args: argparse.Namespace) -> int:
    from .stage5.production_gate import (
        assert_not_production_ready,
        evaluate_production_readiness,
    )

    report = evaluate_production_readiness()
    assert_not_production_ready(report)
    _print_json(report)
    # Exit 0 with NOT_READY is intentional: the gate is correctly closed.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyro runtime",
        description=(
            "Optional external semantic runtime (Stage0–5 experiment). "
            "Production remains NOT_READY."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show module status and production gate summary")
    p_status.set_defaults(func=cmd_status)

    p_gate = sub.add_parser(
        "production-gate",
        help="Print Stage5 production checklist and assert NOT_READY",
    )
    p_gate.set_defaults(func=cmd_production_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AssertionError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
