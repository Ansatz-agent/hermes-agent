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


def _is_update_command(argv: Sequence[str]) -> bool:
    """Return whether argv invokes the source-update maintenance command.

    ``ansatz update`` is intentionally available before the protected runtime
    is authorized: packaged installs must be able to refresh their source and
    dependencies when the auth runtime is signed out or temporarily
    unavailable.  The update parser owns validation of its flags; keeping the
    command-level exemption here also covers the detached Windows hand-off
    invocation (``update --yes --gateway ...``) without exempting any other
    capability command.
    """
    return bool(argv) and argv[0] == "update"


def _is_desktop_build_command(argv: Sequence[str]) -> bool:
    """Return whether argv is the headless desktop rebuild maintenance path.

    The Windows updater invokes ``desktop --force-build --build-only`` after
    applying a source archive.  This operation only rebuilds local assets and
    never starts the agent or accesses account data, so it must be able to run
    while the auth runtime is unavailable, just like ``update`` itself.  Keep
    the exemption narrow: a normal ``desktop`` launch remains protected.
    """
    return bool(argv) and argv[0] in {"desktop", "gui"} and "--build-only" in argv


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
        auth_free=(
            shape in AUTH_FREE
            or _is_update_command(shape)
            or _is_desktop_build_command(shape)
        ),
        interactive=interactive,
    )


def enforce_raw_argv(argv: Sequence[str]) -> None:
    decision = classify_raw_argv(argv)
    if decision.auth_free:
        return
    from hermes_cli.client_auth.runtime import AuthRequired, authorize_entrypoint, external_auth_enabled

    if external_auth_enabled():
        return

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
        external_auth_enabled,
    )

    if external_auth_enabled():
        return

    try:
        authorize_entrypoint(boundary, interactive=False)
    except AuthRequired as error:
        print(
            f"AUTH_REQUIRED {error.reason or error.code}; "
            f"run `{CANONICAL_COMMAND} login`",
            file=sys.stderr,
        )
        raise SystemExit(AUTH_EXIT_CODE) from None
