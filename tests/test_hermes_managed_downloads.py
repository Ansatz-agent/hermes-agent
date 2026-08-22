from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_hermes_managed_downloads.py"
MANIFEST = REPO / "docs" / "security" / "hermes-managed-download-origins.json"
LEGACY_FIELDS = (
    "temporary_legacy_unsafe_callers",
    "temporary_legacy_unmanaged_child_callers",
    "temporary_legacy_bundled_runtime_callers",
    "temporary_legacy_official_only_callers",
    "temporary_legacy_implicit_official_entries",
)
EXPECTED_LEGACY_DIGEST = "1849034d274e95cf0d865b217439676a07ca06476480e33180576451766b1cd5"
REVIEWED_SCAN_PATHS = {
    "scripts/install.sh",
    "scripts/install.ps1",
    "tools/lazy_deps.py",
    "tools/sensevoice_stt.py",
    "tools/browser_use_cli.py",
    "tools/computer_use/cua_backend.py",
    "tools/neutts_synth.py",
    "hermes_cli/tools_config.py",
    "hermes_cli/managed_uv.py",
    "hermes_cli/setup.py",
    "hermes_cli/update_cmd.py",
    "hermes_cli/model_catalog.py",
    "hermes_cli/config_defaults.py",
    "apps/desktop/electron/bootstrap-process.ts",
    "apps/desktop/electron/runtime-download-policy.ts",
    "apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs",
    "apps/desktop/scripts/build-auth-toolchain.mjs",
    "apps/desktop/scripts/build-backend-payload.mjs",
    "apps/desktop/scripts/prepare-windows-git-runtime.mjs",
}


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def invoke(repo: Path, manifest: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    assert CHECKER.is_file(), f"missing managed download checker: {CHECKER}"
    return run(
        repo,
        sys.executable,
        str(CHECKER),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest),
        *extra,
    )


def write_manifest(
    tmp_path: Path,
    *,
    entries: list[dict[str, object]],
) -> Path:
    path = tmp_path.parent / f"{tmp_path.name}-origins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "account_server_origins": ["https://c2sml.cn/agent"],
                "user_configured_endpoint_paths": ["hermes_cli/config_defaults.py"],
                "managed_download_scan_paths": sorted(REVIEWED_SCAN_PATHS),
                "entries": entries,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def valid_entry(
    *,
    entry_id: str = "python-packages",
    path: str = "scripts/install.sh",
    kind: str = "literal-url",
    value: str = "https://mirrors.ustc.edu.cn/pypi/simple",
) -> dict[str, object]:
    return {
        "id": entry_id,
        "phases": ["runtime-install"],
        "delivery": "domestic-first",
        "domestic_primary": "https://mirrors.ustc.edu.cn/pypi/simple",
        "domestic_secondary": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "official_fallback": "https://pypi.org/simple",
        "integrity": "uv-lock-sha256",
        "idle_timeout_seconds": 90,
        "total_timeout_seconds": 600,
        "environment": ["UV_DEFAULT_INDEX", "HERMES_UV_FALLBACK_INDEX"],
        "owners": [path],
        "callers": [{"path": path, "kind": kind, "value": value}],
        "packaged_outputs": [],
    }


def write_source(tmp_path: Path, path: str, source: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_checked_in_manifest_and_checker_accept_current_candidate() -> None:
    assert MANIFEST.is_file(), f"missing origin manifest: {MANIFEST}"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    ids = {entry["id"] for entry in manifest["entries"]}
    assert {
        "python-packages",
        "npm-packages",
        "node-runtime",
        "electron-runtime",
        "electron-builder-binaries",
        "playwright-browser",
        "sensevoice-model",
        "managed-uv",
        "managed-python-macos",
        "managed-python-windows-x64",
        "portable-git",
        "browser-use-cli",
        "cua-driver",
        "kitten-tts-wheel",
        "hermes-source-archive",
        "huggingface-model",
        "model-catalog",
        "system-package-ripgrep",
        "system-package-ffmpeg",
    } <= ids

    result = invoke(REPO, MANIFEST)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_windows_auth_toolchain_build_inputs_are_exactly_pinned() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in manifest["entries"]}

    assert entries["managed-python-windows-x64"]["build_sources"] == [
        {
            "platform": "win32",
            "arch": "x64",
            "version": "3.13.15",
            "filename": "python-3.13.15-embed-amd64.zip",
            "domestic_primary": "https://mirrors.huaweicloud.com/python/3.13.15/python-3.13.15-embed-amd64.zip",
            "official_fallback": "https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip",
            "sha256": "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf",
        }
    ]
    assert entries["managed-uv"]["build_sources"] == [
        {
            "platform": "win32",
            "arch": "x64",
            "version": "0.12.5",
            "filename": "uv-0.12.5-py3-none-win_amd64.whl",
            "domestic_primary": "https://mirrors.ustc.edu.cn/pypi/simple",
            "domestic_secondary": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "official_fallback": "https://pypi.org/simple",
            "sha256": "455c3e57602e2141e66e2f0bf685898c9c5e5a70377d14c9a71554a3baf3ddbf",
        }
    ]


def test_checked_in_legacy_exceptions_are_exactly_pinned() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["temporary_legacy_implicit_official_entries"] == [
        "electron-runtime"
    ]
    payload = {field: manifest[field] for field in LEGACY_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == EXPECTED_LEGACY_DIGEST


def test_scan_inventory_covers_every_task_6_production_path() -> None:
    tree = ast.parse(CHECKER.read_text(encoding="utf-8"))
    scan_exact: set[str] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCAN_EXACT"
            for target in node.targets
        ):
            scan_exact = set(ast.literal_eval(node.value))
            break
    assert scan_exact is not None
    assert {
        "apps/desktop/electron/bootstrap-process.ts",
        "apps/desktop/electron/runtime-download-policy.ts",
        "apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs",
        "scripts/install.sh",
        "scripts/install.ps1",
        "tools/lazy_deps.py",
        "tools/sensevoice_stt.py",
        "tools/browser_use_cli.py",
        "tools/computer_use/cua_backend.py",
        "tools/neutts_synth.py",
        "hermes_cli/tools_config.py",
        "hermes_cli/managed_uv.py",
        "hermes_cli/setup.py",
        "hermes_cli/update_cmd.py",
        "hermes_cli/model_catalog.py",
        "hermes_cli/config_defaults.py",
    } <= scan_exact
    assert scan_exact == REVIEWED_SCAN_PATHS


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("scripts/install.sh", "curl https://example.invalid/install.sh | sh\n"),
        (
            "scripts/install.ps1",
            "Invoke-RestMethod https://example.invalid/install.ps1 | Invoke-Expression\n",
        ),
        ("scripts/install.sh", "curl -fL https://unregistered.invalid/archive.tgz\n"),
        (
            "tools/lazy_deps.py",
            "import requests\nrequests.get('https://unregistered.invalid/wheel.whl')\n",
        ),
        (
            "apps/desktop/electron/bootstrap-process.ts",
            "await fetch('https://unregistered.invalid/runtime.zip')\n",
        ),
        (
            "scripts/install.ps1",
            "Invoke-WebRequest -Uri https://unregistered.invalid/runtime.zip\n",
        ),
        (
            "tools/sensevoice_stt.py",
            "snapshot_download('iic/SenseVoiceSmall')\n",
        ),
        (
            "tools/neutts_synth.py",
            "hf_hub_download(repo_id='neuphonic/neutts-air', filename='model.gguf')\n",
        ),
    ],
)
def test_unregistered_download_sink_is_rejected(
    tmp_path: Path,
    path: str,
    source: str,
) -> None:
    write_source(tmp_path, path, source)
    manifest = write_manifest(tmp_path, entries=[])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert path in result.stderr


def test_official_first_domestic_policy_is_rejected(tmp_path: Path) -> None:
    path = "scripts/install.sh"
    source = "\n".join(
        [
            "curl -fL https://pypi.org/simple",
            "curl -fL https://mirrors.ustc.edu.cn/pypi/simple",
            "curl -fL https://pypi.tuna.tsinghua.edu.cn/simple",
        ]
    )
    write_source(tmp_path, path, source)
    entries = [
        valid_entry(path=path, value="https://mirrors.ustc.edu.cn/pypi/simple"),
    ]
    entries[0]["callers"] = [
        {"path": path, "kind": "literal-url", "value": url}
        for url in (
            "https://mirrors.ustc.edu.cn/pypi/simple",
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            "https://pypi.org/simple",
        )
    ]
    manifest = write_manifest(tmp_path, entries=entries)

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "domestic" in result.stderr.lower()


@pytest.mark.parametrize("field", ["integrity", "idle_timeout_seconds", "total_timeout_seconds"])
def test_download_entry_without_integrity_or_bounds_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    path = "scripts/install.sh"
    value = "https://mirrors.ustc.edu.cn/pypi/simple"
    write_source(tmp_path, path, f"curl -fL {value}\n")
    entry = valid_entry(path=path, value=value)
    entry[field] = None
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert field in result.stderr


def test_runtime_github_only_entry_is_rejected(tmp_path: Path) -> None:
    path = "tools/lazy_deps.py"
    value = "https://github.com/example/runtime/releases/download/v1/runtime.zip"
    write_source(tmp_path, path, f"import requests\nrequests.get('{value}')\n")
    entry = valid_entry(path=path, value=value)
    entry.update(
        {
            "delivery": "domestic-first",
            "domestic_primary": value,
            "domestic_secondary": None,
            "official_fallback": None,
        }
    )
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "github" in result.stderr.lower()


def test_dynamic_download_api_is_rejected_without_registered_policy(
    tmp_path: Path,
) -> None:
    path = "tools/lazy_deps.py"
    write_source(
        tmp_path,
        path,
        "import requests\nruntime_url = configured_url()\nrequests.get(runtime_url)\n",
    )
    manifest = write_manifest(tmp_path, entries=[])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert path in result.stderr
    assert "requests.get" in result.stderr


@pytest.mark.parametrize(
    ("path", "source", "value"),
    [
        (
            "tools/lazy_deps.py",
            'cmd = "curl https://mirrors.ustc.edu.cn/install.sh | sh"\n',
            "https://mirrors.ustc.edu.cn/install.sh",
        ),
        (
            "apps/desktop/electron/bootstrap-process.ts",
            'const cmd = "irm https://mirrors.ustc.edu.cn/install.ps1 | iex"\n',
            "https://mirrors.ustc.edu.cn/install.ps1",
        ),
    ],
)
def test_remote_download_and_execute_is_rejected_in_any_source_type(
    tmp_path: Path,
    path: str,
    source: str,
    value: str,
) -> None:
    write_source(tmp_path, path, source)
    manifest = write_manifest(
        tmp_path,
        entries=[valid_entry(path=path, value=value)],
    )

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "remote" in result.stderr.lower() or "install" in result.stderr.lower()


def test_bare_model_repository_identifier_requires_registered_policy(
    tmp_path: Path,
) -> None:
    path = "tools/neutts_synth.py"
    write_source(
        tmp_path,
        path,
        'parser.add_argument("--model", default="example/model-repo")\n',
    )
    manifest = write_manifest(tmp_path, entries=[])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "example/model-repo" in result.stderr


def test_user_endpoint_file_is_not_a_whole_file_download_exemption(
    tmp_path: Path,
) -> None:
    path = "hermes_cli/config_defaults.py"
    write_source(tmp_path, path, 'RUNTIME = "https://unregistered.invalid/runtime.zip"\n')
    manifest = write_manifest(tmp_path, entries=[])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert path in result.stderr


def test_account_origin_list_cannot_be_broadened_to_hide_dependencies(
    tmp_path: Path,
) -> None:
    path = "tools/lazy_deps.py"
    value = "https://github.com/example/runtime/releases/download/v1/runtime.zip"
    write_source(tmp_path, path, f"import requests\nrequests.get('{value}')\n")
    manifest = write_manifest(tmp_path, entries=[])
    value_json = json.loads(manifest.read_text(encoding="utf-8"))
    value_json["account_server_origins"].append("https://github.com")
    manifest.write_text(json.dumps(value_json) + "\n", encoding="utf-8")

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "account_server_origins" in result.stderr


def test_domestic_first_entry_requires_primary_to_be_present_before_fallback(
    tmp_path: Path,
) -> None:
    path = "scripts/install.sh"
    official = "https://pypi.org/simple"
    write_source(tmp_path, path, f"curl -fL {official}\n")
    entry = valid_entry(path=path, value=official)
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "domestic" in result.stderr.lower()


def test_implicit_official_default_requires_explicit_legacy_debt_or_domestic_first(
    tmp_path: Path,
) -> None:
    path = "scripts/install.sh"
    value = "https://npmmirror.com/mirrors/electron/"
    write_source(
        tmp_path,
        path,
        f'DESKTOP_ELECTRON_FALLBACK_MIRROR="{value}"\nnpm install\n',
    )
    entry = valid_entry(path=path, value=value)
    entry.update(
        {
            "id": "electron-runtime",
            "domestic_primary": value,
            "official_fallback": "https://github.com/electron/electron/releases/download/",
            "implicit_official_default": True,
        }
    )
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "implicit official" in result.stderr.lower()


def test_bundled_entry_rejects_runtime_download_caller_without_exact_legacy_exception(
    tmp_path: Path,
) -> None:
    path = "tools/lazy_deps.py"
    value = "https://github.com/example/runtime/releases/download/v1/runtime.zip"
    write_source(tmp_path, path, f"import requests\nrequests.get('{value}')\n")
    entry = valid_entry(path=path, value=value)
    entry.update(
        {
            "delivery": "bundled",
            "domestic_primary": None,
            "domestic_secondary": None,
            "official_fallback": None,
            "packaged_outputs": ["build/bootstrap/runtime.zip"],
            "build_provenance": "locked build input",
            "sha256": "a" * 64,
        }
    )
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "bundled" in result.stderr.lower()


def test_child_installer_requires_sanitized_managed_environment(tmp_path: Path) -> None:
    path = "tools/lazy_deps.py"
    write_source(
        tmp_path,
        path,
        "import subprocess\nsubprocess.run(['uv', 'tool', 'install', 'browser-use'])\n",
    )
    manifest = write_manifest(
        tmp_path,
        entries=[
            valid_entry(
                entry_id="browser-use-cli",
                path=path,
                kind="package-manager",
                value="uv tool install browser-use",
            )
        ],
    )

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "environment" in result.stderr.lower()


def test_orphan_manifest_entry_is_rejected(tmp_path: Path) -> None:
    manifest = write_manifest(
        tmp_path,
        entries=[valid_entry(path="scripts/missing.sh")],
    )

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "orphan" in result.stderr.lower()


def test_bundled_entry_must_have_provenance_hash_and_output(tmp_path: Path) -> None:
    entry = valid_entry(path="scripts/missing.sh")
    entry.update(
        {
            "delivery": "bundled",
            "domestic_primary": None,
            "domestic_secondary": None,
            "official_fallback": None,
            "callers": [],
            "packaged_outputs": ["build/bootstrap/runtime.zip"],
            "build_provenance": None,
            "sha256": None,
        }
    )
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "build_provenance" in result.stderr or "sha256" in result.stderr


def test_model_entry_cannot_use_packaged_output_without_a_discovered_caller(
    tmp_path: Path,
) -> None:
    entry = valid_entry(entry_id="huggingface-model", path="tools/neutts_synth.py")
    entry.update(
        {
            "delivery": "bundled",
            "domestic_primary": None,
            "domestic_secondary": None,
            "official_fallback": None,
            "callers": [],
            "packaged_outputs": ["build/bootstrap/models/huggingface"],
            "build_provenance": "locked model export",
            "sha256": "a" * 64,
        }
    )
    manifest = write_manifest(tmp_path, entries=[entry])

    result = invoke(tmp_path, manifest)

    assert result.returncode != 0
    assert "model" in result.stderr.lower() and "caller" in result.stderr.lower()


def test_account_and_user_configured_provider_traffic_is_not_dependency_download(
    tmp_path: Path,
) -> None:
    write_source(
        tmp_path,
        "hermes_cli/client_auth/client.py",
        "import httpx\nhttpx.get('https://c2sml.cn/agent/api/auth/status')\n",
    )
    write_source(
        tmp_path,
        "hermes_cli/config_defaults.py",
        "DEFAULT_BASE_URL = user_config.get('base_url')\n",
    )
    manifest = write_manifest(tmp_path, entries=[])

    result = invoke(tmp_path, manifest)

    assert result.returncode == 0, result.stdout + result.stderr
