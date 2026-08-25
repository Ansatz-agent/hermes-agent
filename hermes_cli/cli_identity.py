from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

CANONICAL_COMMAND = "ansatz"
CANONICAL_AGENT_COMMAND = "ansatz-agent"
CANONICAL_ACP_COMMAND = "ansatz-acp"
LEGACY_COMMAND = "hermes"
CLI_DISCOVERY_ORDER = (CANONICAL_COMMAND, LEGACY_COMMAND)

LEGACY_TO_CANONICAL = {
    LEGACY_COMMAND: CANONICAL_COMMAND,
    "hermes-agent": CANONICAL_AGENT_COMMAND,
    "hermes-acp": CANONICAL_ACP_COMMAND,
}


def executable_name(path: str) -> str:
    """Return a normalized CLI executable name across packaging shims."""
    name = Path(path).name.casefold()
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def is_legacy_cli_executable(path: str) -> bool:
    """Return whether *path* names the deprecated primary CLI alias."""
    return executable_name(path) == LEGACY_COMMAND


def maybe_warn_legacy_invocation(
    legacy_name: str,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    input_stream = sys.stdin if stdin is None else stdin
    error_stream = sys.stderr if stderr is None else stderr
    try:
        interactive = input_stream.isatty() and error_stream.isatty()
    except Exception:
        interactive = False
    if not interactive:
        return
    canonical = LEGACY_TO_CANONICAL[legacy_name]
    print(
        f"Deprecated command `{legacy_name}`; use `{canonical}` instead.",
        file=error_stream,
    )
