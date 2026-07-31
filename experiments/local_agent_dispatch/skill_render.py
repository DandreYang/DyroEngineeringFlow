"""Render a host skill that only lists available backends (ADR-0002 L3)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from .adapters.registry import (
    adapter_is_authenticated,
    get_adapter,
    list_real_provider_ids,
    probe_backends,
)
from .errors import DispatchValidationError
from .json_store import atomic_write_json, read_json
from .paths import dispatch_home_path, skills_dir


def load_routes(home: Path | None = None) -> list[dict[str, str]]:
    path = dispatch_home_path(home) / "skills" / "routes.json"
    payload = read_json(path)
    if not payload:
        return []
    routes = payload.get("routes")
    if not isinstance(routes, list):
        return []
    out: list[dict[str, str]] = []
    for item in routes:
        if isinstance(item, dict) and item.get("scene") and item.get("backend"):
            out.append(
                {"scene": str(item["scene"]), "backend": str(item["backend"])}
            )
    return out


def selected_route(*, home: Path | None = None, scene: str = "default") -> str | None:
    return next(
        (route["backend"] for route in load_routes(home) if route["scene"] == scene),
        None,
    )


def save_route(scene: str, backend: str, *, home: Path | None = None) -> None:
    scene = scene.strip()
    backend = backend.strip()
    if not scene:
        raise DispatchValidationError("route scene must not be empty")
    if backend not in list_real_provider_ids():
        raise DispatchValidationError(
            "route backend must be an integrated real provider; "
            "offline simulation and discovery-only commands cannot be routed"
        )
    adapter = get_adapter(backend)
    if not adapter.available() or not adapter_is_authenticated(adapter):
        raise DispatchValidationError(f"route backend is not ready: {backend}")
    path = skills_dir(home) / "routes.json"
    routes = load_routes(home)
    routes = [r for r in routes if r["scene"] != scene]
    routes.append({"scene": scene, "backend": backend})
    atomic_write_json(path, {"schema_version": 1, "routes": routes})


def render_skill_markdown(
    *,
    home: Path | None = None,
    routes: Sequence[Mapping[str, str]] | None = None,
) -> str:
    backends = probe_backends()
    providers = [b for b in backends if b.get("execution_kind") == "provider"]
    available = [
        b
        for b in providers
        if b["available"] and b["authenticated"] and b.get("supported")
    ]
    unavailable = [b for b in providers if b not in available]
    discovery_only = [
        b for b in backends if b.get("execution_kind") == "unintegrated"
    ]
    route_rows = list(routes) if routes is not None else load_routes(home)

    lines = [
        "---",
        "name: dyro-local-agent-dispatch",
        "description: >-",
        "  Dispatch read-only or isolated-edit tasks to local agent CLIs via",
        "  `python -m experiments.local_agent_dispatch`. Only backends listed",
        "  as available below may be used. Results are advisory.",
        "---",
        "",
        "# Dyro Local Agent Dispatch",
        "",
        "Use only when outsourcing survey/review work or multi-backend panels.",
        "Never merge/push/signoff from this tool. Verify evidence before adopting.",
        "",
        "## Available backends (probed on this machine)",
        "",
    ]
    if available:
        for row in available:
            lines.append(f"- `{row['id']}` (command: `{row['command']}`)")
    else:
        lines.append("- none ready; configure and authenticate an integrated Provider")

    if unavailable:
        lines.append("")
        lines.append("## Not available (do not route here)")
        lines.append("")
        for row in unavailable:
            state = "not found" if not row["available"] else "not authenticated"
            lines.append(f"- `{row['id']}` (`{row['command']}`: {state})")

    if discovery_only:
        lines.extend(
            [
                "",
                "## Discovered but not integrated (do not dispatch)",
                "",
            ]
        )
        for row in discovery_only:
            state = "found" if row["available"] else "not found"
            lines.append(
                f"- `{row['id']}` (`{row['command']}`: {state}; needs an audited adapter)"
            )

    lines.extend(
        [
            "",
            "## User routes",
            "",
        ]
    )
    if route_rows:
        for row in route_rows:
            lines.append(f"- {row['scene']} → `{row['backend']}`")
    else:
        lines.append("- (none configured; use `route add`)")

    lines.extend(
        [
            "",
            "## Commands",
            "",
            "```bash",
            "python -m experiments.local_agent_dispatch backends",
            "python -m experiments.local_agent_dispatch run --project . --stdin < task.json",
            "python -m experiments.local_agent_dispatch result <run_id>",
            "python -m experiments.local_agent_dispatch panel --project . --stdin < task.json",
            "```",
            "",
            "## Discipline",
            "",
            "1. Task JSON must be self-contained (five-part contract).",
            "2. Prefer minimal `files` globs; never `**/*`.",
            "3. `auto` never falls back to echo; add a ready route when several Providers exist.",
            "4. `echo` is an explicit offline simulation only and needs `allow_offline_simulation=true`.",
            "5. Bring back only summary/evidence; do not load full event logs.",
            "",
        ]
    )
    return "\n".join(lines)


def write_skill(
    destination: Path | None = None, *, home: Path | None = None
) -> Path:
    text = render_skill_markdown(home=home)
    if destination is None:
        destination = skills_dir(home) / "SKILL.md"
    else:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination
