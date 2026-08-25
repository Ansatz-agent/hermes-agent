"""Regression tests for install.sh Python environment sanitization.

When install.sh is launched from another Python-driven tool session, inherited
PYTHONPATH/PYTHONHOME can shadow the freshly installed checkout. The installer
must sanitize those vars both during installation and at runtime launch.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_install_script_unsets_pythonpath_and_pythonhome_early() -> None:
    text = INSTALL_SH.read_text()

    # During install, inherited Python env must be sanitized before pip/venv use.
    assert 'unset PYTHONPATH' in text
    assert 'unset PYTHONHOME' in text


def test_ansatz_launcher_wrapper_clears_python_env_before_exec() -> None:
    text = INSTALL_SH.read_text()

    # Wrapper should clear env and forward args untouched to the venv entrypoint.
    assert 'cat > "$command_link_dir/ansatz" <<EOF' in text
    assert 'unset PYTHONPATH' in text
    assert 'unset PYTHONHOME' in text
    assert 'exec $quoted_ansatz_bin $quoted_ansatz_entrypoint "\\$@"' in text


def test_hermes_compatibility_launcher_delegates_to_ansatz() -> None:
    text = INSTALL_SH.read_text()

    assert 'cat > "$command_link_dir/hermes" <<EOF' in text
    assert 'exec $quoted_command_link_dir/ansatz "\\$@"' in text
