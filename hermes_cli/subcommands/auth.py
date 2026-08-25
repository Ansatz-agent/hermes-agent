"""Ansatz remote-account status parser."""

from __future__ import annotations

from typing import Callable


def build_auth_parser(subparsers, *, cmd_auth_status: Callable) -> None:
    auth_parser = subparsers.add_parser(
        "auth",
        help="Ansatz remote account status",
    )
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_action",
        required=True,
    )
    auth_status = auth_subparsers.add_parser(
        "status",
        help="Show Ansatz remote account status",
    )
    auth_status.set_defaults(func=cmd_auth_status)
