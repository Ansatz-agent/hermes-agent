from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
DESKTOP_PACKAGE = REPO / "apps" / "desktop" / "package.json"
SENSEVOICE = REPO / "tools" / "sensevoice_stt.py"

VOICE_REFERENCE_BLOBS = {
    "apps/desktop/src/lib/voice-barge-in.ts": "8ec3008af50111ebfe2bcf6f7b9c9038c19d1c4a",
    "apps/desktop/src/lib/voice-timing.ts": "90ab87e49cd51158ad21e0f683ec8a4c7c6902e8",
    "tools/sensevoice_stt.py": "a7c4345a6dca461f43cfe1d9fbca535f7df0b2de",
}

FORBIDDEN_PACKAGE_PARTS = {
    ".github",
    "docs",
    "e2e",
    "spec",
    "test",
    "tests",
}
FORBIDDEN_MODEL_SUFFIXES = {
    ".bin",
    ".gguf",
    ".onnx",
    ".pt",
    ".safetensors",
    ".tar.bz2",
}


def git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def collect_resource_sources(build: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for scope in (build, build.get("mac", {}), build.get("win", {})):
        if not isinstance(scope, dict):
            continue
        for entry in scope.get("extraResources", []):
            if isinstance(entry, str):
                sources.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("from"), str):
                sources.append(entry["from"])
    return sources


def sensevoice_manifest() -> dict[str, Any]:
    tree = ast.parse(SENSEVOICE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "MODEL_MANIFEST"
            for target in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Call)
        return {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in node.value.keywords
            if keyword.arg is not None
        }
    raise AssertionError("SenseVoice MODEL_MANIFEST is missing")


def test_voice_only_files_match_the_accepted_dmg_reference() -> None:
    for relative_path, expected_blob in VOICE_REFERENCE_BLOBS.items():
        path = REPO / relative_path
        assert path.is_file(), f"missing accepted Voice path: {relative_path}"
        assert git_blob_sha1(path.read_bytes()) == expected_blob, (
            f"Voice path diverged from accepted DMG reference: {relative_path}"
        )


def test_shared_python_and_desktop_wiring_contains_complete_sensevoice_flow() -> None:
    required_markers = {
        "agent/transcription_registry.py": ['"sensevoice"'],
        "tools/transcription_tools.py": [
            '"sensevoice"',
            "def _transcribe_sensevoice(",
            'if provider == "sensevoice":',
        ],
        "hermes_cli/config_defaults.py": ['"sensevoice": {'],
        "hermes_cli/tools_config.py": ['"stt_provider": "sensevoice"'],
        "hermes_cli/web_server.py": [
            '@app.post("/api/audio/stt/prepare")',
            "prepare_sensevoice",
        ],
        "apps/desktop/src/app/chat/composer/hooks/use-composer-voice.ts": [
            "useSenseVoiceReadiness",
            "prepareSenseVoice(true)",
            "senseVoiceReadiness.ready",
        ],
    }
    for relative_path, markers in required_markers.items():
        source = (REPO / relative_path).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in source, f"{relative_path} lacks {marker!r}"


def test_desktop_package_keeps_both_platform_build_commands_and_safe_inputs() -> None:
    package = json.loads(DESKTOP_PACKAGE.read_text(encoding="utf-8"))
    scripts = package["scripts"]
    assert scripts["dist:mac:dmg"] == (
        "npm run prepare:package:mac && npm run build && npm run builder -- --mac dmg"
    )
    assert scripts["dist:win:nsis"] == (
        "npm run prepare:package:win && npm run build && npm run builder -- --win nsis"
    )

    build = package["build"]
    assert build["files"] == ["dist/**", "assets/**", "public/**", "package.json"]
    inputs = [*build["files"], *collect_resource_sources(build)]
    for value in inputs:
        lowered = value.lower()
        parts = set(Path(lowered).parts)
        assert not parts.intersection(FORBIDDEN_PACKAGE_PARTS), value
        assert not any(lowered.endswith(suffix) for suffix in FORBIDDEN_MODEL_SUFFIXES), value


def test_packaged_backend_bootstrap_contract_is_present_without_model_weights() -> None:
    required = (
        "apps/desktop/electron/bootstrap-payload.ts",
        "apps/desktop/electron/bootstrap-runner.ts",
        "apps/desktop/electron/bundled-runtime-state.ts",
        "apps/desktop/scripts/build-backend-payload.mjs",
    )
    assert all((REPO / path).is_file() for path in required)

    package = json.loads(DESKTOP_PACKAGE.read_text(encoding="utf-8"))
    mac_sources = collect_resource_sources({"mac": package["build"]["mac"]})
    assert "build/bootstrap/install.sh" in mac_sources
    assert "build/bootstrap/hermes-backend.tar.gz" in mac_sources
    assert "build/bootstrap/payload-manifest.json" in mac_sources


def test_sensevoice_download_is_modelscope_first_and_hash_pinned() -> None:
    manifest = sensevoice_manifest()
    urls = manifest["urls"]
    assert len(urls) == 3
    assert urls[0].startswith("https://www.modelscope.cn/models/fuyuantech/")
    assert urls[1].startswith("https://modelscope.cn/models/fuyuantech/")
    assert urls[2].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
    assert manifest["size"] == 163_002_883
    assert manifest["sha256"] == (
        "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
    )
