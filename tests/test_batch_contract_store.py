from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from experiments.local_agent_dispatch.batch_contract import (
    BatchMemberPlan,
    BatchPlan,
    batch_plan_sha256,
    effects_for_members,
    parse_batch_request,
)
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.orchestration_store import (
    MAX_MANIFEST_BYTES,
    OrchestrationStore,
    orchestration_id_for,
    run_id_for,
)


def _contract(*, backend: str = "codex", mode: str = "read-only") -> dict:
    return {
        "schema_version": 1,
        "backend": backend,
        "mode": mode,
        "strict": False,
        "allow_unconfined_provider": True,
        "allow_offline_simulation": False,
        "files": ["src/*.py"],
        "task": {
            "briefing": "small project",
            "locations": "src/",
            "objective": "review the implementation",
            "constraints": "do not mutate unrelated files",
            "output_contract": "summary and evidence",
        },
    }


def _request(*, request_id: str = "batch-001") -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "strategy": "independent",
        "members": [
            {
                "role_id": "reviewer",
                "timeout_seconds": 60,
                "contract": _contract(),
            },
            {
                "role_id": "challenger",
                "timeout_seconds": 90,
                "contract": _contract(backend="claude"),
            },
        ],
    }


def _plan(project_root: Path, *, request_id: str = "batch-001") -> BatchPlan:
    request = parse_batch_request(_request(request_id=request_id))
    members = tuple(
        BatchMemberPlan(
            role_id=member.role_id,
            resolved_backend=member.contract.backend,
            context_file_count=1,
            context_sha256=("a" if index == 0 else "b") * 64,
            base_head=None,
            execution_profile={
                "backend": member.contract.backend,
                "command_path": member.contract.backend,
            },
            timeout_seconds=member.timeout_seconds,
            normalized_contract=member.contract.to_mapping(),
        )
        for index, member in enumerate(request.members)
    )
    return BatchPlan(
        project_root=project_root,
        request_id=request.request_id,
        strategy=request.strategy,
        effects=effects_for_members(members),
        members=members,
    )


class BatchContractTests(unittest.TestCase):
    def test_strict_schema_unknown_fields_and_complete_contract(self) -> None:
        valid = _request()
        self.assertEqual(parse_batch_request(valid).to_mapping(), valid)
        for mutation in ("schema", "top_unknown", "member_unknown", "task_unknown"):
            with self.subTest(mutation=mutation):
                payload = _request()
                if mutation == "schema":
                    payload["schema_version"] = 2
                elif mutation == "top_unknown":
                    payload["surprise"] = True
                elif mutation == "member_unknown":
                    payload["members"][0]["surprise"] = True
                else:
                    payload["members"][0]["contract"]["task"]["surprise"] = True
                with self.assertRaises(DispatchValidationError):
                    parse_batch_request(payload)

        incomplete = _request()
        del incomplete["members"][0]["contract"]["strict"]
        with self.assertRaisesRegex(DispatchValidationError, "missing required"):
            parse_batch_request(incomplete)

    def test_rejects_unsafe_roles_timeouts_multiple_edits_and_simulation(self) -> None:
        cases = []
        unsafe = _request()
        unsafe["members"][0]["role_id"] = "../reviewer"
        cases.append(unsafe)
        duplicate = _request()
        duplicate["members"][1]["role_id"] = "reviewer"
        cases.append(duplicate)
        timeout = _request()
        timeout["members"][0]["timeout_seconds"] = math.inf
        cases.append(timeout)
        too_slow = _request()
        too_slow["members"][0]["timeout_seconds"] = 3601
        cases.append(too_slow)
        edits = _request()
        edits["members"][0]["contract"]["mode"] = "edit"
        edits["members"][1]["contract"]["mode"] = "edit"
        cases.append(edits)
        echo = _request()
        echo["members"][0]["contract"]["backend"] = "echo"
        cases.append(echo)
        offline = _request()
        offline["members"][0]["contract"]["allow_offline_simulation"] = True
        cases.append(offline)
        for index, payload in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(
                DispatchValidationError
            ):
                parse_batch_request(payload)

    def test_rejects_batch_request_that_cannot_fit_persisted_manifest(self) -> None:
        payload = _request()
        for member in payload["members"]:
            for field in member["contract"]["task"]:
                member["contract"]["task"][field] = "x" * 150_000
        with self.assertRaisesRegex(DispatchValidationError, "batch request exceeds"):
            parse_batch_request(payload)

    def test_plan_digest_is_canonical_and_excludes_manifest_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _plan(root / "child" / "..")
            reordered_effects = dict(reversed(list(first.effects.items())))
            second = replace(first, effects=reordered_effects)
            self.assertEqual(first.project_root, str(root.resolve()))
            self.assertEqual(batch_plan_sha256(first), batch_plan_sha256(second))
            self.assertEqual(first.plan_sha256, batch_plan_sha256(first))
            self.assertEqual(
                first.to_mapping()["kind"], "local-agent-dispatch-batch-plan"
            )
            self.assertNotIn("plan_sha256", first.to_canonical_mapping())

    def test_plan_base_head_tracks_edit_effect_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            read_only = _plan(Path(tmp))
            with self.assertRaisesRegex(DispatchValidationError, "read-only.*null"):
                replace(read_only.members[0], base_head="c" * 40)

            edit_contract = _contract(mode="edit")
            with self.assertRaisesRegex(DispatchValidationError, "edit.*Git hash"):
                replace(
                    read_only.members[0],
                    normalized_contract=edit_contract,
                    base_head=None,
                )
            edited = replace(
                read_only.members[0],
                normalized_contract=edit_contract,
                base_head="c" * 40,
            )
            self.assertEqual(edited.base_head, "c" * 40)


class OrchestrationStoreTests(unittest.TestCase):
    def test_create_load_is_idempotent_and_rejects_request_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            plan = _plan(Path(tmp) / "project")
            store = OrchestrationStore(home)
            first = store.create_or_load(plan)
            second = store.create_or_load(plan)
            self.assertEqual(first.to_mapping(), second.to_mapping())
            self.assertEqual(
                first.orchestration_id,
                orchestration_id_for(plan.request_id, batch_plan_sha256(plan)),
            )
            self.assertEqual(
                [member.run_id for member in first.members],
                [run_id_for(first.orchestration_id, 0), run_id_for(first.orchestration_id, 1)],
            )

            changed_member = replace(
                plan.members[0], context_sha256="d" * 64
            )
            changed_members = (changed_member, plan.members[1])
            changed = replace(
                plan,
                members=changed_members,
                effects=effects_for_members(changed_members),
            )
            with self.assertRaisesRegex(DispatchValidationError, "different plan"):
                store.create_or_load(changed)

    def test_load_rejects_symlink_oversize_and_corrupt_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store = OrchestrationStore(home)
            plan = _plan(Path(tmp) / "project")
            manifest = store.create_or_load(plan)
            path = store.root / f"{manifest.orchestration_id}.json"

            good = path.read_bytes()
            path.unlink()
            target = Path(tmp) / "target.json"
            target.write_bytes(good)
            os.symlink(target, path)
            with self.assertRaisesRegex(DispatchValidationError, "symbolic link"):
                store.load(manifest.orchestration_id)

            path.unlink()
            path.write_bytes(b"{" + (b" " * MAX_MANIFEST_BYTES) + b"}")
            with self.assertRaisesRegex(DispatchValidationError, "exceeds"):
                store.load(manifest.orchestration_id)

            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(DispatchValidationError, "corrupt"):
                store.load(manifest.orchestration_id)

            if os.name == "posix" and hasattr(os, "mkfifo"):
                path.unlink()
                os.mkfifo(path)
                errors: list[Exception] = []

                def load_fifo() -> None:
                    try:
                        store.load(manifest.orchestration_id)
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        errors.append(exc)

                import threading

                reader = threading.Thread(target=load_fifo)
                reader.start()
                reader.join(timeout=0.5)
                was_blocked = reader.is_alive()
                if was_blocked:
                    descriptor = os.open(
                        path,
                        os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
                    )
                    os.close(descriptor)
                    reader.join(timeout=1.0)
                self.assertFalse(
                    was_blocked,
                    "orchestration manifest reader blocked on FIFO",
                )
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIn("regular file", str(errors[0]))

    def test_load_rejects_tampering_and_cancel_is_cas_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store = OrchestrationStore(home)
            manifest = store.create_or_load(_plan(Path(tmp) / "project"))
            with self.assertRaisesRegex(DispatchValidationError, "revision conflict"):
                store.request_cancel(
                    manifest.orchestration_id, expected_revision=manifest.revision + 1
                )
            cancelled = store.request_cancel(
                manifest.orchestration_id, expected_revision=manifest.revision
            )
            self.assertTrue(cancelled.cancel_requested)
            self.assertEqual(cancelled.revision, manifest.revision + 1)
            repeated = store.request_cancel(
                manifest.orchestration_id, expected_revision=manifest.revision
            )
            self.assertEqual(cancelled.to_mapping(), repeated.to_mapping())

            path = store.root / f"{manifest.orchestration_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["members"][0]["run_id"] = "run-0000000000000000"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(DispatchValidationError, "run_id"):
                store.load(manifest.orchestration_id)


if __name__ == "__main__":
    unittest.main()
