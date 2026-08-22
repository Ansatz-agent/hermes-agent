from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.managed_downloads import managed_download_environment, managed_origin


REPO = Path(__file__).resolve().parents[2]


def test_packaged_policy_is_exact_projection_of_security_manifest() -> None:
    security = json.loads(
        (REPO / "docs/security/hermes-managed-download-origins.json").read_text(encoding="utf-8")
    )
    packaged = json.loads(
        (REPO / "hermes_cli/data/managed-download-origins.json").read_text(encoding="utf-8")
    )
    ids = {entry["id"] for entry in packaged["entries"]}
    projected = [
        {
            key: entry[key]
            for key in ("id", "domestic_primary", "domestic_secondary", "official_fallback")
        }
        for entry in security["entries"]
        if entry["id"] in ids
    ]
    assert packaged == {"schema_version": security["schema_version"], "entries": projected}


@pytest.mark.parametrize(
    "phase",
    ["auth-payload-build", "runtime-install", "repair", "update", "lazy-feature"],
)
def test_managed_environment_strips_hostile_redirects_and_credentials(phase: str) -> None:
    source = {
        "HOME": "/Users/example",
        "PATH": "/usr/bin:/bin",
        "PIP_INDEX_URL": "https://attacker.invalid/pypi",
        "PIP_EXTRA_INDEX_URL": "https://attacker.invalid/extra",
        "UV_INDEX": "https://attacker.invalid/uv",
        "UV_CONFIG_FILE": "/tmp/attacker-uv.toml",
        "npm_config_registry": "https://attacker.invalid/npm",
        "NPM_CONFIG_USERCONFIG": "/tmp/attacker-npmrc",
        "NODEJS_ORG_MIRROR": "https://attacker.invalid/node",
        "ELECTRON_MIRROR": "https://attacker.invalid/electron",
        "PLAYWRIGHT_DOWNLOAD_HOST": "https://attacker.invalid/playwright",
        "HF_TOKEN": "secret-token",
        "HUGGING_FACE_HUB_TOKEN": "secret-token",
        "PYTHONPATH": "/tmp/injected",
        "PYTHONHOME": "/tmp/injected-home",
    }
    env = managed_download_environment(phase, source=source)

    assert env["HOME"] == source["HOME"]
    assert env["UV_DEFAULT_INDEX"] == "https://mirrors.ustc.edu.cn/pypi/simple"
    assert env["HERMES_UV_FALLBACK_INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmmirror.com"
    assert env["NODEJS_ORG_MIRROR"] == "https://registry.npmmirror.com/-/binary/node/"
    assert env["ELECTRON_MIRROR"] == "https://npmmirror.com/mirrors/electron/"
    assert env["PLAYWRIGHT_DOWNLOAD_HOST"] == "https://registry.npmmirror.com/-/binary/playwright"
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert "UV_CONFIG_FILE" not in env
    assert "HF_TOKEN" not in env
    assert "HUGGING_FACE_HUB_TOKEN" not in env
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert "attacker.invalid" not in repr(env)
    assert "secret-token" not in repr(env)


def test_unknown_phase_and_origin_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown managed download phase"):
        managed_download_environment("unregistered", source={})
    with pytest.raises(RuntimeError, match="origin unregistered"):
        managed_origin("unregistered")
