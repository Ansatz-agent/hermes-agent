from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_toml(relative: str) -> dict[str, object]:
    return tomllib.loads((REPO / relative).read_text(encoding="utf-8"))


def test_common_and_minimal_auth_dependencies_are_exact() -> None:
    root = load_toml("pyproject.toml")
    auth = load_toml("desktop_auth_runtime/pyproject.toml")

    root_dependencies = root["project"]["dependencies"]
    auth_dependencies = auth["project"]["dependencies"]
    assert "keyring==25.7.0" in root_dependencies
    assert "keyring==25.7.0" in auth_dependencies
    assert "httpx[socks]==0.28.1" in auth_dependencies

    sensevoice = root["project"]["optional-dependencies"]["sensevoice"]
    assert sensevoice == ["sherpa-onnx==1.13.4", "numpy==2.4.3"]


def test_minimal_auth_lock_contains_hashed_macos_and_windows_artifacts() -> None:
    lock = load_toml("desktop_auth_runtime/uv.lock")
    packages = {
        package["name"]: package
        for package in lock["package"]
        if "name" in package
    }
    assert {"httpx", "keyring", "cryptography", "cffi"} <= packages.keys()

    registry_packages = [
        package
        for package in packages.values()
        if isinstance(package.get("source"), dict)
        and "registry" in package["source"]
    ]
    assert registry_packages
    for package in registry_packages:
        artifacts = list(package.get("wheels", []))
        if package.get("sdist"):
            artifacts.append(package["sdist"])
        assert artifacts, f"locked registry package has no artifacts: {package['name']}"
        assert all(SHA256.fullmatch(artifact["hash"]) for artifact in artifacts)

    keyring_dependencies = packages["keyring"]["dependencies"]
    assert {
        dependency["name"]
        for dependency in keyring_dependencies
        if dependency.get("marker") == "sys_platform == 'win32'"
    } == {"pywin32-ctypes"}
    assert any(
        wheel["url"].endswith("-py3-none-any.whl")
        for wheel in packages["pywin32-ctypes"]["wheels"]
    )
    assert any(
        wheel["url"].endswith("-py3-none-any.whl")
        for wheel in packages["keyring"]["wheels"]
    )


def test_npm_lock_matches_root_and_desktop_manifests() -> None:
    lock = json.loads((REPO / "package-lock.json").read_text(encoding="utf-8"))
    root = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
    desktop = json.loads((REPO / "apps/desktop/package.json").read_text(encoding="utf-8"))

    locked_root = lock["packages"][""]
    locked_desktop = lock["packages"]["apps/desktop"]
    assert locked_root["workspaces"] == root["workspaces"]
    assert locked_root["devDependencies"] == root["devDependencies"]
    assert locked_desktop["dependencies"] == desktop["dependencies"]
    assert locked_desktop["devDependencies"] == desktop["devDependencies"]
