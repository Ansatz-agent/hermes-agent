"""Lazy console-script targets that authorize before capability imports."""

from __future__ import annotations


def ansatz_agent() -> object:
    from hermes_cli.client_auth.guard import enforce_direct_entrypoint

    enforce_direct_entrypoint("console.hermes_agent")
    from run_agent import main

    return main()


def ansatz_acp() -> object:
    from hermes_cli.client_auth.guard import enforce_direct_entrypoint

    enforce_direct_entrypoint("console.hermes_acp")
    from acp_adapter.entry import main

    return main()


def hermes_agent() -> object:
    from hermes_cli.cli_identity import maybe_warn_legacy_invocation

    maybe_warn_legacy_invocation("hermes-agent")
    return ansatz_agent()


def hermes_acp() -> object:
    from hermes_cli.cli_identity import maybe_warn_legacy_invocation

    maybe_warn_legacy_invocation("hermes-acp")
    return ansatz_acp()
