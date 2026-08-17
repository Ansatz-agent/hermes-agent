"""Optional on-device SenseVoice speech recognition.

The native runtime and the model are both prepared lazily.  Importing this
module is intentionally cheap and does not import ``sherpa_onnx``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from typing import Any, Callable
import wave

from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags


logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 600 * 1024 * 1024
HTTP_TIMEOUT = (10, 60)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
READY_MARKER = ".ready"
SPEECH_FRAME_MS = 20
MIN_ACTIVITY_RMS = 0.0015
MIN_ACTIVITY_DELTA_RMS = 0.001
MIN_ACTIVITY_RATIO = 0.15
_CONTENT_RANGE_RE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


@dataclass(frozen=True)
class ModelManifest:
    version: str
    urls: tuple[str, ...]
    size: int
    sha256: str


MODEL_MANIFEST = ModelManifest(
    version="2024-07-17-int8",
    urls=(
        "https://www.modelscope.cn/models/fuyuantech/"
        "fuyuan-sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/"
        "resolve/09e1d219be6d1c55a88223f49bd6617e9f531611/"
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
    ),
    size=163_002_883,
    sha256="7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
)


def model_cache_root() -> Path:
    return get_hermes_home() / "cache" / "stt" / "sensevoice"


def model_directory() -> Path:
    return model_cache_root() / MODEL_MANIFEST.version


class PreparationError(RuntimeError):
    """A stable, user-visible SenseVoice preparation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _CONTENT_RANGE_RE.fullmatch(value)
    if match is None:
        return None
    start, end, total = (int(group) for group in match.groups())
    if total <= 0 or end < start or end >= total:
        return None
    return start, end, total


def _part_path(cache_root: Path, manifest: ModelManifest) -> Path:
    return cache_root / f"{manifest.version}.tar.bz2.part"


def _is_model_ready(path: Path, manifest: ModelManifest = MODEL_MANIFEST) -> bool:
    marker = path / READY_MARKER
    try:
        required_files_exist = all(
            candidate.is_file() and candidate.stat().st_size > 0
            for candidate in (path / "model.int8.onnx", path / "tokens.txt", marker)
        )
        return required_files_exist and marker.read_text(encoding="utf-8") == (
            f"{manifest.version}\n{manifest.sha256}\n"
        )
    except OSError:
        return False


def _remove_invalid_version_dir(
    path: Path, *, cache_root: Path, manifest: ModelManifest
) -> None:
    """Remove only the exact pinned version child of the SenseVoice cache."""
    root = cache_root.resolve()
    resolved = path.resolve()
    if resolved.parent != root or resolved.name != manifest.version:
        raise ValueError(
            f"refusing to remove path outside the SenseVoice cache: {path}"
        )
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"SenseVoice version path is not a directory: {path}")
        shutil.rmtree(resolved)
    if resolved.exists():
        raise PreparationError(
            "download_failed", f"could not clear incomplete model: {path}"
        )


def _verify_download(path: Path, manifest: ModelManifest) -> None:
    size = path.stat().st_size if path.is_file() else -1
    if size < manifest.size:
        raise PreparationError(
            "download_failed",
            "SenseVoice model download ended early "
            f"(expected {manifest.size}, got {size})",
        )
    if size > manifest.size:
        path.unlink(missing_ok=True)
        raise PreparationError(
            "checksum_mismatch",
            f"SenseVoice model size mismatch (expected {manifest.size}, got {size})",
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != manifest.sha256:
        path.unlink(missing_ok=True)
        raise PreparationError(
            "checksum_mismatch",
            "SenseVoice model checksum did not match the pinned release",
        )


def _download_from_source(
    url: str,
    manifest: ModelManifest,
    *,
    part: Path,
    http_get: Callable[..., Any],
    progress: Callable[[int, int], None] | None,
) -> Path:
    existing = part.stat().st_size if part.is_file() else 0
    if existing > manifest.size:
        part.unlink(missing_ok=True)
        existing = 0
    elif existing == manifest.size:
        try:
            _verify_download(part, manifest)
        except PreparationError:
            existing = 0
        else:
            return part
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    try:
        with http_get(
            url,
            headers=headers,
            stream=True,
            timeout=HTTP_TIMEOUT,
        ) as response:
            response.raise_for_status()
            response_headers = response.headers
            content_range_present = "Content-Range" in response_headers
            content_range = response_headers.get("Content-Range")
            parsed_range = (
                _parse_content_range(content_range)
                if content_range_present
                else None
            )

            if existing:
                if response.status_code == 200 and not content_range_present:
                    # A server is allowed to ignore Range.  Truncate the
                    # partial file and treat the response as a fresh byte-zero
                    # download.
                    mode = "wb"
                    downloaded = 0
                elif (
                    response.status_code in (200, 206)
                    and parsed_range is not None
                    and parsed_range[0] == existing
                    and parsed_range[2] == manifest.size
                ):
                    mode = "ab"
                    downloaded = existing
                else:
                    raise PreparationError(
                        "download_failed", "SenseVoice model source response was invalid"
                    )
            elif response.status_code == 200:
                if content_range_present and (
                    parsed_range is None
                    or parsed_range[0] != 0
                    or parsed_range[2] != manifest.size
                ):
                    raise PreparationError(
                        "download_failed", "SenseVoice model source response was invalid"
                    )
                mode = "wb"
                downloaded = 0
            elif (
                response.status_code == 206
                and parsed_range is not None
                and parsed_range[0] == 0
                and parsed_range[2] == manifest.size
            ):
                mode = "wb"
                downloaded = 0
            else:
                raise PreparationError(
                    "download_failed", "SenseVoice model source response was invalid"
                )
            with part.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    remaining = manifest.size - downloaded
                    if len(chunk) > remaining:
                        chunk = chunk[: remaining + 1]
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, manifest.size)
                    if downloaded > manifest.size:
                        break
    except PreparationError:
        raise
    except Exception as exc:
        try:
            current_size = part.stat().st_size if part.is_file() else 0
        except OSError:
            current_size = 0
        if current_size >= manifest.size:
            try:
                _verify_download(part, manifest)
            except PreparationError as verification_error:
                raise verification_error from exc
            return part
        raise PreparationError(
            "download_failed", "SenseVoice model source failed"
        ) from exc

    _verify_download(part, manifest)
    return part


def _download_archive(
    manifest: ModelManifest,
    *,
    cache_root: Path,
    http_get: Callable[..., Any],
    progress: Callable[[int, int], None] | None,
) -> Path:
    part = _part_path(cache_root, manifest)
    existing = part.stat().st_size if part.is_file() else 0
    if existing > manifest.size:
        part.unlink(missing_ok=True)
    elif existing == manifest.size:
        try:
            _verify_download(part, manifest)
        except PreparationError:
            pass
        else:
            return part

    if not manifest.urls:
        raise PreparationError(
            "download_failed", "No SenseVoice model download sources are configured"
        )

    last_error: PreparationError | None = None
    source_count = len(manifest.urls)
    for source_index, url in enumerate(manifest.urls, start=1):
        try:
            return _download_from_source(
                url,
                manifest,
                part=part,
                http_get=http_get,
                progress=progress,
            )
        except PreparationError as exc:
            last_error = exc
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            logger.warning(
                "SenseVoice model source %d/%d failed: code=%s cause=%s",
                source_index,
                source_count,
                exc.code,
                type(cause).__name__,
            )

    assert last_error is not None
    if last_error.code == "checksum_mismatch":
        message = (
            "SenseVoice model download failed integrity verification "
            "after all sources were tried"
        )
    else:
        message = "All SenseVoice model sources failed; retry the download"
    raise PreparationError(last_error.code, message) from last_error


def _publish_archive(
    archive_path: Path, *, cache_root: Path, manifest: ModelManifest
) -> Path:
    target = cache_root / manifest.version
    if _is_model_ready(target, manifest):
        return target

    workspace = Path(
        tempfile.mkdtemp(prefix=f".{manifest.version}.preparing-", dir=cache_root)
    )
    extract_root = workspace / "extract"
    publish_dir = workspace / "publish"
    extract_root.mkdir()
    try:
        with tarfile.open(archive_path, "r:bz2") as archive:
            archive.extractall(extract_root, filter="data")

        models = sorted(extract_root.rglob("model.int8.onnx"))
        model_source = next(
            (
                candidate
                for candidate in models
                if candidate.is_file()
                and candidate.stat().st_size > 0
                and (candidate.parent / "tokens.txt").is_file()
                and (candidate.parent / "tokens.txt").stat().st_size > 0
            ),
            None,
        )
        if model_source is None:
            raise PreparationError(
                "download_failed",
                "verified SenseVoice archive did not contain model.int8.onnx and tokens.txt",
            )

        tokens_source = model_source.parent / "tokens.txt"
        publish_dir.mkdir()
        # The extracted files and publication workspace share a filesystem, so
        # rename only the two runtime assets instead of duplicating the whole
        # upstream directory (README/test audio included) at peak disk usage.
        os.replace(model_source, publish_dir / "model.int8.onnx")
        os.replace(tokens_source, publish_dir / "tokens.txt")
        # Extraction is complete and the required files are now staged. Free
        # the verified archive before the atomic publication step so the
        # 600 MiB preflight retains meaningful headroom on small disks.
        archive_path.unlink(missing_ok=True)
        # Publication validity is represented by this marker.  It is written
        # only after every required model file is present.
        (publish_dir / READY_MARKER).write_text(
            f"{manifest.version}\n{manifest.sha256}\n", encoding="utf-8"
        )

        if target.exists():
            _remove_invalid_version_dir(
                target, cache_root=cache_root, manifest=manifest
            )
        os.replace(publish_dir, target)
        return target
    except PreparationError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise PreparationError(
            "download_failed", f"SenseVoice model publication failed: {exc}"
        ) from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _download_and_publish(
    manifest: ModelManifest,
    *,
    cache_root: Path,
    http_get: Callable[..., Any],
    disk_usage: Callable[[Path], Any],
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    target = cache_root / manifest.version
    if _is_model_ready(target, manifest):
        return target
    cache_root.mkdir(parents=True, exist_ok=True)
    free = int(disk_usage(cache_root).free)
    if free < MIN_FREE_BYTES:
        raise PreparationError(
            "insufficient_disk",
            "SenseVoice needs at least 600 MiB of free disk space to prepare",
        )
    archive_path = _download_archive(
        manifest,
        cache_root=cache_root,
        http_get=http_get,
        progress=progress,
    )
    target = _publish_archive(archive_path, cache_root=cache_root, manifest=manifest)
    archive_path.unlink(missing_ok=True)
    return target


_state_lock = threading.Lock()
_prepare_threads: dict[Path, threading.Thread] = {}
_preparation_statuses: dict[Path, dict[str, Any]] = {}


def _resolved_cache_root(cache_root: Path | None = None) -> Path:
    return Path(cache_root if cache_root is not None else model_cache_root()).resolve()


def _set_status(cache_root: Path, status: dict[str, Any]) -> None:
    with _state_lock:
        _preparation_statuses[cache_root] = dict(status)


def _set_download_progress(cache_root: Path, downloaded: int, total: int) -> None:
    _set_status(cache_root, {
        "state": "preparing",
        "phase": "download",
        "downloaded": downloaded,
        "total": total,
    })


def get_preparation_status(*, cache_root: Path | None = None) -> dict[str, Any]:
    root = _resolved_cache_root(cache_root)
    with _state_lock:
        return dict(_preparation_statuses.get(root, {"state": "not_ready"}))


def _ensure_runtime_dependencies() -> None:
    from tools import lazy_deps

    try:
        lazy_deps.ensure("stt.sensevoice", prompt=False)
    except Exception as exc:
        raise PreparationError(
            "dependency_install_failed",
            f"SenseVoice dependency installation failed: {exc}",
        ) from exc


def _prepare_once(cache_root: Path) -> Path:
    _set_status(cache_root, {"state": "preparing", "phase": "dependencies"})
    _ensure_runtime_dependencies()
    target = cache_root / MODEL_MANIFEST.version
    if _is_model_ready(target):
        return target

    import requests

    return _download_and_publish(
        MODEL_MANIFEST,
        cache_root=cache_root,
        http_get=requests.get,
        disk_usage=shutil.disk_usage,
        progress=lambda downloaded, total: _set_download_progress(
            cache_root, downloaded, total
        ),
    )


def prepare_sensevoice(
    *, retry: bool = False, cache_root: Path | None = None
) -> dict[str, Any]:
    """Start preparation for one profile cache and return its current state."""
    root = _resolved_cache_root(cache_root)
    target = root / MODEL_MANIFEST.version
    with _state_lock:
        status = _preparation_statuses.get(root, {"state": "not_ready"})
        if _is_model_ready(target):
            status = {"state": "ready"}
            _preparation_statuses[root] = status
            return dict(status)
        if status.get("state") == "ready":
            return dict(status)
        thread = _prepare_threads.get(root)
        if thread is not None and thread.is_alive():
            return dict(status)
        if status.get("state") == "error" and not retry:
            return dict(status)
        status = {"state": "preparing", "phase": "dependencies"}
        _preparation_statuses[root] = status

        def run() -> None:
            try:
                _prepare_once(root)
            except PreparationError as exc:
                _set_status(root, {
                    "state": "error",
                    "code": exc.code,
                    "message": exc.message,
                })
            except Exception as exc:  # defensive ownership of worker failures
                logger.exception("Unexpected SenseVoice preparation failure")
                _set_status(root, {
                    "state": "error",
                    "code": "download_failed",
                    "message": f"SenseVoice preparation failed: {exc}",
                })
            else:
                _set_status(root, {"state": "ready"})

        thread = threading.Thread(
            target=run, name="hermes-sensevoice-prepare", daemon=True
        )
        _prepare_threads[root] = thread
        thread.start()
        return dict(status)


def _reset_state_for_tests() -> None:
    """Reset process-local preparation state after a completed test worker."""
    with _state_lock:
        if any(thread.is_alive() for thread in _prepare_threads.values()):
            raise RuntimeError(
                "cannot reset SenseVoice state while preparation is running"
            )
        _prepare_threads.clear()
        _preparation_statuses.clear()


_recognizer_lock = threading.Lock()
_recognizer: Any = None
_recognizer_key: tuple[str, bool, str] | None = None


def _runtime_config(language: str | None) -> tuple[str, bool, int]:
    try:
        from tools.transcription_tools import (
            _get_idle_unload_seconds,
            _load_stt_config,
        )

        config = _load_stt_config().get("sensevoice") or {}
        idle_config = dict(config)
        idle_config.setdefault("unload_after_idle_seconds", 300)
        idle_seconds = _get_idle_unload_seconds(idle_config)
    except Exception:
        config = {}
        idle_seconds = 300
    resolved_language = str(language or config.get("language") or "zh").strip() or "zh"
    use_itn = config.get("use_itn", True)
    if isinstance(use_itn, str):
        use_itn = use_itn.strip().lower() not in {"0", "false", "no", "off"}
    return resolved_language, bool(use_itn), idle_seconds


def _normalize_to_wav(source: Path, output: Path) -> None:
    from tools.environments.local import hermes_subprocess_env
    from tools.transcription_tools import _find_ffmpeg_binary

    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found for SenseVoice audio normalization")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        str(output),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        stdin=subprocess.DEVNULL,
        env=hermes_subprocess_env(inherit_credentials=False),
        creationflags=windows_hide_flags(),
    )


def _read_pcm_wav(path: Path) -> tuple[int, Any]:
    import numpy as np

    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != 16_000
        ):
            raise RuntimeError("ffmpeg produced an unsupported SenseVoice WAV format")
        frames = audio.readframes(audio.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return 16_000, samples


def _has_speech_activity(sample_rate: int, samples: Any) -> bool:
    """Conservatively reject silence and stationary background noise.

    SenseVoice can emit a short Mandarin token for an all-zero waveform.  A
    small PCM activity gate prevents that failure without adding another
    model or download.  Comparing loud and quiet frame percentiles keeps
    low-volume speech while rejecting a steady microphone noise floor.
    """
    import numpy as np

    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate <= 0 or values.size < sample_rate // 10:
        return False
    if not np.all(np.isfinite(values)):
        values = np.nan_to_num(values, copy=False)

    frame_size = max(1, sample_rate * SPEECH_FRAME_MS // 1000)
    frame_count = values.size // frame_size
    if frame_count == 0:
        return False
    framed = values[: frame_count * frame_size].reshape(frame_count, frame_size)
    frame_rms = np.sqrt(np.mean(framed * framed, axis=1))
    # Use the loudest sustained tail rather than requiring speech to occupy at
    # least 5% of the whole recording. Dictation begins on click, so several
    # seconds of thinking silence followed by one short Mandarin word is a
    # normal input shape; the 99th percentile still spans multiple 20 ms frames
    # for recordings long enough to hit this gate.
    quiet_rms, active_rms = np.percentile(frame_rms, (20, 99))
    required_delta = max(
        MIN_ACTIVITY_DELTA_RMS, float(quiet_rms) * MIN_ACTIVITY_RATIO
    )
    return bool(
        active_rms >= MIN_ACTIVITY_RMS
        and active_rms - quiet_rms >= required_delta
    )


def _load_recognizer(model_dir: Path, *, language: str, use_itn: bool) -> Any:
    global _recognizer, _recognizer_key
    key = (language, use_itn, str(model_dir.resolve()))
    recognizer = _recognizer
    if recognizer is not None and _recognizer_key == key:
        return recognizer
    with _recognizer_lock:
        if _recognizer is None or _recognizer_key != key:
            sherpa_onnx = importlib.import_module("sherpa_onnx")
            _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_dir / "model.int8.onnx"),
                tokens=str(model_dir / "tokens.txt"),
                language=language,
                use_itn=use_itn,
            )
            _recognizer_key = key
        return _recognizer


def _unload_recognizer() -> None:
    global _recognizer, _recognizer_key
    with _recognizer_lock:
        if _recognizer is not None:
            logger.info("Unloading SenseVoice recognizer after idle timeout")
        _recognizer = None
        _recognizer_key = None


def _is_recognizer_loaded() -> bool:
    with _recognizer_lock:
        return _recognizer is not None


def _touch_activity() -> None:
    from tools.transcription_tools import _touch_transcription_time

    _touch_transcription_time("sensevoice")


def _schedule_idle_unload(seconds: int) -> None:
    if seconds <= 0:
        return
    from tools.transcription_tools import _start_idle_unload_watcher

    _start_idle_unload_watcher(
        seconds,
        provider_key="sensevoice",
        unload_callback=_unload_recognizer,
        is_loaded_callback=_is_recognizer_loaded,
    )


def transcribe_sensevoice(
    file_path: str, *, language: str | None = None
) -> dict[str, Any]:
    """Transcribe an audio file with the cached, local SenseVoice model."""
    try:
        # Re-check the lazy group on every call: a Hermes update can rebuild
        # the venv while the model cache remains valid.
        _ensure_runtime_dependencies()
    except PreparationError as exc:
        return {
            "success": False,
            "transcript": "",
            "provider": "sensevoice",
            "error_type": exc.code,
            "error": exc.message,
        }

    model_dir = model_directory()
    if not _is_model_ready(model_dir):
        status = prepare_sensevoice(cache_root=model_dir.parent)
        result: dict[str, Any] = {
            "success": False,
            "transcript": "",
            "provider": "sensevoice",
            "error": "SenseVoice is still being prepared",
            "preparation": status,
        }
        if status.get("state") == "error":
            result["error_type"] = status.get("code")
            result["error"] = status.get("message")
        return result

    resolved_language, use_itn, idle_seconds = _runtime_config(language)
    try:
        _touch_activity()
        with tempfile.TemporaryDirectory(prefix="hermes-sensevoice-") as work_dir:
            normalized = Path(work_dir) / "audio.wav"
            try:
                _normalize_to_wav(Path(file_path), normalized)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                logger.error(
                    "SenseVoice audio normalization failed: %s", exc, exc_info=True
                )
                return {
                    "success": False,
                    "transcript": "",
                    "provider": "sensevoice",
                    "error_type": "audio_conversion_failed",
                    "error": f"SenseVoice audio conversion failed: {exc}",
                }
            sample_rate, samples = _read_pcm_wav(normalized)
            if not _has_speech_activity(sample_rate, samples):
                _touch_activity()
                _schedule_idle_unload(idle_seconds)
                return {
                    "success": False,
                    "transcript": "",
                    "provider": "sensevoice",
                    "error_type": "no_speech",
                    "error": "No speech was detected",
                    "no_speech": True,
                }
            recognizer = _load_recognizer(
                model_dir, language=resolved_language, use_itn=use_itn
            )
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            raw_result = stream.result
            transcript = str(getattr(raw_result, "text", raw_result) or "").strip()
        _touch_activity()
        _schedule_idle_unload(idle_seconds)
    except Exception as exc:
        logger.error("SenseVoice runtime failed: %s", exc, exc_info=True)
        return {
            "success": False,
            "transcript": "",
            "provider": "sensevoice",
            "error_type": "runtime_load_failed",
            "error": f"SenseVoice runtime failed: {exc}",
        }

    if not transcript:
        return {
            "success": False,
            "transcript": "",
            "provider": "sensevoice",
            "error_type": "no_speech",
            "error": "No speech was detected",
            "no_speech": True,
        }
    return {
        "success": True,
        "transcript": transcript,
        "provider": "sensevoice",
    }


def _reset_recognizer_for_tests() -> None:
    _unload_recognizer()
