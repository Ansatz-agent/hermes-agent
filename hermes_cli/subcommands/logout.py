"""``ansatz logout`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_logout_parser(subparsers, *, cmd_logout: Callable) -> None:
    """Attach the ``logout`` subcommand to ``subparsers``."""
    # =========================================================================
    # logout command
    # =========================================================================
    logout_parser = subparsers.add_parser(
        "logout",
        help="Sign out of the Ansatz remote account",
        description="Revoke the remote account Session without changing provider credentials.",
    )
    logout_parser.set_defaults(func=cmd_logout)
