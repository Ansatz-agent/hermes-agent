import json
from collections.abc import Iterable

import httpx
import pytest

from hermes_cli.client_auth.client import (
    AUTH_ORIGIN,
    CSRF_COOKIE,
    SESSION_COOKIE,
    TRACE_TOKEN_PATH,
    AuthClient,
    AuthServiceError,
    CookieRecord,
    SessionRejected,
    TraceCredential,
)


def html_response(
    status: int = 200,
    *,
    csrf: str | None = "csrf-1",
    cookie: str | None = None,
) -> httpx.Response:
    hidden = (
        f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">'
        if csrf is not None
        else ""
    )
    headers = {"content-type": "text/html; charset=utf-8"}
    if cookie is not None:
        headers["set-cookie"] = cookie
    return httpx.Response(status, headers=headers, text=f"<form>{hidden}</form>")


def redirect_response(
    status: int = 302,
    *,
    location: str = "/traces/",
    cookies: Iterable[str] = (),
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=[("location", location), *(("set-cookie", item) for item in cookies)],
    )


def json_response(status: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def trace_response(
    status: int = 200,
    body: dict[str, object] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={
            "content-type": "application/json",
            "cache-control": "private, no-store",
        },
        content=json.dumps(
            body
            or {
                "access_token": "trace-token-sentinel-1234567890",
                "expires_at": "2099-08-23T14:15:00+00:00",
                "expires_in": 900,
                "installation_id": "11111111-1111-4111-8111-111111111111",
            }
        ).encode(),
    )


def valid_status_body() -> dict[str, object]:
    return {
        "authenticated": True,
        "sub": "7",
        "username": "alice",
        "role": "user",
        "server_time": "2026-08-18T12:00:00+00:00",
        "session_expires_at": "2026-09-01T12:00:00+00:00",
        "trace_dashboard_url": "/traces/",
    }


def valid_cookie_record() -> CookieRecord:
    return CookieRecord(
        cookies={SESSION_COOKIE: "session-1", CSRF_COOKIE: "csrf-1"},
        username="alice",
        session_expires_at="2026-09-01T12:00:00+00:00",
    )


def make_client(responses: Iterable[httpx.Response]):
    queued = iter(responses)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            return next(queued)
        except StopIteration as error:
            raise AssertionError("unexpected request") from error

    return AuthClient(transport=httpx.MockTransport(handler)), requests


def test_login_uses_only_fixed_origin_and_cookie_names():
    client, requests = make_client(
        [
            html_response(
                cookie=(
                    "__Host-ansatz_csrftoken=csrf-1; Secure; Path=/; SameSite=Lax"
                )
            ),
            redirect_response(
                cookies=(
                    "__Host-ansatz_sessionid=session-1; Secure; HttpOnly; "
                    "Path=/; SameSite=Lax",
                )
            ),
            json_response(200, valid_status_body()),
        ]
    )

    result = client.login("alice", bytearray(b"secret"))

    assert result.username == "alice"
    assert [request.url.path for request in requests] == [
        "/auth/login/",
        "/auth/login/",
        "/auth/api/session/",
    ]
    assert {request.url.copy_with(path="/") for request in requests} == {
        httpx.URL(f"{AUTH_ORIGIN}/")
    }
    assert set(result.cookies) == {SESSION_COOKIE, CSRF_COOKIE}


def test_login_falls_back_to_the_tls_verified_direct_route_after_a_network_failure():
    fallback_responses = iter(
        [
            html_response(
                cookie=(
                    "agent_history_csrftoken=csrf-1; Secure; Path=/agent/; SameSite=Lax"
                )
            ),
            redirect_response(
                cookies=(
                    "agent_history_sessionid=session-1; Secure; HttpOnly; "
                    "Path=/agent/; SameSite=Lax",
                )
            ),
            json_response(200, valid_status_body()),
        ]
    )
    primary_requests: list[httpx.Request] = []
    fallback_requests: list[httpx.Request] = []

    def primary_handler(request: httpx.Request) -> httpx.Response:
        primary_requests.append(request)
        raise httpx.ConnectTimeout("environment route unavailable", request=request)

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        fallback_requests.append(request)
        return next(fallback_responses)

    client = AuthClient(
        transport=httpx.MockTransport(primary_handler),
        fallback_transport=httpx.MockTransport(fallback_handler),
    )

    result = client.login("alice", bytearray(b"secret"))

    assert result.username == "alice"
    assert len(primary_requests) == 1
    assert [request.url.path for request in fallback_requests] == [
        "/agent/accounts/login/",
        "/agent/accounts/login/",
        "/agent/api/session/",
    ]
    assert {request.url.copy_with(path="/") for request in fallback_requests} == {
        httpx.URL(f"{AUTH_ORIGIN}/")
    }


def test_login_accepts_django_masked_form_token_distinct_from_cookie_secret():
    client, requests = make_client(
        [
            html_response(
                csrf="masked-csrf-token",
                cookie="__Host-ansatz_csrftoken=csrf-secret; Secure; Path=/",
            ),
            redirect_response(
                cookies=(
                    "__Host-ansatz_sessionid=session-1; Secure; HttpOnly; Path=/",
                )
            ),
            json_response(200, valid_status_body()),
        ]
    )

    client.login("alice", bytearray(b"secret"))

    assert "csrfmiddlewaretoken=masked-csrf-token" in requests[1].content.decode()


@pytest.mark.parametrize(
    "bad",
    [
        html_response(200),
        redirect_response(302, location="https://evil.example/"),
        json_response(200, {"authenticated": True}),
    ],
)
def test_status_rejects_html_cross_origin_and_schema_drift(bad):
    client, _ = make_client([bad])

    with pytest.raises(AuthServiceError):
        client.status(valid_cookie_record().cookies)


@pytest.mark.parametrize(
    "set_cookie",
    [
        "__Host-ansatz_sessionid=session-1; HttpOnly; Path=/",
        "__Host-ansatz_sessionid=session-1; Secure; HttpOnly; Path=/auth/",
        "__Host-ansatz_sessionid=session-1; Secure; HttpOnly; Path=/; Domain=c2sml.cn",
    ],
)
def test_login_rejects_session_cookie_without_secure_host_scope(set_cookie):
    client, _ = make_client(
        [
            html_response(
                cookie="__Host-ansatz_csrftoken=csrf-1; Secure; Path=/"
            ),
            redirect_response(cookies=(set_cookie,)),
        ]
    )

    with pytest.raises(AuthServiceError, match="invalid_cookie"):
        client.login("alice", bytearray(b"secret"))


def test_login_rejects_missing_csrf_without_posting_password():
    client, requests = make_client([html_response(csrf=None)])

    with pytest.raises(AuthServiceError, match="invalid_csrf"):
        client.login("alice", bytearray(b"secret"))

    assert len(requests) == 1


def test_status_rejects_unauthenticated_response_with_typed_error():
    client, _ = make_client([json_response(401, {"authenticated": False})])

    with pytest.raises(SessionRejected, match="session_rejected"):
        client.status(valid_cookie_record().cookies)


def test_status_parses_stable_identity_role_and_dashboard_path():
    client, _ = make_client([json_response(200, valid_status_body())])

    status = client.status(valid_cookie_record().cookies)

    assert status.sub == "7"
    assert status.username == "alice"
    assert status.role == "user"
    assert status.trace_dashboard_url == "/traces/"


def test_errors_never_include_password_cookie_or_response_body():
    client, _ = make_client(
        [
            html_response(
                cookie="__Host-ansatz_csrftoken=csrf-1; Secure; Path=/"
            ),
            httpx.Response(
                500,
                headers={"content-type": "text/plain"},
                text="secret session-1 diagnostic",
            ),
        ]
    )

    with pytest.raises(AuthServiceError) as caught:
        client.login("alice", bytearray(b"secret"))

    rendered = repr(caught.value)
    assert "secret" not in rendered
    assert "session-1" not in rendered
    assert "diagnostic" not in rendered


def test_logout_clears_local_cookie_jar_when_server_is_unavailable():
    client, _ = make_client([httpx.Response(503)])
    client._http.cookies.set(SESSION_COOKIE, "session-1", path="/")
    client._http.cookies.set(CSRF_COOKIE, "csrf-1", path="/")

    with pytest.raises(AuthServiceError):
        client.logout(valid_cookie_record().cookies)

    assert list(client._http.cookies.jar) == []


def test_trace_token_uses_fixed_authenticated_route_and_exact_installation_identity():
    client, requests = make_client([trace_response()])

    credential = client.trace_token(
        valid_cookie_record().cookies,
        installation_id="11111111-1111-4111-8111-111111111111",
        client_version="0.17.0",
        telemetry_schema_version="1",
    )

    assert credential == TraceCredential(
        access_token="trace-token-sentinel-1234567890",
        expires_at="2099-08-23T14:15:00+00:00",
        expires_in=900,
        installation_id="11111111-1111-4111-8111-111111111111",
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL(f"{AUTH_ORIGIN}{TRACE_TOKEN_PATH}")
    assert request.headers["cookie"] == (
        "__Host-ansatz_sessionid=session-1; __Host-ansatz_csrftoken=csrf-1"
    )
    assert request.headers["x-csrftoken"] == "csrf-1"
    assert request.headers["referer"] == f"{AUTH_ORIGIN}/auth/"
    assert json.loads(request.content) == {
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "client_version": "0.17.0",
        "telemetry_schema_version": "1",
    }


@pytest.mark.parametrize(
    "response",
    [
        trace_response(body={"access_token": "too-short"}),
        trace_response(
            body={
                "access_token": "trace-token-sentinel-1234567890",
                "expires_at": "2000-08-23T14:15:00+00:00",
                "expires_in": 900,
                "installation_id": "11111111-1111-4111-8111-111111111111",
            }
        ),
        trace_response(
            body={
                "access_token": "trace-token-sentinel-1234567890",
                "expires_at": "2099-08-23T14:15:00+00:00",
                "expires_in": 901,
                "installation_id": "11111111-1111-4111-8111-111111111111",
            }
        ),
        trace_response(
            body={
                "access_token": "trace-token-sentinel-1234567890",
                "expires_at": "2099-08-23T14:15:00+00:00",
                "expires_in": 900,
                "installation_id": "22222222-2222-4222-8222-222222222222",
            }
        ),
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "access_token": "trace-token-sentinel-1234567890",
                "expires_at": "2099-08-23T14:15:00+00:00",
                "expires_in": 900,
                "installation_id": "11111111-1111-4111-8111-111111111111",
            },
        ),
    ],
)
def test_trace_token_rejects_schema_drift_stale_credentials_and_cacheable_responses(response):
    client, _ = make_client([response])

    with pytest.raises(AuthServiceError, match="invalid_response"):
        client.trace_token(
            valid_cookie_record().cookies,
            installation_id="11111111-1111-4111-8111-111111111111",
            client_version="0.17.0",
            telemetry_schema_version="1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("installation_id", "not-a-uuid"),
        ("client_version", ""),
        ("telemetry_schema_version", "0"),
    ],
)
def test_trace_token_rejects_invalid_request_before_network(field, value):
    client, requests = make_client([])
    params = {
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "client_version": "0.17.0",
        "telemetry_schema_version": "1",
    }
    params[field] = value

    with pytest.raises(AuthServiceError, match="invalid_request"):
        client.trace_token(valid_cookie_record().cookies, **params)

    assert requests == []
