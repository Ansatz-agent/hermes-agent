"""Release-server source acquisition for managed Ansatz installations.

Hermes' normal updater obtains a new source tree from Git and then runs the
dependency/build/migration stages against that tree.  A packaged Ansatz
installation has no repository credentials or ``.git`` directory, so it uses
the same downstream update pipeline with one different input: a release API
describes a reviewed ``hermes-backend.tar.gz`` archive hosted by the operator.

The production endpoint has a built-in default.  Developers may point a VM at
an ordinary HTTP server on the host with ``ANSATZ_UPDATE_BASE_URL``; this
module intentionally does not impose loopback, private-address, TLS, or DNS
policy on that explicit override.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version


UPDATE_BASE_URL_ENV = "ANSATZ_UPDATE_BASE_URL"
DEFAULT_UPDATE_BASE_URL = "https://setup.hermes-agent.nousresearch.com"
LATEST_RELEASE_PATH = "/api/v1/ansatz/releases/latest"
RELEASE_SCHEMA_VERSION = 1
SOURCE_MARKER_NAME = ".hermes-bundled-source.json"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 200_000
_ARCHIVE_ROOT = "hermes-agent"
_REQUIRED_SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "hermes_cli/main.py",
    "scripts/install.ps1",
    "scripts/install.sh",
    "apps/desktop/package.json",
    "desktop_auth_runtime/uv.lock",
)


class UpdateSourceError(RuntimeError):
    """The release endpoint or source archive violated its contract."""


@dataclass(frozen=True)
class ReleaseArchive:
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    commit: str
    channel: str
    archive: ReleaseArchive
    published_at: str | None = None


def resolve_update_base_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the operator endpoint, accepting normal VM/LAN HTTP overrides."""
    source = os.environ if environ is None else environ
    value = str(source.get(UPDATE_BASE_URL_ENV) or DEFAULT_UPDATE_BASE_URL).strip()
    if not value:
        raise UpdateSourceError(f"{UPDATE_BASE_URL_ENV} cannot be empty")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateSourceError(
            f"{UPDATE_BASE_URL_ENV} must be an absolute http(s) URL"
        )
    return value.rstrip("/")


def current_platform() -> str:
    if os.name == "nt":
        return "windows"
    if platform.system() == "Darwin":
        return "macos"
    return "linux"


def current_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine or "unknown"


def latest_release_url(
    base_url: str,
    *,
    channel: str = "stable",
    target_platform: str | None = None,
    architecture: str | None = None,
) -> str:
    query = urlencode(
        {
            "channel": channel,
            "platform": target_platform or current_platform(),
            "arch": architecture or current_architecture(),
        }
    )
    return f"{base_url.rstrip('/')}{LATEST_RELEASE_PATH}?{query}"


def _request(url: str) -> Request:
    try:
        from hermes_cli import __version__
    except Exception:
        __version__ = "unknown"
    return Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"ansatz-agent/{__version__}",
        },
    )


def _read_limited(response, limit: int) -> bytes:
    value = response.read(limit + 1)
    if len(value) > limit:
        raise UpdateSourceError("release metadata response is too large")
    return value


def parse_release_metadata(raw: object, *, base_url: str) -> ReleaseMetadata:
    if not isinstance(raw, dict):
        raise UpdateSourceError("release metadata must be a JSON object")
    if raw.get("schemaVersion") != RELEASE_SCHEMA_VERSION:
        raise UpdateSourceError(
            f"release metadata schemaVersion must be {RELEASE_SCHEMA_VERSION}"
        )
    if raw.get("product") not in {"ansatz", "ansatz-agent"}:
        raise UpdateSourceError("release metadata product must identify Ansatz")

    version = str(raw.get("version") or "").strip()
    try:
        Version(version)
    except InvalidVersion as exc:
        raise UpdateSourceError("release metadata version is invalid") from exc

    commit = str(raw.get("commit") or "").lower()
    if not _COMMIT_RE.fullmatch(commit) or set(commit) == {"0"}:
        raise UpdateSourceError("release metadata commit must be a real Git SHA")

    channel = str(raw.get("channel") or "stable").strip() or "stable"
    archive_raw = raw.get("archive")
    if not isinstance(archive_raw, dict):
        raise UpdateSourceError("release metadata archive is missing")
    archive_url = str(archive_raw.get("url") or "").strip()
    if not archive_url:
        raise UpdateSourceError("release metadata archive.url is missing")
    archive_url = urljoin(base_url.rstrip("/") + "/", archive_url)
    parsed_archive_url = urlparse(archive_url)
    if parsed_archive_url.scheme not in {"http", "https"} or not parsed_archive_url.netloc:
        raise UpdateSourceError("release archive URL must use http(s)")

    size = archive_raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= _MAX_ARCHIVE_BYTES):
        raise UpdateSourceError("release archive size is invalid")
    sha256 = str(archive_raw.get("sha256") or "").lower()
    if not _SHA256_RE.fullmatch(sha256):
        raise UpdateSourceError("release archive SHA-256 is invalid")

    published = raw.get("publishedAt")
    if published is not None and not isinstance(published, str):
        raise UpdateSourceError("release metadata publishedAt must be a string")

    return ReleaseMetadata(
        version=version,
        commit=commit,
        channel=channel,
        archive=ReleaseArchive(url=archive_url, size=size, sha256=sha256),
        published_at=published,
    )


def fetch_latest_release(
    *,
    base_url: str | None = None,
    channel: str = "stable",
    target_platform: str | None = None,
    architecture: str | None = None,
    timeout: float = 15.0,
) -> ReleaseMetadata:
    base = (base_url or resolve_update_base_url()).rstrip("/")
    url = latest_release_url(
        base,
        channel=channel,
        target_platform=target_platform,
        architecture=architecture,
    )
    try:
        with urlopen(_request(url), timeout=timeout) as response:
            payload = _read_limited(response, _MAX_METADATA_BYTES)
    except UpdateSourceError:
        raise
    except Exception as exc:
        raise UpdateSourceError(f"could not query release server: {exc}") from exc
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateSourceError("release server returned invalid JSON") from exc
    return parse_release_metadata(raw, base_url=base)


def release_is_newer(
    release: ReleaseMetadata,
    *,
    current_version: str,
    current_commit: str | None = None,
) -> bool:
    """Compare releases; a rebuilt commit at the same version is still new."""
    try:
        remote = Version(release.version)
        local = Version(current_version)
    except InvalidVersion:
        return bool(current_commit and release.commit != current_commit.lower())
    if remote != local:
        return remote > local
    return bool(current_commit and release.commit != current_commit.lower())


def read_source_marker(source_root: Path) -> dict | None:
    try:
        raw = json.loads((source_root / SOURCE_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        return None
    commit = str(raw.get("commit") or "").lower()
    if not _COMMIT_RE.fullmatch(commit):
        return None
    return raw


def download_release_archive(
    release: ReleaseMetadata,
    destination: Path,
    *,
    timeout: float = 120.0,
) -> Path:
    """Stream one pinned archive and verify both declared size and digest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.part-{os.getpid()}")
    digest = hashlib.sha256()
    total = 0
    request = Request(
        release.archive.url,
        headers={"Accept": "application/gzip, application/octet-stream"},
    )
    try:
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > release.archive.size or total > _MAX_ARCHIVE_BYTES:
                    raise UpdateSourceError("release archive exceeds its declared size")
                digest.update(chunk)
                output.write(chunk)
        if total != release.archive.size:
            raise UpdateSourceError(
                f"release archive size mismatch (expected {release.archive.size}, got {total})"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != release.archive.sha256:
            raise UpdateSourceError("release archive SHA-256 mismatch")
        os.replace(temporary, destination)
        return destination
    except UpdateSourceError:
        raise
    except Exception as exc:
        raise UpdateSourceError(f"could not download release archive: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _safe_member_path(name: str) -> PurePosixPath:
    if "\\" in name:
        raise UpdateSourceError(f"archive member uses a backslash path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise UpdateSourceError(f"archive member escapes extraction root: {name}")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or parts[0] != _ARCHIVE_ROOT:
        raise UpdateSourceError(f"archive member is outside {_ARCHIVE_ROOT}/: {name}")
    return PurePosixPath(*parts)


def extract_release_archive(archive_path: Path, destination: Path) -> Path:
    """Extract regular files/directories only, without ``tarfile.extractall``."""
    destination.mkdir(parents=True, exist_ok=True)
    destination_real = destination.resolve()
    member_count = 0
    total_size = 0
    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise UpdateSourceError(f"release archive is not a valid tar.gz: {exc}") from exc

    with archive:
        for member in archive:
            member_count += 1
            if member_count > _MAX_ARCHIVE_MEMBERS:
                raise UpdateSourceError("release archive contains too many entries")
            relative = _safe_member_path(member.name)
            target = destination.joinpath(*relative.parts)
            try:
                target.resolve().relative_to(destination_real)
            except ValueError as exc:
                raise UpdateSourceError(
                    f"archive member escapes extraction root: {member.name}"
                ) from exc

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise UpdateSourceError(
                    f"archive contains unsupported link or special entry: {member.name}"
                )
            total_size += member.size
            if total_size > _MAX_EXTRACTED_BYTES:
                raise UpdateSourceError("release archive expands beyond the safety limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UpdateSourceError(f"could not read archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if os.name != "nt":
                os.chmod(target, stat.S_IMODE(member.mode) & 0o777)

    source_root = destination / _ARCHIVE_ROOT
    for relative in _REQUIRED_SOURCE_FILES:
        if not (source_root / relative).is_file():
            raise UpdateSourceError(f"release archive is missing required file: {relative}")
    return source_root


def write_source_marker(source_root: Path, release: ReleaseMetadata) -> None:
    """Publish the active source identity after a successful tree swap."""
    marker = {
        "schemaVersion": 1,
        "commit": release.commit,
        "archiveSha256": release.archive.sha256,
        "version": release.version,
        "installedAt": datetime.now(timezone.utc).isoformat(),
        "source": "release-server",
    }
    destination = source_root / SOURCE_MARKER_NAME
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{SOURCE_MARKER_NAME}.", suffix=".tmp", dir=source_root
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def stage_desktop_payload_inputs(
    extracted_root: Path,
    archive_path: Path,
    release: ReleaseMetadata,
) -> None:
    """Seed future Desktop builds with the exact source payload just applied.

    Without this, rebuilding the shell after a source update would retain the
    old app's embedded bootstrap payload; its next launch could then replace
    the freshly-updated runtime with that older snapshot.
    """
    bootstrap = extracted_root / "apps" / "desktop" / "build" / "bootstrap"
    bootstrap.mkdir(parents=True, exist_ok=True)
    existing_manifest: dict = {}
    try:
        candidate = json.loads((bootstrap / "payload-manifest.json").read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            existing_manifest = candidate
    except (OSError, json.JSONDecodeError):
        pass
    installer_name = "install.ps1" if os.name == "nt" else "install.sh"
    installer_path = bootstrap / installer_name
    shutil.copy2(archive_path, bootstrap / "hermes-backend.tar.gz")
    shutil.copy2(extracted_root / "scripts" / installer_name, installer_path)
    manifest = {
        "schemaVersion": 1,
        "commit": release.commit,
        "branch": None,
        "version": release.version,
        "archive": {
            "file": "hermes-backend.tar.gz",
            "size": release.archive.size,
            "sha256": release.archive.sha256,
        },
        "installer": {
            "file": installer_name,
            "size": installer_path.stat().st_size,
            "sha256": hashlib.sha256(installer_path.read_bytes()).hexdigest(),
        },
        **(
            {"gitBashRuntime": existing_manifest["gitBashRuntime"]}
            if installer_name == "install.ps1"
            and isinstance(existing_manifest.get("gitBashRuntime"), dict)
            else {}
        ),
    }
    (bootstrap / "payload-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
