"""Lazy console-script targets that authorize before capability imports."""

from __future__ import annotations


def hermes_agent() -> object:
    from hermes_cli.client_auth.guard import enforce_direct_entrypoint

    enforce_direct_entrypoint("console.hermes_agent")
    from run_agent import main

    return main()


def hermes_acp() -> object:
    from hermes_cli.client_auth.guard import enforce_direct_entrypoint

    enforce_direct_entrypoint("console.hermes_acp")
    from acp_adapter.entry import main

    return main()
