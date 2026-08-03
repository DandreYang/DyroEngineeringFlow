"""Public compatibility façade for the durable continuation Action Journal.

Action records, secure journal persistence, and scheduler-owner fencing have
separate implementation modules.  This stable surface keeps downstream code
from depending on that internal storage layout.
"""

from .action_journal import (
    apply_action_cancellation,
    cancel_unstarted_actions,
    list_actions,
    prepare_action_cancellation,
    read_action,
    record_action_receipt,
    reserve_action,
    start_action,
)
from .action_models import (
    ACTION_SCHEMA_VERSION,
    ActionIntent,
    ActionReceipt,
    ActionRecord,
    ActionStart,
    ActionStatus,
    action_idempotency_key,
)
from .owner_lease import (
    OWNER_LEASE_SCHEMA_VERSION,
    OWNER_TAKEOVER_SCHEMA_VERSION,
    OwnerLease,
    OwnerLeaseGrant,
    acquire_owner_lease,
    read_owner_lease,
    release_owner_lease,
    renew_owner_lease,
    verify_owner_lease,
)


__all__ = [
    "ACTION_SCHEMA_VERSION",
    "OWNER_LEASE_SCHEMA_VERSION",
    "OWNER_TAKEOVER_SCHEMA_VERSION",
    "ActionIntent",
    "ActionReceipt",
    "ActionRecord",
    "ActionStart",
    "ActionStatus",
    "OwnerLease",
    "OwnerLeaseGrant",
    "acquire_owner_lease",
    "action_idempotency_key",
    "apply_action_cancellation",
    "cancel_unstarted_actions",
    "list_actions",
    "prepare_action_cancellation",
    "read_action",
    "read_owner_lease",
    "record_action_receipt",
    "release_owner_lease",
    "renew_owner_lease",
    "reserve_action",
    "start_action",
    "verify_owner_lease",
]
