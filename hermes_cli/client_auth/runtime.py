from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar

from hermes_cli.client_auth.client import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthClient,
    AuthServiceError,
    CookieRecord,
    SessionRejected,
    SessionStatus,
)

AUTH_EXIT_CODE = 20
LEASE_SECONDS = 60.0
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60.0
OWNER_IDLE_SECONDS = 15.0 * 60.0
BACKEND_SCOPE_TOKEN_TTL_SECONDS = 60.0
_BACKEND_SCOPE_TOKEN_BYTES = 32


class AuthState(StrEnum):
    CHECKING = "checking"
    AUTHENTICATED = "authenticated"
    SIGNED_OUT = "signed_out"
    LOCKED = "locked"


class LockedWaitingResult(StrEnum):
    AUTHENTICATED = "authenticated"
    OWNER_STOPPED = "owner_stopped"


@dataclass(frozen=True)
class AuthScope:
    runtime_instance_id: str
    epoch: int


@dataclass(frozen=True)
class ConnectionScope:
    connection_id: str
    auth: AuthScope


class AuthRequired(RuntimeError):
    code = "AUTH_REQUIRED"

    def __init__(self, reason: str | None = None) -> None:
        normalized = reason or self.code
        super().__init__(normalized)
        self.reason = reason


@dataclass(frozen=True)
class BackendScopeTokenRegistration:
    bearer: str
    connection_id: str
    auth: AuthScope
    ttl_seconds: float


@dataclass(frozen=True)
class BackendScopeGrant:
    connection_id: str
    auth: AuthScope
    valid_until: float
    token_digest: str

    def claim(self) -> dict[str, object]:
        return {
            "connection_id": self.connection_id,
            "runtime_instance_id": self.auth.runtime_instance_id,
            "epoch": self.auth.epoch,
            "valid_until": self.valid_until,
            "token_digest": self.token_digest,
        }


class BackendScopeTokenRegistry:
    """Process-local, hashed bearer grants for Desktop backend traffic."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        authorize: Callable[..., AuthScope] | None = None,
    ) -> None:
        self._clock = clock
        self._authorize = authorize or (
            lambda boundary, *, expected: require_authorized(
                boundary,
                expected=expected,
            )
        )
        self._lock = threading.RLock()
        self._records: dict[bytes, BackendScopeGrant] = {}

    def register(
        self,
        bearer: str,
        *,
        connection_id: str,
        expected: AuthScope,
        ttl_seconds: float,
    ) -> BackendScopeGrant:
        _validate_backend_scope_bearer(bearer)
        _validate_connection_id(connection_id)
        _validate_auth_scope(expected)
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not 0 < float(ttl_seconds) <= BACKEND_SCOPE_TOKEN_TTL_SECONDS
        ):
            raise AuthRequired("runtime_unavailable")
        self._authorize("backend.scope_token.register", expected=expected)
        now = self._clock()
        digest = hashlib.sha256(bearer.encode("ascii")).digest()
        grant = BackendScopeGrant(
            connection_id=connection_id,
            auth=expected,
            valid_until=now + float(ttl_seconds),
            token_digest=digest.hex(),
        )
        with self._lock:
            self._prune_locked(now)
            self._records[digest] = grant
        return grant

    def authorize(
        self,
        bearer: str,
        boundary: str,
        *,
        connection_id: str | None = None,
    ) -> BackendScopeGrant:
        _validate_backend_scope_bearer(bearer)
        digest = hashlib.sha256(bearer.encode("ascii")).digest()
        with self._lock:
            grant = self._records.get(digest)
        if grant is None:
            raise AuthRequired("runtime_unavailable")
        if connection_id is not None and grant.connection_id != connection_id:
            raise AuthRequired("runtime_unavailable")
        return self._authorize_grant(grant, boundary)

    def authorize_claim(
        self,
        claim: object,
        boundary: str,
    ) -> BackendScopeGrant:
        grant = _grant_from_claim(claim)
        try:
            digest = bytes.fromhex(grant.token_digest)
        except ValueError:
            raise AuthRequired("runtime_unavailable") from None
        if len(digest) != hashlib.sha256().digest_size:
            raise AuthRequired("runtime_unavailable")
        with self._lock:
            current = self._records.get(digest)
        if current != grant:
            raise AuthRequired("runtime_unavailable")
        return self._authorize_grant(current, boundary)

    def revoke(self, *, connection_id: str, expected: AuthScope) -> None:
        _validate_connection_id(connection_id)
        _validate_auth_scope(expected)
        with self._lock:
            doomed = [
                digest
                for digest, grant in self._records.items()
                if grant.connection_id == connection_id and grant.auth == expected
            ]
            for digest in doomed:
                self._records.pop(digest, None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _authorize_grant(
        self,
        grant: BackendScopeGrant,
        boundary: str,
    ) -> BackendScopeGrant:
        now = self._clock()
        if now >= grant.valid_until:
            with self._lock:
                self._records.pop(bytes.fromhex(grant.token_digest), None)
            raise AuthRequired("session_expired")
        self._authorize(boundary, expected=grant.auth)
        return grant

    def _prune_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, grant in self._records.items()
            if now >= grant.valid_until
        ]
        for digest in expired:
            self._records.pop(digest, None)


def parse_backend_scope_token_registration(
    value: object,
) -> BackendScopeTokenRegistration:
    expected_keys = {
        "version",
        "operation",
        "bearer",
        "connection_id",
        "runtime_instance_id",
        "epoch",
        "ttl_seconds",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AuthRequired("runtime_unavailable")
    if value.get("version") != 1 or value.get("operation") != "register_scope_token":
        raise AuthRequired("runtime_unavailable")
    bearer = value.get("bearer")
    connection_id = value.get("connection_id")
    runtime_instance_id = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    ttl_seconds = value.get("ttl_seconds")
    if not isinstance(bearer, str) or not isinstance(connection_id, str):
        raise AuthRequired("runtime_unavailable")
    auth = AuthScope(runtime_instance_id, epoch)  # type: ignore[arg-type]
    _validate_backend_scope_bearer(bearer)
    _validate_connection_id(connection_id)
    _validate_auth_scope(auth)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not 0 < float(ttl_seconds) <= BACKEND_SCOPE_TOKEN_TTL_SECONDS
    ):
        raise AuthRequired("runtime_unavailable")
    return BackendScopeTokenRegistration(
        bearer=bearer,
        connection_id=connection_id,
        auth=auth,
        ttl_seconds=float(ttl_seconds),
    )


def _validate_backend_scope_bearer(bearer: str) -> None:
    if not isinstance(bearer, str) or len(bearer) != 43:
        raise AuthRequired("runtime_unavailable")
    try:
        decoded = base64.b64decode(
            bearer + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError):
        raise AuthRequired("runtime_unavailable") from None
    if len(decoded) != _BACKEND_SCOPE_TOKEN_BYTES:
        raise AuthRequired("runtime_unavailable")


def _validate_connection_id(connection_id: str) -> None:
    if (
        not isinstance(connection_id, str)
        or not 0 < len(connection_id) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in connection_id)
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_auth_scope(scope: AuthScope) -> None:
    if not isinstance(scope, AuthScope):
        raise AuthRequired("runtime_unavailable")
    if (
        len(scope.runtime_instance_id) != 32
        or any(character not in "0123456789abcdef" for character in scope.runtime_instance_id)
        or not isinstance(scope.epoch, int)
        or isinstance(scope.epoch, bool)
        or scope.epoch < 0
    ):
        raise AuthRequired("runtime_unavailable")


def _grant_from_claim(value: object) -> BackendScopeGrant:
    expected_keys = {
        "connection_id",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "token_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AuthRequired("runtime_unavailable")
    connection_id = value.get("connection_id")
    auth = AuthScope(
        value.get("runtime_instance_id"),  # type: ignore[arg-type]
        value.get("epoch"),  # type: ignore[arg-type]
    )
    valid_until = value.get("valid_until")
    token_digest = value.get("token_digest")
    if not isinstance(connection_id, str):
        raise AuthRequired("runtime_unavailable")
    _validate_connection_id(connection_id)
    _validate_auth_scope(auth)
    if (
        not isinstance(valid_until, (int, float))
        or isinstance(valid_until, bool)
        or not isinstance(token_digest, str)
        or len(token_digest) != 64
    ):
        raise AuthRequired("runtime_unavailable")
    return BackendScopeGrant(
        connection_id=connection_id,
        auth=auth,
        valid_until=float(valid_until),
        token_digest=token_digest,
    )


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: AuthState
    epoch: int
    valid_until: float
    runtime_instance_id: str
    boot_id: str
    username: str | None
    session_expires_at: str | None
    reason: str | None

    @classmethod
    def new_authenticated(
        cls,
        username: str,
        *,
        now: float,
        ttl: float,
    ) -> RuntimeSnapshot:
        if not username or ttl <= 0:
            raise AuthRequired("session_expired")
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=1,
            valid_until=now + ttl,
            runtime_instance_id=secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=username,
            session_expires_at=None,
            reason=None,
        )

    @classmethod
    def from_session_status(
        cls,
        status: SessionStatus,
        *,
        now: float,
        runtime_instance_id: str | None = None,
        epoch: int = 1,
    ) -> RuntimeSnapshot:
        valid_until = _lease_deadline(status, now=now)
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=epoch,
            valid_until=valid_until,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=status.username,
            session_expires_at=status.session_expires_at,
            reason=None,
        )

    @classmethod
    def signed_out(
        cls,
        *,
        epoch: int = 0,
        runtime_instance_id: str | None = None,
        reason: str | None = None,
    ) -> RuntimeSnapshot:
        return cls(
            state=AuthState.SIGNED_OUT,
            epoch=epoch,
            valid_until=0.0,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=None,
            session_expires_at=None,
            reason=reason,
        )

    @property
    def scope(self) -> AuthScope:
        return AuthScope(self.runtime_instance_id, self.epoch)

    def refreshed(self, status: SessionStatus, *, now: float) -> RuntimeSnapshot:
        if self.state is not AuthState.AUTHENTICATED or status.username != self.username:
            raise AuthRequired("session_rejected")
        if self.session_expires_at is None:
            absolute_cap = None
            absolute_text = status.session_expires_at
        else:
            current_expiry = _parse_aware_datetime(self.session_expires_at)
            response_expiry = _parse_aware_datetime(status.session_expires_at)
            if current_expiry <= response_expiry:
                absolute_cap = current_expiry
                absolute_text = self.session_expires_at
            else:
                absolute_cap = response_expiry
                absolute_text = status.session_expires_at
        return replace(
            self,
            valid_until=_lease_deadline(status, now=now, absolute_cap=absolute_cap),
            session_expires_at=absolute_text,
            reason=None,
        )

    def locked(self, reason: str, *, now: float) -> RuntimeSnapshot:
        return replace(
            self,
            state=AuthState.LOCKED,
            epoch=self.epoch + 1,
            valid_until=now,
            runtime_instance_id=secrets.token_hex(16),
            reason=reason,
        )

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope,
        now: float,
    ) -> AuthScope:
        if not boundary:
            raise AuthRequired("runtime_unavailable")
        if self.state is not AuthState.AUTHENTICATED:
            raise AuthRequired(self.reason or "signed_out")
        if now >= self.valid_until or _read_boot_id() != self.boot_id:
            raise AuthRequired("session_expired")
        if expected != self.scope:
            raise AuthRequired("runtime_unavailable")
        return self.scope

    def public_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "username": self.username,
            "runtime_instance_id": self.runtime_instance_id,
            "epoch": self.epoch,
            "valid_until": self.valid_until,
            "session_expires_at": self.session_expires_at,
            "reason": self.reason,
        }


_unix_lock_registry_guard = threading.Lock()
_held_unix_locks: set[Path] = set()


@dataclass(frozen=True)
class WindowsNamedPipeEndpoint:
    pipe_name: str
    owner_sid: str
    first_instance: bool

    @classmethod
    def for_current_sid(
        cls,
        *,
        first_instance: bool,
    ) -> WindowsNamedPipeEndpoint:
        if os.name != "nt" or not first_instance:
            raise AuthRequired("runtime_unavailable")
        owner_sid = _windows_current_sid()
        compact_sid = hashlib.blake2s(
            owner_sid.encode("ascii"),
            digest_size=16,
        ).hexdigest()
        return cls(
            pipe_name=rf"\\.\pipe\hermes-auth-{compact_sid}",
            owner_sid=owner_sid,
            first_instance=True,
        )

    def bind_owner(self) -> WindowsOwnerServer:
        if os.name != "nt" or not self.first_instance:
            raise AuthRequired("runtime_unavailable")
        try:
            import ntsecuritycon
            import pywintypes
            import win32api
            import win32con
            import win32pipe
            import win32security

            owner = win32security.ConvertStringSidToSid(self.owner_sid)
            system = win32security.CreateWellKnownSid(
                win32security.WinLocalSystemSid,
                None,
            )
            dacl = win32security.ACL()
            access = ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, owner)
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, system)
            descriptor = win32security.SECURITY_DESCRIPTOR()
            descriptor.SetSecurityDescriptorDacl(True, dacl, False)
            attributes = pywintypes.SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = descriptor
            attributes.bInheritHandle = False
            handle = win32pipe.CreateNamedPipe(
                self.pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX | 0x00080000,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT
                | 0x00000008,
                1,
                65_536,
                65_536,
                0,
                attributes,
            )
            win32api.SetHandleInformation(
                handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
        except Exception:
            try:
                handle.Close()
            except (AttributeError, UnboundLocalError):
                pass
            raise AuthRequired("runtime_unavailable") from None
        return WindowsOwnerServer(self, handle)

    def connect_current(self) -> WindowsPipeConnection:
        if os.name != "nt":
            raise AuthRequired("runtime_unavailable")
        try:
            import win32api
            import win32con
            import win32file

            handle = win32file.CreateFile(
                self.pipe_name,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                0x00100000 | 0x00010000,
                None,
            )
            win32api.SetHandleInformation(
                handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
        except Exception:
            try:
                handle.Close()
            except (AttributeError, UnboundLocalError):
                pass
            raise AuthRequired("runtime_unavailable") from None
        return WindowsPipeConnection(handle)


class WindowsPipeConnection:
    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._closed = False

    @property
    def handle(self) -> object:
        return self._handle

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.Close()  # type: ignore[attr-defined]
        except Exception:
            return

    def recv(self, size: int) -> bytes:
        if self._closed or size <= 0:
            return b""
        try:
            import win32file

            _status, data = win32file.ReadFile(self._handle, size)
            return bytes(data)
        except Exception:
            raise AuthRequired("runtime_unavailable") from None

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise AuthRequired("runtime_unavailable")
        try:
            import win32file

            win32file.WriteFile(self._handle, data)
        except Exception:
            raise AuthRequired("runtime_unavailable") from None


class WindowsOwnerServer:
    def __init__(self, endpoint: WindowsNamedPipeEndpoint, handle: object) -> None:
        self._endpoint = endpoint
        self._handle: object | None = handle
        self._closed = False

    def accept(self) -> WindowsPipeConnection:
        if self._closed or self._handle is None:
            raise AuthRequired("runtime_unavailable")
        try:
            import pywintypes
            import win32api
            import win32con
            import win32pipe
            import win32security

            try:
                win32pipe.ConnectNamedPipe(self._handle, None)
            except pywintypes.error as error:
                if error.winerror != 535:
                    raise
            win32security.ImpersonateNamedPipeClient(self._handle)
            try:
                token = win32security.OpenThreadToken(
                    win32api.GetCurrentThread(),
                    win32security.TOKEN_QUERY,
                    True,
                )
                try:
                    peer_sid = win32security.ConvertSidToStringSid(
                        win32security.GetTokenInformation(
                            token,
                            win32security.TokenUser,
                        )[0]
                    )
                finally:
                    token.Close()
            finally:
                win32security.RevertToSelf()
            if peer_sid != self._endpoint.owner_sid:
                raise AuthRequired("runtime_unavailable")
            win32api.SetHandleInformation(
                self._handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
            connection = WindowsPipeConnection(self._handle)
            self._handle = None
        except Exception:
            self.close()
            raise AuthRequired("runtime_unavailable") from None
        return connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            import win32pipe

            if self._handle is not None:
                win32pipe.DisconnectNamedPipe(self._handle)
        except Exception:
            pass
        try:
            if self._handle is not None:
                self._handle.Close()  # type: ignore[attr-defined]
        except Exception:
            pass


@dataclass(frozen=True)
class UnixEndpoint:
    root: Path
    socket_path: Path
    pointer_path: Path
    lock_path: Path

    @classmethod
    def for_directory(cls, root: Path, *, random_name: str) -> UnixEndpoint:
        _validate_random_name(random_name)
        _ensure_private_directory(root)
        compact_name = hashlib.blake2s(
            random_name.encode("ascii"),
            digest_size=10,
        ).hexdigest()
        return cls(
            root=root,
            socket_path=root / f"h-{compact_name}.s",
            pointer_path=root / "current.json",
            lock_path=root / "owner.lock",
        )

    @classmethod
    def for_current_user(
        cls,
        *,
        random_name: str,
        forbid_abstract: bool,
        darwin_user_temp: bool,
    ) -> UnixEndpoint:
        if sys.platform.startswith("linux"):
            if not forbid_abstract:
                raise AuthRequired("runtime_unavailable")
            runtime_root = _linux_runtime_root()
        elif sys.platform == "darwin":
            if not darwin_user_temp:
                raise AuthRequired("runtime_unavailable")
            runtime_root = _darwin_user_temp_dir() / "ha"
        else:
            raise AuthRequired("runtime_unavailable")
        return cls.for_directory(runtime_root, random_name=random_name)

    def acquire_owner_lock(self) -> UnixOwnerLock:
        try:
            import fcntl

            fd = _open_private_file(self.lock_path, os.O_RDWR | os.O_CREAT)
            resolved = self.lock_path.resolve(strict=True)
            with _unix_lock_registry_guard:
                if resolved in _held_unix_locks:
                    os.close(fd)
                    raise AuthRequired("runtime_unavailable")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise AuthRequired("runtime_unavailable") from None
                _held_unix_locks.add(resolved)
            return UnixOwnerLock(fd, resolved)
        except ImportError:
            raise AuthRequired("runtime_unavailable") from None

    def bind_owner(self, owner_lock: UnixOwnerLock) -> UnixOwnerServer:
        if owner_lock.closed or owner_lock.path != self.lock_path.resolve(strict=True):
            raise AuthRequired("runtime_unavailable")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AuthRequired("runtime_unavailable")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.set_inheritable(False)
        try:
            if len(os.fsencode(self.socket_path)) >= 104:
                raise AuthRequired("runtime_unavailable")
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            _validate_socket_file(self.socket_path)
            listener.listen(16)
            self._write_pointer()
        except Exception:
            listener.close()
            try:
                self.socket_path.unlink()
            except OSError:
                pass
            raise
        return UnixOwnerServer(self, listener)

    def connect_current(self) -> socket.socket:
        socket_path = self._read_pointer()
        _validate_socket_file(socket_path)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.set_inheritable(False)
        try:
            connection.connect(str(socket_path))
            _validate_peer_uid(connection)
        except (AuthRequired, OSError):
            connection.close()
            raise AuthRequired("runtime_unavailable") from None
        return connection

    def _write_pointer(self) -> None:
        payload = json.dumps(
            {"version": 1, "socket": self.socket_path.name},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temp_path = self.root / f".current-{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = _open_private_file(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp_path, self.pointer_path)
            os.chmod(self.pointer_path, 0o600, follow_symlinks=False)
            _validate_private_regular_file(self.pointer_path)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise AuthRequired("runtime_unavailable") from None

    def _read_pointer(self) -> Path:
        fd = -1
        try:
            fd = _open_private_file(self.pointer_path, os.O_RDONLY, create=False)
            details = os.fstat(fd)
            if details.st_size <= 0 or details.st_size > 512:
                raise AuthRequired("runtime_unavailable")
            raw = os.read(fd, 513)
        except (AuthRequired, OSError):
            raise AuthRequired("runtime_unavailable") from None
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError):
            raise AuthRequired("runtime_unavailable") from None
        if not isinstance(payload, dict) or set(payload) != {"version", "socket"}:
            raise AuthRequired("runtime_unavailable")
        name = payload.get("socket")
        if payload.get("version") != 1 or not isinstance(name, str):
            raise AuthRequired("runtime_unavailable")
        _validate_socket_name(name)
        candidate = self.root / name
        if candidate.parent != self.root:
            raise AuthRequired("runtime_unavailable")
        return candidate


class UnixOwnerLock:
    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self.path = path
        self.closed = False
        os.set_inheritable(fd, False)

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            with _unix_lock_registry_guard:
                _held_unix_locks.discard(self.path)


class UnixOwnerServer:
    def __init__(self, endpoint: UnixEndpoint, listener: socket.socket) -> None:
        self._endpoint = endpoint
        self._listener = listener
        self._closed = False

    def fileno(self) -> int:
        return self._listener.fileno()

    def accept(self) -> socket.socket:
        try:
            connection, _ = self._listener.accept()
            connection.set_inheritable(False)
            _validate_peer_uid(connection)
            return connection
        except (AuthRequired, OSError):
            try:
                connection.close()
            except (OSError, UnboundLocalError):
                pass
            raise AuthRequired("runtime_unavailable") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._listener.close()
        try:
            if self._endpoint._read_pointer() == self._endpoint.socket_path:
                self._endpoint.pointer_path.unlink()
        except (AuthRequired, OSError):
            pass
        try:
            self._endpoint.socket_path.unlink()
        except OSError:
            pass


class SocketLivenessProbe:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._connection.set_inheritable(False)
        self._closed = False

    def __call__(self) -> bool:
        if self._closed:
            return False
        try:
            readable, _, _ = select.select([self._connection], [], [], 0)
            if not readable:
                return True
            return self._connection.recv(1, socket.MSG_PEEK) != b""
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


class RuntimeConsumer:
    def __init__(
        self,
        snapshot: RuntimeSnapshot,
        *,
        liveness_probe: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        on_authorized: Callable[[], None] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._liveness_probe = liveness_probe
        self._clock = clock
        self._on_authorized = on_authorized or (lambda: None)
        self._lock = threading.RLock()

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            if snapshot.runtime_instance_id != self._snapshot.runtime_instance_id:
                self._snapshot = self._snapshot.locked(
                    "runtime_unavailable",
                    now=self._clock(),
                )
                raise AuthRequired("runtime_unavailable")
            if snapshot.epoch < self._snapshot.epoch:
                raise AuthRequired("runtime_unavailable")
            self._snapshot = snapshot

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope | None = None,
        now: float | None = None,
    ) -> AuthScope:
        checked_at = self._clock() if now is None else now
        if not self._liveness_probe():
            with self._lock:
                if self._snapshot.state is AuthState.AUTHENTICATED:
                    self._snapshot = self._snapshot.locked(
                        "runtime_unavailable",
                        now=checked_at,
                    )
            raise AuthRequired("runtime_unavailable")
        snapshot = self.snapshot()
        scope = snapshot.require_authorized(
            boundary,
            expected=expected or snapshot.scope,
            now=checked_at,
        )
        self._on_authorized()
        return scope


class _SecretBackend(Protocol):
    def read(self) -> str | None: ...

    def write(self, raw: str) -> None: ...

    def delete(self) -> None: ...


class _Hardener(Protocol):
    def apply_required(self) -> None: ...


class _MemorySecretBackend:
    def __init__(self) -> None:
        self._raw: str | None = None

    def read(self) -> str | None:
        return self._raw

    def write(self, raw: str) -> None:
        self._raw = raw

    def delete(self) -> None:
        self._raw = None


class _KeyringSecretBackend:
    SERVICE = "cn.c2sml.hermes.remote-auth"
    ACCOUNT = "django-session"

    def read(self) -> str | None:
        import keyring

        return keyring.get_password(self.SERVICE, self.ACCOUNT)

    def write(self, raw: str) -> None:
        import keyring

        keyring.set_password(self.SERVICE, self.ACCOUNT, raw)

    def delete(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.SERVICE, self.ACCOUNT)
        except PasswordDeleteError:
            return


class ProcessHardener:
    def apply_required(self) -> None:
        if sys.platform.startswith("linux"):
            _linux_prctl_dumpable_zero()
            _disable_core_dumps()
            return
        if sys.platform == "darwin":
            _darwin_deny_attach()
            _disable_core_dumps()
            return
        if os.name == "nt":
            _windows_disable_wer_dump()
            _windows_apply_process_mitigations()
            return
        raise AuthRequired("runtime_unavailable")


class S6LifecycleAdapter:
    """Apply auth transitions to the fixed container capability slots."""

    _STATIC_SERVICES = frozenset({"dashboard", "main-hermes"})

    def __init__(
        self,
        *,
        service_root: Path,
        hermes_home: Path,
        environment: dict[str, str],
        signal_service: Callable[[str, str], None] | None = None,
    ) -> None:
        self._service_root = service_root
        self._hermes_home = hermes_home
        self._dashboard_enabled = environment.get("HERMES_DASHBOARD", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._signal_service = signal_service or self._signal_s6_service

    def transition(self, snapshot: RuntimeSnapshot) -> None:
        authenticated = snapshot.state is AuthState.AUTHENTICATED
        for service in self._capability_services():
            desired = authenticated and self._desired_running(service)
            try:
                self._signal_service(service, "up" if desired else "down")
            except Exception:
                # The in-process and per-request guards remain authoritative.
                # Lifecycle signaling is best-effort so a broken s6 slot can
                # never prevent publication of a locked transition.
                continue

    def _capability_services(self) -> list[str]:
        services: set[str] = set()
        try:
            entries = tuple(self._service_root.iterdir())
        except OSError:
            return []
        for entry in entries:
            name = entry.name
            if name in self._STATIC_SERVICES:
                services.add(name)
                continue
            if not name.startswith("gateway-"):
                continue
            profile = name.removeprefix("gateway-")
            try:
                from hermes_cli.service_manager import validate_profile_name

                validate_profile_name(profile)
            except (ImportError, ValueError):
                continue
            services.add(name)
        return sorted(services)

    def _desired_running(self, service: str) -> bool:
        if service == "dashboard":
            return self._dashboard_enabled
        if service == "main-hermes":
            return False
        profile = service.removeprefix("gateway-")
        profile_dir = (
            self._hermes_home
            if profile == "default"
            else self._hermes_home / "profiles" / profile
        )
        state_file = profile_dir / "gateway_state.json"
        try:
            details = state_file.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_size > 65_536:
                return False
            value = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return False
        if not isinstance(value, dict):
            return False
        desired = value.get("desired_state")
        if desired is not None:
            return desired == "running"
        return value.get("gateway_state") in {"running", "draining", "degraded"}

    def _signal_s6_service(self, service: str, action: str) -> None:
        flag = {"down": "-d", "up": "-u"}.get(action)
        if flag is None:
            raise AuthRequired("runtime_unavailable")
        result = subprocess.run(
            ["/command/s6-svc", flag, str(self._service_root / service)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise AuthRequired("runtime_unavailable")


class _OwnerCore:
    def __init__(
        self,
        client: AuthClient,
        *,
        secret_backend: _SecretBackend,
        hardener: _Hardener | None,
        vault_required: bool,
        clock: Callable[[], float],
        jitter: Callable[[float, float], float],
        on_transition: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        self._client = client
        self._secret_backend = secret_backend
        self._hardener = hardener
        self._vault_required = vault_required
        self._clock = clock
        self._jitter = jitter
        self._snapshot = RuntimeSnapshot.signed_out()
        self._on_transition = on_transition
        self._record: CookieRecord | None = None
        self._record_loaded = False
        self._consumers: list[RuntimeConsumer] = []
        self._alive = True
        self._lock = threading.RLock()
        self._next_refresh_at: float | None = None
        self._failed_login_attempts: list[float] = []
        self._last_authenticated_activity = self._clock()
        if self._on_transition is not None:
            self._on_transition(self._snapshot)

    @property
    def next_refresh_at(self) -> float | None:
        with self._lock:
            return self._next_refresh_at

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def connect_consumer(self, *, profile: str | None = None) -> RuntimeConsumer:
        del profile
        with self._lock:
            consumer = RuntimeConsumer(
                self._snapshot,
                liveness_probe=lambda: self._alive,
                clock=self._clock,
                on_authorized=self._record_authenticated_activity,
            )
            self._consumers.append(consumer)
            return consumer

    def login(self, username: str, password: bytearray) -> RuntimeSnapshot:
        self._check_login_rate_limit()
        self._prepare_cookie_acquisition()
        try:
            record = self._client.login(username, password)
            status = self._client.status(record.cookies)
        except AuthServiceError as error:
            if error.reason == "invalid_credentials":
                self._record_failed_login()
            self._lock_with_reason(error.reason)
            raise AuthRequired(error.reason) from None
        if status.username != record.username or status.username != username:
            self._best_effort_remote_logout(record)
            self._lock_with_reason("session_rejected")
            raise AuthRequired("session_rejected")
        try:
            self._secret_backend.write(_encode_cookie_blob(record))
        except Exception:
            self._best_effort_remote_logout(record)
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            self._lock_with_reason(reason)
            raise AuthRequired(reason) from None

        now = self._clock()
        with self._lock:
            self._record = record
            self._record_loaded = True
            self._failed_login_attempts.clear()
            self._last_authenticated_activity = now
            snapshot = RuntimeSnapshot.from_session_status(
                status,
                now=now,
                runtime_instance_id=self._snapshot.runtime_instance_id,
                epoch=self._snapshot.epoch + 1,
            )
            self._publish_locked(snapshot)
            self._schedule_locked(now)
            return snapshot

    def refresh(self) -> RuntimeSnapshot:
        record = self._load_record()
        if record is None:
            return self.snapshot()
        try:
            status = self._client.status(record.cookies)
        except AuthServiceError as error:
            if isinstance(error, SessionRejected):
                self._delete_record_best_effort()
            self._lock_with_reason(error.reason)
            raise AuthRequired(error.reason) from None

        now = self._clock()
        with self._lock:
            current = self._snapshot
            try:
                if current.state is AuthState.AUTHENTICATED:
                    snapshot = current.refreshed(status, now=now)
                else:
                    snapshot = RuntimeSnapshot.from_session_status(
                        status,
                        now=now,
                        runtime_instance_id=current.runtime_instance_id,
                        epoch=current.epoch + 1,
                    )
            except AuthRequired as error:
                self._publish_locked(current.locked(error.reason or error.code, now=now))
                self._next_refresh_at = None
                raise
            shortened_record = CookieRecord(
                cookies=dict(record.cookies),
                username=status.username,
                session_expires_at=snapshot.session_expires_at or status.session_expires_at,
            )
            if shortened_record != record:
                try:
                    self._secret_backend.write(_encode_cookie_blob(shortened_record))
                except Exception:
                    reason = (
                        "vault_unavailable" if self._vault_required else "runtime_unavailable"
                    )
                    self._publish_locked(current.locked(reason, now=now))
                    self._next_refresh_at = None
                    raise AuthRequired(reason) from None
                self._record = shortened_record
            self._publish_locked(snapshot)
            self._schedule_locked(now)
            return snapshot

    def logout(self) -> RuntimeSnapshot:
        with self._lock:
            record = self._record
            current = self._snapshot
            signed_out = RuntimeSnapshot.signed_out(
                epoch=current.epoch + 1,
            )
            self._record = None
            self._record_loaded = True
            self._next_refresh_at = None
            self._publish_locked(signed_out)
        delete_failed = False
        try:
            self._secret_backend.delete()
        except Exception:
            delete_failed = True
        if record is not None:
            self._best_effort_remote_logout(record)
        if delete_failed:
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            raise AuthRequired(reason)
        return signed_out

    def close(self) -> None:
        now = self._clock()
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            self._next_refresh_at = None
            self._publish_locked(self._snapshot.locked("runtime_unavailable", now=now))

    def maintenance(self) -> bool:
        now = self._clock()
        with self._lock:
            if not self._alive:
                return False
            if now - self._last_authenticated_activity >= OWNER_IDLE_SECONDS:
                self.close()
                return False
            refresh_due = (
                self._next_refresh_at is not None and now >= self._next_refresh_at
            )
        if refresh_due:
            self.refresh()
        return True

    def _record_authenticated_activity(self) -> None:
        with self._lock:
            if self._alive and self._snapshot.state is AuthState.AUTHENTICATED:
                self._last_authenticated_activity = self._clock()

    def _check_login_rate_limit(self) -> None:
        now = self._clock()
        with self._lock:
            cutoff = now - LOGIN_ATTEMPT_WINDOW_SECONDS
            self._failed_login_attempts = [
                attempt for attempt in self._failed_login_attempts if attempt > cutoff
            ]
            limited = len(self._failed_login_attempts) >= LOGIN_ATTEMPT_LIMIT
        if limited:
            self._lock_with_reason("rate_limited")
            raise AuthRequired("rate_limited")

    def _record_failed_login(self) -> None:
        with self._lock:
            self._failed_login_attempts.append(self._clock())

    def _prepare_cookie_acquisition(self) -> None:
        if self._hardener is None:
            return
        try:
            self._hardener.apply_required()
        except AuthRequired:
            self._lock_with_reason("runtime_unavailable")
            raise
        except Exception:
            self._lock_with_reason("runtime_unavailable")
            raise AuthRequired("runtime_unavailable") from None

    def _load_record(self) -> CookieRecord | None:
        with self._lock:
            if self._record_loaded:
                return self._record
        try:
            raw = self._secret_backend.read()
            record = _decode_cookie_blob(raw) if raw else None
        except Exception:
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            self._lock_with_reason(reason)
            raise AuthRequired(reason) from None
        with self._lock:
            self._record = record
            self._record_loaded = True
            return record

    def _schedule_locked(self, now: float) -> None:
        delay = self._jitter(57.0, 60.0)
        if not 57.0 <= delay <= 60.0:
            self._publish_locked(self._snapshot.locked("runtime_unavailable", now=now))
            self._next_refresh_at = None
            raise AuthRequired("runtime_unavailable")
        self._next_refresh_at = min(now + delay, self._snapshot.valid_until)

    def _lock_with_reason(self, reason: str) -> RuntimeSnapshot:
        now = self._clock()
        with self._lock:
            locked = self._snapshot.locked(reason, now=now)
            self._publish_locked(locked)
            self._next_refresh_at = None
            return locked

    def _publish_locked(self, snapshot: RuntimeSnapshot) -> None:
        if self._on_transition is not None:
            self._on_transition(snapshot)
        self._snapshot = snapshot
        retained: list[RuntimeConsumer] = []
        for consumer in self._consumers:
            try:
                consumer.publish(snapshot)
            except AuthRequired:
                continue
            retained.append(consumer)
        self._consumers = retained

    def _delete_record_best_effort(self) -> None:
        with self._lock:
            self._record = None
            self._record_loaded = True
        try:
            self._secret_backend.delete()
        except Exception:
            return

    def _best_effort_remote_logout(self, record: CookieRecord) -> None:
        try:
            self._client.logout(record.cookies)
        except AuthServiceError:
            return


class VaultOwner(_OwnerCore):
    SERVICE = _KeyringSecretBackend.SERVICE
    ACCOUNT = _KeyringSecretBackend.ACCOUNT

    def __init__(
        self,
        client: AuthClient,
        *,
        secret_backend: _SecretBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        random_source = secrets.SystemRandom()
        super().__init__(
            client,
            secret_backend=secret_backend or _KeyringSecretBackend(),
            hardener=None,
            vault_required=True,
            clock=clock,
            jitter=jitter or random_source.uniform,
            on_transition=None,
        )


class MemoryOwner(_OwnerCore):
    def __init__(
        self,
        client: AuthClient,
        *,
        hardener: _Hardener,
        secret_backend: _SecretBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] | None = None,
        on_transition: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        random_source = secrets.SystemRandom()
        super().__init__(
            client,
            secret_backend=secret_backend or _MemorySecretBackend(),
            hardener=hardener,
            vault_required=False,
            clock=clock,
            jitter=jitter or random_source.uniform,
            on_transition=on_transition,
        )


class _EntryPointOwner(Protocol):
    def refresh(self) -> RuntimeSnapshot: ...

    def login(self, username: str, password: bytearray) -> RuntimeSnapshot: ...

    def snapshot(self) -> RuntimeSnapshot: ...

    def connect_consumer(self, *, profile: str | None = None) -> RuntimeConsumer: ...


_RUNTIME_FRAME_LIMIT = 65_536
_RUNTIME_PROTOCOL_VERSION = 1


class RemoteRuntimeConsumer:
    def __init__(
        self,
        owner: RemoteRuntimeOwner,
        snapshot: RuntimeSnapshot,
    ) -> None:
        self._owner = owner
        self._snapshot = snapshot
        self._lock = threading.RLock()

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope | None = None,
        now: float | None = None,
    ) -> AuthScope:
        del now
        current = self.snapshot()
        required = expected or current.scope
        snapshot = self._owner.authorize(boundary, expected=required)
        self.publish(snapshot)
        return snapshot.scope


class RemoteRuntimeOwner:
    """A same-OS-user client for the single native auth owner."""

    def __init__(self, endpoint: UnixEndpoint | WindowsNamedPipeEndpoint) -> None:
        self._endpoint = endpoint
        self._snapshot = RuntimeSnapshot.signed_out(reason="runtime_unavailable")
        self._lock = threading.RLock()

    def refresh(self) -> RuntimeSnapshot:
        return self._request({"operation": "status"})

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def login(self, username: str, password: bytearray) -> RuntimeSnapshot:
        try:
            password_text = password.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise AuthRequired("invalid_credentials") from None
        try:
            return self._request(
                {
                    "operation": "login",
                    "username": username,
                    "password": password_text,
                }
            )
        finally:
            password_text = ""

    def logout(self) -> RuntimeSnapshot:
        return self._request({"operation": "logout"})

    def connect_consumer(
        self,
        *,
        profile: str | None = None,
    ) -> RemoteRuntimeConsumer:
        del profile
        return RemoteRuntimeConsumer(self, self.snapshot())

    def authorize(
        self,
        boundary: str,
        *,
        expected: AuthScope,
    ) -> RuntimeSnapshot:
        return self._request(
            {
                "operation": "authorize",
                "boundary": boundary,
                "expected": {
                    "runtime_instance_id": expected.runtime_instance_id,
                    "epoch": expected.epoch,
                },
            }
        )

    def _request(self, params: dict[str, object]) -> RuntimeSnapshot:
        request = {
            "version": _RUNTIME_PROTOCOL_VERSION,
            **params,
        }
        encoded = (
            json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > _RUNTIME_FRAME_LIMIT:
            raise AuthRequired("runtime_unavailable")
        connection: Any = self._endpoint.connect_current()
        try:
            if isinstance(connection, socket.socket):
                connection.settimeout(15.0)
            connection.sendall(encoded)
            raw = _read_runtime_frame(connection)
        except (AuthRequired, OSError, TimeoutError):
            raise AuthRequired("runtime_unavailable") from None
        finally:
            connection.close()
        try:
            response = json.loads(raw)
        except (UnicodeError, ValueError):
            raise AuthRequired("runtime_unavailable") from None
        if not isinstance(response, dict) or response.get("version") != 1:
            raise AuthRequired("runtime_unavailable")
        if response.get("ok") is not True:
            reason = response.get("reason")
            if not isinstance(reason, str) or not reason:
                reason = "runtime_unavailable"
            raise AuthRequired(reason)
        if set(response) != {"version", "ok", "snapshot"}:
            raise AuthRequired("runtime_unavailable")
        snapshot = _snapshot_from_public(response.get("snapshot"))
        with self._lock:
            self._snapshot = snapshot
        return snapshot


class OwnerBroker:
    """Bounded native IPC broker for one VaultOwner or MemoryOwner."""

    def __init__(
        self,
        owner: _EntryPointOwner,
        endpoint: UnixEndpoint | WindowsNamedPipeEndpoint,
        server: UnixOwnerServer | WindowsOwnerServer,
        owner_lock: UnixOwnerLock | None,
    ) -> None:
        self._owner = owner
        self._endpoint = endpoint
        self._server = server
        self._owner_lock = owner_lock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve,
            name="hermes-auth-owner",
            daemon=True,
        )
        self._maintenance_thread = threading.Thread(
            target=self._maintain,
            name="hermes-auth-maintenance",
            daemon=True,
        )

    @classmethod
    def start(
        cls,
        owner: _EntryPointOwner,
        *,
        endpoint: UnixEndpoint | WindowsNamedPipeEndpoint | None = None,
    ) -> OwnerBroker:
        selected = endpoint or runtime_endpoint()
        owner_lock: UnixOwnerLock | None = None
        if isinstance(selected, UnixEndpoint):
            owner_lock = selected.acquire_owner_lock()
            try:
                server = selected.bind_owner(owner_lock)
            except Exception:
                owner_lock.close()
                raise
        else:
            server = selected.bind_owner()
        broker = cls(owner, selected, server, owner_lock)
        broker._thread.start()
        broker._maintenance_thread.start()
        return broker

    @property
    def endpoint(self) -> UnixEndpoint | WindowsNamedPipeEndpoint:
        return self._endpoint

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._server.close()
        if self._owner_lock is not None:
            self._owner_lock.close()

    def wait(self) -> None:
        self._stop.wait()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection = self._server.accept()
            except AuthRequired:
                if not self._stop.is_set():
                    self._stop.set()
                return
            try:
                raw = _read_runtime_frame(connection)
                response = self._dispatch(raw)
                encoded = (
                    json.dumps(response, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                if len(encoded) > _RUNTIME_FRAME_LIMIT:
                    raise AuthRequired("runtime_unavailable")
                connection.sendall(encoded)
            except Exception:
                try:
                    connection.sendall(
                        b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
                    )
                except Exception:
                    pass
            finally:
                connection.close()
            if isinstance(self._endpoint, WindowsNamedPipeEndpoint):
                try:
                    self._server = self._endpoint.bind_owner()
                except AuthRequired:
                    self._stop.set()
                    return

    def _maintain(self) -> None:
        maintenance = getattr(self._owner, "maintenance", None)
        while not self._stop.wait(0.5):
            if maintenance is None:
                continue
            try:
                if maintenance() is False:
                    self.close()
                    return
            except AuthRequired:
                continue
            except Exception:
                self.close()
                return

    def _dispatch(self, raw: bytes) -> dict[str, object]:
        try:
            request = json.loads(raw)
        except (UnicodeError, ValueError):
            return _runtime_error("runtime_unavailable")
        if not isinstance(request, dict) or request.get("version") != 1:
            return _runtime_error("runtime_unavailable")
        operation = request.get("operation")
        try:
            if operation == "status" and set(request) == {"version", "operation"}:
                snapshot = self._owner.refresh()
            elif operation == "logout" and set(request) == {"version", "operation"}:
                snapshot = self._owner.logout()  # type: ignore[attr-defined]
            elif operation == "login" and set(request) == {
                "version",
                "operation",
                "username",
                "password",
            }:
                username = request.get("username")
                password_text = request.get("password")
                if not isinstance(username, str) or not isinstance(password_text, str):
                    raise AuthRequired("invalid_credentials")
                password = bytearray(password_text.encode("utf-8"))
                password_text = ""
                try:
                    snapshot = self._owner.login(username, password)
                finally:
                    password[:] = b"\0" * len(password)
            elif operation == "authorize" and set(request) == {
                "version",
                "operation",
                "boundary",
                "expected",
            }:
                boundary = request.get("boundary")
                expected = _scope_from_wire(request.get("expected"))
                if not isinstance(boundary, str) or not 0 < len(boundary) <= 256:
                    raise AuthRequired("runtime_unavailable")
                snapshot = self._owner.snapshot()
                consumer = self._owner.connect_consumer()
                consumer.require_authorized(boundary, expected=expected)
            else:
                raise AuthRequired("runtime_unavailable")
        except AuthRequired as error:
            return _runtime_error(error.reason or error.code)
        except Exception:
            return _runtime_error("runtime_unavailable")
        return {
            "version": 1,
            "ok": True,
            "snapshot": snapshot.public_dict(),
        }


def connect_runtime_owner(
    *,
    endpoint: UnixEndpoint | WindowsNamedPipeEndpoint | None = None,
) -> RemoteRuntimeOwner:
    remote = RemoteRuntimeOwner(endpoint or runtime_endpoint())
    remote.refresh()
    return remote


def start_runtime_owner(*, timeout: float = 5.0) -> RemoteRuntimeOwner:
    """Start the fixed auth-only owner process and wait for its native endpoint."""
    try:
        return connect_runtime_owner()
    except AuthRequired:
        pass
    environment = _owner_process_environment()
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hermes_cli.client_auth.runtime",
                "owner",
            ],
            **kwargs,
        )
    except Exception:
        raise AuthRequired("runtime_unavailable") from None

    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            return connect_runtime_owner()
        except AuthRequired:
            time.sleep(0.05)
    raise AuthRequired("runtime_unavailable")


def _owner_process_environment() -> dict[str, str]:
    exact = {
        "APPDATA",
        "DISPLAY",
        "HOME",
        "KUBERNETES_SERVICE_HOST",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "SSH_CONNECTION",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_RUNTIME_DIR",
        "container",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in exact and isinstance(value, str)
    }


def _owner_election_context() -> OwnerElectionContext:
    ssh_connection = bool(os.environ.get("SSH_CONNECTION"))
    containerized = bool(
        os.environ.get("container")
        or os.environ.get("KUBERNETES_SERVICE_HOST")
        or os.path.exists("/.dockerenv")
    )
    graphical_session = not ssh_connection and not containerized and (
        sys.platform in {"darwin", "win32"}
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    return OwnerElectionContext(
        ssh_connection=ssh_connection,
        containerized=containerized,
        graphical_session=graphical_session,
        platform=sys.platform,
    )


def _create_elected_owner() -> VaultOwner | MemoryOwner:
    context = _owner_election_context()

    def memory_owner() -> MemoryOwner:
        transition: Callable[[RuntimeSnapshot], None] | None = None
        service_root = Path("/run/service")
        if context.containerized and service_root.is_dir():
            transition = S6LifecycleAdapter(
                service_root=service_root,
                hermes_home=Path(os.environ.get("HERMES_HOME", "/opt/data")),
                environment=dict(os.environ),
            ).transition
        return MemoryOwner(
            AuthClient(),
            hardener=ProcessHardener(),
            on_transition=transition,
        )

    return resolve_owner(
        context,
        live_owner=lambda: None,
        vault_factory=lambda: VaultOwner(AuthClient()),
        memory_factory=memory_owner,
    )


def run_owner_service() -> int:
    try:
        connect_runtime_owner()
    except AuthRequired:
        pass
    else:
        return 0
    owner = _create_elected_owner()
    try:
        broker = OwnerBroker.start(owner)
    except AuthRequired:
        return 0
    install_entrypoint_owner(owner)
    try:
        broker.wait()
    finally:
        clear_entrypoint_owner()
        broker.close()
        owner.close()
    return 0


def _read_runtime_frame(connection: Any) -> bytes:
    data = bytearray()
    while True:
        chunk = connection.recv(min(4096, _RUNTIME_FRAME_LIMIT + 1 - len(data)))
        if not chunk:
            raise AuthRequired("runtime_unavailable")
        data.extend(chunk)
        if len(data) > _RUNTIME_FRAME_LIMIT:
            raise AuthRequired("runtime_unavailable")
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise AuthRequired("runtime_unavailable")
            return bytes(data[:newline])


def _scope_from_wire(value: object) -> AuthScope:
    if not isinstance(value, dict) or set(value) != {
        "runtime_instance_id",
        "epoch",
    }:
        raise AuthRequired("runtime_unavailable")
    instance = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    if not isinstance(instance, str) or len(instance) != 32:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AuthRequired("runtime_unavailable")
    return AuthScope(instance, epoch)


def _snapshot_from_public(value: object) -> RuntimeSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "state",
        "username",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "session_expires_at",
        "reason",
    }:
        raise AuthRequired("runtime_unavailable")
    try:
        state = AuthState(value["state"])
    except (KeyError, TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    username = value["username"]
    instance = value["runtime_instance_id"]
    epoch = value["epoch"]
    valid_until = value["valid_until"]
    expires = value["session_expires_at"]
    reason = value["reason"]
    if username is not None and not isinstance(username, str):
        raise AuthRequired("runtime_unavailable")
    if not isinstance(instance, str) or len(instance) != 32:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(valid_until, (int, float)) or isinstance(valid_until, bool):
        raise AuthRequired("runtime_unavailable")
    if expires is not None and not isinstance(expires, str):
        raise AuthRequired("runtime_unavailable")
    if reason is not None and not isinstance(reason, str):
        raise AuthRequired("runtime_unavailable")
    return RuntimeSnapshot(
        state=state,
        epoch=epoch,
        valid_until=float(valid_until),
        runtime_instance_id=instance,
        boot_id=_read_boot_id(),
        username=username,
        session_expires_at=expires,
        reason=reason,
    )


def _runtime_error(reason: str) -> dict[str, object]:
    return {
        "version": 1,
        "ok": False,
        "reason": reason,
    }


_entrypoint_owner_lock = threading.RLock()
_entrypoint_owner: _EntryPointOwner | None = None


def install_entrypoint_owner(owner: _EntryPointOwner) -> None:
    global _entrypoint_owner
    with _entrypoint_owner_lock:
        _entrypoint_owner = owner


def clear_entrypoint_owner() -> None:
    global _entrypoint_owner
    with _entrypoint_owner_lock:
        _entrypoint_owner = None
    clear_runtime_consumer()


def authorize_entrypoint(boundary: str, *, interactive: bool) -> AuthScope:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner()
        except AuthRequired:
            if not interactive:
                raise
            owner = start_runtime_owner()

    recoverable = {"session_rejected", "session_expired", "signed_out"}
    try:
        snapshot = owner.refresh()
    except AuthRequired as error:
        if not interactive or error.reason not in recoverable:
            raise
        snapshot = owner.snapshot()

    if snapshot.state is not AuthState.AUTHENTICATED:
        reason = snapshot.reason or "signed_out"
        if not interactive or reason in {
            "rate_limited",
            "server_unavailable",
            "invalid_response",
            "runtime_unavailable",
            "vault_unavailable",
        }:
            raise AuthRequired(reason)
        try:
            username = input("Username: ").strip()
            import getpass

            password_text = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            raise AuthRequired("signed_out") from None
        password = bytearray(password_text.encode("utf-8"))
        password_text = ""
        try:
            snapshot = owner.login(username, password)
        finally:
            password[:] = b"\0" * len(password)

    consumer = owner.connect_consumer()
    scope = consumer.require_authorized(
        boundary,
        expected=snapshot.scope,
    )
    install_runtime_consumer(consumer)
    return scope


def account_status() -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner()
        except AuthRequired:
            return RuntimeSnapshot.signed_out(reason="runtime_unavailable")
    try:
        return owner.refresh()
    except AuthRequired:
        return owner.snapshot()


def account_login(username: str, password: bytearray) -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner()
        except AuthRequired:
            owner = start_runtime_owner()
    return owner.login(username, password)


def account_logout() -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner()
        except AuthRequired:
            return RuntimeSnapshot.signed_out()
    return owner.logout()


@dataclass(frozen=True)
class OwnerElectionContext:
    ssh_connection: bool
    containerized: bool
    graphical_session: bool
    platform: str


_Owner = TypeVar("_Owner")


def resolve_owner(
    context: OwnerElectionContext,
    *,
    live_owner: Callable[[], _Owner | None],
    vault_factory: Callable[[], _Owner],
    memory_factory: Callable[[], _Owner],
) -> _Owner:
    existing = live_owner()
    if existing is not None:
        return existing
    if context.ssh_connection or context.containerized:
        return memory_factory()
    if context.graphical_session and context.platform in {"darwin", "linux", "win32"}:
        return vault_factory()
    return memory_factory()


def runtime_endpoint() -> UnixEndpoint | WindowsNamedPipeEndpoint:
    if os.name == "nt":
        return WindowsNamedPipeEndpoint.for_current_sid(first_instance=True)
    return UnixEndpoint.for_current_user(
        random_name=secrets.token_hex(16),
        forbid_abstract=sys.platform.startswith("linux"),
        darwin_user_temp=sys.platform == "darwin",
    )


def _encode_cookie_blob(record: CookieRecord) -> str:
    payload = {
        "version": 1,
        "cookies": dict(record.cookies),
        "username": record.username,
        "session_expires_at": record.session_expires_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_cookie_blob(raw: str) -> CookieRecord:
    if not isinstance(raw, str) or not raw or len(raw) > 16_384:
        raise AuthRequired("runtime_unavailable")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "cookies",
        "username",
        "session_expires_at",
    }:
        raise AuthRequired("runtime_unavailable")
    if payload.get("version") != 1:
        raise AuthRequired("runtime_unavailable")
    cookies = payload.get("cookies")
    username = payload.get("username")
    session_expires_at = payload.get("session_expires_at")
    if not isinstance(cookies, dict) or set(cookies) != {SESSION_COOKIE, CSRF_COOKIE}:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(username, str) or not username:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(session_expires_at, str) or not session_expires_at:
        raise AuthRequired("runtime_unavailable")
    normalized: dict[str, str] = {}
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        value = cookies.get(name)
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n;"):
            raise AuthRequired("runtime_unavailable")
        normalized[name] = value
    _parse_aware_datetime(session_expires_at)
    return CookieRecord(normalized, username, session_expires_at)


_consumer_lock = threading.RLock()
_consumer: RuntimeConsumer | None = None
_BACKEND_SCOPE_CONTROL_FRAME_LIMIT = 4_096
_backend_scope_control_lock = threading.Lock()
_backend_scope_control_thread: threading.Thread | None = None


def install_runtime_consumer(consumer: RuntimeConsumer) -> None:
    global _consumer
    with _consumer_lock:
        _consumer = consumer


def clear_runtime_consumer() -> None:
    global _consumer
    with _consumer_lock:
        _consumer = None


def require_authorized(
    boundary: str,
    *,
    expected: AuthScope | None = None,
) -> AuthScope:
    with _consumer_lock:
        consumer = _consumer
    if consumer is None:
        raise AuthRequired("runtime_unavailable")
    return consumer.require_authorized(boundary, expected=expected)


def wait_until_authorized(
    boundary: str,
    *,
    stop_event: threading.Event,
    on_state: Callable[[RuntimeSnapshot], None],
    poll_seconds: float = 0.5,
    start_owner_if_missing: bool = False,
) -> LockedWaitingResult:
    """Wait without prompting until the shared owner grants a fresh scope."""
    if not boundary or poll_seconds < 0:
        raise AuthRequired("runtime_unavailable")
    owner: RemoteRuntimeOwner | None = None
    while not stop_event.is_set():
        if owner is None:
            try:
                owner = connect_runtime_owner()
            except AuthRequired as error:
                if start_owner_if_missing and error.reason == "runtime_unavailable":
                    try:
                        owner = start_runtime_owner()
                    except AuthRequired as start_error:
                        snapshot = RuntimeSnapshot.signed_out(
                            reason=start_error.reason or start_error.code,
                        )
                    else:
                        snapshot = owner.snapshot()
                else:
                    snapshot = RuntimeSnapshot.signed_out(
                        reason=error.reason or error.code,
                    )
                on_state(snapshot)
                stop_event.wait(poll_seconds)
                continue
        try:
            snapshot = owner.refresh()
        except AuthRequired as error:
            if error.reason == "runtime_unavailable":
                owner = None
            snapshot = RuntimeSnapshot.signed_out(
                reason=error.reason or error.code,
            )
        on_state(snapshot)
        if snapshot.state is AuthState.AUTHENTICATED and owner is not None:
            consumer = owner.connect_consumer()
            consumer.require_authorized(boundary, expected=snapshot.scope)
            install_runtime_consumer(consumer)  # type: ignore[arg-type]
            return LockedWaitingResult.AUTHENTICATED
        stop_event.wait(poll_seconds)
    return LockedWaitingResult.OWNER_STOPPED


backend_scope_tokens = BackendScopeTokenRegistry()


def register_backend_scope_token(value: object) -> BackendScopeGrant:
    registration = parse_backend_scope_token_registration(value)
    return backend_scope_tokens.register(
        registration.bearer,
        connection_id=registration.connection_id,
        expected=registration.auth,
        ttl_seconds=registration.ttl_seconds,
    )


def _run_backend_scope_token_control(stream: Any) -> None:
    try:
        while True:
            try:
                raw = stream.readline(_BACKEND_SCOPE_CONTROL_FRAME_LIMIT + 1)
            except (OSError, ValueError):
                break
            if not raw:
                break
            if len(raw) > _BACKEND_SCOPE_CONTROL_FRAME_LIMIT or not raw.endswith(b"\n"):
                break
            try:
                value = json.loads(raw)
                register_backend_scope_token(value)
            except (AuthRequired, UnicodeError, ValueError):
                break
    finally:
        backend_scope_tokens.clear()


def start_backend_scope_token_control(
    stream: Any | None = None,
) -> threading.Thread:
    """Read the Desktop-only token protocol from inherited stdin.

    The raw bearer exists only in the bounded registration frame and the
    caller's request object. The registry stores its SHA-256 digest. EOF or
    malformed input revokes every grant for this backend process.
    """
    global _backend_scope_control_thread
    selected = stream if stream is not None else sys.stdin.buffer
    with _backend_scope_control_lock:
        running = _backend_scope_control_thread
        if running is not None and running.is_alive():
            return running
        thread = threading.Thread(
            target=_run_backend_scope_token_control,
            args=(selected,),
            daemon=True,
            name="hermes-backend-scope-control",
        )
        _backend_scope_control_thread = thread
        thread.start()
        return thread


def _lease_deadline(
    status: SessionStatus,
    *,
    now: float,
    absolute_cap: datetime | None = None,
) -> float:
    server_time = _parse_aware_datetime(status.server_time)
    response_expiry = _parse_aware_datetime(status.session_expires_at)
    expiry = min(response_expiry, absolute_cap) if absolute_cap is not None else response_expiry
    absolute_remaining = (expiry - server_time).total_seconds()
    if absolute_remaining <= 0:
        raise AuthRequired("session_expired")
    return now + min(LEASE_SECONDS, absolute_remaining)


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthRequired("runtime_unavailable")
    return parsed


def _validate_random_name(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise AuthRequired("runtime_unavailable")


def _validate_socket_name(value: str) -> None:
    if not value.startswith("h-") or not value.endswith(".s"):
        raise AuthRequired("runtime_unavailable")
    compact = value[2:-2]
    if len(compact) != 20 or any(
        character not in "0123456789abcdef" for character in compact
    ):
        raise AuthRequired("runtime_unavailable")
    if Path(value).name != value:
        raise AuthRequired("runtime_unavailable")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_private_regular_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_socket_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISSOCK(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise AuthRequired("runtime_unavailable")


def _open_private_file(
    path: Path,
    requested_flags: int,
    *,
    create: bool = True,
) -> int:
    flags = requested_flags
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if create and requested_flags & os.O_CREAT:
        mode = 0o600
    else:
        mode = 0
    try:
        fd = os.open(path, flags, mode)
        os.set_inheritable(fd, False)
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise AuthRequired("runtime_unavailable")
        return fd
    except Exception:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise AuthRequired("runtime_unavailable") from None


def _validate_peer_uid(connection: socket.socket) -> None:
    try:
        if sys.platform == "darwin":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            getpeereid = libc.getpeereid
            getpeereid.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ]
            getpeereid.restype = ctypes.c_int
            peer_uid_value = ctypes.c_uint()
            peer_gid_value = ctypes.c_uint()
            if getpeereid(
                connection.fileno(),
                ctypes.byref(peer_uid_value),
                ctypes.byref(peer_gid_value),
            ) != 0:
                raise OSError(ctypes.get_errno(), "getpeereid failed")
            peer_uid = peer_uid_value.value
        elif sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", raw)
        else:
            raise AuthRequired("runtime_unavailable")
    except (AttributeError, OSError, struct.error):
        raise AuthRequired("runtime_unavailable") from None
    if peer_uid != os.getuid():
        raise AuthRequired("runtime_unavailable")


def _linux_runtime_root() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        base = Path(configured)
        try:
            details = base.lstat()
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise AuthRequired("runtime_unavailable")
        return base / "hermes-remote-auth"
    return Path(tempfile.gettempdir()) / f"hermes-remote-auth-{os.getuid()}"


def _darwin_user_temp_dir() -> Path:
    if sys.platform != "darwin":
        raise AuthRequired("runtime_unavailable")
    try:
        import ctypes

        darwin_user_temp_dir = 65_537
        libc = ctypes.CDLL(None, use_errno=True)
        confstr = libc.confstr
        confstr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
        confstr.restype = ctypes.c_size_t
        size = confstr(darwin_user_temp_dir, None, 0)
        if size <= 1 or size > 4096:
            raise OSError("invalid Darwin user temporary directory size")
        buffer = ctypes.create_string_buffer(size)
        if confstr(darwin_user_temp_dir, buffer, size) != size:
            raise OSError("Darwin user temporary directory changed")
        path = Path(os.fsdecode(buffer.value))
        details = path.lstat()
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise AuthRequired("runtime_unavailable")
    return path


def _windows_current_sid() -> str:
    if os.name != "nt":
        raise AuthRequired("runtime_unavailable")
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_QUERY,
        )
        try:
            sid = win32security.GetTokenInformation(
                token,
                win32security.TokenUser,
            )[0]
            owner_sid = win32security.ConvertSidToStringSid(sid)
        finally:
            token.Close()
    except Exception:
        raise AuthRequired("runtime_unavailable") from None
    if not isinstance(owner_sid, str) or not owner_sid.startswith("S-1-"):
        raise AuthRequired("runtime_unavailable")
    return owner_sid


def _disable_core_dumps() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise OSError("core dump limit was not applied")
    except (ImportError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _linux_prctl_dumpable_zero() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0 or prctl(3, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _darwin_deny_attach() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        ptrace = libc.ptrace
        ptrace.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        ptrace.restype = ctypes.c_int
        if ptrace(31, 0, None, 0) != 0:
            raise OSError(ctypes.get_errno(), "ptrace failed")
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _windows_disable_wer_dump() -> None:
    try:
        import ctypes

        no_ui = 0x20
        wer = ctypes.WinDLL("wer", use_last_error=True)
        wer.WerSetFlags.argtypes = [ctypes.c_uint]
        wer.WerSetFlags.restype = ctypes.c_long
        if wer.WerSetFlags(no_ui) != 0:
            raise OSError(ctypes.get_last_error(), "WerSetFlags failed")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _windows_apply_process_mitigations() -> None:
    try:
        import ctypes
        from ctypes import wintypes

        class _ExtensionPointPolicy(ctypes.Structure):
            _fields_ = [("flags", wintypes.DWORD)]

        process_extension_point_disable_policy = 6
        disable_extension_points = 0x1
        policy = _ExtensionPointPolicy(disable_extension_points)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        apply_policy = kernel32.SetProcessMitigationPolicy
        apply_policy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        apply_policy.restype = wintypes.BOOL
        if not apply_policy(
            process_extension_point_disable_policy,
            ctypes.byref(policy),
            ctypes.sizeof(policy),
        ):
            raise OSError(ctypes.get_last_error(), "process mitigation failed")
    except (AttributeError, ImportError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


@lru_cache(maxsize=1)
def _read_boot_id() -> str:
    if sys.platform.startswith("linux"):
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            )
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        normalized = boot_id.strip()
        if not normalized:
            raise AuthRequired("runtime_unavailable")
        return normalized
    if sys.platform == "darwin":
        try:
            record = os.stat("/var/run/utmpx", follow_symlinks=False)
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        birth_time_ns = getattr(record, "st_birthtime_ns", None)
        if birth_time_ns is None:
            birth_time = getattr(record, "st_birthtime", None)
            birth_time_ns = int(birth_time * 1_000_000_000) if birth_time else None
        if (
            not stat.S_ISREG(record.st_mode)
            or record.st_uid != 0
            or not birth_time_ns
        ):
            raise AuthRequired("runtime_unavailable")
        return f"darwin:{record.st_dev}:{record.st_ino}:{birth_time_ns}"
    if os.name == "nt":
        return _windows_boot_id()
    raise AuthRequired("runtime_unavailable")


def _windows_boot_id() -> str:
    try:
        import ctypes

        uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    approximate_boot = int((time.time() - (uptime_ms / 1000.0)) // 10)
    return f"windows:{approximate_boot}"


_LOCKED_WAIT_BOUNDARIES = frozenset(
    {
        "container.dashboard.start",
        "container.gateway.start",
        "container.main.start",
        "service.cron.start",
        "service.gateway.start",
        "service.kanban.start",
        "service.web.start",
    }
)


def _run_locked_wait(boundary: str) -> int:
    if boundary not in _LOCKED_WAIT_BOUNDARIES:
        return 2
    stop = threading.Event()
    last_state: tuple[AuthState, str | None] | None = None

    def report(snapshot: RuntimeSnapshot) -> None:
        nonlocal last_state
        current = (snapshot.state, snapshot.reason)
        if current == last_state:
            return
        last_state = current
        payload = {
            "auth_state": (
                "authenticated"
                if snapshot.state is AuthState.AUTHENTICATED
                else "locked-waiting"
            ),
            "reason": snapshot.reason,
            "guidance": "run `hermes login`",
        }
        print(json.dumps(payload, sort_keys=True), flush=True)

    try:
        wait_until_authorized(
            boundary,
            stop_event=stop,
            on_state=report,
        )
    except KeyboardInterrupt:
        stop.set()
    return 0


def _notify_service_manager(snapshot: RuntimeSnapshot) -> None:
    """Keep a systemd service healthy while its capability is locked."""
    endpoint = os.environ.get("NOTIFY_SOCKET", "")
    if not endpoint:
        return
    address = ("\0" + endpoint[1:]) if endpoint.startswith("@") else endpoint
    state = (
        "authenticated"
        if snapshot.state is AuthState.AUTHENTICATED
        else "locked-waiting"
    )
    payload = f"READY=1\nWATCHDOG=1\nSTATUS=auth_state={state}".encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.sendto(payload, address)
    except OSError:
        # The request-boundary guard remains authoritative if the optional
        # service-manager status channel is unavailable.
        return


def _run_locked_service(arguments: list[str]) -> int:
    profile: str | None = None
    if len(arguments) in {2, 3} and arguments[:2] == ["service", "gateway"]:
        profile = arguments[2] if len(arguments) == 3 else None
        boundary = "service.gateway.start"
        target = [sys.executable, "-m", "hermes_cli.main"]
        if profile is not None:
            target.extend(["-p", profile])
        target.extend(["gateway", "run"])
    elif arguments == ["service", "kanban"]:
        boundary = "service.kanban.start"
        target = [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "daemon",
            "--force",
            "--interval",
            "60",
        ]
    else:
        return 2
    if profile is not None:
        try:
            from hermes_cli.service_manager import validate_profile_name

            validate_profile_name(profile)
        except (ImportError, ValueError):
            return 2

    stop = threading.Event()
    last: tuple[AuthState, str | None] | None = None

    def report(snapshot: RuntimeSnapshot) -> None:
        nonlocal last
        _notify_service_manager(snapshot)
        current = (snapshot.state, snapshot.reason)
        if current == last:
            return
        last = current
        print(
            json.dumps(
                {
                    "auth_state": (
                        "authenticated"
                        if snapshot.state is AuthState.AUTHENTICATED
                        else "locked-waiting"
                    ),
                    "reason": snapshot.reason,
                    "guidance": "run `hermes login`",
                },
                sort_keys=True,
            ),
            flush=True,
        )

    try:
        result = wait_until_authorized(
            boundary,
            stop_event=stop,
            on_state=report,
            start_owner_if_missing=True,
        )
    except KeyboardInterrupt:
        return 0
    if result is not LockedWaitingResult.AUTHENTICATED:
        return 0
    os.execv(sys.executable, target)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["owner"]:
        return run_owner_service()
    if len(arguments) == 2 and arguments[0] == "wait":
        return _run_locked_wait(arguments[1])
    if arguments[:1] == ["service"]:
        return _run_locked_service(arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
