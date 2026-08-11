# Exact-SHA CI evidence index — Agent Bridge Phase 0

- PR: https://github.com/DandreYang/DyroEngineeringFlow/pull/19
- Branch tip (feat/dev): `abca42cdbdd9cd5125e0a4045a8c79d53b1c0187`
- CI run: https://github.com/DandreYang/DyroEngineeringFlow/actions/runs/31480022379
- Artifact: `dyro-bridge-zero-effect-evidence` (see `artifact-meta.json`)
- Six-report summary: `six-report-summary.json` (all `passed=true`, package/contract digests unique)

Note: pull_request jobs may record GitHub’s temporary merge commit in report
`evidence.commit` while the workflow run `headSha` is the PR branch tip. Publish
gates must use the publish workflow’s exact-SHA verification against the
trusted main checkout, not this index alone.
