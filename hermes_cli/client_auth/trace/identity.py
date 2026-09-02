"""Strict identities shared by every Ansatz Trace entrypoint."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class TraceEntrypoint(str, Enum):
    """An explicit product producer; deliberately has no default member."""

    CLI = "cli"
    DASHBOARD = "dashboard"
    DESKTOP = "desktop"
    VOICE = "voice"

    @classmethod
    def parse(cls, raw: str | None) -> TraceEntrypoint:
        try:
            return cls(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Trace entrypoint") from exc


@dataclass(frozen=True, slots=True)
class TraceInstallationIdentity:
    """A canonical UUIDv4 installation identifier."""

    value: str

    @classmethod
    def parse(cls, raw: str | None) -> TraceInstallationIdentity:
        try:
            parsed = uuid.UUID(raw)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("invalid Trace installation identity") from exc
        if parsed.version != 4 or str(parsed) != raw:
            raise ValueError("invalid Trace installation identity")
        return cls(value=raw)

    @classmethod
    def new(cls) -> TraceInstallationIdentity:
        return cls(value=str(uuid.uuid4()))

    def __str__(self) -> str:
        return self.value
