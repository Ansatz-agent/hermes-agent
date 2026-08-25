#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "hermes_cli" / "client_auth" / "static_help.txt"


def _render_real_help() -> str:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from hermes_cli.client_auth import guard

    # This build-only process deliberately substitutes the gate before
    # importing main.py. There is no runtime flag or environment variable that
    # can perform this substitution in a packaged Hermes process.
    guard.enforce_raw_argv = lambda _argv: None
    sys.argv = ["ansatz", "__build_auth_free_help__"]

    from hermes_cli import main as cli_main

    for name in (
        "_set_process_title",
        "_advertise_agent_env",
        "_cleanup_quarantined_exes",
        "_sweep_stale_bytecode_if_checkout_changed",
        "_recover_from_interrupted_install",
    ):
        setattr(cli_main, name, lambda *args, **kwargs: None)
    for name in (
        "_try_termux_fast_tui_launch",
        "_try_termux_fast_cli_launch",
        "_try_fast_chat_launch",
    ):
        setattr(cli_main, name, lambda: False)

    sys.argv = ["ansatz", "--help"]
    rendered = io.StringIO()
    with contextlib.redirect_stdout(rendered):
        try:
            cli_main.main()
        except SystemExit as error:
            if error.code != 0:
                raise RuntimeError("real parser help generation failed") from error
        else:
            raise RuntimeError("real parser did not exit after --help")
    value = rendered.getvalue()
    if not value.startswith("usage: ansatz ") or not value.endswith("\n"):
        raise RuntimeError("real parser returned malformed help")
    return value


def _write_atomic(value: str) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=OUTPUT.parent,
        prefix=".static-help-",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = _render_real_help()
    if args.check:
        try:
            current = OUTPUT.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != rendered:
            print(
                "auth-free help is stale; run scripts/generate_auth_free_help.py",
                file=sys.stderr,
            )
            return 1
        return 0
    _write_atomic(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
