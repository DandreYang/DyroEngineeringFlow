"""Render a host skill that only lists available backends (ADR-0002 L3)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from .adapters.registry import probe_backends
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


def save_route(scene: str, backend: str, *, home: Path | None = None) -> None:
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
    available = [b for b in backends if b["available"]]
    unavailable = [b for b in backends if not b["available"]]
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
        lines.append("- none probed; `echo` offline adapter still works for dry runs")

    if unavailable:
        lines.append("")
        lines.append("## Not available (do not route here)")
        lines.append("")
        for row in unavailable:
            lines.append(f"- `{row['id']}` (`{row['command']}` not found)")

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
            "3. Dispatch first, continue other work, then collect results.",
            "4. Bring back only summary/evidence; do not load full event logs.",
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
