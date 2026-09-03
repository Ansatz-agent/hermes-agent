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
    raw_seen_count: int = 0
    first_seen_request_sequence: int | None = None
    last_seen_request_sequence: int | None = None
    first_seen_success_sequence: int | None = None
    last_seen_success_sequence: int | None = None
    eligibility_success_sequence: int | None = None
    projection_epoch_id: str = ""
    projected_at_request_sequence: int | None = None


@dataclass(frozen=True)
class PendingLedgerAccrual:
    """One immutable gain snapshot carried in a successful raw request."""

    delta_id: str
    gain_tokens: int
    ledger_generation: int


@dataclass(frozen=True)
class PendingLedgerRecord:
    """Durable amortized-compression state for one still-raw Delta."""

    conversation_id: str
    delta_id: str
    entered_success_sequence: int
    bucket_sequence: int
    raw_tokens: int
    projected_tokens: int
    gain_tokens: int
    wait_area_token_requests: int
    last_accrued_success_sequence: int | None
    ledger_generation: int
    estimator_version: str
    pending_reason: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class SuccessfulRequestObservationResult:
    """Result of an exactly-once successful-request observation."""

    request_attempt_id: str
    conversation_id: str
    success_sequence: int
    exposure_request_sequence: int
    route_namespace_hash: str
    outcome: str
    duplicate: bool
    raw_exposed_delta_ids: tuple[str, ...] = ()
    accrued_delta_ids: tuple[str, ...] = ()
    skipped_pending_delta_ids: tuple[str, ...] = ()
    newly_eligible_delta_ids: tuple[str, ...] = ()


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
    origin: dict[str, Any] = field(default_factory=dict)
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
