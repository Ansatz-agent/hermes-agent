"""Static contract for Nix's complete public console-script family."""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = (
    "ansatz",
    "ansatz-agent",
    "ansatz-acp",
    "hermes",
    "hermes-agent",
    "hermes-acp",
)


def test_nix_package_wraps_all_canonical_and_compatibility_launchers():
    package = (REPO_ROOT / "nix" / "hermes-agent.nix").read_text(encoding="utf-8")
    checks = (REPO_ROOT / "nix" / "checks.nix").read_text(encoding="utf-8")

    wrapper_start = package.index("${lib.concatMapStringsSep")
    wrapper_end = package.index("${lib.optionalString (extraPythonPackages", wrapper_start)
    wrapper_names = re.findall(
        r'^\s+"([^"]+)"\s*$',
        package[wrapper_start:wrapper_end],
        flags=re.MULTILINE,
    )
    assert tuple(wrapper_names) == LAUNCHERS

    checks_start = checks.index("entry-points-sync")
    checks_end = checks.index("# Verify CLI subcommands", checks_start)
    loop = re.search(
        r"for bin in (?P<names>[^;]+); do",
        checks[checks_start:checks_end],
    )
    assert loop is not None
    assert tuple(loop.group("names").split()) == LAUNCHERS
