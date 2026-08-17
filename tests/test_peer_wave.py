from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from dyro.errors import ValidationError
from dyro.peer_wave import (
    AUTO_EXECUTOR,
    bind_wave_executors,
    empty_conflict_group_warnings,
    recommended_max_parallel,
    write_capable_dispatch_ids,
)
from dyro.task_dispatch import build_bound_contract, run_task_bound_dispatch
from dyro.tasks import SchedulePlan, Task, select_task_wave


class PeerWaveTests(unittest.TestCase):
    def test_recommended_parallel_keeps_request_when_no_ready_providers(self) -> None:
        self.assertEqual(recommended_max_parallel(3, 0), 3)
        self.assertEqual(recommended_max_parallel(3, 1), 1)
        self.assertEqual(recommended_max_parallel(3, 9), 3)

    def test_empty_conflict_group_warns_only_in_parallel_waves(self) -> None:
        tasks = (self._task("A"), self._task("B", conflict_group="api"))
        self.assertEqual(empty_conflict_group_warnings(tasks, max_parallel=1), ())
        warnings = empty_conflict_group_warnings(tasks, max_parallel=3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("A", warnings[0])
        self.assertNotIn("B", warnings[0])

    def test_select_wave_allows_distinct_conflict_groups_and_blocks_same_group(
        self,
    ) -> None:
        ready = (
            self._task("API-1", conflict_group="api"),
            self._task("WEB-1", conflict_group="web"),
            self._task("API-2", conflict_group="api"),
        )
        wave = select_task_wave(SchedulePlan(ready=ready, blocked=()), limit=3)
        self.assertEqual([task.id for task in wave.tasks], ["API-1", "WEB-1"])
        self.assertEqual(wave.deferred[0].task.id, "API-2")
        self.assertIn("api", wave.deferred[0].reason)

    def test_bind_wave_assigns_auto_and_caps_ready_backends(self) -> None:
        tasks = (
            self._task("T-A", executor=AUTO_EXECUTOR),
            self._task("T-B", executor=AUTO_EXECUTOR),
            self._task("T-C", executor=AUTO_EXECUTOR),
        )
        decision = bind_wave_executors(tasks, ("claude", "kimi"), max_per_backend=1)
        self.assertEqual(
            [item.executor for item in decision.bindings], ["claude", "kimi"]
        )
        self.assertEqual(decision.deferred[0].task.id, "T-C")
        self.assertFalse(decision.bindings[0].pinned)

    def test_bind_wave_rejects_cursor_write_and_keeps_unready_pin_on_profile(
        self,
    ) -> None:
        tasks = (
            self._task("T-CUR", executor="cursor-agent"),
            self._task("T-PIN", executor="codex"),
        )
        decision = bind_wave_executors(tasks, ())
        self.assertEqual(decision.deferred[0].task.id, "T-CUR")
        self.assertEqual(decision.bindings[0].task_id, "T-PIN")
        self.assertEqual(decision.bindings[0].source, "profile")

    def test_cursor_cannot_run_bound_write_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
            task = self._task("T-CUR", executor="cursor-agent")
            with self.assertRaisesRegex(ValidationError, "写波次"):
                run_task_bound_dispatch(
                    task,
                    executor="cursor-agent",
                    workspace=workspace,
                    prompt="do not write",
                    timeout_seconds=1.0,
                    capabilities={},
                )

    def test_echo_bound_dispatch_writes_in_given_worktree_not_detached_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
            task = self._task("T-ECHO", executor="echo")
            result = run_task_bound_dispatch(
                task,
                executor="echo",
                workspace=workspace,
                prompt="result: DONE",
                timeout_seconds=1.0,
                capabilities={},
            )
            self.assertEqual(result.code, 0)
            self.assertIn("echo-adapter", result.stdout)
            self.assertFalse(any(workspace.rglob("changes.patch")))
            self.assertTrue((workspace / "module.py").is_file())

    def test_write_capable_ids_exclude_cursor(self) -> None:
        self.assertIn("codex", write_capable_dispatch_ids())
        self.assertNotIn("cursor-agent", write_capable_dispatch_ids())

    def test_observe_only_card_is_not_bound_into_write_wave(self) -> None:
        cards = {"codex": SimpleNamespace(intents=("observe",))}
        decision = bind_wave_executors(
            (self._task("T-OBS", executor="codex"),),
            ("codex",),
            capabilities=cards,
        )
        self.assertEqual(decision.bindings, ())
        self.assertEqual(decision.deferred[0].task.id, "T-OBS")
        self.assertIn("未授予 execute", decision.deferred[0].reason)

    def test_auto_pool_skips_observe_only_ready_provider(self) -> None:
        cards = {"codex": SimpleNamespace(intents=("observe",))}
        decision = bind_wave_executors(
            (self._task("T-AUTO", executor=AUTO_EXECUTOR),),
            ("codex", "claude"),
            capabilities=cards,
        )
        self.assertEqual(decision.bindings[0].executor, "claude")
        self.assertEqual(decision.deferred, ())

    def test_bound_contract_does_not_silently_unconfine_real_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
            contract = build_bound_contract(
                self._task("T-CONFINE", executor="codex"),
                executor="codex",
                workspace=workspace,
                prompt="do not write",
            )
            self.assertFalse(contract.allow_unconfined_provider)

    @staticmethod
    def _task(
        task_id: str, *, conflict_group: str = "", executor: str = "codex"
    ) -> Task:
        return Task(
            id=task_id,
            title=task_id,
            line="alpha",
            risk="write",
            executor=executor,
            reviewer="reviewer",
            repositories=("api",),
            conflict_group=conflict_group,
            directory=Path("/tmp") / task_id,
        )


if __name__ == "__main__":
    unittest.main()
