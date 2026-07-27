from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import ssl
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dyro.audit_remote import (
    AUDIT_EXPORT_PURPOSE,
    AUDIT_RECEIPT_PURPOSE,
    GENESIS_HEAD,
    sync_trust_audit,
)
from dyro.canonical import canonical_json_bytes
from dyro.errors import ValidationError
from dyro.signing import generate_keypair, sign_record, trust_public_key
from dyro.cli import main
from dyro.witness import (
    WITNESS_PATH,
    WitnessConfig,
    WitnessRequestError,
    WitnessStore,
    create_witness_http_server,
)


def _head(
    event: dict[str, object],
    *,
    previous_head: str = GENESIS_HEAD,
    sequence: int = 1,
) -> str:
    return hashlib.sha256(
        bytes.fromhex(previous_head)
        + canonical_json_bytes({"sequence": sequence, "event": event})
    ).hexdigest()


def _batch(
    *,
    private_key: Path,
    witness_key_id: str,
    receipt_key_epoch: int,
    event: dict[str, object] | None,
    workspace_id: str = "workspace-1",
    previous_sequence: int = 0,
    previous_head: str = GENESIS_HEAD,
) -> dict[str, object]:
    events = [] if event is None else [{"sequence": previous_sequence + 1, "event": event}]
    head = (
        previous_head
        if event is None
        else _head(
            event,
            previous_head=previous_head,
            sequence=previous_sequence + 1,
        )
    )
    return sign_record(
        {
            "schema_version": 1,
            "type": "dyro.audit.batch",
            "workspace_id": workspace_id,
            "witness": "witness-1",
            "endpoint": "https://audit.example.test/v1/dyro/batches",
            "request_id": hashlib.sha256(canonical_json_bytes(events)).hexdigest()[:32],
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "requested_witness_key_id": witness_key_id,
            "recovery_key_id": None,
            "receipt_key_epoch": receipt_key_epoch,
            "from_sequence": previous_sequence + 1,
            "to_sequence": previous_sequence + len(events),
            "previous_head_sha256": previous_head,
            "head_sha256": head,
            "events": events,
        },
        purpose=AUDIT_EXPORT_PURPOSE,
        key_id="client-1",
        private_key=private_key,
    )


def _write_tls_certificate(root: Path) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = root / "tls.crt"
    private_key_path = root / "tls.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, private_key_path


class WitnessStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client_private = self.root / "client.pem"
        self.client_public = self.root / "client.pub.pem"
        self.receipt_private = self.root / "receipt.pem"
        self.receipt_public = self.root / "receipt.pub.pem"
        generate_keypair("client-1", private_key=self.client_private, public_key=self.client_public)
        generate_keypair("receipt-1", private_key=self.receipt_private, public_key=self.receipt_public)
        trust_root = self.root / "trust"
        trust_public_key(
            trust_root,
            "client-1",
            purpose=AUDIT_EXPORT_PURPOSE,
            source=self.client_public,
        )
        self.config = WitnessConfig(
            storage_root=self.root / "ledger",
            client_trust_root=trust_root,
            witness_id="witness-1",
            receipt_key_id="receipt-1",
            receipt_signing_key=self.receipt_private,
            workspace_id="workspace-1",
            expected_endpoint="https://audit.example.test/v1/dyro/batches",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_idempotent_batch_and_rejects_stale_replay(self) -> None:
        store = WitnessStore(self.config)
        first_batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        first_hash = hashlib.sha256(canonical_json_bytes(first_batch)).hexdigest()
        first = store.accept(first_batch, idempotency_key=first_hash)
        self.assertTrue(first.created)
        self.assertEqual(first.receipt["to_sequence"], 1)
        repeated = store.accept(first_batch, idempotency_key=first_hash)
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.receipt, first.receipt)
        (self.root / "ledger/workspaces/workspace-1/witness-1/checkpoint.json").unlink()
        recovered = store.accept(first_batch, idempotency_key=first_hash)
        self.assertFalse(recovered.created)
        self.assertEqual(recovered.receipt, first.receipt)

        second_event = {"event": "trust", "key_id": "runner-2"}
        second_batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event=second_event,
            previous_sequence=1,
            previous_head=str(first_batch["head_sha256"]),
        )
        second_hash = hashlib.sha256(canonical_json_bytes(second_batch)).hexdigest()
        self.assertTrue(store.accept(second_batch, idempotency_key=second_hash).created)
        with self.assertRaises(WitnessRequestError) as stale:
            store.accept(first_batch, idempotency_key=first_hash)
        self.assertEqual(stale.exception.code, "audit_fork")

    def test_http_endpoint_requires_token_and_returns_signed_receipt(self) -> None:
        config = WitnessConfig(
            **{**self.config.__dict__, "auth_token": "test-token"}
        )
        server = create_witness_http_server(config, host="127.0.0.1", port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            batch = _batch(
                private_key=self.client_private,
                witness_key_id="receipt-1",
                receipt_key_epoch=1,
                event={"event": "trust", "key_id": "runner-1"},
            )
            body = canonical_json_bytes(batch)
            request = Request(
                f"http://127.0.0.1:{server.server_port}{WITNESS_PATH}",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Idempotency-Key": hashlib.sha256(body).hexdigest()},
            )
            with self.assertRaises(HTTPError) as rejected:
                urlopen(request)
            self.assertEqual(rejected.exception.code, 401)
            rejected.exception.close()
            request.add_header("Authorization", "Bearer test-token")
            with urlopen(request) as response:
                self.assertEqual(response.status, 201)
                receipt = json.loads(response.read())
            self.assertEqual(receipt["type"], "dyro.audit.receipt")
            self.assertEqual(receipt["signature"]["purpose"], AUDIT_RECEIPT_PURPOSE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rejects_idempotency_key_mismatch_before_persisting(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        with self.assertRaisesRegex(WitnessRequestError, "Idempotency-Key"):
            store.accept(batch, idempotency_key="0" * 64)

    def test_rejects_client_batch_for_another_workspace(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        unsigned = dict(batch)
        unsigned.pop("signature")
        unsigned["workspace_id"] = "workspace-2"
        other_workspace = sign_record(
            unsigned,
            purpose=AUDIT_EXPORT_PURPOSE,
            key_id="client-1",
            private_key=self.client_private,
        )
        with self.assertRaises(WitnessRequestError) as rejected:
            store.accept(
                other_workspace,
                idempotency_key=hashlib.sha256(canonical_json_bytes(other_workspace)).hexdigest(),
            )
        self.assertEqual(rejected.exception.code, "wrong_workspace")
        self.assertFalse((self.root / "ledger/workspaces/workspace-2").exists())

    def test_shared_workspace_bindings_accept_only_the_bound_workspace(self) -> None:
        config = WitnessConfig(
            **{
                **self.config.__dict__,
                "workspace_id": None,
                "client_workspace_bindings": {"client-1": "workspace-1"},
            }
        )
        store = WitnessStore(config)
        accepted = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        self.assertTrue(
            store.accept(
                accepted,
                idempotency_key=hashlib.sha256(canonical_json_bytes(accepted)).hexdigest(),
            ).created
        )
        rejected_batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-2"},
            workspace_id="workspace-2",
        )
        with self.assertRaises(WitnessRequestError) as rejected:
            store.accept(
                rejected_batch,
                idempotency_key=hashlib.sha256(canonical_json_bytes(rejected_batch)).hexdigest(),
            )
        self.assertEqual(rejected.exception.code, "unauthorized_client_workspace")
        self.assertFalse((self.root / "ledger/workspaces/workspace-2").exists())

    def test_directory_sync_failure_does_not_advance_checkpoint(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        with patch("dyro.witness._fsync_directory", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                store.accept(
                    batch,
                    idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
                )
        self.assertFalse(
            (self.root / "ledger/workspaces/workspace-1/witness-1/checkpoint.json").exists()
        )

    def test_recovery_resyncs_existing_record_before_checkpoint(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        record_directory = store._record_path("workspace-1", "placeholder").parent
        original_fsync = __import__("dyro.witness", fromlist=["_fsync_directory"])._fsync_directory

        def fail_record_directory(path: Path) -> None:
            if path == record_directory:
                raise OSError("record directory sync failure")
            original_fsync(path)

        with patch("dyro.witness._fsync_directory", side_effect=fail_record_directory):
            with self.assertRaisesRegex(OSError, "record directory sync failure"):
                store.accept(
                    batch,
                    idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
                )
        self.assertTrue(store._record_path("workspace-1", hashlib.sha256(canonical_json_bytes(batch)).hexdigest()).exists())
        self.assertFalse(
            (self.root / "ledger/workspaces/workspace-1/witness-1/checkpoint.json").exists()
        )
        recovered = store.accept(
            batch,
            idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
        )
        self.assertFalse(recovered.created)

    def test_cached_receipt_resyncs_checkpoint_after_sync_failure(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        checkpoint_path = store._state_path("workspace-1")
        original_fsync = __import__("dyro.witness", fromlist=["_fsync_directory"])._fsync_directory

        def fail_checkpoint_directory(path: Path) -> None:
            if path == checkpoint_path.parent and checkpoint_path.exists():
                raise OSError("checkpoint directory sync failure")
            original_fsync(path)

        with patch("dyro.witness._fsync_directory", side_effect=fail_checkpoint_directory):
            with self.assertRaisesRegex(OSError, "checkpoint directory sync failure"):
                store.accept(
                    batch,
                    idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
                )
        self.assertTrue(checkpoint_path.exists())
        with patch("dyro.witness._fsync_directory", wraps=original_fsync) as synced:
            repeated = store.accept(
                batch,
                idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
            )
        self.assertFalse(repeated.created)
        self.assertTrue(
            any(item.args == (checkpoint_path.parent,) for item in synced.call_args_list)
        )

    def test_retry_resyncs_previously_created_workspace_directory(self) -> None:
        store = WitnessStore(self.config)
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        workspace_root = store.config.storage_root
        workspaces = workspace_root / "workspaces"
        original_fsync = __import__("dyro.witness", fromlist=["_fsync_directory"])._fsync_directory

        def fail_workspace_parent(path: Path) -> None:
            if path == workspace_root and workspaces.exists():
                raise OSError("workspace parent sync failure")
            original_fsync(path)

        with patch("dyro.witness._fsync_directory", side_effect=fail_workspace_parent):
            with self.assertRaisesRegex(OSError, "workspace parent sync failure"):
                store.accept(
                    batch,
                    idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
                )
        self.assertTrue(workspaces.exists())
        with patch("dyro.witness._fsync_directory", wraps=original_fsync) as synced:
            accepted = store.accept(
                batch,
                idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
            )
        self.assertTrue(accepted.created)
        self.assertTrue(any(item.args == (workspace_root,) for item in synced.call_args_list))

    def test_rejects_workspace_ancestor_symbolic_link(self) -> None:
        store = WitnessStore(self.config)
        (self.root / "ledger/workspaces").symlink_to(self.root / "outside")
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        with self.assertRaisesRegex(ValidationError, "不含符号链接"):
            store.accept(
                batch,
                idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
            )

    def test_rejects_symbolic_link_state_lock(self) -> None:
        store = WitnessStore(self.config)
        workspace_directory = self.root / "ledger/workspaces/workspace-1/witness-1"
        workspace_directory.mkdir(parents=True)
        (workspace_directory / ".lock").symlink_to(self.root / "outside-lock")
        batch = _batch(
            private_key=self.client_private,
            witness_key_id="receipt-1",
            receipt_key_epoch=1,
            event={"event": "trust", "key_id": "runner-1"},
        )
        with self.assertRaisesRegex(ValidationError, "状态锁不能是符号链接"):
            store.accept(
                batch,
                idempotency_key=hashlib.sha256(canonical_json_bytes(batch)).hexdigest(),
            )

    def test_control_plane_syncs_against_real_witness_service(self) -> None:
        client_root = self.root / "control-plane"
        trust_public_key(
            client_root,
            "receipt-1",
            purpose=AUDIT_RECEIPT_PURPOSE,
            source=self.receipt_public,
        )
        config = WitnessConfig(**{**self.config.__dict__, "expected_endpoint": None})
        server = create_witness_http_server(config, host="127.0.0.1", port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = sync_trust_audit(
                client_root,
                workspace_id="workspace-1",
                witness="witness-1",
                endpoint=f"http://127.0.0.1:{server.server_port}{WITNESS_PATH}",
                signing_key=self.client_private,
                key_id="client-1",
                witness_key_id="receipt-1",
                allow_insecure_http=True,
            )
            self.assertTrue(result.synced)
            self.assertEqual(result.receipt["type"], "dyro.audit.receipt")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_dry_run_refuses_to_start_witness_server(self) -> None:
        with self.assertRaises(SystemExit) as rejected:
            main(
                [
                    "--dry-run",
                    "witness",
                    "serve",
                    "--storage-root",
                    str(self.root / "ledger"),
                    "--client-trust-root",
                    str(self.root / "trust"),
                    "--witness-id",
                    "witness-1",
                    "--receipt-key-id",
                    "receipt-1",
                    "--receipt-signing-key",
                    str(self.receipt_private),
                ]
            )
        self.assertEqual(rejected.exception.code, 2)

    def test_unauthenticated_witness_refuses_public_listener(self) -> None:
        with self.assertRaises(SystemExit) as rejected:
            main(
                [
                    "witness",
                    "serve",
                    "--storage-root",
                    str(self.root / "ledger"),
                    "--client-trust-root",
                    str(self.root / "trust"),
                    "--witness-id",
                    "witness-1",
                    "--receipt-key-id",
                    "receipt-1",
                    "--receipt-signing-key",
                    str(self.receipt_private),
                    "--workspace-id",
                    "workspace-1",
                    "--allow-unauthenticated",
                    "--host",
                    "0.0.0.0",
                    "--tls-cert",
                    "unused.crt",
                    "--tls-key",
                    "unused.key",
                ]
            )
        self.assertEqual(rejected.exception.code, 2)

    def test_rejects_non_finite_read_timeout(self) -> None:
        with self.assertRaisesRegex(ValidationError, "read timeout"):
            create_witness_http_server(
                self.config,
                host="127.0.0.1",
                port=0,
                read_timeout_seconds=float("nan"),
            )

    def test_request_deadline_terminates_slow_untrusted_connection(self) -> None:
        server = create_witness_http_server(
            self.config,
            host="127.0.0.1",
            port=0,
            max_concurrent_requests=1,
            read_timeout_seconds=0.1,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as slow:
                slow.settimeout(1)
                slow.sendall(b"POST /v1/dyro/batches HTTP/1.1\\r\\n")
                time.sleep(0.2)
                self.assertEqual(slow.recv(1), b"")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_request_deadline_terminates_slow_tls_handshake(self) -> None:
        certificate, private_key = _write_tls_certificate(self.root)
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(certificate, private_key)
        server = create_witness_http_server(
            self.config,
            host="127.0.0.1",
            port=0,
            read_timeout_seconds=0.1,
            ssl_context=tls_context,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as slow:
                slow.settimeout(1)
                time.sleep(0.2)
                self.assertEqual(slow.recv(1), b"")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
