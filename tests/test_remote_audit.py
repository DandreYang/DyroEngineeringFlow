from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
from threading import Thread
import unittest

from dyro.audit_remote import (
    AUDIT_EXPORT_PURPOSE,
    AUDIT_KEY_TRANSITION_TYPE,
    AUDIT_RECOVERY_PURPOSE,
    AUDIT_RECEIPT_PURPOSE,
    AUDIT_RECEIPT_TYPE,
    GENESIS_HEAD,
    default_audit_workspace_id,
    sync_trust_audit,
    validate_audit_batch,
)
from dyro.canonical import canonical_json_bytes
from dyro.cli import build_parser
from dyro.errors import DyroError, ValidationError
from dyro.signing import (
    generate_keypair,
    revoke_public_key,
    sign_record,
    trust_public_key,
    trusted_keys_directory,
    verify_record,
)


class AuditWitness:
    def __init__(
        self,
        *,
        server_root: Path,
        client_key_id: str,
        witness: str,
        witness_key_id: str,
        witness_private_key: Path,
    ) -> None:
        self.server_root = server_root
        self.client_key_id = client_key_id
        self.witness = witness
        self.witness_key_id = witness_key_id
        self.receipt_key_epoch = 1
        self.recovery_key_id: str | None = None
        self.witness_private_key = witness_private_key
        self.sequence = 0
        self.head = "0" * 64
        self.requests = 0
        self.receipts: dict[str, dict[str, object]] = {}
        self.next_receipt_key: tuple[str, Path] | None = None
        self.rotation_authorizer: tuple[str, str, Path] | None = None
        self.drop_after_accept_once = False
        self.accepted_at: str | None = None

    def rotate_to(
        self,
        key_id: str,
        private_key: Path,
        *,
        authorizer: tuple[str, str, Path] | None = None,
    ) -> None:
        self.next_receipt_key = (key_id, private_key)
        self.rotation_authorizer = authorizer

    def handler(self) -> type[BaseHTTPRequestHandler]:
        witness = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                batch_hash = hashlib.sha256(body).hexdigest()
                witness.requests += 1
                if batch_hash in witness.receipts:
                    cached = witness.receipts[batch_hash]
                    if (
                        cached["to_sequence"] != witness.sequence
                        or cached["head_sha256"] != witness.head
                        or cached["witness_key_id"] != witness.witness_key_id
                        or cached["receipt_key_epoch"] != witness.receipt_key_epoch
                        or cached.get("recovery_key_id") != witness.recovery_key_id
                    ):
                        self._send(
                            409,
                            {
                                "code": "stale_checkpoint",
                                "error": "stale checkpoint",
                            },
                        )
                        return
                    self._send(200, cached)
                    return
                batch = json.loads(body)
                if body != canonical_json_bytes(batch):
                    self._send(400, {"error": "non-canonical batch"})
                    return
                verify_record(
                    batch,
                    purpose=AUDIT_EXPORT_PURPOSE,
                    trust_directory=trusted_keys_directory(
                        witness.server_root,
                        AUDIT_EXPORT_PURPOSE,
                    ),
                    required=True,
                )
                if batch["signature"]["key_id"] != witness.client_key_id:
                    self._send(409, {"error": "audit fork"})
                    return
                try:
                    sequence, head = validate_audit_batch(
                        batch,
                        workspace_id=str(batch["workspace_id"]),
                        witness=witness.witness,
                        previous_sequence=witness.sequence,
                        previous_head=witness.head,
                    )
                except ValidationError:
                    self._send(409, {"error": "audit fork"})
                    return
                requested_key_id = str(batch["requested_witness_key_id"])
                requested_epoch = int(batch["receipt_key_epoch"])
                expected_epoch = (
                    witness.receipt_key_epoch + 1
                    if requested_key_id != witness.witness_key_id
                    else witness.receipt_key_epoch
                )
                if requested_epoch != expected_epoch:
                    self._send(
                        409,
                        {"code": "key_epoch_mismatch", "error": "key epoch mismatch"},
                    )
                    return
                requested_recovery_key_id = batch.get("recovery_key_id")
                if (
                    witness.recovery_key_id is not None
                    and requested_recovery_key_id != witness.recovery_key_id
                ):
                    self._send(
                        409,
                        {
                            "code": "recovery_key_mismatch",
                            "error": "recovery key mismatch",
                        },
                    )
                    return
                receipt_private_key = witness.witness_private_key
                receipt_payload: dict[str, object] = {
                    "schema_version": 1,
                    "type": AUDIT_RECEIPT_TYPE,
                    "witness": witness.witness,
                    "workspace_id": batch["workspace_id"],
                    "from_sequence": batch["from_sequence"],
                    "to_sequence": sequence,
                    "head_sha256": head,
                    "batch_sha256": batch_hash,
                    "witness_key_id": requested_key_id,
                    "recovery_key_id": batch.get("recovery_key_id"),
                    "receipt_key_epoch": requested_epoch,
                    "accepted_at": witness.accepted_at or batch["requested_at"],
                }
                if requested_key_id != witness.witness_key_id:
                    if (
                        witness.next_receipt_key is None
                        or witness.next_receipt_key[0] != requested_key_id
                    ):
                        self._send(409, {"error": "unknown receipt key"})
                        return
                    authorizer = witness.rotation_authorizer or (
                        AUDIT_RECEIPT_PURPOSE,
                        witness.witness_key_id,
                        witness.witness_private_key,
                    )
                    receipt_payload["key_transition"] = sign_record(
                        {
                            "schema_version": 1,
                            "type": AUDIT_KEY_TRANSITION_TYPE,
                            "witness": witness.witness,
                            "workspace_id": batch["workspace_id"],
                            "sequence": sequence,
                            "head_sha256": head,
                            "batch_sha256": batch_hash,
                            "previous_key_id": witness.witness_key_id,
                            "next_key_id": requested_key_id,
                            "previous_receipt_key_epoch": witness.receipt_key_epoch,
                            "next_receipt_key_epoch": requested_epoch,
                        },
                        purpose=authorizer[0],
                        key_id=authorizer[1],
                        private_key=authorizer[2],
                    )
                    receipt_private_key = witness.next_receipt_key[1]
                receipt = sign_record(
                    receipt_payload,
                    purpose=AUDIT_RECEIPT_PURPOSE,
                    key_id=requested_key_id,
                    private_key=receipt_private_key,
                )
                witness.sequence = sequence
                witness.head = head
                witness.witness_key_id = requested_key_id
                witness.receipt_key_epoch = requested_epoch
                witness.recovery_key_id = (
                    str(requested_recovery_key_id)
                    if requested_recovery_key_id is not None
                    else None
                )
                witness.witness_private_key = receipt_private_key
                witness.next_receipt_key = None
                witness.rotation_authorizer = None
                witness.receipts[batch_hash] = receipt
                if witness.drop_after_accept_once:
                    witness.drop_after_accept_once = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self._send(201, receipt)

            def _send(self, status: int, payload: dict[str, object]) -> None:
                content = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


class RemoteAuditSyncTests(unittest.TestCase):
    def test_signed_incremental_sync_is_idempotent_and_detects_local_fork(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "client"
            server_root = Path(temporary) / "server"
            root.mkdir()
            server_root.mkdir()
            client_private = Path(temporary) / "client.pem"
            client_public = Path(temporary) / "client.pub.pem"
            witness_private = Path(temporary) / "witness.pem"
            witness_public = Path(temporary) / "witness.pub.pem"
            audited_private = Path(temporary) / "audited.pem"
            audited_public = Path(temporary) / "audited.pub.pem"
            generate_keypair(
                "client-export",
                private_key=client_private,
                public_key=client_public,
            )
            generate_keypair(
                "witness-key",
                private_key=witness_private,
                public_key=witness_public,
            )
            generate_keypair(
                "audited-key",
                private_key=audited_private,
                public_key=audited_public,
            )
            trust_public_key(
                server_root,
                "client-export",
                purpose=AUDIT_EXPORT_PURPOSE,
                source=client_public,
            )
            trust_public_key(
                root,
                "witness-key",
                purpose=AUDIT_RECEIPT_PURPOSE,
                source=witness_public,
            )
            trust_public_key(
                root,
                "audited-key",
                purpose="execution",
                source=audited_public,
            )

            witness = AuditWitness(
                server_root=server_root,
                client_key_id="client-export",
                witness="witness-1",
                witness_key_id="witness-key",
                witness_private_key=witness_private,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), witness.handler())
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/audit"
            try:
                for invalid_accepted_at in (
                    "invalid",
                    "2999-01-01T00:00:00+00:00",
                ):
                    witness.accepted_at = invalid_accepted_at
                    with self.assertRaisesRegex(ValidationError, "accepted_at"):
                        sync_trust_audit(
                            root,
                            workspace_id="workspace-1",
                            witness="witness-1",
                            endpoint=endpoint,
                            signing_key=client_private,
                            key_id="client-export",
                            witness_key_id="witness-key",
                            allow_insecure_http=True,
                        )
                    witness.sequence = 0
                    witness.head = GENESIS_HEAD
                    witness.receipts.clear()
                witness.accepted_at = None
                first = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-key",
                    allow_insecure_http=True,
                )
                self.assertTrue(first.synced)
                self.assertEqual(witness.requests, 3)

                repeated = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-key",
                    allow_insecure_http=True,
                )
                self.assertTrue(repeated.synced)
                self.assertEqual(repeated.batch["events"], [])
                self.assertEqual(witness.requests, 4)

                forged_head = deepcopy(first.batch)
                forged_head["head_sha256"] = "f" * 64
                with self.assertRaisesRegex(ValidationError, "重算结果"):
                    validate_audit_batch(
                        forged_head,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        previous_sequence=0,
                        previous_head=GENESIS_HEAD,
                    )

                forged_range = deepcopy(first.batch)
                forged_range["to_sequence"] = int(forged_range["to_sequence"]) + 1
                with self.assertRaisesRegex(ValidationError, "范围"):
                    validate_audit_batch(
                        forged_range,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        previous_sequence=0,
                        previous_head=GENESIS_HEAD,
                    )

                audit_path = root / ".dyro/trust/ed25519/audit.jsonl"
                lines = audit_path.read_text(encoding="utf-8").splitlines()
                first_event = json.loads(lines[0])
                first_event["key_id"] = "tampered"
                lines[0] = json.dumps(first_event, sort_keys=True)
                audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

                with self.assertRaisesRegex(ValidationError, "篡改或分叉"):
                    sync_trust_audit(
                        root,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        endpoint=endpoint,
                        signing_key=client_private,
                        key_id="client-export",
                        witness_key_id="witness-key",
                        allow_insecure_http=True,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_pending_batch_replays_after_lost_response_and_key_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "client"
            server_root = Path(temporary) / "server"
            root.mkdir()
            server_root.mkdir()
            client_private = Path(temporary) / "client.pem"
            client_public = Path(temporary) / "client.pub.pem"
            witness_private = Path(temporary) / "witness.pem"
            witness_public = Path(temporary) / "witness.pub.pem"
            next_private = Path(temporary) / "witness-next.pem"
            next_public = Path(temporary) / "witness-next.pub.pem"
            recovery_private = Path(temporary) / "witness-recovery.pem"
            recovery_public = Path(temporary) / "witness-recovery.pub.pem"
            audited_private = Path(temporary) / "audited.pem"
            audited_public = Path(temporary) / "audited.pub.pem"
            later_private = Path(temporary) / "later.pem"
            later_public = Path(temporary) / "later.pub.pem"
            for key_id, private_key, public_key in (
                ("client-export", client_private, client_public),
                ("witness-key", witness_private, witness_public),
                ("witness-next", next_private, next_public),
                ("witness-recovery", recovery_private, recovery_public),
                ("audited-key", audited_private, audited_public),
                ("later-key", later_private, later_public),
            ):
                generate_keypair(
                    key_id,
                    private_key=private_key,
                    public_key=public_key,
                )
            trust_public_key(
                server_root,
                "client-export",
                purpose=AUDIT_EXPORT_PURPOSE,
                source=client_public,
            )
            for key_id, public_key in (
                ("witness-key", witness_public),
                ("witness-next", next_public),
            ):
                trust_public_key(
                    root,
                    key_id,
                    purpose=AUDIT_RECEIPT_PURPOSE,
                    source=public_key,
                )
            trust_public_key(
                root,
                "witness-recovery",
                purpose=AUDIT_RECOVERY_PURPOSE,
                source=recovery_public,
            )
            trust_public_key(
                root,
                "audited-key",
                purpose="execution",
                source=audited_public,
            )
            witness = AuditWitness(
                server_root=server_root,
                client_key_id="client-export",
                witness="witness-1",
                witness_key_id="witness-key",
                witness_private_key=witness_private,
            )
            witness.accepted_at = (
                datetime.now(timezone.utc) + timedelta(minutes=4)
            ).isoformat(timespec="microseconds")
            witness.drop_after_accept_once = True
            server = ThreadingHTTPServer(("127.0.0.1", 0), witness.handler())
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/audit"
            try:
                with self.assertRaises(DyroError):
                    sync_trust_audit(
                        root,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        endpoint=endpoint,
                        signing_key=client_private,
                        key_id="client-export",
                        witness_key_id="witness-key",
                        recovery_key_id="witness-recovery",
                        allow_insecure_http=True,
                    )
                state_path = root / ".dyro/audit-witnesses/witness-1.json"
                pending_snapshot = state_path.read_bytes()
                pending_state = json.loads(pending_snapshot)
                pending_hash = hashlib.sha256(
                    canonical_json_bytes(pending_state["pending"])
                ).hexdigest()
                trust_public_key(
                    root,
                    "later-key",
                    purpose="execution",
                    source=later_public,
                )
                recovered = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-key",
                    recovery_key_id="witness-recovery",
                    allow_insecure_http=True,
                )
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(recovered.batch)).hexdigest(),
                    pending_hash,
                )

                advanced = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-key",
                    recovery_key_id="witness-recovery",
                    allow_insecure_http=True,
                )
                current_snapshot = state_path.read_bytes()
                state_path.write_bytes(pending_snapshot)
                with self.assertRaisesRegex(DyroError, "stale_checkpoint"):
                    sync_trust_audit(
                        root,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        endpoint=endpoint,
                        signing_key=client_private,
                        key_id="client-export",
                        witness_key_id="witness-key",
                        recovery_key_id="witness-recovery",
                        allow_insecure_http=True,
                    )
                state_path.write_bytes(current_snapshot)
                self.assertTrue(advanced.synced)

                revoke_public_key(
                    root,
                    "witness-key",
                    purpose=AUDIT_RECEIPT_PURPOSE,
                    reason="receipt key compromised",
                )
                witness.rotate_to(
                    "witness-next",
                    next_private,
                    authorizer=(
                        AUDIT_RECOVERY_PURPOSE,
                        "witness-recovery",
                        recovery_private,
                    ),
                )
                rotated = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-next",
                    recovery_key_id="witness-recovery",
                    allow_insecure_http=True,
                )
                self.assertEqual(
                    rotated.receipt["signature"]["key_id"],
                    "witness-next",
                )
                self.assertEqual(rotated.receipt["receipt_key_epoch"], 2)
                final = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=endpoint,
                    signing_key=client_private,
                    key_id="client-export",
                    witness_key_id="witness-next",
                    recovery_key_id="witness-recovery",
                    allow_insecure_http=True,
                )
                self.assertTrue(final.synced)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_global_dry_run_and_workspace_id_fallback(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--dry-run",
                "key",
                "audit-sync",
                "--witness",
                "witness-1",
                "--endpoint",
                "https://audit.example.test",
                "--signing-key",
                "/secure/client.pem",
                "--key-id",
                "client",
                "--witness-key-id",
                "witness-key",
            ]
        )
        self.assertTrue(args.dry_run)
        self.assertRegex(
            default_audit_workspace_id("中文 workspace"),
            r"^workspace-[0-9a-f]{24}$",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValidationError, "有限正数"):
                sync_trust_audit(
                    Path(temporary),
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint="https://audit.example.test",
                    signing_key=Path(temporary) / "missing.pem",
                    key_id="client",
                    witness_key_id="witness-key",
                    timeout_seconds=0,
                    dry_run=True,
                )

    def test_redirect_is_rejected_without_forwarding_authorization(self) -> None:
        received_authorization: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_thread = Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        target = f"http://127.0.0.1:{sink.server_port}/stolen"

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                private_key = root / "client.pem"
                public_key = root / "client.pub.pem"
                generate_keypair(
                    "client",
                    private_key=private_key,
                    public_key=public_key,
                )
                dry_run = sync_trust_audit(
                    root,
                    workspace_id="workspace-1",
                    witness="witness-1",
                    endpoint=(
                        f"http://127.0.0.1:{redirect.server_port}/redirect"
                    ),
                    signing_key=private_key,
                    key_id="client",
                    witness_key_id="witness-key",
                    token="secret",
                    allow_insecure_http=True,
                    dry_run=True,
                )
                self.assertFalse(dry_run.synced)
                self.assertFalse(
                    (root / ".dyro/audit-witnesses/witness-1.json").exists()
                )
                self.assertEqual(received_authorization, [])
                with self.assertRaisesRegex(DyroError, "HTTP 302"):
                    sync_trust_audit(
                        root,
                        workspace_id="workspace-1",
                        witness="witness-1",
                        endpoint=(
                            f"http://127.0.0.1:{redirect.server_port}/redirect"
                        ),
                        signing_key=private_key,
                        key_id="client",
                        witness_key_id="witness-key",
                        token="secret",
                        allow_insecure_http=True,
                    )
                self.assertEqual(received_authorization, [])
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=5)
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=5)
