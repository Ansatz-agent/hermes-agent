from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

from hermes_cli.cli_identity import CANONICAL_COMMAND


AUTH_FREE = frozenset(
    {
        ("login",),
        ("logout",),
        ("auth", "status"),
        ("--help",),
        ("-h",),
        ("--version",),
        ("-V",),
    }
)


@dataclass(frozen=True)
class GuardDecision:
    auth_free: bool
    interactive: bool


class GuardRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_raw_argv(argv: Sequence[str]) -> GuardDecision:
    shape = tuple(argv)
    try:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    except Exception:
        interactive = False
    return GuardDecision(
        auth_free=shape in AUTH_FREE,
        interactive=interactive,
    )


def enforce_raw_argv(argv: Sequence[str]) -> None:
    decision = classify_raw_argv(argv)
    if decision.auth_free:
        return
    from hermes_cli.client_auth.runtime import AuthRequired, authorize_entrypoint

    try:
        authorize_entrypoint("cli.start", interactive=decision.interactive)
    except AuthRequired as error:
        raise GuardRejected(error.reason or error.code) from None


def enforce_direct_entrypoint(boundary: str) -> None:
    """Fail closed before a noninteractive direct entry can import capabilities."""
    from hermes_cli.client_auth.runtime import (
        AUTH_EXIT_CODE,
        AuthRequired,
        authorize_entrypoint,
    )

    try:
        authorize_entrypoint(boundary, interactive=False)
    except AuthRequired as error:
        print(
            f"AUTH_REQUIRED {error.reason or error.code}; "
            f"run `{CANONICAL_COMMAND} login`",
            file=sys.stderr,
        )
        raise SystemExit(AUTH_EXIT_CODE) from None
