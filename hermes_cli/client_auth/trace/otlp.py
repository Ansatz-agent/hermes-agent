"""Minimal bounded OTLP protobuf correlation parser.

The auth owner deliberately parses only protobuf wire fields needed for
correlation. It does not depend on the full OpenTelemetry SDK at startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_IDENTIFIER = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:-]{0,127}", re.ASCII)
_MAX_FIELDS = 100_000
_MAX_BODY_BYTES = 8 * 1024 * 1024
_SESSION_KEYS = (
    "hermes.session.id",
    "session.id",
    "langfuse.session.id",
    "gen_ai.conversation.id",
    "nemo_relay.scope.metadata.session_id",
    "nemo_relay.mark.metadata.session_id",
    "nemo_relay.mark.data.session_id",
    "nemo_relay.scope.data.session_id",
    "nemo_relay.session.instance_id",
)
_RUN_KEYS = (
    "hermes.run.id",
    "run.id",
    "nemo_relay.scope.metadata.turn_id",
    "nemo_relay.mark.metadata.turn_id",
    "nemo_relay.scope.metadata.task_id",
)


@dataclass(frozen=True, slots=True)
class OtlpCorrelation:
    session_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class _Field:
    number: int
    wire_type: int
    value: bytes | None = None


def derive_correlation(export_request: bytes) -> OtlpCorrelation | None:
    if not isinstance(export_request, bytes) or len(export_request) > _MAX_BODY_BYTES:
        return None
    request = _read_fields(export_request)
    if request is None:
        return None
    attributes: dict[str, str] = {}
    trace_id = ""
    for resource_spans in _messages(request, 1):
        resource_fields = _read_fields(resource_spans)
        if resource_fields is None:
            return None
        for resource in _messages(resource_fields, 1):
            fields = _read_fields(resource)
            if fields is None or not _collect_key_values(fields, 1, attributes):
                return None
        for scope_spans in _messages(resource_fields, 2):
            scope_fields = _read_fields(scope_spans)
            if scope_fields is None:
                return None
            for span in _messages(scope_fields, 2):
                span_fields = _read_fields(span)
                if span_fields is None:
                    return None
                if not trace_id:
                    candidate = next(
                        (field.value for field in span_fields if field.number == 1 and field.wire_type == 2),
                        None,
                    )
                    if candidate is not None and len(candidate) == 16:
                        trace_id = candidate.hex()
                if not _collect_key_values(span_fields, 9, attributes):
                    return None
    if _IDENTIFIER.fullmatch(trace_id) is None:
        return None
    return OtlpCorrelation(
        session_id=_first_identifier(attributes, _SESSION_KEYS) or trace_id,
        run_id=_first_identifier(attributes, _RUN_KEYS) or trace_id,
    )


def _collect_key_values(
    fields: list[_Field],
    field_number: int,
    target: dict[str, str],
) -> bool:
    for message in _messages(fields, field_number):
        key_value = _read_fields(message)
        if key_value is None:
            return False
        key_bytes = next(
            (field.value for field in key_value if field.number == 1 and field.wire_type == 2),
            None,
        )
        value_message = next(
            (field.value for field in key_value if field.number == 2 and field.wire_type == 2),
            None,
        )
        if key_bytes is None or value_message is None:
            continue
        value_fields = _read_fields(value_message)
        if value_fields is None:
            return False
        value_bytes = next(
            (field.value for field in value_fields if field.number == 1 and field.wire_type == 2),
            None,
        )
        if value_bytes is None:
            continue
        try:
            key = key_bytes.decode("utf-8", errors="strict")
            value = value_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        target.setdefault(key, value)
    return True


def _messages(fields: list[_Field], number: int) -> list[bytes]:
    return [
        field.value
        for field in fields
        if field.number == number and field.wire_type == 2 and field.value is not None
    ]


def _first_identifier(attributes: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attributes.get(key)
        if value is not None and _IDENTIFIER.fullmatch(value) is not None:
            return value
    return None


def _read_fields(message: bytes) -> list[_Field] | None:
    fields: list[_Field] = []
    offset = 0
    while offset < len(message):
        if len(fields) >= _MAX_FIELDS:
            return None
        key = _read_varint(message, offset)
        if key is None or key[0] == 0:
            return None
        key_value, offset = key
        number = key_value >> 3
        wire_type = key_value & 7
        if number < 1:
            return None
        if wire_type == 0:
            value = _read_varint(message, offset)
            if value is None:
                return None
            _, offset = value
            fields.append(_Field(number, wire_type))
        elif wire_type in {1, 5}:
            size = 8 if wire_type == 1 else 4
            if offset + size > len(message):
                return None
            offset += size
            fields.append(_Field(number, wire_type))
        elif wire_type == 2:
            length = _read_varint(message, offset)
            if length is None:
                return None
            size, start = length
            end = start + size
            if end > len(message):
                return None
            fields.append(_Field(number, wire_type, message[start:end]))
            offset = end
        else:
            return None
    return fields


def _read_varint(buffer: bytes, start: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    for offset in range(start, min(len(buffer), start + 10)):
        byte = buffer[offset]
        value |= (byte & 0x7F) << shift
        if value > (1 << 63) - 1:
            return None
        if byte & 0x80 == 0:
            return value, offset + 1
        shift += 7
    return None


__all__ = ["OtlpCorrelation", "derive_correlation"]
