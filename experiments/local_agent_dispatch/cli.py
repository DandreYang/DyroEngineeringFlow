"""CLI for local agent dispatch (also exposed as ``dyro dispatch``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapters.registry import probe_backends
from .errors import DispatchValidationError
from .gc import gc
from .panel import run_panel
from .paths import dispatch_home
from .skill_render import render_skill_markdown, save_route, write_skill
from .stage5_bridge import dry_run_stage5_pack
from .supervisor import DispatchSupervisor


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "stdin", False):
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DispatchValidationError(f"invalid JSON on stdin: {exc}") from exc
    elif getattr(args, "file", None):
        try:
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DispatchValidationError(f"invalid task file: {exc}") from exc
    else:
        raise DispatchValidationError("provide --stdin or --file")
    if not isinstance(payload, dict):
        raise DispatchValidationError("task payload must be a JSON object")
    return payload


def cmd_backends(_: argparse.Namespace) -> int:
    _print_json({"backends": probe_backends()})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    payload = _load_payload(args)
    if args.backend:
        payload["backend"] = args.backend
    home = Path(args.home) if args.home else None
    supervisor = DispatchSupervisor(home=home)
    record = supervisor.accept(payload, project_root=Path(args.project).resolve())
    if args.wait:
        record = supervisor.execute(
            record.run_id, timeout_seconds=args.timeout, sync=True
        )
    _print_json(
        {
            "run_id": record.run_id,
            "status": record.status,
            "backend": record.backend,
            "thread_id": record.thread_id,
            "result": record.result,
            "error": record.error,
        }
    )
    return 0 if record.status in {"accepted", "completed"} else 1


def cmd_result(args: argparse.Namespace) -> int:
    home = Path(args.home) if args.home else None
    supervisor = DispatchSupervisor(home=home)
    if args.wait:
        records = supervisor.wait(
            list(args.run_ids), timeout_seconds=args.timeout
        )
    else:
        records = [supervisor.result(rid) for rid in args.run_ids]
    _print_json(
        {
            "results": [
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "backend": r.backend,
                    "result": r.result,
                    "error": r.error,
                }
                for r in records
            ]
        }
    )
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    payload = _load_payload(args)
    home = Path(args.home) if args.home else None
    members = args.members.split(",") if args.members else None
    board = run_panel(
        payload,
        project_root=Path(args.project).resolve(),
        members=members,
        home=home,
        timeout_seconds=args.timeout,
    )
    _print_json(board)
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    home = Path(args.home) if args.home else None
    report = gc(
        home=home,
        max_age_seconds=args.max_age_days * 86400,
        dry_run=args.dry_run,
    )
    _print_json(report)
    return 0


def cmd_skill_render(args: argparse.Namespace) -> int:
    home = Path(args.home) if args.home else None
    if args.write:
        path = write_skill(
            Path(args.write) if args.write not in {"", "1", "true"} else None,
            home=home,
        )
        _print_json({"written": str(path)})
    else:
        print(render_skill_markdown(home=home))
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    home = Path(args.home) if args.home else None
    if args.route_cmd == "add":
        save_route(args.scene, args.backend, home=home)
        _print_json({"ok": True, "scene": args.scene, "backend": args.backend})
        return 0
    if args.route_cmd == "list":
        from .skill_render import load_routes

        _print_json({"routes": load_routes(home)})
        return 0
    raise DispatchValidationError(f"unknown route command: {args.route_cmd}")


def cmd_bridge(args: argparse.Namespace) -> int:
    report = dry_run_stage5_pack(Path(args.pack_dir))
    _print_json(report)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = dispatch_home(Path(args.home) if args.home else None)
    _print_json(
        {
            "home": str(home),
            "backends": probe_backends(),
            "notes": [
                "This tool is experimental and not part of the installed dyro package.",
                "Never merge/push/signoff from dispatch results.",
            ],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments.local_agent_dispatch",
        description="Optional local agent dispatch (ADR-0002, removable experiment)",
    )
    parser.add_argument(
        "--home",
        default="",
        help="Override state home (default ~/.dyro/local-agent-dispatch)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backends", help="Probe available backends")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("doctor", help="Show home + backends")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="Accept and optionally execute a task")
    p.add_argument("--project", required=True, help="Project root")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--file", default="")
    p.add_argument("--backend", default="")
    p.add_argument("--wait", action="store_true", help="Execute synchronously")
    p.add_argument("--timeout", type=float, default=120.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("result", help="Fetch run results")
    p.add_argument("run_ids", nargs="+")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("panel", help="Run multi-backend panel")
    p.add_argument("--project", required=True)
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--file", default="")
    p.add_argument("--members", default="", help="Comma backends, e.g. echo,codex")
    p.add_argument("--timeout", type=float, default=120.0)
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("gc", help="Garbage-collect aged state")
    p.add_argument("--max-age-days", type=float, default=7.0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("skill-render", help="Render host skill markdown")
    p.add_argument(
        "--write",
        nargs="?",
        const="1",
        default="",
        help="Write SKILL.md to dispatch home or given path",
    )
    p.set_defaults(func=cmd_skill_render)

    p = sub.add_parser("route", help="Manage routing preferences")
    route_sub = p.add_subparsers(dest="route_cmd", required=True)
    add = route_sub.add_parser("add")
    add.add_argument("scene")
    add.add_argument("backend")
    add.set_defaults(func=cmd_route)
    lst = route_sub.add_parser("list")
    lst.set_defaults(func=cmd_route)

    p = sub.add_parser(
        "stage5-bridge",
        help="Dry-run validate an external-workflow evidence pack (L4)",
    )
    p.add_argument("pack_dir")
    p.set_defaults(func=cmd_bridge)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.home:
        args.home = ""
    try:
        return int(args.func(args))
    except DispatchValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
