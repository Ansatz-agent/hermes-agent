from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.client_auth.runtime import AuthState
from hermes_cli.subcommands.auth import build_auth_parser
from hermes_cli.subcommands.login import build_login_parser
from hermes_cli.subcommands.logout import build_logout_parser
from hermes_cli.subcommands.provider import build_provider_parser


REPO_ROOT = Path(__file__).resolve().parents[3]


def _handler(_args):
    return None


@pytest.fixture
def parser():
    value = argparse.ArgumentParser(prog="hermes")
    subparsers = value.add_subparsers(dest="command")
    build_login_parser(subparsers, cmd_login=_handler)
    build_logout_parser(subparsers, cmd_logout=_handler)
    build_auth_parser(subparsers, cmd_auth_status=_handler)
    build_provider_parser(subparsers, cmd_provider=_handler)
    return value


def test_account_commands_have_no_registration_or_server_flags(parser):
    assert parser.parse_args(["login"]).command == "login"
    assert parser.parse_args(["logout"]).command == "logout"
    assert parser.parse_args(["auth", "status"]).auth_action == "status"
    for argv in (
        ["login", "--server", "x"],
        ["login", "--register"],
        ["logout", "--provider", "nous"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_provider_commands_retain_old_provider_handlers(parser):
    args = parser.parse_args(["provider", "status", "nous"])

    assert args.provider_action == "status"
    assert args.provider == "nous"
    assert args.func is _handler


class _Tty:
    def __init__(self, value: bool = True) -> None:
        self.value = value

    def isatty(self) -> bool:
        return self.value


@pytest.mark.parametrize("is_tty", [True, False])
def test_valid_login_is_idempotent_without_prompt(monkeypatch, capsys, is_tty):
    from hermes_cli import main

    snapshot = SimpleNamespace(state=AuthState.AUTHENTICATED, username="alice")
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.account_status",
        lambda: snapshot,
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("authenticated login must not prompt"),
    )
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _prompt: pytest.fail("authenticated login must not prompt"),
    )
    monkeypatch.setattr(sys, "stdin", _Tty(is_tty))
    monkeypatch.setattr(sys, "stderr", _Tty(is_tty))

    main.cmd_login(SimpleNamespace())

    assert capsys.readouterr().out == "Authenticated as alice\n"


def test_signed_out_login_prompts_once_and_wipes_password(monkeypatch, capsys):
    from hermes_cli import main

    signed_out = SimpleNamespace(state=AuthState.SIGNED_OUT, username=None)
    authenticated = SimpleNamespace(state=AuthState.AUTHENTICATED, username="alice")
    captured: list[bytearray] = []
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.account_status",
        lambda: signed_out,
    )

    def login(username: str, password: bytearray):
        assert username == "alice"
        assert password == bytearray(b"secret")
        captured.append(password)
        return authenticated

    monkeypatch.setattr("hermes_cli.client_auth.runtime.account_login", login)
    prompts = iter(["alice"])
    passwords = iter(["secret"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(passwords))
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stderr", _Tty())

    main.cmd_login(SimpleNamespace())

    assert captured == [bytearray(b"\0" * 6)]
    assert capsys.readouterr().out == "Authenticated as alice\n"


def test_logout_uses_account_runtime_and_mentions_provider_credentials(monkeypatch, capsys):
    from hermes_cli import main

    calls: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.account_logout",
        lambda: calls.append("logout"),
    )

    main.cmd_logout(SimpleNamespace())

    assert calls == ["logout"]
    assert capsys.readouterr().out == (
        "Remote Hermes account signed out; provider credentials were not modified.\n"
    )


def test_noninteractive_login_returns_structured_auth_required():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "login"],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 20
    assert result.stdout == ""
    assert result.stderr == (
        "AUTH_REQUIRED interactive_login_required; run `hermes login`\n"
    )
    assert "Traceback" not in result.stderr


def test_installed_console_callable_returns_structured_auth_required():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.main import main; main()",
            "login",
        ],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 20
    assert result.stdout == ""
    assert result.stderr == (
        "AUTH_REQUIRED interactive_login_required; run `hermes login`\n"
    )
    assert "Traceback" not in result.stderr


def test_account_status_is_auth_free_and_contains_no_secret():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "auth", "status"],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "signed_out"
    assert payload["reason"] == "runtime_unavailable"
    assert "cookie" not in result.stdout.casefold()
    assert "password" not in result.stdout.casefold()


@pytest.mark.parametrize(
    ("argv", "expected_code", "expected_stdout", "expected_stderr"),
    [
        (
            ("auth", "status"),
            0,
            '{"epoch": 0, "reason": "runtime_unavailable", "runtime_instance_id": "test", "session_expires_at": null, "state": "signed_out", "username": null, "valid_until": 0.0}\n',
            "",
        ),
        (
            ("logout",),
            0,
            "Remote Hermes account signed out; provider credentials were not modified.\n",
            "",
        ),
        (
            ("login",),
            20,
            "",
            "AUTH_REQUIRED interactive_login_required; run `hermes login`\n",
        ),
    ],
)
def test_auth_free_commands_run_with_only_the_auth_runtime(
    tmp_path: Path,
    argv: tuple[str, ...],
    expected_code: int,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    site_dir.joinpath("sitecustomize.py").write_text(
        """
import importlib.abc
import sys
import types
from enum import StrEnum

class _NoHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return None

sys.meta_path.insert(0, _NoHeavyImports())

runtime = types.ModuleType("hermes_cli.client_auth.runtime")

class AuthState(StrEnum):
    AUTHENTICATED = "authenticated"
    SIGNED_OUT = "signed_out"

class AuthRequired(RuntimeError):
    code = "AUTH_REQUIRED"
    def __init__(self, reason=None):
        super().__init__(reason or self.code)
        self.reason = reason

class Snapshot:
    state = AuthState.SIGNED_OUT
    username = None
    def public_dict(self):
        return {
            "state": "signed_out",
            "username": None,
            "runtime_instance_id": "test",
            "epoch": 0,
            "valid_until": 0.0,
            "session_expires_at": None,
            "reason": "runtime_unavailable",
        }

runtime.AuthState = AuthState
runtime.AuthRequired = AuthRequired
runtime.account_status = lambda: Snapshot()
runtime.account_logout = lambda: Snapshot()
runtime.account_login = lambda username, password: Snapshot()
sys.modules["hermes_cli.client_auth.runtime"] = runtime
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(site_dir), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )

    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_code
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert "Traceback" not in result.stderr
