"""Read-only ``next.commands`` projection. Isolated Console may import this."""

from __future__ import annotations

from ..config import Config
from ..errors import DyroError, ValidationError
from ..onboarding import validate_bootstrap_destination
from ..read_limits import ReadBudget
from ..workspace import doctor, list_lines
from .ready_briefing import briefing_command


def next_commands(
    config: Config,
    alias: str | None = None,
    *,
    read_budget: ReadBudget | None = None,
) -> list[str]:
    """Same list ``dyro next`` puts in JSON ``commands``.

    Isolated workers may call this. It does not import launcher, provider,
    or session credentials. Commands never embed ``--root`` paths.
    """
    token = alias if isinstance(alias, str) and alias else getattr(config, "name", "")
    if not isinstance(token, str) or not token:
        return []
    try:
        findings = doctor(config, read_budget=read_budget)
    except (DyroError, ValidationError, OSError, TypeError, AttributeError):
        return []
    failures = [
        item
        for item in findings
        if isinstance(item, str) and item.startswith("FAIL")
    ]
    if failures:
        return repair_commands(config, token, failures)
    try:
        lines = list_lines(config, read_budget=read_budget)
    except (DyroError, ValidationError, OSError, TypeError, AttributeError):
        return []
    if not lines:
        return [briefing_command(token, "line", "create", "dev", "--yes")]
    return []


def repair_commands(config: Config, alias: str, failures: list[str]) -> list[str]:
    """Scoped repair command for doctor FAILs. Doctor is a read, not ``--yes``."""
    if not failures:
        return []
    if bootstrap_repair_applicable(config, failures):
        return [briefing_command(alias, "bootstrap", "--yes")]
    return [briefing_command(alias, "doctor")]


def bootstrap_repair_applicable(config: Config, failures: list[str]) -> bool:
    """True only when every FAIL is a missing repo that bootstrap can clone."""
    repositories = getattr(config, "repositories", {})
    if not isinstance(repositories, dict) or not failures:
        return False
    root = getattr(config, "root", None)
    if root is None:
        return False
    absent: set[str] = set()
    for repo_id, repository in repositories.items():
        remote = getattr(repository, "remote", "")
        path = getattr(repository, "path", "")
        if not remote or not path:
            continue
        destination = root / path
        try:
            present = destination.exists() or destination.is_symlink()
        except OSError:
            continue
        if present:
            continue
        try:
            validate_bootstrap_destination(config, path)
        except (DyroError, OSError):
            continue
        absent.add(str(repo_id))
    if not absent:
        return False
    expected = {
        f"FAIL repository {repo_id}: missing or not Git: "
        f"{root / repositories[repo_id].path}"
        for repo_id in absent
    }
    return set(failures) == expected
