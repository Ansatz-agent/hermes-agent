from __future__ import annotations

import json
import sys
from collections.abc import Sequence


def login() -> None:
    """Run the interactive remote-account login without full CLI imports."""
    from getpass import getpass

    from hermes_cli.client_auth.runtime import (
        AuthRequired,
        AuthState,
        account_login,
        account_status,
    )

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise AuthRequired("interactive_login_required")
    current = account_status()
    if current.state is AuthState.AUTHENTICATED:
        print(f"Authenticated as {current.username}")
        return
    username = input("Hermes account: ").strip()
    password = bytearray(getpass("Password: ").encode("utf-8"))
    try:
        result = account_login(username, password)
    finally:
        password[:] = b"\0" * len(password)
    print(f"Authenticated as {result.username}")


def logout() -> None:
    """Clear only the remote-account session stored by the auth runtime."""
    from hermes_cli.client_auth.runtime import account_logout

    account_logout()
    print("Remote Hermes account signed out; provider credentials were not modified.")


def status() -> None:
    """Print the redacted public remote-account snapshot."""
    from hermes_cli.client_auth.runtime import account_status

    print(json.dumps(account_status().public_dict(), sort_keys=True))


def try_handle(argv: Sequence[str]) -> bool:
    """Handle exact auth-free commands before the full CLI import wall."""
    shape = tuple(argv)
    if shape not in {("login",), ("logout",), ("auth", "status")}:
        return False

    from hermes_cli.client_auth.runtime import AuthRequired

    try:
        if shape == ("login",):
            login()
        elif shape == ("logout",):
            logout()
        else:
            status()
    except AuthRequired as error:
        print(
            f"AUTH_REQUIRED {error.reason or error.code}; run `hermes login`",
            file=sys.stderr,
        )
        raise SystemExit(20) from None
    return True
