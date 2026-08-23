"""Patch only test subprocesses onto their isolated native auth endpoint."""

from __future__ import annotations

import os
from pathlib import Path


root = os.environ.get("HERMES_AUTH_TEST_RUNTIME_ROOT")
if root and os.name != "nt":
    import hermes_cli.client_auth.runtime as runtime

    endpoint = runtime.UnixEndpoint.for_directory(
        Path(root),
        random_name="0123456789abcdef0123456789abcdef",
    )
    runtime.runtime_endpoint = lambda: endpoint
