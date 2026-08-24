from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser

import httpx

AUTH_ORIGIN = "https://c2sml.cn"
AUTH_PREFIX = "/auth"
LOGIN_PATH = f"{AUTH_PREFIX}/login/"
LOGOUT_PATH = f"{AUTH_PREFIX}/logout/"
SESSION_PATH = f"{AUTH_PREFIX}/api/session/"
TRACE_TOKEN_PATH = f"{AUTH_PREFIX}/api/trace-token/"
SESSION_COOKIE = "__Host-ansatz_sessionid"
CSRF_COOKIE = "__Host-ansatz_csrftoken"

_COOKIE_NAMES = frozenset({SESSION_COOKIE, CSRF_COOKIE})
_STATUS_KEYS = frozenset(
    {
        "authenticated",
        "sub",
        "username",
        "role",
        "server_time",
        "session_expires_at",
        "trace_dashboard_url",
    }
)
_TRACE_CREDENTIAL_KEYS = frozenset(
    {"access_token", "expires_at", "expires_in", "installation_id"}
)
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CLIENT_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SCHEMA_VERSION = re.compile(r"^[1-9][0-9]{0,15}$")


class AuthServiceError(RuntimeError):
    def __init__(self, reason: str = "server_unavailable") -> None:
        super().__init__(reason)
        self.reason = reason


class SessionRejected(AuthServiceError):
    def __init__(self) -> None:
        super().__init__("session_rejected")


class RateLimited(AuthServiceError):
    def __init__(self) -> None:
        super().__init__("rate_limited")


@dataclass(frozen=True)
class CookieRecord:
    cookies: dict[str, str]
    username: str
    session_expires_at: str


@dataclass(frozen=True)
class SessionStatus:
    sub: str
    username: str
    role: str
    server_time: str
    session_expires_at: str
    trace_dashboard_url: str


@dataclass(frozen=True)
class TraceCredential:
    access_token: str
    expires_at: str
    expires_in: int
    installation_id: str


class AuthClient:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=AUTH_ORIGIN,
            transport=transport,
            follow_redirects=False,
            timeout=15.0,
        )

    def login(self, username: str, password: bytearray) -> CookieRecord:
        if not isinstance(username, str) or not username.strip():
            raise AuthServiceError("invalid_credentials")
        try:
            decoded_password = password.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise AuthServiceError("invalid_credentials") from None

        login_page = self._request("GET", LOGIN_PATH)
        if login_page.status_code != 200 or not _is_content_type(login_page, "text/html"):
            raise AuthServiceError("invalid_response")
        csrf = _extract_csrf(login_page.text)
        _require_cookie(self._http.cookies.jar, CSRF_COOKIE)

        try:
            response = self._request(
                "POST",
                LOGIN_PATH,
                data={
                    "csrfmiddlewaretoken": csrf,
                    "username": username,
                    "password": decoded_password,
                },
                headers={
                    "Referer": f"{AUTH_ORIGIN}{LOGIN_PATH}",
                    "X-CSRFToken": csrf,
                },
            )
        finally:
            decoded_password = ""
        if response.status_code == 200:
            raise AuthServiceError("invalid_credentials")
        if response.status_code != 302:
            raise AuthServiceError("invalid_response")
        _require_same_origin_redirect(response)

        cookies = _validated_cookie_record(self._http.cookies.jar)
        status = self.status(cookies)
        return CookieRecord(cookies, status.username, status.session_expires_at)

    def status(self, cookies: Mapping[str, str]) -> SessionStatus:
        normalized = _validate_cookie_mapping(cookies)
        response = self._request(
            "GET",
            SESSION_PATH,
            headers={"Cookie": _cookie_header(normalized)},
        )
        return _parse_session_status(response)

    def logout(self, cookies: Mapping[str, str]) -> None:
        normalized = _validate_cookie_mapping(cookies)
        csrf = normalized[CSRF_COOKIE]
        try:
            response = self._request(
                "POST",
                LOGOUT_PATH,
                data={"csrfmiddlewaretoken": csrf},
                headers={
                    "Cookie": _cookie_header(normalized),
                    "Referer": f"{AUTH_ORIGIN}{LOGIN_PATH}",
                    "X-CSRFToken": csrf,
                },
            )
            if response.status_code == 302:
                _require_same_origin_redirect(response)
            elif response.status_code != 200:
                raise AuthServiceError("invalid_response")
        finally:
            self._http.cookies.clear()

    def trace_token(
        self,
        cookies: Mapping[str, str],
        *,
        installation_id: str,
        client_version: str,
        telemetry_schema_version: str,
    ) -> TraceCredential:
        normalized = _validate_cookie_mapping(cookies)
        _validate_trace_request(
            installation_id=installation_id,
            client_version=client_version,
            telemetry_schema_version=telemetry_schema_version,
        )
        csrf = normalized[CSRF_COOKIE]
        response = self._request(
            "POST",
            TRACE_TOKEN_PATH,
            json={
                "installation_id": installation_id,
                "client_version": client_version,
                "telemetry_schema_version": telemetry_schema_version,
            },
            headers={
                "Cookie": _cookie_header(normalized),
                "Referer": f"{AUTH_ORIGIN}{AUTH_PREFIX}/",
                "X-CSRFToken": csrf,
            },
        )
        return _parse_trace_credential(
            response,
            expected_installation_id=installation_id,
        )

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        if path not in {LOGIN_PATH, LOGOUT_PATH, SESSION_PATH, TRACE_TOKEN_PATH}:
            raise AuthServiceError("invalid_request")
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise AuthServiceError("server_unavailable") from None
        if response.status_code == 429:
            raise RateLimited()
        if response.status_code >= 500:
            raise AuthServiceError("server_unavailable")
        return response


class _CsrfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "input":
            return
        values = dict(attrs)
        if values.get("name") != "csrfmiddlewaretoken":
            return
        value = values.get("value")
        if isinstance(value, str) and value:
            self.values.append(value)


def _extract_csrf(document: str) -> str:
    parser = _CsrfParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception:
        raise AuthServiceError("invalid_csrf") from None
    if len(parser.values) != 1:
        raise AuthServiceError("invalid_csrf")
    return parser.values[0]


def _require_same_origin_redirect(response: httpx.Response) -> None:
    location = response.headers.get("location")
    if not location:
        raise AuthServiceError("invalid_redirect")
    try:
        target = httpx.URL(AUTH_ORIGIN).join(location)
    except Exception:
        raise AuthServiceError("invalid_redirect") from None
    if (
        target.scheme != "https"
        or target.host != httpx.URL(AUTH_ORIGIN).host
        or target.port != httpx.URL(AUTH_ORIGIN).port
        or not (
            target.path.startswith(f"{AUTH_PREFIX}/")
            or target.path.startswith("/traces/")
        )
    ):
        raise AuthServiceError("invalid_redirect")


def _require_cookie(jar: object, name: str):
    matches = [cookie for cookie in jar if cookie.name == name]
    if len(matches) != 1:
        raise AuthServiceError("invalid_cookie")
    cookie = matches[0]
    if (
        not cookie.value
        or not cookie.secure
        or cookie.path != "/"
        or cookie.domain_specified
    ):
        raise AuthServiceError("invalid_cookie")
    return cookie


def _validated_cookie_record(jar: object) -> dict[str, str]:
    cookies = list(jar)
    if {cookie.name for cookie in cookies} != _COOKIE_NAMES:
        raise AuthServiceError("invalid_cookie")
    return {
        name: _require_cookie(cookies, name).value
        for name in (SESSION_COOKIE, CSRF_COOKIE)
    }


def _validate_cookie_mapping(cookies: Mapping[str, str]) -> dict[str, str]:
    if set(cookies) != _COOKIE_NAMES:
        raise AuthServiceError("invalid_cookie")
    normalized: dict[str, str] = {}
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        value = cookies.get(name)
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n;"):
            raise AuthServiceError("invalid_cookie")
        normalized[name] = value
    return normalized


def _cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(
        f"{name}={cookies[name]}" for name in (SESSION_COOKIE, CSRF_COOKIE)
    )


def _parse_session_status(response: httpx.Response) -> SessionStatus:
    if not _is_content_type(response, "application/json"):
        raise AuthServiceError("invalid_response")
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        raise AuthServiceError("invalid_response") from None
    if response.status_code == 401:
        if body != {"authenticated": False}:
            raise AuthServiceError("invalid_response")
        raise SessionRejected()
    if response.status_code != 200 or not isinstance(body, dict):
        raise AuthServiceError("invalid_response")
    if set(body) != _STATUS_KEYS or body.get("authenticated") is not True:
        raise AuthServiceError("invalid_response")

    sub = body.get("sub")
    username = body.get("username")
    role = body.get("role")
    server_time = body.get("server_time")
    session_expires_at = body.get("session_expires_at")
    trace_dashboard_url = body.get("trace_dashboard_url")
    if (
        not isinstance(sub, str)
        or not 1 <= len(sub) <= 128
        or any(character.isspace() or ord(character) < 32 for character in sub)
        or not isinstance(username, str)
        or not 1 <= len(username) <= 150
        or role not in {"user", "admin"}
        or trace_dashboard_url != "/traces/"
        or not all(
            isinstance(value, str) and value
            for value in (server_time, session_expires_at)
        )
    ):
        raise AuthServiceError("invalid_response")
    server_datetime = _parse_aware_datetime(server_time)
    expiry_datetime = _parse_aware_datetime(session_expires_at)
    if expiry_datetime <= server_datetime:
        raise SessionRejected()
    return SessionStatus(
        sub=sub,
        username=username,
        role=role,
        server_time=server_time,
        session_expires_at=session_expires_at,
        trace_dashboard_url=trace_dashboard_url,
    )


def _validate_trace_request(
    *,
    installation_id: str,
    client_version: str,
    telemetry_schema_version: str,
) -> None:
    if (
        not isinstance(installation_id, str)
        or _UUID_V4.fullmatch(installation_id) is None
        or not isinstance(client_version, str)
        or _CLIENT_VERSION.fullmatch(client_version) is None
        or not isinstance(telemetry_schema_version, str)
        or _SCHEMA_VERSION.fullmatch(telemetry_schema_version) is None
    ):
        raise AuthServiceError("invalid_request")


def _parse_trace_credential(
    response: httpx.Response,
    *,
    expected_installation_id: str,
) -> TraceCredential:
    if response.status_code == 401:
        raise SessionRejected()
    cache_directives = {
        directive.partition("=")[0].strip().casefold()
        for directive in response.headers.get("cache-control", "").split(",")
    }
    if (
        response.status_code not in {200, 201}
        or not _is_content_type(response, "application/json")
        or "no-store" not in cache_directives
    ):
        raise AuthServiceError("invalid_response")
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        raise AuthServiceError("invalid_response") from None
    if not isinstance(body, dict) or set(body) != _TRACE_CREDENTIAL_KEYS:
        raise AuthServiceError("invalid_response")
    access_token = body.get("access_token")
    expires_at = body.get("expires_at")
    expires_in = body.get("expires_in")
    installation_id = body.get("installation_id")
    if (
        not isinstance(access_token, str)
        or not 20 <= len(access_token) <= 4096
        or any(character in access_token for character in "\r\n")
        or not isinstance(expires_at, str)
        or len(expires_at) > 128
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= 900
        or installation_id != expected_installation_id
        or not isinstance(installation_id, str)
        or _UUID_V4.fullmatch(installation_id) is None
    ):
        raise AuthServiceError("invalid_response")
    expiry = _parse_aware_datetime(expires_at)
    if expiry <= datetime.now(tz=expiry.tzinfo):
        raise AuthServiceError("invalid_response")
    return TraceCredential(
        access_token=access_token,
        expires_at=expires_at,
        expires_in=expires_in,
        installation_id=installation_id,
    )


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AuthServiceError("invalid_response") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthServiceError("invalid_response")
    return parsed


def _is_content_type(response: httpx.Response, expected: str) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.partition(";")[0].strip().casefold() == expected
