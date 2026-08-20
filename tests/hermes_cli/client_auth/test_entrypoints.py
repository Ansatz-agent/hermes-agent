from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_auth_entrypoints as entrypoint_scanner
from scripts.check_auth_entrypoints import scan_entrypoints


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_scanner_finds_packaged_direct_service_and_ui_entries(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "fixture"
version = "1"
[project.scripts]
hermes = "pkg.cli:main"
hermes-agent = "run_agent:main"
""".strip(),
    )
    _write(
        tmp_path / "run_agent.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(tmp_path / "pkg" / "__main__.py", "main()\n")
    _write(
        tmp_path / "docker" / "entrypoint.sh",
        "#!/bin/sh\nexec python -m pkg.cli \"$@\"\n",
    )
    _write(
        tmp_path / "units" / "hermes.service",
        "[Service]\nExecStart=/usr/bin/hermes gateway run\n",
    )
    _write(
        tmp_path / "docker" / "s6-rc.d" / "main-hermes" / "run",
        "#!/bin/sh\nexec hermes-agent\n",
    )
    _write(
        tmp_path / "Dockerfile",
        'ENTRYPOINT ["/app/docker/entrypoint.sh"]\n',
    )
    _write(
        tmp_path / "apps" / "desktop" / "package.json",
        '{"name":"desktop","main":"dist-electron/main.js"}',
    )
    _write(
        tmp_path / "apps" / "desktop" / "electron" / "main.ts",
        'spawn("python", ["-m", "tui_gateway.entry"]);\n',
    )
    _write(
        tmp_path / "scripts" / "install.sh",
        "#!/bin/sh\nsystemctl --user enable hermes.service\n",
    )
    _write(
        tmp_path / "scripts" / "discord-voice-doctor.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "scripts" / "keystroke_diagnostic.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "cron" / "scripts" / "classify_items.py",
        "if __name__ == '__main__':\n    main()\n",
    )

    discovered = scan_entrypoints(tmp_path)

    assert {
        "pyproject:hermes",
        "pyproject:hermes-agent",
        "python:run_agent.py",
        "python:pkg/__main__.py",
        "shell:docker/entrypoint.sh",
        "service:units/hermes.service",
        "s6:docker/s6-rc.d/main-hermes/run",
        "docker:Dockerfile:entrypoint",
        "electron:primary-backend",
        "spawn:apps/desktop/electron/main.ts:tui_gateway.entry",
        "installer:scripts/install.sh",
        "python:scripts/discord-voice-doctor.py",
        "python:scripts/keystroke_diagnostic.py",
        "python:cron/scripts/classify_items.py",
    }.issubset(discovered)


def test_distribution_scanner_finds_every_shipped_direct_python_script(tmp_path):
    _write(
        tmp_path / "scripts" / "runtime_helper.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "scripts" / "ci_helper.py",
        "if __name__ == '__main__':\n    main()\n",
    )

    discovered = entrypoint_scanner.scan_distribution_entrypoints(tmp_path)

    assert {
        "python:scripts/runtime_helper.py",
        "python:scripts/ci_helper.py",
    }.issubset(discovered)


def test_scanner_ignores_tests_skills_and_registration_lifecycle(tmp_path):
    _write(
        tmp_path / "tests" / "test_cli.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "skills" / "demo" / "run.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "registration_lifecycle.py",
        "if __name__ == '__main__':\n    replace_plugin()\n",
    )
    _write(
        tmp_path / "plugins" / "demo" / "tests" / "test_plugin.py",
        "if __name__ == '__main__':\n    main()\n",
    )
    _write(
        tmp_path / "apps" / "desktop" / "scripts" / "perf_probe.py",
        "if __name__ == '__main__':\n    main()\n",
    )

    discovered = scan_entrypoints(tmp_path)

    assert discovered == set()


def test_console_wrappers_guard_before_capability_target_import(monkeypatch):
    wrappers = importlib.import_module(
        "hermes_cli.client_auth.entrypoint_wrappers"
    )
    guarded = []

    def reject(boundary):
        guarded.append(boundary)
        raise SystemExit(20)

    monkeypatch.setattr(
        "hermes_cli.client_auth.guard.enforce_direct_entrypoint",
        reject,
    )
    sys.modules.pop("run_agent", None)
    sys.modules.pop("acp_adapter.entry", None)

    with pytest.raises(SystemExit) as agent_error:
        wrappers.hermes_agent()
    with pytest.raises(SystemExit) as acp_error:
        wrappers.hermes_acp()

    assert agent_error.value.code == acp_error.value.code == 20
    assert guarded == ["console.hermes_agent", "console.hermes_acp"]
    assert "run_agent" not in sys.modules
    assert "acp_adapter.entry" not in sys.modules


@pytest.mark.parametrize(
    ("relative", "argv", "capability_modules"),
    [
        (
            "cron/scripts/classify_items.py",
            ["--criteria", "probe", "--input-file", "{empty_items}"],
            ["agent.auxiliary_client"],
        ),
        (
            "scripts/discord-voice-doctor.py",
            [],
            ["requests", "discord", "hermes_cli.env_loader"],
        ),
        (
            "scripts/keystroke_diagnostic.py",
            [],
            ["prompt_toolkit"],
        ),
    ],
)
def test_packaged_direct_scripts_reject_before_capability_import(
    tmp_path,
    relative,
    argv,
    capability_modules,
):
    root = Path(__file__).resolve().parents[3]
    empty_items = tmp_path / "items.json"
    empty_items.write_text("[]\n", encoding="utf-8")
    resolved_argv = [value.format(empty_items=empty_items) for value in argv]
    probe = (
        "import json,runpy,sys\n"
        "path=sys.argv[1]\n"
        "entry_argv=json.loads(sys.argv[2])\n"
        "capabilities=json.loads(sys.argv[3])\n"
        "sys.argv=[path,*entry_argv]\n"
        "code=0\n"
        "try:\n"
        " runpy.run_path(path, run_name='__main__')\n"
        "except SystemExit as error:\n"
        " code=error.code\n"
        "loaded=sorted(name for name in sys.modules "
        "if any(name == item or name.startswith(item + '.') for item in capabilities))\n"
        "print('CAPABILITY_MODULES=' + ','.join(loaded))\n"
        "raise SystemExit(code)\n"
    )

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(root / relative),
                json.dumps(resolved_argv),
                json.dumps(capability_modules),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{relative} did not reject before starting its capability")

    assert result.returncode == 20
    assert result.stdout == "CAPABILITY_MODULES=\n"
    assert result.stderr == (
        "AUTH_REQUIRED runtime_unavailable; run `hermes login`\n"
    )


@pytest.mark.parametrize(
    "module",
    [
        "gateway.run",
        "cron.scripts.classify_items",
    ],
)
def test_module_entrypoints_reject_before_writing_hermes_home(tmp_path, module):
    root = Path(__file__).resolve().parents[3]
    hermes_home = tmp_path / "home"
    runtime_dir = tmp_path / "runtime"
    hermes_home.mkdir()
    runtime_dir.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_HOME": str(hermes_home),
            "PYTHONPATH": str(root),
            "XDG_RUNTIME_DIR": str(runtime_dir),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 20
    assert "AUTH_REQUIRED" in result.stderr
    assert list(hermes_home.rglob("*")) == []


def test_every_guarded_python_entry_exits_locked_before_capability_imports():
    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "hermes_cli"
        / "client_auth"
        / "entrypoints.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[3]
    guarded = [
        item["id"].split(":", 1)[1]
        for item in manifest["entrypoints"]
        if item["id"].startswith("python:")
        and item["startup"] == "guarded"
    ]
    probe = (
        "import runpy,sys\n"
        "try:\n"
        " runpy.run_path(sys.argv[1], run_name='__main__')\n"
        "except SystemExit as error:\n"
        " raise SystemExit(error.code)\n"
    )

    failures = []
    for relative in guarded:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(root / relative)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 20 or "AUTH_REQUIRED" not in result.stderr:
            failures.append(
                (relative, result.returncode, result.stdout, result.stderr)
            )

    assert failures == []


def test_legacy_gateway_launcher_exits_locked_before_dotenv_import():
    root = Path(__file__).resolve().parents[3]
    probe = (
        "import runpy,sys\n"
        "try:\n"
        " runpy.run_path(sys.argv[1], run_name='__main__')\n"
        "except SystemExit as error:\n"
        " print('dotenv_loaded=' + str('dotenv' in sys.modules))\n"
        " raise SystemExit(error.code)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe, str(root / "scripts/hermes-gateway")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 20
    assert result.stdout == "dotenv_loaded=False\n"
    assert result.stderr == (
        "AUTH_REQUIRED runtime_unavailable; run `hermes login`\n"
    )


def test_manifest_auth_shell_and_locked_waiting_entries_are_covered():
    """New exceptional startup modes must be added to explicit evidence."""
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (root / "hermes_cli" / "client_auth" / "entrypoints.json").read_text(
            encoding="utf-8"
        )
    )
    by_mode = {
        mode: {
            item["id"]
            for item in manifest["entrypoints"]
            if item["startup"] == mode
        }
        for mode in ("auth-shell", "locked-waiting")
    }

    assert by_mode["auth-shell"] == {
        "electron:primary-backend",
        "python:hermes_cli/client_auth/bridge.py",
        "python:hermes_cli/client_auth/runtime.py",
        "python:hermes_cli/container_boot.py",
        "python:tui_gateway/entry.py",
        "tui:tui-gateway",
    }
    assert by_mode["locked-waiting"] == {
        "docker:Dockerfile:cmd",
        "docker:Dockerfile:entrypoint",
        "s6:docker/s6-rc.d/dashboard/run",
        "s6:docker/s6-rc.d/hermes-auth-runtime/run",
        "s6:docker/s6-rc.d/main-hermes/run",
        "service:plugins/kanban/systemd/hermes-kanban-dispatcher.service",
    }
    kanban_unit = (
        root / "plugins" / "kanban" / "systemd" / "hermes-kanban-dispatcher.service"
    ).read_text(encoding="utf-8")
    assert "hermes_cli.client_auth.runtime service kanban" in kanban_unit
