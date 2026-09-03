"""Trace bootstrap surface routing."""

from __future__ import annotations


def test_bootstrap_registers_exact_lease_without_exposing_cloud_token(monkeypatch) -> None:
    from agent import relay_runtime
    from hermes_cli.client_auth import runtime
    from hermes_cli.client_auth.runtime import TraceTransportRegistration
    from hermes_cli.client_auth.trace.bootstrap import bootstrap_trace
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint

    opens = []
    registrations = []
    monkeypatch.setattr(
        runtime,
        "account_trace_ingress_open",
        lambda **values: opens.append(values)
        or TraceTransportRegistration(
            endpoint="http://127.0.0.1:49152/v1/traces",
            authorization="Bearer " + "a" * 43,
            installation_id="11111111-1111-4111-8111-111111111111",
            entrypoint="dashboard",
            plugins_toml="/opt/Ansatz/config/ansatz-voice-trace/plugins.toml",
        ),
    )
    monkeypatch.setattr(
        relay_runtime,
        "register_ansatz_product_trace_transport",
        lambda **values: registrations.append(values),
    )

    assert bootstrap_trace(TraceEntrypoint.DASHBOARD, consumer_id="dashboard-1")
    assert opens == [{"entrypoint": "dashboard", "consumer_id": "dashboard-1"}]
    assert registrations[0]["entrypoint"] == "dashboard"
    assert "access_token" not in registrations[0]


def test_bootstrap_failure_degrades_trace_only(monkeypatch) -> None:
    from hermes_cli.client_auth import runtime
    from hermes_cli.client_auth.trace.bootstrap import bootstrap_trace
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint

    monkeypatch.setattr(
        runtime,
        "account_trace_ingress_open",
        lambda **_values: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    assert not bootstrap_trace(TraceEntrypoint.CLI, consumer_id="cli-1")
