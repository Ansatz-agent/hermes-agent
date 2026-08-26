"""Typed records for Context Compression Strategy V1."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeltaState(str, Enum):
    HOT = "hot"
    COMPRESSION_ELIGIBLE = "compression_eligible"
    COMPRESSING = "compressing"
    COMPRESSED = "compressed"
    COMPRESSION_SKIPPED = "compression_skipped"
    COMPRESSION_FAILED = "compression_failed"


class ObjectType(str, Enum):
    CODE = "code"
    FILE_CONTENT = "file_content"
    TOOL_RESULT = "tool_result"
    LOG = "log"
    ERROR_TRACE = "error_trace"
    STRUCTURED_DATA = "structured_data"
    TABLE = "table"
    ARTIFACT = "artifact"


class ActivityState(str, Enum):
    ACTIVE = "active"
    INACTIVE_CANDIDATE = "inactive_candidate"
    EVICTABLE = "evictable"
    ARCHIVED = "archived"


class ObjectLocation(str, Enum):
    WORKING_MEMORY = "working_memory"
    COLD_ARCHIVE = "cold_archive"


@dataclass(frozen=True)
class DetectedObject:
    """One exact structured-object span inside a Delta message."""

    object_type: ObjectType
    content: str
    message_ordinal: int
    part_ordinal: int
    start: int
    end: int
    whole_part: bool
    detection_method: str
    source_role: str
    message_key: str
    occurrence_key: str
    name: str = ""
    language: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeltaRecord:
    delta_id: str
    conversation_id: str
    session_id: str
    turn_id: str
    kind: str
    inference_id: str
    turn_sequence: int
    global_sequence: int
    raw_token_count: int
    state: DeltaState
    raw_view: tuple[dict[str, Any], ...]
    object_refs: tuple[str, ...] = ()
    compressed_view: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class ObjectRecord:
    object_ref: str
    object_id: str
    version: int
    object_type: ObjectType
    content: str
    sha256: str
    byte_size: int
    char_count: int
    token_count: int
    conversation_id: str
    source_delta_id: str
    source_message_key: str
    source_message_ordinal: int
    source_part_ordinal: int
    source_start: int
    source_end: int
    name: str = ""
    language: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    supersedes: str = ""
    derived_from: tuple[str, ...] = ()
    summary: str = ""
    contains: dict[str, Any] = field(default_factory=dict)
    card_text: str = ""
    activity_state: ActivityState = ActivityState.ACTIVE
    pinned: bool = False
    created_at_delta: int = 0
    last_accessed_delta: int = 0
    inactive_since_delta: int | None = None
    location: ObjectLocation = ObjectLocation.WORKING_MEMORY


@dataclass(frozen=True)
class ObjectCard:
    schema_version: str
    object_ref: str
    object_id: str
    version: int
    object_type: ObjectType
    name: str
    language: str
    summary: str
    contains: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    supersedes: str = ""
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalLease:
    turn_id: str
    object_ref: str
    mounted_at_delta: int
    tool_call_id: str
    reason: str
    expires_at: str = "turn_end"
