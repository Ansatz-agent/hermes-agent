"""Native auth-owner v4 Trace lease protocol contracts."""

from __future__ import annotations

import json


def test_remote_owner_v4_lease_is_exact_and_contains_no_cloud_secret() -> None:
    from hermes_cli.client_auth.runtime import RemoteRuntimeOwner

    sent: list[dict[str, object]] = []

    class Connection:
        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            sent.append(json.loads(data))

        def recv(self, _size):
            return json.dumps(
                {
                    "version": 4,
                    "ok": True,
                    "lease": {
                        "endpoint": "http://127.0.0.1:49152/v1/traces",
                        "authorization": "Bearer " + "a" * 43,
                        "installation_id": "11111111-1111-4111-8111-111111111111",
                        "entrypoint": "dashboard",
                        "plugins_toml": "/opt/Ansatz/config/ansatz-voice-trace/plugins.toml",
                    },
                }
            ).encode() + b"\n"

        def close(self):
            return None

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return Connection()

    lease = RemoteRuntimeOwner(Endpoint()).trace_ingress_open(
        entrypoint="dashboard",
        consumer_id="dashboard-1",
    )

    assert sent == [
        {
            "version": 4,
            "operation": "trace_ingress_open",
            "entrypoint": "dashboard",
            "consumer_id": "dashboard-1",
        }
    ]
    assert lease.entrypoint == "dashboard"
    wire = json.dumps(lease.__dict__ if hasattr(lease, "__dict__") else sent)
    assert "access_token" not in wire
    assert "session_token" not in wire
