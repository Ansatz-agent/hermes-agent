# Ansatz CLI Brand Migration Design

**Date:** 2026-08-25

## Goal

Make `ansatz` the only canonical public CLI identity while retaining the old
`hermes` commands in a small, isolated compatibility layer that can be deleted
in a later phase without changing command implementations.

## Decisions

- The canonical public commands are `ansatz`, `ansatz-agent`, and
  `ansatz-acp`.
- `hermes`, `hermes-agent`, and `hermes-acp` remain temporary compatibility
  aliases only.
- Public help, examples, errors, login prompts, Desktop-provided terminal
  commands, completions, and relaunches always use `ansatz`.
- An interactive terminal invocation through a legacy alias prints one
  migration notice to stderr. Non-interactive invocations are silent so their
  stdout, JSON output, and exit status remain compatible.
- `ansatz-voice-trace` remains a Desktop product launcher, but documentation
  and terminal guidance use `ansatz` as the standard CLI.
- Internal implementation identifiers are not renamed in this phase.

## Architecture

### Canonical command identity

A focused CLI identity module owns the canonical command names, legacy alias
names, executable discovery order, and legacy invocation notice. No business
command should decide independently whether to spell the program name as
`ansatz` or `hermes`.

The real console-script entry points invoke the canonical implementation. The
legacy console-script entry points invoke thin wrappers that optionally emit
the interactive notice and then call the same canonical implementation. The
parser always uses `ansatz` as its `prog`, even when reached through a legacy
alias.

### Executable discovery and relaunch

Relaunches and child-process entry points resolve `ansatz` first. A single
legacy fallback may resolve `hermes` when upgrading an existing installation
that does not yet expose the canonical launcher. Desktop remote discovery uses
the same order so current Hermes-only remote installations remain reachable
during the compatibility period.

All legacy lookup behavior must be located in the compatibility boundary or a
small adapter that imports it. Business modules must not add new direct
`which("hermes")`, `command -v hermes`, or hard-coded `hermes <subcommand>`
logic.

### Help and user-facing strings

The top-level parser, generated auth-free static help, shell completion,
authentication guidance, account prompts, background-service JSON guidance,
Dashboard/API errors, setup tips, and Desktop-generated terminal commands use
the canonical public identity.

Actual command examples in the README, CLI reference, and user guides change
from `hermes <subcommand>` to `ansatz <subcommand>`. Text that specifically
describes the upstream Hermes project or historical behavior is not renamed
mechanically.

### Packaging and installers

Python packaging exposes both canonical scripts and temporary aliases. The
POSIX installer publishes the canonical launchers and keeps legacy launchers
as compatibility wrappers. The Desktop installation also publishes `ansatz`
alongside its product-specific `ansatz-voice-trace` launcher.

Update, repair, and uninstall code treats the canonical script set as primary
and the legacy script set as removable compatibility artifacts. Windows shim
discovery and quarantine logic obtains the complete script set from packaging
metadata rather than maintaining scattered hard-coded lists.

## Compatibility behavior

When a user runs a legacy alias from an interactive terminal, stderr receives
a concise notice directing them to the corresponding Ansatz command. The
command then behaves exactly like the canonical entry point.

When stdin or stderr is not a TTY, no notice is emitted. This preserves shell
pipelines, CI jobs, machine-readable auth status, and scripts that assert exact
stderr output. The alias does not alter arguments, stdout, return values, or
exit codes.

Authentication failures always direct users to `ansatz login`, regardless of
which alias started the process. Account prompts and success/logout messages
use the Ansatz product name.

## Scope

### Included

- Canonical and legacy Python console scripts.
- POSIX and Desktop launcher installation.
- Top-level parser identity and generated static help.
- Login, logout, auth status, auth guards, and background-service guidance.
- CLI relaunch and child-process executable resolution.
- Desktop onboarding, provider setup, local backend, and remote CLI discovery.
- Update, repair, uninstall, completion, Docker public entry points, and their
  tests where they expose a CLI command name.
- README, CLI reference, and user-guide command examples.
- A contract check preventing new legacy command literals in governed public
  CLI surfaces.

### Excluded

- The `hermes_cli` Python package name.
- `HERMES_HOME`, `.hermes`, data directories, databases, and configuration
  compatibility fields.
- Docker internal users, internal event/schema keys, protocol keys, and model
  source identifiers.
- Repository names, dependency distribution names, upstream URLs, and prose
  that intentionally identifies the upstream Hermes project.

## Error handling

- Canonical launcher discovery failures report `ansatz` and the concrete
  missing path or installation action.
- Legacy fallback is attempted only where an existing installation may
  legitimately lack the new launcher.
- Legacy notices go to stderr and never replace the original error.
- Static help generation fails if the output does not begin with
  `usage: ansatz` or if the checked-in artifact is stale.
- Authentication failures retain their existing exit code and structured
  fields; only the product and command guidance change.

## Testing and verification

Behavior tests are written before implementation changes and executed through
`scripts/run_tests.sh` as required by the repository.

Required coverage includes:

- `ansatz --help`, `ansatz login`, `ansatz logout`, and `ansatz auth status`.
- Parser and static-help output containing canonical command examples.
- Interactive legacy invocation producing one stderr notice.
- Non-interactive legacy invocation producing no compatibility notice and
  preserving exact stdout, stderr, and exit status.
- Canonical-first relaunch and Desktop remote discovery with a legacy fallback.
- Installer, update, repair, and uninstall ownership of both script sets.
- Desktop onboarding/provider commands using `ansatz`.
- Contract scanning of governed public surfaces for legacy command literals.

Platform evidence is intentionally asymmetric:

- macOS receives direct local installation and CLI behavior verification.
- Linux receives the repository's POSIX installer, shell, Docker, and other
  available Linux-oriented automated tests.
- Windows receives static packaging contracts and existing automated tests in
  this environment. The work will not claim a real Windows installation was
  verified without access to a Windows runner.

## Phase-two removal

The later removal of Hermes command compatibility consists of deleting:

1. The three legacy console-script registrations.
2. The POSIX/Desktop legacy launcher installation and cleanup paths.
3. The interactive legacy notice and canonical-first legacy fallback.
4. Legacy alias behavior tests and the explicit compatibility allowlist.

Business command handlers, parser construction, help text, Desktop-generated
commands, and documentation do not change in phase two because they already
use the canonical Ansatz identity. Renaming internal packages, environment
variables, data paths, or protocols is a separate migration and is not a
prerequisite for removing the old CLI aliases.
