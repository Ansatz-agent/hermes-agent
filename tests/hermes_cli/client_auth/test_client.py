import json
from collections.abc import Iterable

import httpx
import pytest

from hermes_cli.client_auth.client import (
    AUTH_ORIGIN,
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthClient,
    AuthServiceError,
    CookieRecord,
    SessionRejected,
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
    location: str = "/agent/",
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


def valid_status_body() -> dict[str, object]:
    return {
        "authenticated": True,
        "username": "alice",
        "server_time": "2026-08-18T12:00:00+00:00",
        "session_expires_at": "2026-09-01T12:00:00+00:00",
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

    result = client.login("alice", bytearray(b"secret"))

    assert result.username == "alice"
    assert [request.url.path for request in requests] == [
        "/agent/accounts/login/",
        "/agent/accounts/login/",
        "/agent/api/session/",
    ]
    assert {request.url.copy_with(path="/") for request in requests} == {
        httpx.URL(f"{AUTH_ORIGIN}/")
    }
    assert set(result.cookies) == {SESSION_COOKIE, CSRF_COOKIE}


def test_login_accepts_django_masked_form_token_distinct_from_cookie_secret():
    client, requests = make_client(
        [
            html_response(
                csrf="masked-csrf-token",
                cookie="agent_history_csrftoken=csrf-secret; Secure; Path=/agent/",
            ),
            redirect_response(
                cookies=(
                    "agent_history_sessionid=session-1; Secure; HttpOnly; Path=/agent/",
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
        "agent_history_sessionid=session-1; HttpOnly; Path=/agent/",
        "agent_history_sessionid=session-1; Secure; HttpOnly; Path=/",
    ],
)
def test_login_rejects_session_cookie_without_secure_agent_scope(set_cookie):
    client, _ = make_client(
        [
            html_response(
                cookie="agent_history_csrftoken=csrf-1; Secure; Path=/agent/"
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


def test_errors_never_include_password_cookie_or_response_body():
    client, _ = make_client(
        [
            html_response(
                cookie="agent_history_csrftoken=csrf-1; Secure; Path=/agent/"
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
    client._http.cookies.set(SESSION_COOKIE, "session-1", path="/agent/")
    client._http.cookies.set(CSRF_COOKIE, "csrf-1", path="/agent/")

    with pytest.raises(AuthServiceError):
        client.logout(valid_cookie_record().cookies)

    assert list(client._http.cookies.jar) == []
