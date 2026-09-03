"""Trace producer identity contracts."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("raw", ["cli", "dashboard", "desktop", "voice"])
def test_trace_entrypoint_accepts_exact_product_values(raw: str) -> None:
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint

    assert TraceEntrypoint.parse(raw).value == raw


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "Desktop", "web", "unknown", "desktop "],
)
def test_trace_entrypoint_has_no_default_or_fuzzy_alias(raw: str | None) -> None:
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint

    with pytest.raises(ValueError, match="entrypoint"):
        TraceEntrypoint.parse(raw)


def test_installation_identity_accepts_only_canonical_uuid_v4() -> None:
    from hermes_cli.client_auth.trace.identity import TraceInstallationIdentity

    raw = "11111111-1111-4111-8111-111111111111"

    identity = TraceInstallationIdentity.parse(raw)

    assert identity.value == raw
    assert str(identity) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "11111111111141118111111111111111",
        "11111111-1111-1111-8111-111111111111",
        "11111111-1111-4111-8111-11111111111A",
        "not-a-uuid",
    ],
)
def test_installation_identity_rejects_missing_non_v4_or_noncanonical_values(
    raw: str | None,
) -> None:
    from hermes_cli.client_auth.trace.identity import TraceInstallationIdentity

    with pytest.raises(ValueError, match="installation"):
        TraceInstallationIdentity.parse(raw)
