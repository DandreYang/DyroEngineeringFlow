# Agent Bridge Phase 0 — Release readiness (abca42c / PR #19)

## Green now
- Ubuntu CI run https://github.com/DandreYang/DyroEngineeringFlow/actions/runs/31480022379 SUCCESS
- `bridge-zero-effects` passed (~4m39s); artifact `dyro-bridge-zero-effect-evidence` not expired
- Six reports passed with matching package/contract digests (see `six-report-summary.json`)
- F04 local byte-budget evidence: `host/f04-context-budget.json` PASS
- Adversarial review board for local-fix: `docs/superpowers/reviews/2026-08-10-dyro-agent-bridge-phase-0-fix-adversarial-review-board.md`

## Blocked for formal Phase 0 Go / publish
- F01 MCP tool discovery: macOS `dyro-mcp` → `CORE_HANDSHAKE_UNAVAILABLE` (by design). Need Ubuntu Codex host (`G008`).
- F02 sandbox permission boundary: blocked without working MCP on host.
- F03: 3/10 fresh-session channel-choice samples PASS; remaining 7 + live Bridge success journeys need Ubuntu.
- Skill discovery alone is PASS on macOS; Skill beta still needs complete F01/F03/F04 host package per acceptance §8.

## Not done by this ultragoal yet
- Merge PR #19 to main
- Tag / GitHub Release / PyPI publish (require separate explicit authorization after Go)

## Recommended next host
Linux Ubuntu 24.04 machine with Codex CLI + `uv run --extra mcp dyro integration install codex` + `codex mcp add dyro-readonly -- $(pwd)/.venv/bin/dyro-mcp`, then re-run F01 tools / F02 / remaining F03.
