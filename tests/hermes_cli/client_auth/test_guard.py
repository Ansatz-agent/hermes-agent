import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.client_auth.guard import classify_raw_argv, enforce_direct_entrypoint
from hermes_cli.client_auth.runtime import AuthRequired


REPO_ROOT = Path(__file__).resolve().parents[3]

ALLOWED = [
    ["login"],
    ["logout"],
    ["auth", "status"],
    ["--help"],
    ["-h"],
    ["--version"],
    ["-V"],
]


@pytest.mark.parametrize("argv", ALLOWED)
def test_exact_unauthenticated_shapes(argv):
    assert classify_raw_argv(argv).auth_free is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["login", "--provider", "nous"],
        ["logout", "--"],
        ["auth"],
        ["auth", "status", "extra"],
        ["--help", "extra"],
        ["-hV"],
        ["--version", "--help"],
        ["doctor"],
        ["gateway", "status"],
    ],
)
def test_every_shape_variant_is_protected(argv):
    assert classify_raw_argv(argv).auth_free is False


def test_guard_and_package_imports_are_stdlib_only():
    probe = (
        "import json,sys\n"
        "import hermes_cli.client_auth\n"
        "import hermes_cli.client_auth.guard\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert not loaded.intersection({"httpx", "keyring", "argparse", "yaml"})


def test_protected_main_import_exits_before_recovery_config_or_parser_modules():
    probe = (
        "import json,sys\n"
        "sys.argv=['hermes','doctor']\n"
        "try:\n"
        " import hermes_cli.main\n"
        "except SystemExit as error:\n"
        " print(json.dumps({'code': error.code, 'modules': sorted(sys.modules)}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["code"] == 20
    loaded = set(payload["modules"])
    assert not loaded.intersection(
        {
            "argparse",
            "hermes_cli._early_recovery",
            "hermes_cli.config",
            "hermes_cli.profiles",
            "hermes_cli._parser",
            "yaml",
        }
    )
    assert result.stderr == "AUTH_REQUIRED runtime_unavailable; run `ansatz login`\n"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_auth_free_help_is_the_packaged_real_parser_snapshot(flag):
    expected = (
        REPO_ROOT / "hermes_cli" / "client_auth" / "static_help.txt"
    ).read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", flag],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: ansatz ")
    assert result.stdout == expected
    assert result.stderr == ""


def test_runtime_import_failure_is_not_misreported_as_auth_required():
    probe = (
        "import sys\n"
        "class BlockRuntime:\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname == 'hermes_cli.client_auth.runtime':\n"
        "   raise RuntimeError('runtime import broke')\n"
        "sys.meta_path.insert(0, BlockRuntime())\n"
        "sys.argv=['hermes','doctor']\n"
        "try:\n"
        " import hermes_cli.main\n"
        "except RuntimeError as error:\n"
        " print(str(error))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "runtime import broke\n"
    assert "AUTH_REQUIRED" not in result.stderr


def test_direct_entrypoint_is_noninteractive_and_exits_with_auth_code(
    monkeypatch,
    capsys,
):
    calls = []

    def reject(boundary, *, interactive):
        calls.append((boundary, interactive))
        raise AuthRequired("signed_out")

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.authorize_entrypoint",
        reject,
    )

    with pytest.raises(SystemExit) as error:
        enforce_direct_entrypoint("direct.batch")

    assert error.value.code == 20
    assert calls == [("direct.batch", False)]
    assert capsys.readouterr().err == (
        "AUTH_REQUIRED signed_out; run `ansatz login`\n"
    )


def test_direct_entrypoint_does_not_mask_runtime_failure(monkeypatch):
    def broken(boundary, *, interactive):
        del boundary, interactive
        raise RuntimeError("runtime broke")

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.authorize_entrypoint",
        broken,
    )

    with pytest.raises(RuntimeError, match="runtime broke"):
        enforce_direct_entrypoint("direct.batch")
