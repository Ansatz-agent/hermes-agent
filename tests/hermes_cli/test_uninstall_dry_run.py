from pathlib import Path
from types import SimpleNamespace

from hermes_cli import uninstall


def test_remove_wrapper_script_removes_both_command_families(monkeypatch, tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    names = (
        "ansatz",
        "ansatz-agent",
        "ansatz-acp",
        "hermes",
        "hermes-agent",
        "hermes-acp",
    )
    for name in names:
        local_bin.joinpath(name).write_text(
            "#!/usr/bin/env bash\n"
            f'exec "{tmp_path}/.hermes/hermes-agent/{name}" "$@"\n',
            encoding="utf-8",
        )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    removed = uninstall.remove_wrapper_script()

    assert set(removed) == {local_bin / name for name in names}
    assert all(not local_bin.joinpath(name).exists() for name in names)


def test_remove_wrapper_script_preserves_unowned_ansatz_script(monkeypatch, tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    unrelated = local_bin / "ansatz"
    body = "#!/usr/bin/env bash\necho 'my ansatz helper'\n"
    unrelated.write_text(body, encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    removed = uninstall.remove_wrapper_script()

    assert unrelated not in removed
    assert unrelated.read_text(encoding="utf-8") == body


def test_dry_run_prints_plan_without_mutating(monkeypatch, tmp_path, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: {}\n")

    called = False

    def _fail_if_called(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(uninstall, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [])
    monkeypatch.setattr(uninstall, "_perform_uninstall", _fail_if_called)

    uninstall.run_uninstall(SimpleNamespace(dry_run=True, yes=True, full=True))

    output = capsys.readouterr().out
    assert called is False
    assert "Dry run" in output
    assert str(project_root) in output
    assert str(hermes_home) in output
    assert project_root.exists()
    assert hermes_home.exists()


def test_build_uninstall_parser_accepts_dry_run():
    import argparse
    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_uninstall_parser(subparsers, cmd_uninstall=lambda args: args)

    args = parser.parse_args(["uninstall", "--dry-run", "--full"])

    assert args.dry_run is True
    assert args.full is True
