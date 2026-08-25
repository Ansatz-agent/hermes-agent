"""Ansatz remote-account login parser."""

from __future__ import annotations

from typing import Callable


def build_login_parser(subparsers, *, cmd_login: Callable) -> None:
    login_parser = subparsers.add_parser(
        "login",
        help="Sign in to the fixed Ansatz remote account server",
        description="Sign in with an account created by the server administrator.",
    )
    login_parser.set_defaults(func=cmd_login)
