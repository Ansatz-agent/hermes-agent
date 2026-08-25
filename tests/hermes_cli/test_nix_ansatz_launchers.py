"""Static contract for Nix's complete public console-script family."""

from pathlib import Path


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

    for launcher in LAUNCHERS:
        assert f'"{launcher}"' in package
        assert f"for bin in" in checks
        assert launcher in checks
