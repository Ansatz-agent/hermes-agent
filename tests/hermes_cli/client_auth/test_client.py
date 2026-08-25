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


ACCOUNT_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
INSTALLATION_ID = "11111111-1111-4111-8111-111111111111"
SESSION_TOKEN = "native-session-token-sentinel-1234567890"


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


def native_response(
    status: int,
    body: dict[str, object] | None = None,
    *,
    content_type: str = "application/json",
    no_store: bool = True,
) -> httpx.Response:
    headers = {"content-type": content_type}
    if no_store:
        headers["cache-control"] = "no-store"
    return httpx.Response(status, headers=headers, json=body)


def native_credential():
    from hermes_cli.client_auth.client import NativeSessionCredential

    return NativeSessionCredential(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        session_token=SESSION_TOKEN,
        installation_id=INSTALLATION_ID,
        username="alice",
        issued_at="2026-08-24T12:00:00+00:00",
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
        "/auth/login/",
        "/auth/login/",
        "/auth/api/session/",
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


def test_second_login_ignores_stale_session_cookies_from_a_prior_login():
    # A native sign-out revokes the bearer session but never clears the
    # client's browser-style cookie jar, so the next login-page GET used to
    # carry the still-valid Django session and receive the authenticated page
    # variant, which has no login CSRF form. Every credential submission must
    # start from a clean jar.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/auth/login/":
            if "cookie" in request.headers:
                return html_response(csrf=None)
            token = "csrf-1" if len(requests) == 1 else "csrf-2"
            return html_response(
                csrf=token,
                cookie=f"__Host-ansatz_csrftoken={token}; Secure; Path=/",
            )
        if request.method == "POST" and request.url.path == "/auth/login/":
            posts = sum(1 for item in requests if item.method == "POST")
            return redirect_response(
                cookies=(
                    f"__Host-ansatz_sessionid=session-{posts}; Secure; HttpOnly; Path=/",
                )
            )
        return json_response(200, valid_status_body())

    client = AuthClient(transport=httpx.MockTransport(handler))

    client.login("alice", bytearray(b"secret"))
    result = client.login("alice", bytearray(b"secret"))

    assert result.username == "alice"
    login_gets = [
        item
        for item in requests
        if item.method == "GET" and item.url.path == "/auth/login/"
    ]
    assert len(login_gets) == 2
    assert "cookie" not in login_gets[1].headers


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

    credential = client.legacy_trace_token(
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
        client.legacy_trace_token(
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
        client.legacy_trace_token(valid_cookie_record().cookies, **params)

    assert requests == []


@pytest.mark.parametrize(
    ("response", "terminal"),
    [
        (
            native_response(
                401,
                {
                    "state": "unavailable",
                    "code": "invalid_session_credential",
                    "retryable": True,
                },
            ),
            False,
        ),
        (native_response(429, {"detail": "rate_limited"}), False),
        (native_response(503, {"detail": "unavailable"}), False),
        (
            httpx.Response(
                200,
                text="bad",
                headers={
                    "content-type": "application/json",
                    "cache-control": "no-store",
                },
            ),
            False,
        ),
        (
            native_response(
                403,
                {
                    "state": "revoked",
                    "code": "session_revoked",
                    "account_id": ACCOUNT_ID,
                    "session_id": SESSION_ID,
                    "revoked_at": "2026-08-24T12:00:00+00:00",
                    "retryable": False,
                },
            ),
            True,
        ),
    ],
)
def test_native_status_only_accepts_matching_structured_revocation(response, terminal):
    from hermes_cli.client_auth.client import ExplicitSessionRevocation

    client, _ = make_client([response])
    if terminal:
        with pytest.raises(ExplicitSessionRevocation) as caught:
            client.client_session_status(native_credential())
        assert (
            caught.value.account_id,
            caught.value.session_id,
            caught.value.reason,
            caught.value.code,
        ) == (ACCOUNT_ID, SESSION_ID, "session_revoked", "session_revoked")
    else:
        with pytest.raises(AuthServiceError) as caught:
            client.client_session_status(native_credential())
        assert not isinstance(caught.value, ExplicitSessionRevocation)


def test_native_status_rejects_non_rfc3339_server_time():
    client, _ = make_client(
        [
            native_response(
                200,
                {
                    "state": "active",
                    "account_id": ACCOUNT_ID,
                    "session_id": SESSION_ID,
                    "installation_id": INSTALLATION_ID,
                    "username": "alice",
                    "server_time": "2026-08-24 12:00:00+00:00",
                },
            )
        ]
    )

    with pytest.raises(AuthServiceError, match="invalid_response"):
        client.client_session_status(native_credential())


def test_native_status_treats_non_string_revocation_code_as_nonterminal_schema_drift():
    from hermes_cli.client_auth.client import ExplicitSessionRevocation

    client, _ = make_client(
        [
            native_response(
                403,
                {
                    "state": "revoked",
                    "code": ["session_revoked"],
                    "account_id": ACCOUNT_ID,
                    "session_id": SESSION_ID,
                    "revoked_at": "2026-08-24T12:00:00+00:00",
                    "retryable": False,
                },
            )
        ]
    )

    with pytest.raises(AuthServiceError, match="invalid_response") as caught:
        client.client_session_status(native_credential())
    assert not isinstance(caught.value, ExplicitSessionRevocation)


def test_cookie_session_methods_remain_available_under_explicit_legacy_names():
    client, _ = make_client([json_response(200, valid_status_body())])

    status = client.legacy_status(valid_cookie_record().cookies)

    assert status.username == "alice"


def test_native_session_issue_uses_fixed_web_bootstrap_contract_and_immutable_credential(
):
    from dataclasses import FrozenInstanceError

    client, requests = make_client(
        [
            native_response(
                201,
                {
                    "account_id": ACCOUNT_ID,
                    "session_id": SESSION_ID,
                    "session_token": SESSION_TOKEN,
                    "installation_id": INSTALLATION_ID,
                    "username": "alice",
                    "issued_at": "2026-08-24T12:00:00+00:00",
                },
            )
        ]
    )

    credential = client.issue_client_session(
        valid_cookie_record().cookies,
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )

    assert credential == native_credential()
    with pytest.raises(FrozenInstanceError):
        credential.username = "mallory"  # type: ignore[misc]
    request = requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL(f"{AUTH_ORIGIN}/auth/api/client-session/")
    assert request.headers["cookie"] == (
        "__Host-ansatz_sessionid=session-1; __Host-ansatz_csrftoken=csrf-1"
    )
    assert request.headers["x-csrftoken"] == "csrf-1"
    assert request.headers["referer"] == f"{AUTH_ORIGIN}/auth/"
    assert json.loads(request.content) == {
        "installation_id": INSTALLATION_ID,
        "client_version": "0.17.0",
    }


def test_native_status_trace_and_logout_use_only_single_bearer_auth_headers():
    client, requests = make_client(
        [
            native_response(
                200,
                {
                    "state": "active",
                    "account_id": ACCOUNT_ID,
                    "session_id": SESSION_ID,
                    "installation_id": INSTALLATION_ID,
                    "username": "alice",
                    "server_time": "2026-08-24T12:01:00+00:00",
                },
            ),
            trace_response(),
            httpx.Response(204, headers={"cache-control": "no-store"}),
        ]
    )
    client._http.cookies.set(SESSION_COOKIE, "cookie-must-not-leak", path="/")
    credential = native_credential()

    status = client.client_session_status(credential)
    trace = client.trace_token(credential)
    client.logout_client_session(credential)

    assert status.server_time == "2026-08-24T12:01:00+00:00"
    assert trace.installation_id == INSTALLATION_ID
    assert [request.url.path for request in requests] == [
        "/auth/api/client-session/",
        "/auth/api/client-session/trace-token/",
        "/auth/api/client-session/current/",
    ]
    assert [request.method for request in requests] == ["GET", "POST", "DELETE"]
    for request in requests:
        assert request.headers.get_list("authorization") == [f"Bearer {SESSION_TOKEN}"]
        assert request.headers.get_list("x-ansatz-installation-id") == [INSTALLATION_ID]
        assert "cookie" not in request.headers
        assert "x-csrftoken" not in request.headers


@pytest.mark.parametrize(
    "body",
    [
        {
            "state": "revoked",
            "code": "session_revoked",
            "account_id": ACCOUNT_ID,
            "session_id": "44444444-4444-4444-8444-444444444444",
            "revoked_at": "2026-08-24T12:00:00+00:00",
            "retryable": False,
        },
        {
            "state": "revoked",
            "code": "unknown",
            "account_id": ACCOUNT_ID,
            "session_id": SESSION_ID,
            "revoked_at": "2026-08-24T12:00:00+00:00",
            "retryable": False,
        },
        {
            "state": "revoked",
            "code": "session_revoked",
            "account_id": ACCOUNT_ID,
            "session_id": SESSION_ID,
            "revoked_at": "2026-08-24T12:00:00+00:00",
            "retryable": True,
        },
    ],
)
def test_native_status_never_treats_unknown_or_mismatched_forbidden_response_as_terminal(
    body,
):
    from hermes_cli.client_auth.client import ExplicitSessionRevocation

    client, _ = make_client([native_response(403, body)])

    with pytest.raises(AuthServiceError, match="invalid_response") as caught:
        client.client_session_status(native_credential())
    assert not isinstance(caught.value, ExplicitSessionRevocation)


@pytest.mark.parametrize(
    "response",
    [
        native_response(
            200,
            {
                "state": "active",
                "account_id": ACCOUNT_ID,
                "session_id": SESSION_ID,
                "installation_id": INSTALLATION_ID,
                "username": "alice",
                "server_time": "2026-08-24T12:00:00+00:00",
            },
            no_store=False,
        ),
        native_response(
            200,
            {
                "state": "active",
                "account_id": ACCOUNT_ID,
                "session_id": SESSION_ID,
                "installation_id": INSTALLATION_ID,
                "username": "alice",
                "server_time": "2026-08-24T12:00:00+00:00",
            },
            content_type="text/plain",
        ),
    ],
)
def test_native_status_rejects_cacheable_or_wrong_media_type_responses(response):
    client, _ = make_client([response])

    with pytest.raises(AuthServiceError, match="invalid_response"):
        client.client_session_status(native_credential())


@pytest.mark.parametrize(
    "response",
    [
        native_response(
            401,
            {
                "state": "unavailable",
                "code": "invalid_session_credential",
                "retryable": True,
            },
        ),
        native_response(429, {"detail": "rate_limited"}),
        native_response(503, {"detail": "unavailable"}),
        native_response(403, {"detail": "not-a-contract"}),
    ],
)
def test_native_status_failures_are_nonterminal_and_do_not_render_bearer_secret(
    response,
):
    from hermes_cli.client_auth.client import ExplicitSessionRevocation

    client, _ = make_client([response])

    with pytest.raises(AuthServiceError) as caught:
        client.client_session_status(native_credential())
    assert not isinstance(caught.value, ExplicitSessionRevocation)
    assert SESSION_TOKEN not in repr(caught.value)


def test_native_network_failure_is_nonterminal_and_does_not_render_bearer_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns unavailable", request=request)

    client = AuthClient(transport=httpx.MockTransport(handler))

    with pytest.raises(AuthServiceError) as caught:
        client.client_session_status(native_credential())
    assert SESSION_TOKEN not in repr(caught.value)


def test_native_trace_token_rejects_cookie_credential_without_a_network_request():
    client, requests = make_client([])

    with pytest.raises(AuthServiceError, match="invalid_request"):
        client.trace_token(valid_cookie_record().cookies)  # type: ignore[arg-type]

    assert requests == []


def test_native_and_trace_credentials_do_not_render_tokens():
    credential = native_credential()
    trace = TraceCredential(
        access_token="trace-secret-sentinel-1234567890",
        expires_at="2099-08-23T14:15:00+00:00",
        expires_in=900,
        installation_id=INSTALLATION_ID,
    )

    for value, secret in (
        (credential, SESSION_TOKEN),
        (trace, "trace-secret-sentinel-1234567890"),
    ):
        assert secret not in repr(value)
        assert secret not in str(value)


def test_cookie_record_does_not_render_session_or_csrf_secrets():
    record = CookieRecord(
        cookies={
            SESSION_COOKIE: "cookie-session-secret-sentinel",
            CSRF_COOKIE: "cookie-csrf-secret-sentinel",
        },
        username="alice",
        session_expires_at="2026-09-01T12:00:00+00:00",
    )

    for rendered in (repr(record), str(record)):
        assert "cookie-session-secret-sentinel" not in rendered
        assert "cookie-csrf-secret-sentinel" not in rendered


@pytest.mark.parametrize(
    "expires_at",
    [
        "2099-08-23 14:15:00+00:00",
        "2099-08-23T14:15:00",
        "2099-08-23T25:15:00+00:00",
    ],
)
def test_native_trace_token_rejects_non_rfc3339_expiry(expires_at):
    client, _ = make_client(
        [
            trace_response(
                body={
                    "access_token": "trace-token-sentinel-1234567890",
                    "expires_at": expires_at,
                    "expires_in": 900,
                    "installation_id": INSTALLATION_ID,
                }
            )
        ]
    )

    with pytest.raises(AuthServiceError, match="invalid_response"):
        client.trace_token(native_credential())
