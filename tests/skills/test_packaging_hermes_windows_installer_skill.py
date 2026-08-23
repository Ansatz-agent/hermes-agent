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

    def test_skill_declares_macos_packaging_capability(self) -> None:
        self.assertTrue(SKILL_MD.exists(), f"missing skill: {SKILL_MD}")
        text = self.skill_text
        self.assertTrue(text.startswith("---\n"))
        self.assertRegex(text, r"(?m)^name: packaging-hermes-windows-installer$")
        self.assertRegex(text, r"(?m)^platforms: \[macos\]$")

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
            "expected 40-character commit",
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
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_documents_the_verified_macos_cross_build_route(self) -> None:
        for contract in (
            "@mapbox/node-pre-gyp/bin/node-pre-gyp",
            "--target_platform=win32",
            "napi-9-win32-unknown-x64",
            "npm run build --workspace apps/desktop",
            "node_modules/electron-builder/out/cli/cli.js",
            "--win nsis --x64 --publish never",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_requires_audit_and_windows_native_handoff(self) -> None:
        for contract in (
            "package-audit.mjs",
            "test:desktop:windows-contract",
            "windows-auth-toolchain.integration.test.mjs",
            "typecheck --workspace apps/desktop",
            "bsdtar -tf",
            "shasum -a 256",
            "unsigned",
            "Windows-native",
        ):
            self.assertIn(contract, self.skill_text)


if __name__ == "__main__":
    unittest.main()
