"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading

import pytest
import yaml

from tests.hermes_cli.client_auth.subprocess_harness import (
    authenticated_module_command,
    authenticated_subprocess_environment,
)

pytest.importorskip("mcp.server.fastmcp")


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    env, runtime_root = authenticated_subprocess_environment(os.environ.copy())
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    marker = "profile-local-61922"
    server = tmp_path / "fastmcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                        "env": {
                            "PYTHONPATH": env["PYTHONPATH"],
                            "HERMES_AUTH_TEST_RUNTIME_ROOT": str(runtime_root),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        authenticated_module_command(
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdout = proc.stdout
        threading.Thread(
            target=lambda: output.put(stdout.readline()),
            daemon=True,
        ).start()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        try:
            line = output.get(timeout=10)
        except queue.Empty:
            pytest.fail("slash worker produced no /tools response within 10 seconds")
        response = json.loads(line)
        assert response["ok"] is True
        assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(runtime_root)
