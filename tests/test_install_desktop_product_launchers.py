from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
ANSATZ_LAUNCHERS = (
    "ansatz-voice-trace",
    "ansatz-voice-trace-agent",
    "ansatz-voice-trace-acp",
)
CANONICAL_LAUNCHERS = ("ansatz", "ansatz-agent", "ansatz-acp")
LEGACY_LAUNCHERS = ("hermes", "hermes-agent", "hermes-acp")


def _installer_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    install_dir = tmp_path / "ansatz-runtime" / "hermes-agent"
    hermes_home = tmp_path / "ansatz-runtime"
    shell_home = tmp_path / "shell-home"
    venv_python = install_dir / "venv" / "bin" / "python"

    venv_python.parent.mkdir(parents=True)
    shell_home.mkdir()
    venv_python.write_text(
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    for entrypoint in ("ansatz", "hermes"):
        install_dir.joinpath(entrypoint).write_text(
            "#!/usr/bin/env python3\nraise SystemExit(0)\n",
            encoding="utf-8",
        )

    return install_dir, hermes_home, shell_home


def _run_path_stage(
    install_dir: Path,
    hermes_home: Path,
    shell_home: Path,
    *,
    desktop_product: str | None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "bash",
        str(INSTALL_SH),
        "--stage",
        "path",
        "--non-interactive",
        "--json",
        "--dir",
        str(install_dir),
        "--hermes-home",
        str(hermes_home),
    ]
    if desktop_product is not None:
        command.extend(["--desktop-product", desktop_product])

    env = os.environ.copy()
    env.pop("HERMES_INSTALL_DIR", None)
    env.update({"HOME": str(shell_home), "SHELL": "/bin/zsh"})

    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _make_interpreter_observable(install_dir: Path) -> None:
    interpreter = install_dir / "venv" / "bin" / "python"
    interpreter.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

print(json.dumps({
    "argv": sys.argv[1:],
    "hermes_home": os.environ.get("HERMES_HOME"),
    "pythonpath_present": "PYTHONPATH" in os.environ,
    "pythonhome_present": "PYTHONHOME" in os.environ,
}))
""",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)


def test_desktop_product_publishes_product_and_canonical_launchers_and_removes_owned_legacy_wrappers(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)

    command_dir.joinpath("hermes").write_text(
        f'#!/usr/bin/env bash\nexec "{install_dir / "hermes"}" "$@"\n',
        encoding="utf-8",
    )
    command_dir.joinpath("hermes-agent").symlink_to(install_dir / "venv" / "bin" / "python")
    independent = command_dir / "hermes-acp"
    independent.write_text(
        '#!/usr/bin/env bash\nexec "/opt/independent-hermes/bin/hermes" acp "$@"\n',
        encoding="utf-8",
    )

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    for launcher_name in (*CANONICAL_LAUNCHERS, *ANSATZ_LAUNCHERS):
        launcher = command_dir / launcher_name
        assert launcher.is_file(), f"missing {launcher_name}"
        assert launcher.stat().st_mode & stat.S_IXUSR
        text = launcher.read_text(encoding="utf-8")
        assert str(install_dir) in text
        assert "unset PYTHONPATH" in text
        assert "unset PYTHONHOME" in text

    assert "run_agent.py" in command_dir.joinpath("ansatz-voice-trace-agent").read_text(
        encoding="utf-8"
    )
    assert " acp " in command_dir.joinpath("ansatz-voice-trace-acp").read_text(
        encoding="utf-8"
    )
    assert not command_dir.joinpath("hermes").exists()
    assert not command_dir.joinpath("hermes-agent").exists()
    assert independent.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_default_installer_profile_publishes_canonical_and_compat_launchers(tmp_path: Path) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product=None,
    )

    assert result.returncode == 0, result.stderr
    command_dir = shell_home / ".local" / "bin"
    assert all(
        command_dir.joinpath(name).is_file()
        for name in (*CANONICAL_LAUNCHERS, *LEGACY_LAUNCHERS)
    )
    assert all(not command_dir.joinpath(name).exists() for name in ANSATZ_LAUNCHERS)


def test_ansatz_launchers_pin_the_installer_selected_runtime_home(tmp_path: Path) -> None:
    install_dir, _, shell_home = _installer_fixture(tmp_path)
    _make_interpreter_observable(install_dir)
    injection_sentinel = tmp_path / "launcher-injection-sentinel"
    backtick_sentinel = tmp_path / "launcher-backtick-sentinel"
    hermes_home = tmp_path / (
        "Ansatz home 'quoted' \"$dollar "
        "$(touch launcher-injection-sentinel) `touch launcher-backtick-sentinel`"
    )

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert not injection_sentinel.exists()
    assert not backtick_sentinel.exists()

    expected_prefixes = {
        "ansatz-voice-trace": [str(install_dir / "ansatz")],
        "ansatz-voice-trace-agent": [str(install_dir / "run_agent.py")],
        "ansatz-voice-trace-acp": [str(install_dir / "ansatz"), "acp"],
    }
    caller_env = os.environ.copy()
    caller_env.update(
        {
            "HERMES_HOME": str(tmp_path / ".hermes"),
            "PYTHONPATH": str(tmp_path / "legacy-pythonpath"),
            "PYTHONHOME": str(tmp_path / "legacy-pythonhome"),
        }
    )
    for launcher_name, expected_prefix in expected_prefixes.items():
        launched = subprocess.run(
            [
                str(shell_home / ".local" / "bin" / launcher_name),
                "argument with spaces",
                "$literal",
            ],
            cwd=tmp_path,
            env=caller_env,
            capture_output=True,
            text=True,
        )

        assert launched.returncode == 0, launched.stderr
        observed = json.loads(launched.stdout)
        assert observed == {
            "argv": [*expected_prefix, "argument with spaces", "$literal"],
            "hermes_home": str(hermes_home),
            "pythonpath_present": False,
            "pythonhome_present": False,
        }

    assert not injection_sentinel.exists()
    assert not backtick_sentinel.exists()


def test_desktop_product_removes_only_owned_relative_legacy_symlinks(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)

    owned_target = install_dir / "legacy" / "bin" / "owned"
    owned_target.parent.mkdir(parents=True)
    owned_target.write_text("owned", encoding="utf-8")
    install_relative = os.path.relpath(install_dir, command_dir)
    command_dir.joinpath("hermes").symlink_to(
        f"{install_relative}/legacy/bin/owned"
    )
    command_dir.joinpath("hermes-agent").symlink_to(
        f"{install_relative}/legacy/bin/../bin/owned"
    )

    independent_target = tmp_path / "independent-runtime" / "bin" / "hermes"
    independent_target.parent.mkdir(parents=True)
    independent_target.write_text("independent", encoding="utf-8")
    independent_relative = os.path.relpath(independent_target, command_dir)
    independent_launcher = command_dir / "hermes-acp"
    independent_launcher.symlink_to(independent_relative)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert not command_dir.joinpath("hermes").exists()
    assert not command_dir.joinpath("hermes-agent").exists()
    assert independent_launcher.is_symlink()
    assert os.readlink(independent_launcher) == independent_relative


def test_desktop_product_normalizes_terminal_parent_and_current_components(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)
    install_relative = os.path.relpath(install_dir, command_dir)
    escaped_launcher = command_dir / "hermes"
    escaped_launcher.symlink_to(f"{install_relative}/..")
    owned_launcher = command_dir / "hermes-agent"
    owned_launcher.symlink_to(f"{install_relative}/.")

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert escaped_launcher.is_symlink()
    assert os.readlink(escaped_launcher) == f"{install_relative}/.."
    assert not owned_launcher.is_symlink()


def test_desktop_product_removes_dangling_owned_relative_legacy_symlink(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)
    install_relative = os.path.relpath(install_dir, command_dir)
    dangling_owned = command_dir / "hermes"
    dangling_owned.symlink_to(f"{install_relative}/removed/bin/hermes")

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert not dangling_owned.is_symlink()


def test_desktop_product_preserves_dangling_relative_symlink_that_escapes_install_root(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)
    install_relative = os.path.relpath(install_dir, command_dir)
    escaped_target = f"{install_relative}/../independent-removed/bin/hermes"
    dangling_escaped = command_dir / "hermes"
    dangling_escaped.symlink_to(escaped_target)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert dangling_escaped.is_symlink()
    assert os.readlink(dangling_escaped) == escaped_target


def test_desktop_product_preserves_dangling_install_root_prefix_collision(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    command_dir = shell_home / ".local" / "bin"
    command_dir.mkdir(parents=True)
    collision_root = install_dir.with_name(f"{install_dir.name}-other")
    collision_target = os.path.relpath(
        collision_root / "removed" / "bin" / "hermes",
        command_dir,
    )
    dangling_collision = command_dir / "hermes"
    dangling_collision.symlink_to(collision_target)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    assert dangling_collision.is_symlink()
    assert os.readlink(dangling_collision) == collision_target


def test_desktop_product_rejects_unknown_launcher_profiles(tmp_path: Path) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="unreviewed-product",
    )

    assert result.returncode != 0
    assert "unknown desktop product" in result.stderr.lower()
