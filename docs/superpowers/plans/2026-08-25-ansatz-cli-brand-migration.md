# Ansatz CLI Brand Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make Ansatz the canonical public CLI identity while keeping every Hermes command alias inside a removable compatibility boundary.

**Architecture:** A small hermes_cli.cli_identity module owns canonical and legacy command names, TTY-only migration notices, and canonical-first executable lookup. Canonical console scripts, parser output, authentication guidance, installers, Desktop discovery, and documentation use Ansatz; legacy console scripts and fallback lookup are isolated and behavior-tested for later deletion.

**Tech Stack:** Python 3.11+, argparse, setuptools console scripts, Bash installers, Electron/TypeScript, Vitest, pytest through scripts/run_tests.sh, Markdown/Docusaurus.

---

## File map

- Create hermes_cli/cli_identity.py for command names, legacy aliases, notices, and executable helpers.
- Create hermes_cli/entrypoints.py and the checked-in ansatz launcher for canonical and compatibility entry points.
- Create tests/hermes_cli/test_cli_identity.py and tests/hermes_cli/test_public_cli_brand.py for the identity and public-surface contracts.
- Modify pyproject.toml, hermes, and hermes_cli/client_auth/entrypoint_wrappers.py for six canonical/legacy console scripts.
- Modify parser, auth runtime, help generator, API, and WebSocket files for public Ansatz help and errors.
- Modify relaunch, gateway, kanban, profile, service, and process-detection files for canonical-first executable discovery.
- Modify scripts/install.sh and hermes_cli/uninstall.py for canonical POSIX launchers plus a removable alias block.
- Modify Desktop product, remote lifecycle, backend startup, onboarding, and provider-command files for canonical commands.
- Modify README.md, website documentation, Chinese documentation, and Hermes-agent reference materials only where text is an actual CLI invocation.

### Task 1: Introduce the canonical CLI identity and removable aliases

**Files:**
- Create: hermes_cli/cli_identity.py
- Create: hermes_cli/entrypoints.py
- Create: ansatz
- Create: tests/hermes_cli/test_cli_identity.py
- Modify: hermes
- Modify: pyproject.toml
- Modify: hermes_cli/client_auth/entrypoint_wrappers.py
- Modify: tests/hermes_cli/client_auth/test_entrypoints.py
- Modify: tests/hermes_cli/test_verify_console_scripts.py

- [ ] **Step 1: Write failing identity tests**

Create tests/hermes_cli/test_cli_identity.py:

~~~python
from io import StringIO

from hermes_cli.cli_identity import (
    CANONICAL_ACP_COMMAND,
    CANONICAL_AGENT_COMMAND,
    CANONICAL_COMMAND,
    LEGACY_TO_CANONICAL,
    maybe_warn_legacy_invocation,
)


class _Tty(StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(StringIO):
    def isatty(self) -> bool:
        return False


def test_canonical_command_family_is_ansatz():
    assert CANONICAL_COMMAND == "ansatz"
    assert CANONICAL_AGENT_COMMAND == "ansatz-agent"
    assert CANONICAL_ACP_COMMAND == "ansatz-acp"
    assert LEGACY_TO_CANONICAL == {
        "hermes": "ansatz",
        "hermes-agent": "ansatz-agent",
        "hermes-acp": "ansatz-acp",
    }


def test_legacy_notice_is_interactive_only():
    interactive_error = _Tty()
    maybe_warn_legacy_invocation("hermes", stdin=_Tty(), stderr=interactive_error)
    assert interactive_error.getvalue() == (
        "Deprecated command `hermes`; use `ansatz` instead.\n"
    )

    piped_error = _Pipe()
    maybe_warn_legacy_invocation("hermes", stdin=_Pipe(), stderr=piped_error)
    assert piped_error.getvalue() == ""
~~~

Extend the entrypoint scanner fixture and wrapper test so both canonical and legacy scripts are discovered, and canonical agent/ACP wrappers guard before capability imports.

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_cli_identity.py tests/hermes_cli/client_auth/test_entrypoints.py -q
~~~

Expected: collection fails because hermes_cli.cli_identity does not exist, and entrypoint assertions fail because Ansatz scripts are absent.

- [ ] **Step 3: Implement the minimal identity boundary**

Create hermes_cli/cli_identity.py:

~~~python
from __future__ import annotations

import sys
from collections.abc import TextIO

CANONICAL_COMMAND = "ansatz"
CANONICAL_AGENT_COMMAND = "ansatz-agent"
CANONICAL_ACP_COMMAND = "ansatz-acp"

LEGACY_TO_CANONICAL = {
    "hermes": CANONICAL_COMMAND,
    "hermes-agent": CANONICAL_AGENT_COMMAND,
    "hermes-acp": CANONICAL_ACP_COMMAND,
}


def maybe_warn_legacy_invocation(
    legacy_name: str,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    input_stream = sys.stdin if stdin is None else stdin
    error_stream = sys.stderr if stderr is None else stderr
    try:
        interactive = input_stream.isatty() and error_stream.isatty()
    except Exception:
        interactive = False
    if not interactive:
        return
    canonical = LEGACY_TO_CANONICAL[legacy_name]
    print(
        f"Deprecated command `{legacy_name}`; use `{canonical}` instead.",
        file=error_stream,
    )
~~~

Create hermes_cli/entrypoints.py:

~~~python
from __future__ import annotations


def ansatz() -> object:
    from hermes_cli.main import main

    return main()


def hermes() -> object:
    from hermes_cli.cli_identity import maybe_warn_legacy_invocation

    maybe_warn_legacy_invocation("hermes")
    return ansatz()
~~~

Add ansatz_agent and ansatz_acp to entrypoint_wrappers.py. Keep hermes_agent and hermes_acp as wrappers that call maybe_warn_legacy_invocation before delegating.

Register:

~~~toml
[project.scripts]
ansatz = "hermes_cli.entrypoints:ansatz"
ansatz-agent = "hermes_cli.client_auth.entrypoint_wrappers:ansatz_agent"
ansatz-acp = "hermes_cli.client_auth.entrypoint_wrappers:ansatz_acp"
hermes = "hermes_cli.entrypoints:hermes"
hermes-agent = "hermes_cli.client_auth.entrypoint_wrappers:hermes_agent"
hermes-acp = "hermes_cli.client_auth.entrypoint_wrappers:hermes_acp"
~~~

Add executable ansatz importing hermes_cli.entrypoints.ansatz. Change hermes to import hermes_cli.entrypoints.hermes.

- [ ] **Step 4: Run identity and packaging tests**

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_cli_identity.py tests/hermes_cli/client_auth/test_entrypoints.py tests/hermes_cli/test_verify_console_scripts.py -q
~~~

Expected: all selected files pass and the Windows shim fixture sees all six scripts.

- [ ] **Step 5: Commit**

~~~bash
git add ansatz hermes pyproject.toml hermes_cli/cli_identity.py hermes_cli/entrypoints.py hermes_cli/client_auth/entrypoint_wrappers.py tests/hermes_cli/test_cli_identity.py tests/hermes_cli/client_auth/test_entrypoints.py tests/hermes_cli/test_verify_console_scripts.py
git commit -m "feat: add canonical Ansatz CLI entrypoints"
~~~

### Task 2: Make parser and authentication surfaces canonical Ansatz

**Files:**
- Modify: hermes_cli/_parser.py
- Modify: hermes_cli/main.py
- Modify: hermes_cli/auth.py
- Modify: hermes_cli/client_auth/cli.py
- Modify: hermes_cli/client_auth/guard.py
- Modify: hermes_cli/client_auth/runtime.py
- Modify: hermes_cli/client_auth/static_help.txt
- Modify: hermes_cli/subcommands/login.py
- Modify: hermes_cli/subcommands/logout.py
- Modify: hermes_cli/subcommands/auth.py
- Modify: hermes_cli/web_server.py
- Modify: gateway/platforms/api_server.py
- Modify: scripts/generate_auth_free_help.py
- Modify: tests/hermes_cli/client_auth/test_account_commands.py
- Modify: tests/hermes_cli/client_auth/test_guard.py
- Modify: tests/hermes_cli/client_auth/test_background_modes.py
- Modify: tests/hermes_cli/client_auth/test_entrypoints.py
- Modify: tests/hermes_cli/test_subcommands_batch.py
- Modify: tests/hermes_cli/test_dashboard_auth_ws_auth.py
- Modify: tests/hermes_cli/client_auth/test_boundaries.py

- [ ] **Step 1: Change test expectations first**

Require exact public output:

~~~python
assert result.stderr == (
    "AUTH_REQUIRED runtime_unavailable; run `ansatz login`\n"
)
assert "usage: ansatz " in result.stdout
assert captured_prompt == "Ansatz account: "
assert logout_output == (
    "Remote Ansatz account signed out; provider credentials were not modified.\n"
)
~~~

Add API and WebSocket assertions for "Ansatz login required" and parser assertions for "Ansatz remote account".

- [ ] **Step 2: Run focused tests and verify red**

~~~bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_account_commands.py tests/hermes_cli/client_auth/test_guard.py tests/hermes_cli/client_auth/test_background_modes.py tests/hermes_cli/client_auth/test_entrypoints.py tests/hermes_cli/test_subcommands_batch.py tests/hermes_cli/test_dashboard_auth_ws_auth.py tests/hermes_cli/client_auth/test_boundaries.py -q
~~~

Expected: failures show current Hermes help, prompts, guidance, and API messages.

- [ ] **Step 3: Implement canonical parser and authentication strings**

Import CANONICAL_COMMAND in _parser.py, construct the epilogue from it, and set:

~~~python
parser = argparse.ArgumentParser(
    prog=CANONICAL_COMMAND,
    description="Ansatz - AI assistant with tool-calling capabilities",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=_EPILOGUE,
)
~~~

Change authentication guidance to ansatz login, the prompt to "Ansatz account: ", logout text to "Remote Ansatz account signed out", and public account help to "Ansatz remote account".

Replace the obsolete provider-login text in auth.py with:

~~~python
print("The deprecated provider-login flow has been removed.")
print("Use `ansatz provider` to manage credentials,")
print("`ansatz model` to select a provider, or `ansatz setup` for full setup.")
~~~

Change Dashboard, WebSocket, and gateway API errors to "Ansatz login required" with an ansatz login hint.

- [ ] **Step 4: Regenerate auth-free help**

Update generate_auth_free_help.py to set sys.argv to ansatz and require "usage: ansatz ". Run:

~~~bash
python scripts/generate_auth_free_help.py
python scripts/generate_auth_free_help.py --check
~~~

Expected: both exit 0 and static_help.txt begins with "usage: ansatz ".

- [ ] **Step 5: Run focused tests and commit**

~~~bash
scripts/run_tests.sh tests/hermes_cli/client_auth/ tests/hermes_cli/test_subcommands_batch.py tests/hermes_cli/test_dashboard_auth_ws_auth.py -q
git add hermes_cli/_parser.py hermes_cli/main.py hermes_cli/auth.py hermes_cli/client_auth hermes_cli/subcommands/login.py hermes_cli/subcommands/logout.py hermes_cli/subcommands/auth.py hermes_cli/web_server.py gateway/platforms/api_server.py scripts/generate_auth_free_help.py tests/hermes_cli/client_auth tests/hermes_cli/test_subcommands_batch.py tests/hermes_cli/test_dashboard_auth_ws_auth.py
git commit -m "feat: brand CLI authentication as Ansatz"
~~~

### Task 3: Centralize canonical-first executable discovery

**Files:**
- Modify: hermes_cli/cli_identity.py
- Modify: hermes_cli/relaunch.py
- Modify: hermes_cli/profiles.py
- Modify: hermes_cli/linux_desktop_entry.py
- Modify: gateway/run.py
- Modify: gateway/slash_commands.py
- Modify: hermes_cli/kanban_db.py
- Modify: hermes_cli/web_server.py
- Modify: hermes_cli/console_engine.py
- Modify: hermes_cli/commands.py
- Modify: gateway/status.py
- Modify: hermes_cli/update_cmd.py
- Modify: tests/hermes_cli/test_relaunch.py
- Modify: tests/hermes_cli/test_profiles.py
- Modify: tests/gateway/test_update_command.py
- Modify: tests/hermes_cli/test_linux_desktop_entry.py
- Modify: tests/hermes_cli/test_kanban_db.py
- Modify: tests/hermes_cli/test_console_engine.py

- [ ] **Step 1: Write canonical-first lookup tests**

~~~python
def test_resolver_prefers_ansatz_on_path(monkeypatch):
    seen = []

    def which(name):
        seen.append(name)
        return {
            "ansatz": "/usr/bin/ansatz",
            "hermes": "/usr/bin/hermes",
        }.get(name)

    monkeypatch.setattr(relaunch_mod.shutil, "which", which)
    monkeypatch.setattr(relaunch_mod.sys, "argv", ["hermes"])
    assert relaunch_mod.resolve_cli_bin() == "/usr/bin/ansatz"
    assert seen == ["ansatz"]


def test_resolver_falls_back_to_legacy_alias(monkeypatch):
    monkeypatch.setattr(
        relaunch_mod.shutil,
        "which",
        lambda name: "/usr/bin/hermes" if name == "hermes" else None,
    )
    monkeypatch.setattr(relaunch_mod.sys, "argv", ["hermes"])
    assert relaunch_mod.resolve_cli_bin() == "/usr/bin/hermes"
~~~

Add equivalent gateway/kanban tests and console tests accepting canonical ansatz input while retaining explicit Hermes compatibility.

- [ ] **Step 2: Run tests and verify red**

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_relaunch.py tests/hermes_cli/test_profiles.py tests/gateway/test_update_command.py tests/hermes_cli/test_linux_desktop_entry.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_console_engine.py -q
~~~

Expected: canonical lookup tests fail because current code probes Hermes directly.

- [ ] **Step 3: Implement shared discovery**

Add:

~~~python
from pathlib import Path

LEGACY_COMMAND = "hermes"


def executable_name(path: str) -> str:
    name = Path(path).name.casefold()
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def is_legacy_cli_executable(path: str) -> bool:
    return executable_name(path) == LEGACY_COMMAND
~~~

Rename resolve_hermes_bin to resolve_cli_bin. Preserve arbitrary executable argv-zero values for nix/source wrappers, but skip a legacy basename, probe ansatz first, and use Hermes only as fallback.

Use the same order in profiles.py, gateway/run.py, kanban_db.py, linux_desktop_entry.py, and update-shell discovery. Generated service/gateway commands use ansatz. Process detection accepts Ansatz first and Hermes only as compatibility.

- [ ] **Step 4: Verify and commit**

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_relaunch.py tests/hermes_cli/test_profiles.py tests/gateway/ tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_console_engine.py -k 'relaunch or resolve or command or process' -q
git add hermes_cli/cli_identity.py hermes_cli/relaunch.py hermes_cli/profiles.py hermes_cli/linux_desktop_entry.py gateway/run.py gateway/slash_commands.py hermes_cli/kanban_db.py hermes_cli/web_server.py hermes_cli/console_engine.py hermes_cli/commands.py gateway/status.py hermes_cli/update_cmd.py tests/hermes_cli/test_relaunch.py tests/hermes_cli/test_profiles.py tests/gateway/test_update_command.py tests/hermes_cli/test_linux_desktop_entry.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_console_engine.py
git commit -m "refactor: centralize Ansatz CLI process discovery"
~~~

### Task 4: Publish canonical POSIX launchers and isolate installer aliases

**Files:**
- Modify: scripts/install.sh
- Modify: hermes_cli/uninstall.py
- Modify: hermes_cli/main.py
- Modify: tests/test_install_sh_acp_launcher.py
- Create: tests/test_install_sh_ansatz_launchers.py
- Modify: tests/hermes_cli/test_verify_console_scripts.py
- Modify: tests/hermes_cli/test_uninstall_dry_run.py

- [ ] **Step 1: Add failing installer tests**

Drive the real setup_path block under a temporary HOME:

~~~python
for name in (
    "ansatz",
    "ansatz-agent",
    "ansatz-acp",
    "hermes",
    "hermes-agent",
    "hermes-acp",
):
    launcher = local_bin / name
    assert launcher.is_file()
    assert launcher.stat().st_mode & stat.S_IXUSR

assert "ansatz" in (local_bin / "hermes").read_text(encoding="utf-8")
~~~

For DESKTOP_PRODUCT=ansatz-voice-trace, also require ansatz plus all existing product launchers. Add uninstall expectations for both command families.

- [ ] **Step 2: Run tests and verify red**

~~~bash
scripts/run_tests.sh tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py tests/hermes_cli/test_verify_console_scripts.py tests/hermes_cli/test_uninstall_dry_run.py -q
~~~

Expected: canonical launcher assertions fail.

- [ ] **Step 3: Refactor setup_path**

Make ansatz the primary readiness probe. In venv mode use INSTALL_DIR/ansatz as the canonical checked-in entrypoint and INSTALL_DIR/hermes as the legacy entrypoint.

Generate ansatz, ansatz-agent, and ansatz-acp first. Generate the three Hermes aliases only in one adjacent compatibility block. Legacy shell wrappers emit a migration notice only when stdin and stderr are terminals, then dispatch to the same implementation.

Desktop installs retain ansatz-voice-trace launchers and also publish the canonical family. Remove only legacy launchers owned by this installation.

Update uninstall targets:

~~~python
names = (
    "ansatz",
    "ansatz-agent",
    "ansatz-acp",
    "hermes",
    "hermes-agent",
    "hermes-acp",
)
~~~

- [ ] **Step 4: Verify and commit**

~~~bash
bash -n scripts/install.sh
scripts/run_tests.sh tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py tests/hermes_cli/test_verify_console_scripts.py tests/hermes_cli/test_uninstall_dry_run.py -q
git add scripts/install.sh hermes_cli/uninstall.py hermes_cli/main.py tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py tests/hermes_cli/test_verify_console_scripts.py tests/hermes_cli/test_uninstall_dry_run.py
git commit -m "feat: install canonical Ansatz launchers"
~~~

### Task 5: Switch Desktop commands and remote discovery to Ansatz

**Files:**
- Modify: apps/desktop/electron/ansatz-product.ts
- Modify: apps/desktop/electron/ansatz-product.test.ts
- Modify: apps/desktop/electron/remote-lifecycle.ts
- Modify: apps/desktop/electron/remote-lifecycle.test.ts
- Modify: apps/desktop/electron/main.ts
- Modify: apps/desktop/electron/primary-backend-startup.test.ts
- Modify: apps/desktop/electron/first-run-setup-main-process.test.ts
- Modify: apps/desktop/electron/auth-runtime-contract.ts
- Modify: apps/desktop/src/store/onboarding.ts
- Modify: apps/desktop/src/store/onboarding.test.ts
- Modify: apps/desktop/src/components/onboarding/index.test.tsx
- Modify: apps/desktop/src/app/settings/providers-settings.test.tsx
- Modify: hermes_cli/web_server.py

- [ ] **Step 1: Write failing Desktop tests**

~~~typescript
assert.deepEqual(ANSATZ_PRODUCT.canonicalCliLaunchers, [
  'ansatz',
  'ansatz-agent',
  'ansatz-acp'
])
assert.deepEqual(ANSATZ_PRODUCT.legacyCliLaunchers, [
  'hermes',
  'hermes-agent',
  'hermes-acp'
])
~~~

Add:

~~~typescript
test('locateHermes prefers ansatz and falls back to hermes', async () => {
  const ssh = fakeSsh([
    [/command -v ansatz/, '/home/u/.local/bin/ansatz\n'],
    [/command -v hermes/, '/home/u/.local/bin/hermes\n'],
    [/\[ -x .*\.local\/bin\/ansatz/, 'OK']
  ])

  assert.equal(await locateHermes(ssh, ''), '/home/u/.local/bin/ansatz')
  assert.ok(!ssh.calls.some(command => /command -v hermes/.test(command)))
})
~~~

Change onboarding/provider fixtures to ansatz provider add and ansatz login.

- [ ] **Step 2: Run tests and verify red**

~~~bash
cd apps/desktop
npm run test:desktop:platforms -- electron/ansatz-product.test.ts electron/remote-lifecycle.test.ts electron/primary-backend-startup.test.ts electron/first-run-setup-main-process.test.ts
npm run test:ui -- src/store/onboarding.test.ts src/components/onboarding/index.test.tsx src/app/settings/providers-settings.test.tsx
~~~

Expected: tests fail on Hermes command paths and fixtures.

- [ ] **Step 3: Implement canonical Desktop resolution**

Add canonicalCliLaunchers and legacyCliLaunchers without changing product-specific posixLaunchers.

Probe command -v ansatz, ~/.local/bin/ansatz, and the Ansatz venv shim first. Probe the Hermes equivalents only afterward. Preserve explicit remote path behavior.

In main.ts and auth-runtime-contract.ts prefer ansatz/ansatz.exe and route the fallback through a named compatibility helper. User-facing logs and install hints say Ansatz. Change web_server provider cli_command values and UI fixtures to ansatz.

- [ ] **Step 4: Verify and commit**

~~~bash
cd apps/desktop
npm run test:desktop:platforms -- electron/ansatz-product.test.ts electron/remote-lifecycle.test.ts electron/primary-backend-startup.test.ts electron/first-run-setup-main-process.test.ts
npm run test:ui -- src/store/onboarding.test.ts src/components/onboarding/index.test.tsx src/app/settings/providers-settings.test.tsx
npm run typecheck
npm run lint
cd ../..
git add apps/desktop/electron/ansatz-product.ts apps/desktop/electron/ansatz-product.test.ts apps/desktop/electron/remote-lifecycle.ts apps/desktop/electron/remote-lifecycle.test.ts apps/desktop/electron/main.ts apps/desktop/electron/primary-backend-startup.test.ts apps/desktop/electron/first-run-setup-main-process.test.ts apps/desktop/electron/auth-runtime-contract.ts apps/desktop/src/store/onboarding.ts apps/desktop/src/store/onboarding.test.ts apps/desktop/src/components/onboarding/index.test.tsx apps/desktop/src/app/settings/providers-settings.test.tsx hermes_cli/web_server.py
git commit -m "feat: use Ansatz commands in Desktop"
~~~

### Task 6: Migrate public guidance and add a regression contract

**Files:**
- Create: tests/hermes_cli/test_public_cli_brand.py
- Modify: README.md
- Modify: website/docs
- Modify: website/i18n/zh-Hans/docusaurus-plugin-content-docs/current
- Modify: skills/autonomous-ai-agents/hermes-agent/references
- Modify: public Python, Bash, TypeScript, and TUI files reported by the contract

- [ ] **Step 1: Add a failing public-surface contract**

~~~python
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOVERNED = (
    ROOT / "README.md",
    ROOT / "hermes_cli",
    ROOT / "gateway",
    ROOT / "scripts" / "install.sh",
    ROOT / "apps" / "desktop" / "src",
    ROOT / "apps" / "desktop" / "electron",
    ROOT / "ui-tui" / "src",
    ROOT / "website" / "docs",
    ROOT / "website" / "i18n" / "zh-Hans" / "docusaurus-plugin-content-docs" / "current",
    ROOT / "skills" / "autonomous-ai-agents" / "hermes-agent" / "references",
)
EXCLUDED_FILES = {
    ROOT / "hermes_cli" / "cli_identity.py",
    ROOT / "hermes_cli" / "entrypoints.py",
}
COMMAND = re.compile(
    r"(?<![\w-])hermes\s+(?=(?:--?[a-z]|"
    r"login|logout|auth|setup|chat|model|provider|fallback|config|gateway|"
    r"sessions|logs|debug|console|update|dashboard|doctor|tools|mcp|"
    r"completion|uninstall|service|cron|kanban|send|status)\b)"
)


def _files():
    for path in GOVERNED:
        if path.is_file():
            yield path
            continue
        for child in path.rglob("*"):
            if child.suffix in {
                ".py", ".sh", ".ts", ".tsx", ".js", ".md", ".mdx", ".txt"
            }:
                if "test" not in child.name and child not in EXCLUDED_FILES:
                    yield child


def test_public_surfaces_do_not_recommend_legacy_cli():
    violations = []
    for path in _files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            candidate = line.replace(
                "docker exec hermes ",
                "docker exec <container> ",
            )
            if COMMAND.search(candidate):
                relative = path.relative_to(ROOT)
                violations.append(f"{relative}:{line_number}:{line.strip()}")
    assert violations == []
~~~

- [ ] **Step 2: Run the contract and capture the red inventory**

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_public_cli_brand.py -q
~~~

Expected: the failure lists remaining public Hermes command literals.

- [ ] **Step 3: Convert only actual command invocations**

Change every reported command invocation to ansatz. Preserve hermes_cli, HERMES_HOME, .hermes, Docker users, schema keys, package names, repository URLs, upstream names, and historical prose.

Manually correct obsolete login documentation in:

- website/docs/reference/cli-commands.md
- website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/cli-commands.md
- skills/autonomous-ai-agents/hermes-agent/references/cli-reference.md

Document ansatz login as remote-account login and distinguish it from the removed provider-login flow. Use contract output as the exact edit inventory; do not perform unrestricted case-insensitive replacement.

- [ ] **Step 4: Verify and commit**

~~~bash
scripts/run_tests.sh tests/hermes_cli/test_public_cli_brand.py tests/website/ -q
python scripts/generate_auth_free_help.py --check
git add README.md hermes_cli gateway scripts/install.sh apps/desktop/src apps/desktop/electron ui-tui/src website/docs website/i18n/zh-Hans/docusaurus-plugin-content-docs/current skills/autonomous-ai-agents/hermes-agent/references tests/hermes_cli/test_public_cli_brand.py
git commit -m "docs: make Ansatz the public CLI name"
~~~

### Task 7: Verify compatibility and platform evidence

**Files:**
- Modify only if verification exposes a defect in a file already listed above.

- [ ] **Step 1: Verify Python CLI and auth**

~~~bash
scripts/run_tests.sh tests/hermes_cli/client_auth/ tests/hermes_cli/test_cli_identity.py tests/hermes_cli/test_relaunch.py tests/hermes_cli/test_completion.py tests/hermes_cli/test_subcommands_batch.py tests/hermes_cli/test_verify_console_scripts.py tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py tests/hermes_cli/test_public_cli_brand.py -q
~~~

Expected: zero failures.

- [ ] **Step 2: Verify macOS source launchers**

~~~bash
./ansatz --help
./ansatz auth status
./hermes auth status
~~~

Expected: help begins with "usage: ansatz"; canonical auth status emits one JSON object; captured non-interactive legacy behavior emits no compatibility notice. In a real terminal, ./hermes --version emits exactly one stderr migration notice.

- [ ] **Step 3: Verify POSIX/Linux-oriented behavior**

~~~bash
bash -n scripts/install.sh
scripts/run_tests.sh tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py tests/docker/test_docker_exec_privilege_drop.py -q
~~~

Expected: shell syntax and available POSIX/Docker tests pass. If Docker is unavailable, report that coverage as unverified.

- [ ] **Step 4: Verify Desktop and Windows static contracts**

~~~bash
cd apps/desktop
npm run typecheck
npm run lint
npm run test:desktop:platforms -- electron/ansatz-product.test.ts electron/remote-lifecycle.test.ts electron/windows-remote-lifecycle.test.ts electron/desktop-branding.contract.test.ts electron/ansatz-runtime-environment.contract.test.ts
npm run test:ui -- src/store/onboarding.test.ts src/components/onboarding/index.test.tsx src/app/settings/providers-settings.test.tsx
cd ../..
~~~

Expected: all commands exit 0. Record this as static/automated Windows evidence, not real Windows installation evidence.

- [ ] **Step 5: Verify the diff and compatibility isolation**

~~~bash
git diff --check
git status --short
rg -n '\bhermes (login|logout|auth|setup|chat|model|provider|config|gateway|update|doctor)\b' hermes_cli gateway scripts/install.sh apps/desktop/src apps/desktop/electron ui-tui/src README.md website/docs website/i18n/zh-Hans/docusaurus-plugin-content-docs/current skills/autonomous-ai-agents/hermes-agent/references
~~~

Expected: diff check exits 0. Remaining matches are confined to the explicit compatibility boundary or intentional historical/container-name contexts covered by the contract.

If verification exposes a defect, return to the task that owns that behavior,
add a failing regression test there, and repeat its red-green-commit sequence.
Do not create an empty verification commit.

### Task 8: Close cross-platform canonical-launcher gaps found during merge review

**Files:**
- Modify: `scripts/install.ps1`
- Test: `tests/test_install_ps1_ansatz_auth_launcher.py`
- Modify: `hermes_cli/update_cmd.py`
- Modify: `tests/hermes_cli/test_ensure_acp_launcher.py`
- Modify: `docker/main-wrapper.sh`
- Modify: `docker/hermes-exec-shim.sh`
- Modify: `Dockerfile`
- Modify: `tests/docker/test_docker_exec_privilege_drop.py`
- Modify: `tests/hermes_cli/client_auth/test_background_modes.py`
- Modify: `nix/hermes-agent.nix`
- Modify: `nix/checks.nix`
- Create: `tests/hermes_cli/test_nix_ansatz_launchers.py`

**Interfaces:**
- Consumes: the six-script family declared by `pyproject.toml`: `ansatz`,
  `ansatz-agent`, `ansatz-acp`, `hermes`, `hermes-agent`, and `hermes-acp`.
- Produces: the same six public commands on normal Windows installs, upgraded
  POSIX installs, Docker images, and Nix packages. Canonical and legacy Docker
  commands share auth waiting and privilege dropping.

- [ ] **Step 1: Add failing cross-platform launcher contracts**

Extend the PowerShell installer test to assert that the normal PATH-install
stage copies all six `.exe` launchers. Extend the update launcher test with a
legacy-only install containing only `hermes`, then require the update repair to
publish all three canonical launchers without replacing unrelated or symlinked
files. Extend Docker contracts to require both `ansatz` and `hermes` to enter
the same auth-wait dispatch and require `/opt/hermes/bin/ansatz` to use the
privilege-drop shim. Add a Nix static contract requiring all six launchers in
both the package wrapper loop and `nix/checks.nix`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

~~~bash
scripts/run_tests.sh tests/test_install_ps1_ansatz_auth_launcher.py tests/hermes_cli/test_ensure_acp_launcher.py tests/docker/test_docker_exec_privilege_drop.py tests/hermes_cli/client_auth/test_background_modes.py tests/hermes_cli/test_nix_ansatz_launchers.py -q
~~~

Expected: assertions fail because the four packaging/update surfaces expose or
route only the legacy command family.

- [ ] **Step 3: Implement the minimal launcher-family fixes**

In `install.ps1`, derive or enumerate the exact six console scripts and copy
their `.exe` shims into the PATH directory. Replace `_ensure_acp_launcher()`
with an ownership-safe launcher-family repair that publishes the three
canonical POSIX launchers plus missing compatibility aliases but never writes
through symlinks or overwrites unrelated files. Route `ansatz` and `hermes`
through identical auth-wait logic in `docker/main-wrapper.sh`, install the
privilege-drop shim as canonical `ansatz` with `hermes` as a compatibility
alias, and wrap all six scripts in the Nix derivation and checks.

- [ ] **Step 4: Run focused verification and confirm GREEN**

Run the Step 2 command again, then run:

~~~bash
bash -n docker/main-wrapper.sh
bash -n docker/hermes-exec-shim.sh
scripts/run_tests.sh tests/hermes_cli/client_auth/ tests/hermes_cli/test_cli_identity.py tests/hermes_cli/test_verify_console_scripts.py tests/test_install_sh_ansatz_launchers.py tests/test_install_sh_acp_launcher.py -q
~~~

Expected: all selected tests pass with no failures; shell syntax checks exit 0.

- [ ] **Step 5: Commit**

~~~bash
git add scripts/install.ps1 hermes_cli/update_cmd.py docker/main-wrapper.sh docker/hermes-exec-shim.sh Dockerfile nix/hermes-agent.nix nix/checks.nix tests/test_install_ps1_ansatz_auth_launcher.py tests/hermes_cli/test_ensure_acp_launcher.py tests/docker/test_docker_exec_privilege_drop.py tests/hermes_cli/client_auth/test_background_modes.py tests/hermes_cli/test_nix_ansatz_launchers.py docs/superpowers/plans/2026-08-25-ansatz-cli-brand-migration.md
git commit -m "fix(cli): publish canonical launchers across platforms"
~~~
