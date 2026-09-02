"""Bounded OTLP protobuf correlation parsing."""

from __future__ import annotations


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def _field(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _attribute(key: str, value: str) -> bytes:
    any_value = _field(1, value.encode())
    return _field(9, _field(1, key.encode()) + _field(2, any_value))


def export_request(*, session_id: str = "session-1", run_id: str = "run-1") -> bytes:
    span = (
        _field(1, bytes.fromhex("00112233445566778899aabbccddeeff"))
        + _attribute("hermes.session.id", session_id)
        + _attribute("hermes.run.id", run_id)
    )
    scope_spans = _field(2, span)
    resource_spans = _field(2, scope_spans)
    return _field(1, resource_spans)


def test_derives_bounded_session_and_run_from_real_otlp_wire_shape() -> None:
    from hermes_cli.client_auth.trace.otlp import derive_correlation

    correlation = derive_correlation(export_request())

    assert correlation is not None
    assert correlation.session_id == "session-1"
    assert correlation.run_id == "run-1"


def test_invalid_protobuf_is_rejected_and_invalid_attributes_fall_back_to_trace_id() -> None:
    from hermes_cli.client_auth.trace.otlp import derive_correlation

    assert derive_correlation(b"\x0a\xff") is None

    correlation = derive_correlation(export_request(session_id="bad value", run_id="x" * 129))
    assert correlation is not None
    assert correlation.session_id == "00112233445566778899aabbccddeeff"
    assert correlation.run_id == "00112233445566778899aabbccddeeff"
