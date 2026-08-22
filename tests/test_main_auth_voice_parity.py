from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_main_auth_voice_parity.py"
LEDGER = REPO / "docs" / "security" / "main-auth-voice-migration-ledger.json"
PRODUCT_PATHS = REPO / "docs" / "security" / "main-auth-voice-product-paths.txt"
REFERENCE = "80db6d8265f805cec46817d913982e4c5f6405c4"
REASONS = {
    "windows-adapter-addition",
    "package-producer-interface-extraction",
    "neutral-platform-wording",
    "dependency-lock-regeneration",
    "upstream-main-preservation",
    "auth-integration",
    "domestic-mirror-policy",
}


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, text=True, capture_output=True, check=False)


def invoke(
    repo: Path,
    *,
    reference: str,
    ledger: Path,
    product_paths: Path,
) -> subprocess.CompletedProcess[str]:
    return run(
        repo,
        sys.executable,
        str(CHECKER),
        "--repo",
        str(repo),
        "--reference",
        reference,
        "--ledger",
        str(ledger),
        "--product-paths",
        str(product_paths),
    )


def git(repo: Path, *args: str) -> str:
    result = run(repo, "git", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_fixture(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Parity Test")
    (repo / "product.txt").write_text("reference\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "product.test").write_text("contract\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "reference")
    reference = git(repo, "rev-parse", "HEAD")
    (repo / "product.txt").write_text("candidate\n", encoding="utf-8")
    git(repo, "add", "product.txt")
    git(repo, "commit", "-qm", "candidate")

    product_paths = repo / "product-paths.txt"
    product_paths.write_text("product.txt\n", encoding="utf-8")
    ledger = repo / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "dmg_reference": reference,
                "owner_enum": ["common-product"],
                "path_owners": [
                    {"match": "exact", "path": "product.txt", "owner": "common-product"}
                ],
                "parity_reason_enum": sorted(REASONS),
                "parity_waivers": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return repo, reference, ledger, product_paths


def test_checked_in_parity_contract_is_exact_and_current() -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert ledger["dmg_reference"] == REFERENCE
    assert set(ledger["parity_reason_enum"]) == REASONS

    waivers = ledger["parity_waivers"]
    assert len({waiver["path"] for waiver in waivers}) == len(waivers)
    for waiver in waivers:
        assert waiver["reason"] in REASONS
        assert waiver["owner"] in ledger["owner_enum"]
        assert waiver["reference"] == REFERENCE
        assert waiver["tests"]
        assert all((REPO / test).is_file() for test in waiver["tests"])

    result = invoke(
        REPO,
        reference=REFERENCE,
        ledger=LEDGER,
        product_paths=PRODUCT_PATHS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_checker_rejects_unwaived_product_drift(tmp_path: Path) -> None:
    repo, reference, ledger, product_paths = make_fixture(tmp_path)
    result = invoke(repo, reference=reference, ledger=ledger, product_paths=product_paths)
    assert result.returncode == 1
    assert "unwaived product drift: product.txt" in result.stdout


def test_checker_accepts_one_exact_path_specific_waiver(tmp_path: Path) -> None:
    repo, reference, ledger_path, product_paths = make_fixture(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["parity_waivers"] = [
        {
            "path": "product.txt",
            "reason": "auth-integration",
            "owner": "common-product",
            "reference": reference,
            "tests": ["tests/product.test"],
        }
    ]
    ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")
    result = invoke(repo, reference=reference, ledger=ledger_path, product_paths=product_paths)
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_rejects_directory_invalid_and_stale_waivers(tmp_path: Path) -> None:
    repo, reference, ledger_path, product_paths = make_fixture(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["parity_waivers"] = [
        {
            "path": "product.txt/",
            "reason": "made-up-reason",
            "owner": "common-product",
            "reference": reference,
            "tests": ["tests/missing.test"],
        }
    ]
    ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")
    result = invoke(repo, reference=reference, ledger=ledger_path, product_paths=product_paths)
    assert result.returncode == 1
    assert "waiver path must be exact" in result.stdout
    assert "invalid waiver reason" in result.stdout
    assert "waiver test does not exist" in result.stdout


def test_protected_entrypoints_are_a_superset_of_final_dmg() -> None:
    current = json.loads(
        (REPO / "hermes_cli/client_auth/entrypoints.json").read_text(encoding="utf-8")
    )
    reference_result = run(
        REPO,
        "git",
        "show",
        f"{REFERENCE}:hermes_cli/client_auth/entrypoints.json",
    )
    assert reference_result.returncode == 0, reference_result.stderr
    reference = json.loads(reference_result.stdout)
    current_ids = {entry["id"] for entry in current["entrypoints"]}
    reference_ids = {entry["id"] for entry in reference["entrypoints"]}
    assert current_ids >= reference_ids
