from io import StringIO

from hermes_cli.cli_identity import (
    CANONICAL_ACP_COMMAND,
    CANONICAL_AGENT_COMMAND,
    CANONICAL_COMMAND,
    LEGACY_TO_CANONICAL,
    maybe_warn_legacy_invocation,
)


class _Tty(StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(StringIO):
    def isatty(self) -> bool:
        return False


def test_canonical_command_family_is_ansatz():
    assert CANONICAL_COMMAND == "ansatz"
    assert CANONICAL_AGENT_COMMAND == "ansatz-agent"
    assert CANONICAL_ACP_COMMAND == "ansatz-acp"
    assert LEGACY_TO_CANONICAL == {
        "hermes": "ansatz",
        "hermes-agent": "ansatz-agent",
        "hermes-acp": "ansatz-acp",
    }


def test_legacy_notice_is_interactive_only():
    interactive_error = _Tty()
    maybe_warn_legacy_invocation("hermes", stdin=_Tty(), stderr=interactive_error)
    assert interactive_error.getvalue() == (
        "Deprecated command `hermes`; use `ansatz` instead.\n"
    )

    piped_error = _Pipe()
    maybe_warn_legacy_invocation("hermes", stdin=_Pipe(), stderr=piped_error)
    assert piped_error.getvalue() == ""
