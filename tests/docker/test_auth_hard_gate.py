from __future__ import annotations

import json
import time

from tests.docker.conftest import (
    docker_exec,
    docker_exec_sh,
    restart_container,
    start_container,
)


def _service_state(container: str, service: str) -> str:
    result = docker_exec(
        container,
        "/command/s6-svstat",
        f"/run/service/{service}",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.casefold()


def _auth_status(container: str) -> dict[str, object]:
    result = docker_exec(container, "hermes", "auth", "status")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_real_container_boots_locked_and_stays_locked_after_restart(
    built_image: str,
    container_name: str,
):
    """Real s6 boot/reboot cannot start a Hermes capability while signed out."""
    start_container(
        built_image,
        container_name,
        "HERMES_GATEWAY_BOOTSTRAP_STATE=running",
        "HERMES_DASHBOARD=true",
        cmd="sleep 120",
    )

    assert "up" in _service_state(container_name, "hermes-auth-runtime")
    assert "down" in _service_state(container_name, "dashboard")
    assert "down" in _service_state(container_name, "main-hermes")
    assert "down" in _service_state(container_name, "gateway-default")
    assert _auth_status(container_name)["state"] == "signed_out"

    owner = docker_exec_sh(
        container_name,
        "ps -eo user,args | grep '[h]ermes_cli.client_auth.runtime owner'",
        user="root",
    )
    assert owner.returncode == 0, owner.stderr or owner.stdout
    assert owner.stdout.lstrip().startswith("hermes ")
    runtime_dir = docker_exec(
        container_name,
        "stat",
        "-c",
        "%a:%U:%G",
        "/run/hermes-auth",
        user="root",
    )
    assert runtime_dir.stdout.strip() == "700:hermes:hermes"
    persistent_runtime = docker_exec(
        container_name,
        "test",
        "-e",
        "/opt/data/hermes-auth",
    )
    assert persistent_runtime.returncode != 0

    denied = docker_exec(container_name, "hermes", "gateway", "status")
    assert denied.returncode == 20
    assert "AUTH_REQUIRED" in denied.stderr

    # Even a forced supervisor up cannot cross the run-script backstop.
    forced = docker_exec(
        container_name,
        "/command/s6-svc",
        "-u",
        "/run/service/dashboard",
    )
    assert forced.returncode == 0, forced.stderr
    time.sleep(1)
    processes = docker_exec_sh(
        container_name,
        "ps -eo args | grep '[h]ermes dashboard' || true",
        user="root",
    )
    assert processes.stdout.strip() == ""
    port = docker_exec(
        container_name,
        "python3",
        "-c",
        (
            "import socket; s=socket.socket(); s.settimeout(.2); "
            "raise SystemExit(0 if s.connect_ex(('127.0.0.1',9119)) else 1)"
        ),
    )
    assert port.returncode == 0, "dashboard bound port 9119 while signed out"

    secret_scan = docker_exec_sh(
        container_name,
        (
            "grep -R -l -E 'agent_history_(sessionid|csrftoken)=' "
            "/opt/data /run/hermes-auth 2>/dev/null || true"
        ),
        user="root",
    )
    assert secret_scan.stdout.strip() == ""

    restart_container(container_name)
    assert "up" in _service_state(container_name, "hermes-auth-runtime")
    assert "down" in _service_state(container_name, "dashboard")
    assert "down" in _service_state(container_name, "gateway-default")
    assert _auth_status(container_name)["state"] == "signed_out"
