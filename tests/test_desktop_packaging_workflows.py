from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "windows": REPO / ".github/workflows/desktop-windows-package.yml",
    "macos": REPO / ".github/workflows/desktop-macos-package.yml",
}
DIST_COMMANDS = {
    "windows": "npm run dist:win:nsis --workspace apps/desktop",
    "macos": "npm run dist:mac:dmg --workspace apps/desktop",
}
ARTIFACT_SUFFIXES = {
    "windows": (".exe", ".json"),
    "macos": (".dmg", ".json"),
}
ACTION_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load_workflow(platform: str) -> tuple[str, dict[str, object]]:
    path = WORKFLOWS[platform]
    assert path.is_file(), f"missing {platform} packaging workflow"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert isinstance(workflow, dict)
    return text, workflow


def steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict) and len(jobs) == 1
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    raw_steps = job["steps"]
    assert isinstance(raw_steps, list)
    return [step for step in raw_steps if isinstance(step, dict)]


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_workflow_uses_locked_local_build_and_domestic_dependency_mirrors(platform: str) -> None:
    text, workflow = load_workflow(platform)
    assert "node-version: 26.7.0" in text
    assert "npm@11.19.0" in text
    assert "https://registry.npmmirror.com" in text
    assert "https://mirrors.ustc.edu.cn/pypi/simple" in text
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in text
    assert DIST_COMMANDS[platform] in text
    assert "npm run build:desktop:" not in text

    for step in steps(workflow):
        action = step.get("uses")
        if isinstance(action, str):
            assert ACTION_PIN.fullmatch(action), f"action is not commit-pinned: {action}"


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_workflow_never_patches_or_bypasses_product_auth(platform: str) -> None:
    text, _ = load_workflow(platform)
    assert not re.search(r"\b(?:git apply|git checkout|git restore|patch)\b", text)
    assert not re.search(r"\b(?:sed|perl)\s+-i\b", text)
    assert not re.search(
        r"HERMES_(?:AUTH_BYPASS|DISABLE_AUTH|SKIP_AUTH|FAKE_AUTH)|AUTH_(?:BYPASS|DISABLED)",
        text,
        re.IGNORECASE,
    )
    assert "phase1/desktop-windows" not in text


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_secrets_exist_only_in_the_credentialed_login_step(platform: str) -> None:
    _, workflow = load_workflow(platform)
    found = 0
    for step in steps(workflow):
        serialized = json.dumps(step, sort_keys=True)
        if "HERMES_E2E_USERNAME" in serialized or "HERMES_E2E_PASSWORD" in serialized:
            assert "credentialed installed-app login" in str(step.get("name", "")).casefold()
            assert "${{ secrets.HERMES_E2E_USERNAME }}" in serialized
            assert "${{ secrets.HERMES_E2E_PASSWORD }}" in serialized
            assert not re.search(r"\b(?:echo|write-host)\b[^\n]*(?:USERNAME|PASSWORD)", serialized, re.I)
            found += 1
    assert found == 1


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_artifact_audit_rejects_ci_tests_credentials_and_raw_logs(platform: str) -> None:
    text, workflow = load_workflow(platform)
    assert "app.asar" in text
    assert ".github/" in text
    assert "credential-login" in text
    assert "raw logs" in text

    uploads = [step for step in steps(workflow) if "actions/upload-artifact" in str(step.get("uses", ""))]
    assert len(uploads) == 1
    upload = uploads[0]
    paths = str(upload["with"]["path"]).splitlines()
    allowed_suffixes = ARTIFACT_SUFFIXES[platform]
    assert paths
    assert all(path.strip().endswith(allowed_suffixes) for path in paths if path.strip())


def test_electron_builder_source_closure_excludes_workflows_and_test_drivers() -> None:
    package = json.loads((REPO / "apps/desktop/package.json").read_text(encoding="utf-8"))
    assert package["build"]["files"] == ["dist/**", "assets/**", "public/**", "package.json"]
    serialized = json.dumps(package["build"])
    assert ".github" not in serialized
    assert "credential-login" not in serialized
    assert "e2e" not in serialized
