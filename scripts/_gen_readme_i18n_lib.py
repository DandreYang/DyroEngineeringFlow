# internal helper; not part of package
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "docs" / "images" / "diagrams" / "src"
SRC_ZH = SRC / "zh"


def mmd(name: str, *, lang: str = "en") -> str:
    """lang: en → src/*.mmd; zh → src/zh/*.mmd (Chinese legends)."""
    base = SRC_ZH if lang == "zh" else SRC
    return "```mermaid\n" + (base / f"{name}.mmd").read_text(encoding="utf-8").strip() + "\n```"

NAV = "[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)"

def diagram_block(t: dict, *, lang: str = "en") -> str:
    return f"""## {t['h_diag']}

{t['diag_intro']}

### {t['h_diag_arch']}

{mmd("01-architecture", lang=lang)}

### {t['h_diag_state']}

{mmd("03-task-state-machine", lang=lang)}

### {t['h_diag_local']}

{mmd("04-local-delivery-sequence", lang=lang)}

### {t['h_diag_ext']}

{mmd("05-external-evidence-sequence", lang=lang)}

{t['diag_more']}
"""

def assemble(t: dict, *, lang: str = "en") -> str:
    d = diagram_block(t, lang=lang)
    return f"""# DyroEngineeringFlow

{NAV}

{t['lead']}

{t['tagline']}

{t['profile_note']}

## {t['h_enforce']}

{t['enforce_list']}

{d}
## {t['h_quick']}

{t['quick_install']}

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# {t['comment_new_terminal']}
pipx install dyro
dyro --version
```

{t['upgrade_note']}

```bash
python3 -m pip install --user --upgrade dyro
```

{t['setup_intro']}

```bash
mkdir my-workspace && cd my-workspace
# {t['comment_clone']}
dyro setup . --name my-workspace --line dev --yes
```

{t['setup_detail']}

```bash
dyro init . --wizard --name my-workspace
```

{t['repo_add']}

```bash
dyro repo add repositories/services/payments
dyro repo list
```

{t['config_policy']}

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

{t['bootstrap']}

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

{t['start']}

```bash
dyro start
```

## {t['h_delivery']}

{t['delivery_intro']}

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# {t['comment_repo_base']}
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "{t['task_title']}" --line release-2026-10 --repository api
dyro task graph check --line release-2026-10
dyro task graph --line release-2026-10 --format mermaid
dyro task explain API-101
dyro task next
dyro task next --run --yes
dyro task attempts API-101
dyro task binding API-101
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

{t['hotfix']}

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

{t['external']}

```bash
dyro task claim API-101 --by isolated-runner-1 --key-id runner-2026 \
  --output /secure-transfer/API-101.core-claim.json
# {t['comment_claim_renew']}
dyro task claim-renew API-101 --by isolated-runner-1 --lease-seconds 3600
# {t['comment_runner_build']}
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
# {t['comment_control_import']}
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
# {t['comment_claim_release']}
dyro task claim-release API-101 --by isolated-runner-1
```

{t['provenance']}

{t['evidence_gen']}

```bash
dyro task evidence generations API-101
dyro --dry-run task evidence generations API-101 --prune --older-than-days 30 --keep 10
dyro task evidence generations API-101 --prune --older-than-days 30 --keep 10 --yes
```

{t['keys']}

```bash
dyro config set policy.execution_mode external
dyro config set policy.require_signed_execution true
dyro config set policy.require_signed_review true
dyro config set policy.require_external_signoff true
dyro config set policy.require_signed_signoff true

dyro key generate runner-2026 --private-key /secure/runner.pem --public-key /secure/runner.pub.pem
dyro key trust runner-2026 --purpose execution --public-key /secure/runner.pub.pem \
  --not-after 2027-01-01T00:00:00+00:00
dyro task claim API-101 --by isolated-runner-1 --key-id runner-2026 \
  --output /secure-transfer/API-101.core-claim.json
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md \
  --output /runner/out/API-101.zip --claim /runner/in/claim.json \
  --signing-key /secure/runner.pem --key-id runner-2026

dyro key generate reviewer-2026 --private-key /secure/reviewer.pem --public-key /secure/reviewer.pub.pem
dyro key trust reviewer-2026 --purpose review --public-key /secure/reviewer.pub.pem
dyro task evidence review-build API-101 --file /review/out/review.md --reviewer independent-reviewer \
  --output /review/out/review.json --signing-key /secure/reviewer.pem --key-id reviewer-2026
dyro task evidence review API-101 --file /review/out/review.json

dyro key generate approver-2026 --private-key /secure/approver.pem --public-key /secure/approver.pub.pem
dyro key trust approver-2026 --purpose signoff --public-key /secure/approver.pub.pem
dyro task signoff API-101 --by release-manager --signing-key /secure/approver.pem --key-id approver-2026

dyro key list --purpose execution --show-status
dyro key revoke runner-2026 --purpose execution --reason "runner retired"
dyro key audit
```

{t['audit']}

```bash
dyro key generate audit-client-2026 \
  --private-key /secure/audit-client.pem \
  --public-key /secure/audit-client.pub.pem
# {t['comment_oob']}
dyro key trust witness-2026 \
  --purpose audit-receipt \
  --public-key witness-2026.pub.pem
dyro key trust witness-recovery \
  --purpose audit-recovery \
  --public-key witness-recovery.pub.pem

DYRO_AUDIT_TOKEN=... dyro key audit-sync \
  --witness primary \
  --endpoint https://audit.example.com/v1/dyro/batches \
  --signing-key /secure/audit-client.pem \
  --key-id audit-client-2026 \
  --witness-key-id witness-2026 \
  --witness-recovery-key-id witness-recovery
```

{t['witness_detail']}

{t['sig_policy']}

{t['ts_runner']}

{t['dry_run']}

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## {t['h_cmd']}

| {t['col_cmd']} | {t['col_purpose']} |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | {t['cmd_setup']} |
| `doctor` / `status` | {t['cmd_doctor']} |
| `line create/list` | {t['cmd_line']} |
| `hotfix create` | {t['cmd_hotfix']} |
| `changeset create/list/verify` | {t['cmd_cs']} |
| `config get/set` / `agent list/add/test` / `open` | {t['cmd_cfg']} |
| `task create/list/board/status/next/graph/explain/attempts/binding` | {t['cmd_task']} |
| `task run/answer/gates/review/signoff` | {t['cmd_run']} |
| `task claim --output` / `task evidence build/execution/review` | {t['cmd_claim']} |
| `task merge` | {t['cmd_merge']} |
| `task loop/daemon/stats/decisions` | {t['cmd_loop']} |

{t['see_also']}

## {t['h_lang']}

{t['lang_body']}

## {t['h_bound']}

{t['bound_body']}
"""
