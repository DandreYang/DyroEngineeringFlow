"""Agent Bridge Phase 0 contracts.

This package is source-tree only in the 0.7.x Core train. The default wheel
must not list ``dyro.bridge`` or grow ``dyro-bridge`` / ``dyro-mcp`` scripts.
S1 freezes models, the deny-by-default catalog, schemas, and identity hashes.
S2/S3 add source-only observation and plan adapters. S4 adds a source-only
one-shot JSON transport. S5 platform-gates Linux public availability in
source only. S6 adds ``python -m dyro.bridge`` and a non-installable Skill.
No packaged console script, mutation, or CLI handler lives here.
"""

from .catalog import (
    EXCLUDED_OPERATION_IDS,
    IMPLEMENTED_TESTABLE_IDS,
    MANDATORY_OPERATION_IDS,
    ExposureCatalog,
    build_default_catalog,
    catalog_platform,
    compact_catalog,
    validate_catalog,
)
from .identity import (
    CONFIG_REVISION_DOMAIN,
    PROFILE_MAX_BYTES,
    WORKSPACE_IDENTITY_DOMAIN,
    config_revision_v1,
    workspace_identity_v1,
)
from .models import Availability, ProtocolVersion, Risk
from .schemas import operation_schema

__all__ = (
    "Availability",
    "CONFIG_REVISION_DOMAIN",
    "EXCLUDED_OPERATION_IDS",
    "ExposureCatalog",
    "IMPLEMENTED_TESTABLE_IDS",
    "MANDATORY_OPERATION_IDS",
    "PROFILE_MAX_BYTES",
    "ProtocolVersion",
    "Risk",
    "WORKSPACE_IDENTITY_DOMAIN",
    "build_default_catalog",
    "catalog_platform",
    "compact_catalog",
    "config_revision_v1",
    "operation_schema",
    "validate_catalog",
    "workspace_identity_v1",
)
