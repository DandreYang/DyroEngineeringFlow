"""Production readiness gate for the external semantic runtime experiment.

Stage 5 closeout: local isolation may be proven while production remains
explicitly Not ready. This module encodes the ADR-0001 stop conditions and the
operator checklist so CI/tests can assert the gate stays fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from dyro.errors import ValidationError

from .production_acceptance import (
    ATTESTATION_PURPOSES,
    VerifiedProductionAcceptance,
    verify_production_acceptance,
)


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    requirement: str
    status: str  # pass | partial | fail | out_of_scope
    evidence: str
    blocks_production: bool
    category: str
    remediation: str
    verification: str


def production_not_ready_checklist() -> tuple[ChecklistItem, ...]:
    return (
        ChecklistItem(
            id="PROD-01",
            requirement="多宿主与生产容器逃逸评审",
            status="out_of_scope",
            evidence="Stage0–5 目前只证明本机 Docker/Colima 隔离",
            blocks_production=True,
            category="environment-security",
            remediation=(
                "针对实际生产编排器、内核、存储与网络策略执行独立逃逸和"
                "租户边界评估，并闭环发现项。"
            ),
            verification="独立多宿主安全报告及可追踪的修复证据",
        ),
        ChecklistItem(
            id="PROD-02",
            requirement="真实 Codex/Claude 二进制与 operator 舰队凭据交付",
            status="partial",
            evidence="Stage5 已有 host provider 内容钉扎；CI 仍使用 fixture CLI",
            blocks_production=True,
            category="provider-fleet",
            remediation=(
                "在 operator 舰队验证真实 provider 二进制钉扎、仅 Broker 可见的"
                "凭据交付、轮换、撤销和故障恢复。"
            ),
            verification="真实舰队 canary 证据及凭据隔离断言",
        ),
        ChecklistItem(
            id="PROD-03",
            requirement="Stage5 到 Dyro Core 的执行证据交接",
            status="pass",
            evidence=(
                "Runtime claim 权限受 Core claim 到期时间约束；已验证的 Stage5 "
                "pack 身份被写入 receipt 并进入签名 Core bundle，端到端覆盖 "
                "gates、干净 HEAD、provenance 导入与独立 review 绑定"
            ),
            blocks_production=True,
            category="control-plane-integration",
            remediation=(
                "每次发布必须执行 handoff 适配器与权限边界回归测试。"
            ),
            verification=(
                "tests.test_runtime_core_handoff_integration、signed-flow 与"
                "禁止越权测试"
            ),
        ),
        ChecklistItem(
            id="PROD-04",
            requirement="源码树和历史中无第三方 workflow 包或品牌残留",
            status="pass",
            evidence="使用 first-party @dyro/semantic-flow；历史清理已完成",
            blocks_production=False,
            category="supply-chain",
            remediation="发布门禁持续执行依赖与历史扫描。",
            verification="源码、wheel、sdist 与 Git 历史扫描",
        ),
        ChecklistItem(
            id="PROD-05",
            requirement="Sandbox 永不持有 provider token 或 execution key",
            status="pass",
            evidence="Stage1–5 测试验证 token/key 不进入 Sandbox 可见面",
            blocks_production=False,
            category="credential-isolation",
            remediation="持续强制执行恶意 fixture 泄漏测试。",
            verification="Stage1–5 机密泄漏回归测试",
        ),
        ChecklistItem(
            id="PROD-06",
            requirement="关键分支 fail-closed，禁止并行分支静默 null",
            status="pass",
            evidence="@dyro/semantic-flow parallelSettled 与 envelope 校验",
            blocks_production=False,
            category="runtime-correctness",
            remediation="持续强制执行 envelope 与关键分支测试。",
            verification="semantic-flow 与 envelope 回归测试",
        ),
        ChecklistItem(
            id="PROD-07",
            requirement="任何运行后特权动作前必须完成双重清理",
            status="pass",
            evidence="Stage4/5 在 pack 与 dry-run 前验证 CleanupProof",
            blocks_production=False,
            category="lifecycle-isolation",
            remediation="保留所有权标签与有界 cleanup-settle 检查。",
            verification="Sandbox 与 Broker 清理故障注入测试",
        ),
        ChecklistItem(
            id="PROD-08",
            requirement="Supervisor 永不 merge 或 push",
            status="pass",
            evidence="refuse_if_merge_requested 与 forbidden-actions 校验",
            blocks_production=False,
            category="authority-boundary",
            remediation="Runtime API 中持续禁止生产交付动作。",
            verification="禁止越权测试与独立边界评审",
        ),
        ChecklistItem(
            id="PROD-09",
            requirement="所有可写挂载都具备 worktree 存储配额",
            status="partial",
            evidence="目前只有 host worktree 事后配额；Docker 写挂载强制配额未证明",
            blocks_production=True,
            category="resource-isolation",
            remediation=(
                "在每个生产可写挂载强制并故障测试字节、inode 和文件数限制，"
                "不能只依赖运行后测量。"
            ),
            verification="配额耗尽与并发租户负载证据",
        ),
        ChecklistItem(
            id="PROD-10",
            requirement="使用外部语义运行时替换 Dyro TaskGraph",
            status="out_of_scope",
            evidence="ADR-0001 明确否决，且实现从未尝试",
            blocks_production=False,
            category="product-boundary",
            remediation="保持 Dyro Core 为唯一交付控制面。",
            verification="架构评审与禁止越权测试",
        ),
    )


def evaluate_production_readiness(
    *,
    root: Path | None = None,
    release_manifest: Path | None = None,
    attestations: Mapping[str, Path] | Sequence[Path] = (),
) -> dict[str, object]:
    items = list(production_not_ready_checklist())
    acceptance: VerifiedProductionAcceptance | None = None
    if attestations and release_manifest is None:
        raise ValidationError(
            "提供生产验收证明时必须同时提供 --release-manifest"
        )
    if release_manifest is not None:
        if root is None:
            raise ValidationError(
                "验证生产发布清单时必须提供 Dyro trust root"
            )
        acceptance = verify_production_acceptance(
            root=root,
            release_manifest_path=release_manifest,
            attestation_paths=attestations,
        )
        replacements: dict[str, ChecklistItem] = {}
        for check_id, attestation in acceptance.attestations.items():
            replacements[check_id] = replace(
                next(item for item in items if item.id == check_id),
                status=attestation.verdict,
                evidence=(
                    f"已验证绑定发布 {acceptance.release_id} 的 "
                    f"{ATTESTATION_PURPOSES[check_id]} 签名验收；"
                    f"签名者 {attestation.signer_key_id}，"
                    f"不可变证据 {attestation.evidence_count} 项"
                ),
                verification=(
                    f"发布清单 {acceptance.release_manifest_sha256} 与 "
                    f"{check_id} 验收签名均已验证"
                ),
            )
        items = [replacements.get(item.id, item) for item in items]
    blockers = [item for item in items if item.blocks_production]
    open_blockers = [
        item
        for item in blockers
        if item.status != "pass"
    ]
    production_ready = len(open_blockers) == 0
    return {
        "schema_version": 1,
        "kind": "external-semantic-runtime-production-gate",
        "production_ready": production_ready,
        "verdict": "READY" if production_ready else "NOT_READY",
        "exit_code": 0 if production_ready else 3,
        "blocker_count": len(open_blockers),
        "release_approval_required": True,
        "production_acceptance": (
            acceptance.to_mapping()
            if acceptance is not None
            else {
                "provided": False,
                "release_manifest_verified": False,
                "missing_checks": list(ATTESTATION_PURPOSES),
                "required_signing_purposes": {
                    "release": "production-release",
                    **dict(ATTESTATION_PURPOSES),
                },
                "schemas": {
                    "release_manifest": (
                        "schemas/production-deployment-manifest.schema.json"
                    ),
                    "attestation": (
                        "schemas/production-attestation.schema.json"
                    ),
                },
            }
        ),
        "blockers": [
            {
                "id": item.id,
                "requirement": item.requirement,
                "status": item.status,
                "evidence": item.evidence,
                "category": item.category,
                "remediation": item.remediation,
                "verification": item.verification,
            }
            for item in open_blockers
        ],
        "checklist": [
            {
                "id": item.id,
                "requirement": item.requirement,
                "status": item.status,
                "evidence": item.evidence,
                "blocks_production": item.blocks_production,
                "category": item.category,
                "remediation": item.remediation,
                "verification": item.verification,
            }
            for item in items
        ],
        "next_command": "dyro runtime plan",
        "adr_stop_conditions": [
            "must_not_modify_dyro_scheduler_for_internal_phases",
            "must_isolate_credentials_and_execution_keys",
            "must_fail_closed_on_critical_failures",
            "must_not_reintroduce_third_party_workflow_deps",
        ],
    }


def production_readiness_plan() -> dict[str, object]:
    """Return a deterministic, read-only promotion plan for operators."""
    report = evaluate_production_readiness()
    checklist_by_id = {
        item["id"]: item
        for item in report["checklist"]  # type: ignore[index]
    }
    core_handoff_complete = (
        checklist_by_id["PROD-03"]["status"] == "pass"
    )
    return {
        "schema_version": 1,
        "kind": "external-semantic-runtime-production-plan",
        "target": "production",
        "current_verdict": report["verdict"],
        "mutates_state": False,
        "authority_boundary": {
            "runtime_may": [
                "execute_fixed_reviewed_bundle",
                "call_provider_through_broker",
                "seal_runtime_evidence_after_verified_cleanup",
            ],
            "dyro_core_only": [
                "import_execution_evidence",
                "bind_independent_review",
                "record_signoff",
                "merge",
                "push",
            ],
        },
        "phases": [
            {
                "id": "local-diagnostics",
                "state": "operator_check_required",
                "title": "验证本机 runtime 基础环境",
                "covers": ["runtime-lock", "docker-daemon", "pinned-image"],
                "command": "dyro runtime doctor",
                "acceptance": (
                    "全部本地阻断检查通过；该结果只证明本地 PoC 可运行，"
                    "不代表生产就绪。"
                ),
            },
            {
                "id": "core-evidence-handoff",
                "state": (
                    "completed"
                    if core_handoff_complete
                    else "blocked"
                ),
                "title": "把 Stage5 输出绑定到既有 Dyro Core 证据链",
                "covers": ["PROD-03"],
                "command": "dyro runtime handoff --help",
                "acceptance": (
                    "当前签名 claim 以已验证 receipt、gates、干净仓库 HEAD 与 "
                    "provenance 进入 Core；独立 review 绑定相同 attempt 和 plan。"
                ),
            },
            {
                "id": "environment-acceptance",
                "state": "blocked",
                "title": "验证真实生产环境",
                "covers": ["PROD-01", "PROD-02", "PROD-09"],
                "command": "dyro runtime production-gate --help",
                "acceptance": (
                    "发布环境通过独立多宿主安全评审、真实 provider 舰队 canary、"
                    "凭据生命周期证明与可写挂载配额故障测试；四个用途隔离的"
                    "签名绑定同一发布清单。"
                ),
            },
            {
                "id": "independent-release-review",
                "state": "pending",
                "title": "执行最终独立对抗上线决策",
                "covers": ["all-production-checks"],
                "command": "dyro runtime production-gate",
                "acceptance": (
                    "门禁以退出码 0 返回 READY，且复核者确认 runtime 权限仍不包含 "
                    "review、signoff、merge 或 push。"
                ),
            },
        ],
        "open_blockers": [
            {
                "id": blocker["id"],
                "category": blocker["category"],
                "remediation": blocker["remediation"],
                "verification": blocker["verification"],
            }
            for blocker in report["blockers"]  # type: ignore[index]
        ],
    }


def assert_not_production_ready(report: Mapping[str, object] | None = None) -> None:
    payload = report or evaluate_production_readiness()
    if payload.get("production_ready") is True:
        raise AssertionError(
            "production gate unexpectedly READY; experiment closeout expects NOT_READY"
        )
    if payload.get("verdict") != "NOT_READY":
        raise AssertionError(f"unexpected production verdict: {payload.get('verdict')}")
