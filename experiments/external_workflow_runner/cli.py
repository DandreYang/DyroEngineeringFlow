"""Operator-facing CLI for the optional external semantic runtime.

The runtime may execute a fixed reviewed workflow and seal local evidence, but
Dyro Core remains the only authority for evidence import, review, signoff,
merge, and push.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


EXIT_OK = 0
EXIT_ERROR = 2
EXIT_BLOCKED = 3


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _output_mode(args: argparse.Namespace) -> str:
    selected = getattr(args, "output", "auto")
    if selected in {"human", "json"}:
        return str(selected)
    is_tty = getattr(sys.stdout, "isatty", lambda: False)
    return "human" if is_tty() else "json"


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--json",
        dest="output",
        action="store_const",
        const="json",
        help="输出稳定的机器可读 JSON",
    )
    group.add_argument(
        "--human",
        dest="output",
        action="store_const",
        const="human",
        help="输出面向操作人员的摘要与下一步",
    )
    parser.set_defaults(output="auto")


def _status_report() -> dict[str, object]:
    from .stage5.production_gate import evaluate_production_readiness

    report = evaluate_production_readiness()
    return {
        "schema_version": 1,
        "kind": "external-semantic-runtime-status",
        "module": "experiments.external_workflow_runner",
        "shipped_with_dyro_wheel": True,
        "lifecycle": "production-candidate",
        "production": report,
        "capabilities": {
            "stage5_supervisor_api": True,
            "operator_run_cli": False,
            "runtime_doctor": True,
            "production_plan": True,
            "core_evidence_handoff": True,
            "signed_production_acceptance": True,
        },
        "authority_boundary": {
            "runtime_forbidden": [
                "review",
                "signoff",
                "merge",
                "push",
            ],
            "control_plane": "Dyro Core",
        },
        "entry_points": {
            "cli": (
                "dyro runtime "
                "status|doctor|plan|claim|handoff|production-gate"
            ),
            "import": "experiments.external_workflow_runner",
        },
        "next_command": "dyro runtime doctor",
        "notes": [
            "Local Stage0–5 isolation is available through the fixed Supervisor API.",
            "A broad operator run command is intentionally withheld until PROD-02.",
            "Production remains NOT_READY until every blocking check passes.",
            "This surface never performs review, signoff, merge, or push.",
        ],
    }


def _print_status_human(payload: Mapping[str, object]) -> None:
    production = payload.get("production")
    if not isinstance(production, Mapping):
        production = {}
    ready = production.get("production_ready") is True
    blocker_count = production.get("blocker_count", "?")
    print("Dyro 外部语义运行时")
    print("生命周期：生产候选（Production Candidate）")
    if ready:
        print("生产状态：门禁已通过（仍需独立发布批准）")
    else:
        print(f"生产状态：未就绪（{blocker_count} 个阻断项）")
    print("控制边界：运行时不会执行 review、signoff、merge 或 push。")
    print("下一步：dyro runtime doctor")


def cmd_status(args: argparse.Namespace) -> int:
    payload = _status_report()
    if _output_mode(args) == "human":
        _print_status_human(payload)
    else:
        _print_json(payload)
    return EXIT_OK


def _print_gate_human(report: Mapping[str, object]) -> None:
    ready = report.get("production_ready") is True
    blockers = report.get("blockers")
    blocker_list = blockers if isinstance(blockers, list) else []
    acceptance = report.get("production_acceptance")
    acceptance_record = (
        acceptance if isinstance(acceptance, Mapping) else {}
    )
    if acceptance_record.get("provided") is True:
        print(
            "验收对象："
            f"{acceptance_record.get('release_id', '?')} / "
            f"{acceptance_record.get('environment_id', '?')}"
        )
        print(
            "发布清单："
            f"{acceptance_record.get('release_manifest_sha256', '?')}"
        )
    if ready:
        print("Dyro 外部语义运行时生产门禁：通过")
        print("结论：READY")
        print("注意：门禁通过不等于已批准发布，仍需独立发布审批。")
        return
    print("Dyro 外部语义运行时生产门禁：未通过")
    print(f"结论：NOT_READY（{len(blocker_list)} 个阻断项）")
    for item in blocker_list:
        if not isinstance(item, Mapping):
            continue
        print(
            f"- {item.get('id', '?')} [{item.get('status', '?')}] "
            f"{item.get('requirement', '')}"
        )
        print(f"  现状：{item.get('evidence', '')}")
        print(f"  修复：{item.get('remediation', '')}")
        print(f"  验证：{item.get('verification', '')}")
    print("下一步：dyro runtime plan")
    print(f"退出码：{EXIT_BLOCKED}（用于阻断 CI/发布脚本）")


def cmd_production_gate(args: argparse.Namespace) -> int:
    from .stage5.production_gate import evaluate_production_readiness

    attestation_paths = {
        check_id: path
        for check_id, path in (
            ("PROD-01", args.security_attestation),
            ("PROD-02", args.provider_attestation),
            ("PROD-09", args.quota_attestation),
        )
        if path is not None
    }
    report = evaluate_production_readiness(
        root=args.root,
        release_manifest=args.release_manifest,
        attestations=attestation_paths,
    )
    if _output_mode(args) == "human":
        _print_gate_human(report)
    else:
        _print_json(report)
    return EXIT_OK if report["production_ready"] is True else EXIT_BLOCKED


def _print_doctor_human(report: Mapping[str, object]) -> None:
    ready = report.get("ready_for_local_poc") is True
    print("Dyro 外部语义运行时诊断")
    print(f"本地 PoC 环境：{'可用' if ready else '受阻'}")
    print("生产就绪：否")
    checks = report.get("checks")
    status_labels = {
        "pass": "通过",
        "fail": "失败",
        "blocked": "跳过",
        "not_configured": "未配置",
    }
    for item in checks if isinstance(checks, list) else []:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status", "unknown"))
        print(
            f"- {item.get('id', '?')} [{status_labels.get(status, status)}] "
            f"{item.get('label', '')}"
        )
        print(f"  {item.get('detail', '')}")
        remediation = item.get("remediation")
        if remediation and status != "pass":
            print(f"  建议：{remediation}")
    print("说明：doctor 只证明本机可运行本地 PoC，不会放行生产。")
    next_steps = report.get("next_steps")
    if isinstance(next_steps, list) and next_steps:
        print("下一步：")
        for step in next_steps:
            print(f"- {step}")


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import collect_runtime_diagnostics

    report = collect_runtime_diagnostics(
        provider_path=args.provider_path,
        provider_roots=tuple(args.provider_root),
    )
    if _output_mode(args) == "human":
        _print_doctor_human(report)
    else:
        _print_json(report)
    return (
        EXIT_OK
        if report.get("ready_for_local_poc") is True
        else EXIT_BLOCKED
    )


def _print_plan_human(plan: Mapping[str, object]) -> None:
    print("Dyro 外部语义运行时生产晋级计划")
    print(f"当前结论：{plan.get('current_verdict', 'UNKNOWN')}")
    print("计划模式：只读，不修改工作区、容器或控制面状态")
    phases = plan.get("phases")
    state_labels = {
        "completed": "已完成",
        "blocked": "受阻",
        "pending": "待执行",
        "operator_check_required": "需本机检查",
    }
    for index, phase in enumerate(
        phases if isinstance(phases, list) else [],
        start=1,
    ):
        if not isinstance(phase, Mapping):
            continue
        state = str(phase.get("state", "pending"))
        print(
            f"{index}. {phase.get('title', '')} "
            f"[{state_labels.get(state, state)}]"
        )
        print(f"   验收：{phase.get('acceptance', '')}")
        print(f"   检查：{phase.get('command', '')}")
    print("安全边界：runtime 仅运行固定工作流并封存证据；Dyro Core 独占交付权限。")


def cmd_plan(args: argparse.Namespace) -> int:
    from .stage5.production_gate import production_readiness_plan

    plan = production_readiness_plan()
    if _output_mode(args) == "human":
        _print_plan_human(plan)
    else:
        _print_json(plan)
    return EXIT_OK


def _print_claim_prepare_human(report: Mapping[str, object]) -> None:
    print("Dyro runtime claim 准备")
    print(f"结果：{report.get('verdict', 'UNKNOWN')}")
    print(f"任务：{report.get('task_id', '')}")
    print(f"Runner：{report.get('runner_id', '')}")
    print(
        "Core 绑定："
        f"{report.get('control_claim_id', '')} / "
        f"generation {report.get('control_generation', '')}"
    )
    print(f"输出：{report.get('output', '')}")
    print("权限：仅允许运行固定 workflow，不授予交付控制权限。")


def cmd_claim_prepare(args: argparse.Namespace) -> int:
    from .stage5.core_handoff import prepare_stage5_claim

    report = prepare_stage5_claim(
        core_claim=args.core_claim,
        output=args.output,
        dry_run=args.dry_run,
    )
    if _output_mode(args) == "human":
        _print_claim_prepare_human(report)
    else:
        _print_json(report)
    return EXIT_OK


def _print_handoff_human(report: Mapping[str, object]) -> None:
    print("Dyro Stage5 → Core 执行证据交接")
    print(f"结果：{report.get('verdict', 'UNKNOWN')}")
    print(f"任务：{report.get('task_id', '')}")
    print(f"Runtime pack：{report.get('runtime_pack_sha256', '')}")
    print(f"Core bundle：{report.get('core_bundle', '')}")
    print("Core import：未执行")
    print("review / signoff / merge / push：均未执行")
    if report.get("gates_executed") is False:
        print("门禁：dry-run 未执行")
    elif report.get("gates_passed") is False:
        print("门禁：未通过；该 bundle 仅供诊断，不可导入")
    else:
        print("门禁：已通过")
    next_command = report.get("next_command")
    if next_command:
        print(f"下一步：{next_command}")
    remediation = report.get("remediation")
    if remediation:
        print(f"修复：{remediation}")


def cmd_handoff(args: argparse.Namespace) -> int:
    from .stage5.core_handoff import build_core_evidence_handoff

    report = build_core_evidence_handoff(
        root=args.root,
        task_id=args.task,
        pack_dir=args.pack,
        workspace=args.workspace,
        core_claim=args.core_claim,
        output=args.output,
        signing_key=args.signing_key,
        key_id=args.key_id,
        dry_run=args.dry_run,
    )
    if _output_mode(args) == "human":
        _print_handoff_human(report)
    else:
        _print_json(report)
    return (
        EXIT_BLOCKED
        if report.get("verdict") == "BLOCKED"
        else EXIT_OK
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyro runtime",
        description=(
            "可选外部语义运行时：本地诊断、生产晋级计划与 fail-closed 门禁。"
            "当前生产状态为 NOT_READY。"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="验证输入并输出计划，不写 claim 或 Core evidence bundle",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser(
        "status",
        help="显示生命周期、能力与 Dyro Core 权限边界",
    )
    _add_output_options(p_status)
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser(
        "doctor",
        help="只读检查 runtime lock、Docker 与可选 provider pin",
    )
    p_doctor.add_argument(
        "--provider-path",
        type=Path,
        help="可选：待验证的真实 provider 文件绝对路径",
    )
    p_doctor.add_argument(
        "--provider-root",
        type=Path,
        action="append",
        default=[],
        help="provider 允许根目录；可重复，提供 --provider-path 时必需",
    )
    _add_output_options(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_plan = sub.add_parser(
        "plan",
        help="显示从本地 PoC 到生产放行的只读分阶段计划",
    )
    _add_output_options(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_claim = sub.add_parser(
        "claim",
        help="把 Dyro Core claim 缩减为受其到期时间约束的 Stage5 claim",
    )
    claim_sub = p_claim.add_subparsers(
        dest="claim_command",
        required=True,
    )
    p_claim_prepare = claim_sub.add_parser(
        "prepare",
        help="准备 Stage5 Supervisor 使用的 claim 文件",
    )
    p_claim_prepare.add_argument(
        "--core-claim",
        type=Path,
        required=True,
        help="通过安全通道取得的当前 Dyro Core claim.json",
    )
    p_claim_prepare.add_argument(
        "--output",
        type=Path,
        required=True,
        help="新 Stage5 claim 路径；拒绝覆盖",
    )
    _add_output_options(p_claim_prepare)
    p_claim_prepare.set_defaults(func=cmd_claim_prepare)

    p_handoff = sub.add_parser(
        "handoff",
        help="验证 Stage5 pack 并构建可导入的签名 Core execution bundle",
    )
    p_handoff.add_argument(
        "--root",
        type=Path,
        required=True,
        help="包含 dyro.toml 与任务契约的 runner-side Dyro 工作区",
    )
    p_handoff.add_argument("--task", required=True, help="Dyro 任务 ID")
    p_handoff.add_argument(
        "--pack",
        type=Path,
        required=True,
        help="Stage5 双重清理后封存的 evidence pack 目录",
    )
    p_handoff.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="任务分支多仓工作区；将在此运行 Core 声明的 gates",
    )
    p_handoff.add_argument(
        "--core-claim",
        type=Path,
        required=True,
        help="与 Stage5 claim 同源的当前 Dyro Core claim.json",
    )
    p_handoff.add_argument(
        "--output",
        type=Path,
        required=True,
        help="新 Core execution evidence ZIP；拒绝覆盖",
    )
    p_handoff.add_argument(
        "--signing-key",
        type=Path,
        required=True,
        help=(
            "0600 runner execution 私钥 PEM；必须位于 Profile、runner "
            "workspace 与 Stage5 pack 之外"
        ),
    )
    p_handoff.add_argument(
        "--key-id",
        required=True,
        help="Core claim 绑定的 trusted execution key ID",
    )
    _add_output_options(p_handoff)
    p_handoff.set_defaults(func=cmd_handoff)

    p_gate = sub.add_parser(
        "production-gate",
        help="执行生产门禁；NOT_READY 时返回退出码 3",
    )
    p_gate.add_argument(
        "--root",
        type=Path,
        help="包含用途隔离 trust store 的 Dyro 工作区根目录",
    )
    p_gate.add_argument(
        "--release-manifest",
        type=Path,
        help="production-release 签名且绑定当前版本/镜像/制品的发布清单",
    )
    p_gate.add_argument(
        "--security-attestation",
        type=Path,
        help="production-security 签名的 PROD-01 验收证明",
    )
    p_gate.add_argument(
        "--provider-attestation",
        type=Path,
        help="production-provider 签名的 PROD-02 验收证明",
    )
    p_gate.add_argument(
        "--quota-attestation",
        type=Path,
        help="production-quota 签名的 PROD-09 验收证明",
    )
    _add_output_options(p_gate)
    p_gate.set_defaults(func=cmd_production_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AssertionError, OSError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
