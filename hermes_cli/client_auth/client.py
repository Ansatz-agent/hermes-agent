from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

import httpcore
import httpx

AUTH_ORIGIN = "https://c2sml.cn"
AUTH_HOST = "c2sml.cn"
AUTH_FALLBACK_ADDRESS = "121.37.182.49"
AUTH_PREFIX = "/auth"
LOGIN_PATH = f"{AUTH_PREFIX}/login/"
LOGOUT_PATH = f"{AUTH_PREFIX}/logout/"
SESSION_PATH = f"{AUTH_PREFIX}/api/session/"
TRACE_TOKEN_PATH = f"{AUTH_PREFIX}/api/trace-token/"
NATIVE_SESSION_PATH = f"{AUTH_PREFIX}/api/client-session/"
NATIVE_CURRENT_SESSION_PATH = f"{NATIVE_SESSION_PATH}current/"
NATIVE_TRACE_TOKEN_PATH = f"{NATIVE_SESSION_PATH}trace-token/"
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
_NATIVE_CREDENTIAL_KEYS = frozenset(
    {
        "account_id",
        "session_id",
        "session_token",
        "installation_id",
        "username",
        "issued_at",
    }
)
_NATIVE_STATUS_KEYS = frozenset(
    {
        "state",
        "account_id",
        "session_id",
        "installation_id",
        "username",
        "server_time",
    }
)
_NATIVE_UNAVAILABLE_KEYS = frozenset({"state", "code", "retryable"})
_NATIVE_REVOCATION_KEYS = frozenset(
    {"state", "code", "account_id", "session_id", "revoked_at", "retryable"}
)
_EXPLICIT_REVOCATION_CODES = frozenset(
    {"account_disabled", "account_revoked", "session_revoked"}
)
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CLIENT_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SCHEMA_VERSION = re.compile(r"^[1-9][0-9]{0,15}$")
_NATIVE_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NATIVE_SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


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


class ExplicitSessionRevocation(AuthServiceError):
    def __init__(
        self,
        *,
        code: str,
        account_id: str,
        session_id: str,
        revoked_at: str,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.account_id = account_id
        self.session_id = session_id
        self.revoked_at = revoked_at


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
    access_token: str = field(repr=False)
    expires_at: str
    expires_in: int
    installation_id: str


@dataclass(frozen=True)
class NativeSessionCredential:
    account_id: str
    session_id: str
    session_token: str = field(repr=False)
    installation_id: str
    username: str
    issued_at: str


@dataclass(frozen=True)
class NativeSessionStatus:
    account_id: str
    session_id: str
    installation_id: str
    username: str
    server_time: str


class _PinnedAuthNetworkBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        address = AUTH_FALLBACK_ADDRESS if host == AUTH_HOST else host
        return self._backend.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _PinnedAuthTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=2,
            max_keepalive_connections=1,
            keepalive_expiry=30.0,
            network_backend=_PinnedAuthNetworkBackend(),
        )


class AuthClient:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        fallback_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=AUTH_ORIGIN,
            transport=transport,
            follow_redirects=False,
            timeout=15.0,
        )
        if fallback_transport is not None:
            self._fallback_http: httpx.Client | None = httpx.Client(
                base_url=AUTH_ORIGIN,
                transport=fallback_transport,
                follow_redirects=False,
                timeout=15.0,
            )
        elif transport is None:
            self._fallback_http = httpx.Client(
                base_url=AUTH_ORIGIN,
                transport=_PinnedAuthTransport(),
                follow_redirects=False,
                timeout=15.0,
            )
        else:
            self._fallback_http = None

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

    def legacy_status(self, cookies: Mapping[str, str]) -> SessionStatus:
        return self.status(cookies)

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

    def legacy_logout(self, cookies: Mapping[str, str]) -> None:
        self.logout(cookies)

    def legacy_trace_token(
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

    def issue_client_session(
        self,
        cookies: Mapping[str, str],
        *,
        installation_id: str,
        client_version: str,
    ) -> NativeSessionCredential:
        normalized = _validate_cookie_mapping(cookies)
        _validate_native_issue_request(
            installation_id=installation_id,
            client_version=client_version,
        )
        csrf = normalized[CSRF_COOKIE]
        response = self._request(
            "POST",
            NATIVE_SESSION_PATH,
            json={
                "installation_id": installation_id,
                "client_version": client_version,
            },
            headers={
                "Cookie": _cookie_header(normalized),
                "Referer": f"{AUTH_ORIGIN}{AUTH_PREFIX}/",
                "X-CSRFToken": csrf,
            },
        )
        return _parse_native_credential(
            response,
            expected_installation_id=installation_id,
        )

    def client_session_status(
        self, credential: NativeSessionCredential
    ) -> NativeSessionStatus:
        headers = _native_headers(credential)
        response = self._request(
            "GET", NATIVE_SESSION_PATH, headers=headers, native=True
        )
        return _parse_native_session_status(response, credential)

    def logout_client_session(self, credential: NativeSessionCredential) -> None:
        headers = _native_headers(credential)
        response = self._request(
            "DELETE", NATIVE_CURRENT_SESSION_PATH, headers=headers, native=True
        )
        _parse_native_logout(response, credential)

    def trace_token(
        self,
        credential: NativeSessionCredential,
    ) -> TraceCredential:
        headers = _native_headers(credential)
        response = self._request(
            "POST", NATIVE_TRACE_TOKEN_PATH, headers=headers, native=True
        )
        return _parse_native_trace_credential(response, credential)

    def _request(
        self,
        method: str,
        path: str,
        *,
        native: bool = False,
        **kwargs: object,
    ) -> httpx.Response:
        if path not in {
            LOGIN_PATH,
            LOGOUT_PATH,
            SESSION_PATH,
            TRACE_TOKEN_PATH,
            NATIVE_SESSION_PATH,
            NATIVE_CURRENT_SESSION_PATH,
            NATIVE_TRACE_TOKEN_PATH,
        }:
            raise AuthServiceError("invalid_request")

        def request_with(client: httpx.Client) -> httpx.Response:
            if not native:
                return client.request(method, path, **kwargs)
            request = httpx.Request(
                method,
                httpx.URL(AUTH_ORIGIN).join(path),
                **kwargs,
            )
            return client.send(request)

        try:
            response = request_with(self._http)
        except httpx.HTTPError:
            fallback = self._fallback_http
            if fallback is None:
                raise AuthServiceError("server_unavailable") from None
            try:
                if not native:
                    fallback.cookies.update(self._http.cookies)
                response = request_with(fallback)
            except httpx.HTTPError:
                raise AuthServiceError("server_unavailable") from None
            self._http.close()
            self._http = fallback
            self._fallback_http = None
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


def _validate_native_issue_request(
    *, installation_id: str, client_version: str
) -> None:
    if (
        not _is_native_uuid_v4(installation_id)
        or not isinstance(client_version, str)
        or _CLIENT_VERSION.fullmatch(client_version) is None
    ):
        raise AuthServiceError("invalid_request")


def _is_native_uuid_v4(value: object) -> bool:
    return isinstance(value, str) and _NATIVE_UUID_V4.fullmatch(value) is not None


def _native_headers(credential: NativeSessionCredential) -> dict[str, str]:
    if not isinstance(credential, NativeSessionCredential):
        raise AuthServiceError("invalid_request")
    if (
        not _is_native_uuid_v4(credential.account_id)
        or not _is_native_uuid_v4(credential.session_id)
        or not _is_native_uuid_v4(credential.installation_id)
        or not isinstance(credential.session_token, str)
        or _NATIVE_SESSION_TOKEN.fullmatch(credential.session_token) is None
        or not _valid_native_username(credential.username)
        or not _is_rfc3339(credential.issued_at)
    ):
        raise AuthServiceError("invalid_request")
    return {
        "Authorization": f"Bearer {credential.session_token}",
        "X-Ansatz-Installation-ID": credential.installation_id,
    }


def _valid_native_username(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 150
        and all(not character.isspace() and ord(character) >= 32 for character in value)
    )


def _is_rfc3339(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or _RFC3339.fullmatch(value) is None
    ):
        return False
    try:
        _parse_aware_datetime(value)
    except AuthServiceError:
        return False
    return True


def _require_native_json(response: httpx.Response) -> dict[str, object]:
    if not _is_content_type(response, "application/json") or not _has_no_store(
        response
    ):
        raise AuthServiceError("invalid_response")
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        raise AuthServiceError("invalid_response") from None
    if not isinstance(body, dict):
        raise AuthServiceError("invalid_response")
    return body


def _has_no_store(response: httpx.Response) -> bool:
    return "no-store" in {
        directive.partition("=")[0].strip().casefold()
        for directive in response.headers.get("cache-control", "").split(",")
    }


def _raise_native_terminal_if_explicit(
    response: httpx.Response, credential: NativeSessionCredential
) -> None:
    if response.status_code == 403:
        body = _require_native_json(response)
        code = body.get("code")
        account_id = body.get("account_id")
        session_id = body.get("session_id")
        revoked_at = body.get("revoked_at")
        if (
            set(body) != _NATIVE_REVOCATION_KEYS
            or body.get("state") != "revoked"
            or not isinstance(code, str)
            or code not in _EXPLICIT_REVOCATION_CODES
            or body.get("retryable") is not False
            or not _is_native_uuid_v4(account_id)
            or not _is_native_uuid_v4(session_id)
            or not _is_rfc3339(revoked_at)
            or account_id != credential.account_id
            or session_id != credential.session_id
        ):
            raise AuthServiceError("invalid_response")
        raise ExplicitSessionRevocation(
            code=code,
            account_id=account_id,
            session_id=session_id,
            revoked_at=revoked_at,
        )
    if response.status_code == 401:
        body = _require_native_json(response)
        if (
            set(body) != _NATIVE_UNAVAILABLE_KEYS
            or body.get("state") != "unavailable"
            or body.get("code") != "invalid_session_credential"
            or body.get("retryable") is not True
        ):
            raise AuthServiceError("invalid_response")
        raise AuthServiceError("server_unavailable")


def _parse_native_credential(
    response: httpx.Response, *, expected_installation_id: str
) -> NativeSessionCredential:
    if response.status_code != 201:
        raise AuthServiceError("invalid_response")
    body = _require_native_json(response)
    if set(body) != _NATIVE_CREDENTIAL_KEYS:
        raise AuthServiceError("invalid_response")
    account_id = body.get("account_id")
    session_id = body.get("session_id")
    session_token = body.get("session_token")
    installation_id = body.get("installation_id")
    username = body.get("username")
    issued_at = body.get("issued_at")
    if (
        not _is_native_uuid_v4(account_id)
        or not _is_native_uuid_v4(session_id)
        or installation_id != expected_installation_id
        or not _is_native_uuid_v4(installation_id)
        or not isinstance(session_token, str)
        or _NATIVE_SESSION_TOKEN.fullmatch(session_token) is None
        or not _valid_native_username(username)
        or not _is_rfc3339(issued_at)
    ):
        raise AuthServiceError("invalid_response")
    return NativeSessionCredential(
        account_id=account_id,
        session_id=session_id,
        session_token=session_token,
        installation_id=installation_id,
        username=username,
        issued_at=issued_at,
    )


def _parse_native_session_status(
    response: httpx.Response, credential: NativeSessionCredential
) -> NativeSessionStatus:
    _raise_native_terminal_if_explicit(response, credential)
    if response.status_code != 200:
        raise AuthServiceError("invalid_response")
    body = _require_native_json(response)
    if (
        set(body) != _NATIVE_STATUS_KEYS
        or body.get("state") != "active"
        or body.get("account_id") != credential.account_id
        or body.get("session_id") != credential.session_id
        or body.get("installation_id") != credential.installation_id
        or body.get("username") != credential.username
        or not _is_rfc3339(body.get("server_time"))
    ):
        raise AuthServiceError("invalid_response")
    return NativeSessionStatus(
        account_id=credential.account_id,
        session_id=credential.session_id,
        installation_id=credential.installation_id,
        username=credential.username,
        server_time=body["server_time"],
    )


def _parse_native_logout(
    response: httpx.Response, credential: NativeSessionCredential
) -> None:
    _raise_native_terminal_if_explicit(response, credential)
    if response.status_code != 204 or not _has_no_store(response) or response.content:
        raise AuthServiceError("invalid_response")


def _parse_native_trace_credential(
    response: httpx.Response, credential: NativeSessionCredential
) -> TraceCredential:
    _raise_native_terminal_if_explicit(response, credential)
    return _parse_trace_credential(
        response,
        expected_installation_id=credential.installation_id,
        strict_rfc3339_expiry=True,
    )


def _parse_trace_credential(
    response: httpx.Response,
    *,
    expected_installation_id: str,
    strict_rfc3339_expiry: bool = False,
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
        or (strict_rfc3339_expiry and not _is_rfc3339(expires_at))
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
