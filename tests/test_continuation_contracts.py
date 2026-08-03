from __future__ import annotations

import unittest

from dyro.continuation import (
    ActionKind,
    BudgetLimit,
    CompletionRule,
    ContinuationPlan,
    Objective,
    Operation,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
    RequestedMode,
    canonical_contract,
    contract_sha256,
    parse_contract,
    validate_objective_scope,
)
from dyro.errors import ValidationError


MINIMAL = '''schema_version = 1
id = "release-readiness"
title = "Release readiness"
line = "release-2026"
targets = ["API-101"]
'''


class ObjectiveContractTests(unittest.TestCase):
    def test_parses_minimal_contract_with_safe_defaults(self) -> None:
        objective = parse_contract(MINIMAL)

        self.assertEqual(objective.id, "release-readiness")
        self.assertEqual(objective.completion, CompletionRule.ALL_TARGETS_INTEGRATED)
        self.assertEqual(objective.requested_mode, RequestedMode.SUPERVISED)
        self.assertEqual(objective.operations, (Operation.EXECUTE, Operation.REVIEW))
        self.assertEqual(objective.budget.max_parallel, 1)
        self.assertEqual(objective.budget.max_attempts_per_task, 2)

    def test_parses_complete_contract(self) -> None:
        objective = parse_contract(
            MINIMAL
            + '''completion = "all_targets_integrated"

[continuation]
requested_mode = "automatic"
operations = ["review", "execute", "merge"]

[budget]
max_actions = 9
max_attempts_per_task = 4
max_failures = 2
max_no_progress_cycles = 3
max_parallel = 2
deadline = "2026-10-02T12:00:00Z"
'''
        )

        self.assertEqual(objective.requested_mode, RequestedMode.AUTOMATIC)
        self.assertEqual(objective.operations, (Operation.REVIEW, Operation.EXECUTE, Operation.MERGE))
        self.assertEqual(objective.budget.max_actions, 9)
        self.assertEqual(objective.budget.deadline.isoformat(), "2026-10-02T12:00:00+00:00")

    def test_rejects_unknown_fields_at_every_contract_level(self) -> None:
        cases = (
            (MINIMAL + "unknown = true\n", "未知字段"),
            (MINIMAL + "\n[continuation]\nunknown = true\n", "未知字段"),
            (MINIMAL + "\n[budget]\nunknown = true\n", "未知字段"),
        )
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValidationError, message):
                parse_contract(content)

    def test_rejects_empty_duplicate_or_invalid_targets(self) -> None:
        cases = (
            (MINIMAL.replace('["API-101"]', "[]"), "显式 Task ID"),
            (MINIMAL.replace('["API-101"]', '["API-101", "API-101"]'), "不能重复"),
            (MINIMAL.replace('["API-101"]', '["../API-101"]'), "Objective target"),
        )
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValidationError, message):
                parse_contract(content)

    def test_rejects_unsafe_ids_and_type_boundaries(self) -> None:
        cases = (
            (MINIMAL.replace('id = "release-readiness"', 'id = "../escape"'), "Objective ID"),
            (MINIMAL.replace('line = "release-2026"', 'line = "release line"'), "Objective line"),
            (MINIMAL.replace('title = "Release readiness"', 'title = ""'), "非空字符串"),
            (MINIMAL.replace('targets = ["API-101"]', 'targets = "API-101"'), "显式 Task ID"),
            (MINIMAL.replace("schema_version = 1", 'schema_version = "1"'), "schema_version"),
            (MINIMAL.replace("schema_version = 1", "schema_version = true"), "schema_version"),
        )
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValidationError, message):
                parse_contract(content)

    def test_rejects_unknown_operations_and_non_finite_budget_values(self) -> None:
        cases = (
            (MINIMAL + "\n[continuation]\noperations = [\"publish\"]\n", "未知 operation"),
            (MINIMAL + "\n[budget]\nmax_actions = 0\n", "有限整数"),
            (MINIMAL + "\n[budget]\nmax_actions = 1e999\n", "有限整数"),
            (MINIMAL + "\n[budget]\ndeadline = \"2026-10-02T12:00:00\"\n", "时区"),
        )
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValidationError, message):
                parse_contract(content)

    def test_canonical_contract_hash_ignores_toml_and_set_order(self) -> None:
        first = parse_contract(
            '''schema_version = 1
id = "objective-a"
title = "Objective A"
line = "alpha"
targets = ["TASK-2", "TASK-1"]

[continuation]
operations = ["review", "execute"]
'''
        )
        second = parse_contract(
            '''title = "Objective A"
targets = ["TASK-1", "TASK-2"]
line = "alpha"
id = "objective-a"
schema_version = 1

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]
'''
        )

        self.assertEqual(canonical_contract(first), canonical_contract(second))
        self.assertEqual(contract_sha256(first), contract_sha256(second))

    def test_canonical_contract_hash_changes_when_semantics_change(self) -> None:
        baseline = parse_contract(MINIMAL)
        changed = parse_contract(MINIMAL.replace('title = "Release readiness"', 'title = "Different readiness"'))

        self.assertNotEqual(contract_sha256(baseline), contract_sha256(changed))

    def test_scope_validation_accepts_one_line_and_rejects_cross_line_targets(self) -> None:
        objective = parse_contract(
            MINIMAL,
            task_lines={"API-101": "release-2026"},
        )
        validate_objective_scope(objective, {"API-101": "release-2026"})

        with self.assertRaisesRegex(ValidationError, "不能跨 line"):
            parse_contract(MINIMAL, task_lines={"API-101": "other-line"})
        with self.assertRaisesRegex(ValidationError, "缺少 TaskGraph"):
            validate_objective_scope(objective, {})

    def test_public_models_copy_mutable_collections_and_express_all_plan_states(self) -> None:
        targets = ["API-101"]
        operations = [Operation.EXECUTE]
        objective = Objective(
            schema_version=1,
            id="objective-a",
            title="Objective A",
            line="release-2026",
            targets=targets,
            completion=CompletionRule.ALL_TARGETS_INTEGRATED,
            requested_mode=RequestedMode.SUPERVISED,
            operations=operations,
            budget=BudgetLimit(1, 1, 1, 1, 1),
        )
        action_facts = [["source", "planner"]]
        action = PlannedAction(ActionKind.WAIT, "objective-a", ReasonCode.TARGETS_INTEGRATED, action_facts)
        selected_actions = [action]
        plan = ContinuationPlan(
            "objective-a",
            "snapshot-sha",
            "plan-sha",
            PlanCompletion.INCOMPLETE,
            selected_actions=selected_actions,
        )
        targets.append("API-102")
        operations.append(Operation.REVIEW)
        action_facts[0][1] = "mutated"
        selected_actions.append(action)

        self.assertEqual(objective.targets, ("API-101",))
        self.assertEqual(objective.operations, (Operation.EXECUTE,))
        self.assertEqual(action.facts, (("source", "planner"),))
        self.assertEqual(plan.selected_actions, (action,))
        self.assertEqual(
            set(ActionKind),
            {
                ActionKind.EXECUTE_TASK,
                ActionKind.REVIEW_TASK,
                ActionKind.MERGE_TASK,
                ActionKind.PROBE_TRIGGER,
                ActionKind.WAIT,
                ActionKind.ASK_USER,
                ActionKind.PAUSE,
                ActionKind.COMPLETE,
                ActionKind.REPAIR_REQUIRED,
            },
        )


if __name__ == "__main__":
    unittest.main()
