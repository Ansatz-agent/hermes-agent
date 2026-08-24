"""Contract tests for the Hermes macOS ARM64 DMG packaging skill."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "software-development"
    / "packaging-hermes-macos-arm64-dmg"
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


class PackagingHermesMacosArm64DmgSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_text = SKILL_MD.read_text(encoding="utf-8") if SKILL_MD.exists() else ""

    def test_skill_declares_macos_arm64_packaging_capability(self) -> None:
        self.assertTrue(SKILL_MD.exists(), f"missing skill: {SKILL_MD}")
        text = self.skill_text
        self.assertTrue(text.startswith("---\n"))
        self.assertRegex(text, r"(?m)^name: packaging-hermes-macos-arm64-dmg$")
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

    def test_skill_frontmatter_meets_repository_standard(self) -> None:
        for contract in (
            "version: 0.1.0",
            "author: yuxiaoy (Seauagain), Hermes Agent",
            "license: MIT",
            "tags: [desktop, packaging, macos, arm64, dmg, release-engineering]",
            "related_skills: [hermes-agent-skill-authoring, systematic-debugging]",
        ):
            self.assertIn(contract, self.skill_text)

        skills_root = SKILL_MD.parents[2]
        for related in ("hermes-agent-skill-authoring", "systematic-debugging"):
            self.assertTrue(list(skills_root.rglob(f"{related}/SKILL.md")))

    def test_skill_preserves_source_provenance_and_user_changes(self) -> None:
        for contract in (
            "isolated worktree",
            "git status --short",
            "git rev-parse HEAD",
            "40-character commit",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_uses_the_repository_dmg_contract(self) -> None:
        for contract in (
            "bash scripts/build-desktop-dmg.sh --check",
            "bash scripts/build-desktop-dmg.sh",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
            "uv lock --check",
            "dist:mac:dmg",
            "desktop-dmg-contract.mjs validate-log",
            "desktop-dmg-contract.mjs find-dmg",
        ):
            self.assertIn(contract, self.skill_text)

    def test_skill_requires_artifact_verification_and_clear_boundaries(self) -> None:
        for contract in (
            "codesign --verify --deep --strict",
            "hdiutil verify",
            "shasum -a 256",
            "not fully offline",
            "ad-hoc signed",
            "not notarized",
            "resource busy",
            "retry once",
            "fresh-machine",
        ):
            self.assertIn(contract, self.skill_text)


if __name__ == "__main__":
    unittest.main()
