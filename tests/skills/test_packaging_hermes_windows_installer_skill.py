"""Contract tests for the Hermes Windows installer packaging skill."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "software-development"
    / "packaging-hermes-windows-installer"
    / "SKILL.md"
)

REQUIRED_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]


class PackagingHermesWindowsInstallerSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_text = SKILL_MD.read_text(encoding="utf-8") if SKILL_MD.exists() else ""

    def test_skill_declares_host_agnostic_windows_setup_capability(self) -> None:
        self.assertTrue(SKILL_MD.exists(), f"missing skill: {SKILL_MD}")
        text = self.skill_text
        self.assertTrue(text.startswith("---\n"))
        self.assertRegex(text, r"(?m)^name: packaging-hermes-windows-installer$")
        self.assertRegex(text, r"(?m)^platforms: \[linux, macos, windows\]$")
        self.assertIn("Windows Setup installer", text)

        description_match = re.search(r"(?m)^description: (.+)$", text)
        self.assertIsNotNone(description_match)
        description = description_match.group(1).strip('"')
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith("."))

    def test_skill_uses_modern_procedure_structure(self) -> None:
        positions = [self.skill_text.find(section) for section in REQUIRED_SECTIONS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_skill_preserves_source_provenance_and_user_changes(self) -> None:
        for contract in (
            "isolated worktree",
            "git status --short",
            "git rev-parse HEAD",
            "full 40-character commit",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_prepares_the_offline_windows_payload(self) -> None:
        for contract in (
            "PortableGit-2.55.0.3-64-bit.7z.exe",
            "ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d",
            "HERMES_AUTH_TOOLCHAIN_UV_PATH",
            "HERMES_AUTH_TOOLCHAIN_HOST_PYTHON",
            "prepare:package:win",
            "payload-manifest.json",
            "git-bash-runtime.tar.xz",
            "hermes-backend.tar.gz",
            "get-windows-win32-x64.tar.gz",
            "auth-toolchain/uv.exe",
            "auth-toolchain/python-embed.zip",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_documents_host_agnostic_build_route(self) -> None:
        for contract in (
            "npm run prepare:package:win --workspace apps/desktop",
            "npm run build:setup:windows",
            "Windows VM",
            "host-appropriate invocation",
            "fixed host-specific",
        ):
            self.assertIn(contract, self.skill_text)
        for host_specific_instruction in (
            "--target_platform=win32",
            "node-pre-gyp install",
            "node_modules/electron-builder/out/cli/cli.js",
        ):
            self.assertNotIn(host_specific_instruction, self.skill_text)

    def test_skill_documents_bundled_source_and_first_run_lifecycle(self) -> None:
        for contract in (
            "hermes-backend.tar.gz",
            "payload-manifest.json",
            "BundledSource",
            "first-run Desktop stage",
            "without fetching the source from GitHub",
            "app.asar",
            "Ansatz.exe",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_requires_audit_and_windows_native_handoff(self) -> None:
        for contract in (
            "package-audit.mjs",
            "test:desktop:windows-contract",
            "windows-auth-toolchain.integration.test.mjs",
            "typecheck --workspace apps/desktop",
            "archive",
            "SHA-256",
            "Unsigned output",
            "Windows-native",
        ):
            self.assertIn(contract, self.skill_text)


if __name__ == "__main__":
    unittest.main()
