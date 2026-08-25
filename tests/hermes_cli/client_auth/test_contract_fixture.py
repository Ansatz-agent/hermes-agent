import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_contract_fixture_matches_native_session_routes_shapes_and_reasons():
    contract_path = REPO_ROOT / "docs" / "contracts" / "native-client-session-v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract == {
        "version": 1,
        "routes": {
            "session": "/auth/api/client-session/",
            "current": "/auth/api/client-session/current/",
            "trace_token": "/auth/api/client-session/trace-token/",
        },
        "headers": {
            "authorization": "Authorization",
            "installation_id": "X-Ansatz-Installation-ID",
        },
        "explicit_revocations": [
            "account_disabled",
            "account_revoked",
            "session_revoked",
        ],
        "transient_codes": ["invalid_session_credential"],
        "issue_request_keys": ["client_version", "installation_id"],
        "issue_response_keys": [
            "account_id",
            "installation_id",
            "issued_at",
            "session_id",
            "session_token",
            "username",
        ],
        "active_status_keys": [
            "account_id",
            "installation_id",
            "server_time",
            "session_id",
            "state",
            "username",
        ],
    }
