"""Best-effort process-surface bootstrap for the sealed Trace path."""

from __future__ import annotations

import os

from .identity import TraceEntrypoint


def bootstrap_trace(entrypoint: TraceEntrypoint, *, consumer_id: str | None = None) -> bool:
    """Open and register one explicit ingress lease; Trace failure is non-fatal."""
    try:
        parsed = TraceEntrypoint.parse(entrypoint.value)
        identifier = consumer_id or f"{parsed.value}-{os.getpid()}"
        from hermes_cli.client_auth.runtime import account_trace_ingress_open

        registration = account_trace_ingress_open(
            entrypoint=parsed.value,
            consumer_id=identifier,
        )
        if registration.entrypoint != parsed.value:
            return False
        from agent.relay_runtime import register_ansatz_product_trace_transport

        register_ansatz_product_trace_transport(
            endpoint=registration.endpoint,
            authorization=registration.authorization,
            installation_id=registration.installation_id,
            entrypoint=registration.entrypoint,
            plugins_toml=registration.plugins_toml,
        )
    except Exception:
        return False
    return True


__all__ = ["bootstrap_trace"]
