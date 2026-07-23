# Security policy

DyroEngineeringFlow treats a workspace Profile as executable authority: it can choose Git repositories, invoke approved local tools, create worktrees and—only after explicit confirmation—merge or push code.

- Only use Profiles from repositories you trust.
- Keep secrets out of `dyro.toml`, task manifests, receipts and ledger files.
- Prefer argv arrays; do not add `sh -c`, inline tokens or credentialed URLs to adapters and gates.
- Keep `policy.allow_push = false` until a project has a reviewed release policy.
- Report security issues privately to the repository maintainers; do not include credentials or customer data in issues.
