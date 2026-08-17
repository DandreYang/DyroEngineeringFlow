"""Frozen Agent Bridge protocol types. No CLI or filesystem access."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    R0 = "R0"
    PLAN = "PLAN"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class Availability(str, Enum):
    DECLARED = "declared"
    IMPLEMENTED_TESTABLE = "implemented_testable"
    PUBLIC_AVAILABLE = "public_available"


@dataclass(frozen=True)
class ProtocolVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        if type(self.major) is not int or type(self.minor) is not int:
            raise TypeError("ProtocolVersion 必须是整数")
        if self.major < 1 or self.minor < 0:
            raise TypeError("ProtocolVersion 无效")


@dataclass(frozen=True)
class CatalogRecord:
    id: str
    risk: Risk
    availability: Availability
    schema_version: int
    must_be_available: bool
    core_service: str

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise TypeError("CatalogRecord.id 不能为空")
        if not isinstance(self.risk, Risk):
            raise TypeError("CatalogRecord.risk 必须是 Risk")
        if not isinstance(self.availability, Availability):
            raise TypeError("CatalogRecord.availability 必须是 Availability")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise TypeError("CatalogRecord.schema_version 必须是正整数")
        if type(self.must_be_available) is not bool:
            raise TypeError("CatalogRecord.must_be_available 必须是 bool")
        if not self.core_service or not isinstance(self.core_service, str):
            raise TypeError("CatalogRecord.core_service 不能为空")
        if self.core_service.startswith("dyro.cli"):
            raise TypeError("CatalogRecord.core_service 不得指向 CLI")
