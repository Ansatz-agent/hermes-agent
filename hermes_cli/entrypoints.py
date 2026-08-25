from __future__ import annotations


def ansatz() -> object:
    from hermes_cli.main import main

    return main()


def hermes() -> object:
    from hermes_cli.cli_identity import maybe_warn_legacy_invocation

    maybe_warn_legacy_invocation("hermes")
    return ansatz()
