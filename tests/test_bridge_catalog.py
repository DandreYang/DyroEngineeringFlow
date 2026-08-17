from __future__ import annotations

import unittest

from dyro.bridge.catalog import (
    EXCLUDED_OPERATION_IDS,
    IMPLEMENTED_TESTABLE_IDS,
    MANDATORY_OPERATION_IDS,
    ExposureCatalog,
    build_default_catalog,
    compact_catalog,
    validate_catalog,
)
from dyro.bridge.models import Availability, CatalogRecord, Risk
from dyro.bridge.schemas import operation_schema
from dyro.errors import ValidationError


class BridgeCatalogTests(unittest.TestCase):
    def test_fail_closed_hosts_have_no_public_surface(self) -> None:
        catalog = build_default_catalog(platform="darwin")
        ids = {item.id for item in catalog.operations}
        self.assertTrue(MANDATORY_OPERATION_IDS <= ids)
        self.assertFalse(ids & EXCLUDED_OPERATION_IDS)
        self.assertTrue(
            all(
                item.availability
                in {Availability.DECLARED, Availability.IMPLEMENTED_TESTABLE}
                for item in catalog.operations
            )
        )
        self.assertFalse(
            any(item.availability is Availability.PUBLIC_AVAILABLE for item in catalog.operations)
        )
        self.assertEqual(
            {item.id for item in catalog.operations if item.availability is Availability.IMPLEMENTED_TESTABLE},
            set(IMPLEMENTED_TESTABLE_IDS),
        )
        self.assertFalse(IMPLEMENTED_TESTABLE_IDS & EXCLUDED_OPERATION_IDS)
        self.assertTrue(catalog.digest.startswith("sha256:"))
        compact = compact_catalog(catalog)
        self.assertEqual(compact["schema_version"], 1)
        self.assertEqual(len(compact["operations"]), len(catalog.operations))
        with self.assertRaisesRegex(ValidationError, "空的 public surface"):
            validate_catalog(catalog, release=True)
        windows = build_default_catalog(platform="win32")
        self.assertFalse(
            any(item.availability is Availability.PUBLIC_AVAILABLE for item in windows.operations)
        )

    def test_linux_release_catalog_promotes_only_mandatory_ids(self) -> None:
        catalog = build_default_catalog(platform="linux")
        public = {
            item.id
            for item in catalog.operations
            if item.availability is Availability.PUBLIC_AVAILABLE
        }
        self.assertEqual(public, set(MANDATORY_OPERATION_IDS))
        self.assertFalse(public & EXCLUDED_OPERATION_IDS)
        validate_catalog(catalog, release=True)
        self.assertNotEqual(
            catalog.digest, build_default_catalog(platform="darwin").digest
        )

    def test_catalog_rejects_excluded_and_missing_mandatory(self) -> None:
        hello = CatalogRecord(
            id="bridge.hello",
            risk=Risk.R0,
            availability=Availability.DECLARED,
            schema_version=1,
            must_be_available=True,
            core_service="dyro.bridge.transport.hello",
        )
        apply = CatalogRecord(
            id="objective.apply",
            risk=Risk.R2,
            availability=Availability.DECLARED,
            schema_version=1,
            must_be_available=False,
            core_service="dyro.bridge.forbidden",
        )
        with self.assertRaisesRegex(ValidationError, "禁止"):
            validate_catalog(ExposureCatalog(operations=(hello, apply), digest="x"))
        with self.assertRaisesRegex(ValidationError, "缺少强制"):
            validate_catalog(ExposureCatalog(operations=(hello,), digest="x"))

    def test_schema_fetch_rejects_unknown_and_returns_allowlisted(self) -> None:
        schema = operation_schema("bridge.hello", platform="darwin")
        self.assertEqual(schema["operation"], "bridge.hello")
        self.assertEqual(schema["availability"], "implemented_testable")
        self.assertEqual(
            operation_schema("bridge.hello", platform="linux")["availability"],
            "public_available",
        )
        with self.assertRaisesRegex(ValidationError, "未知 operation"):
            operation_schema("objective.apply")
