from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_main_auth_voice_migration.py"
LEDGER = REPO / "docs" / "security" / "main-auth-voice-migration-ledger.json"
PRODUCT_PATHS = REPO / "docs" / "security" / "main-auth-voice-product-paths.txt"

BASE = "9bd88c530716279a089ed18428dc785732b6e1be"
DMG_REFERENCE = "80db6d8265f805cec46817d913982e4c5f6405c4"
WINDOWS_REFERENCE = "c2d3d09aab921130171ff611e260c13e9c6d477c"
PLANNED_CANDIDATE_TIP = "b0a9dfc4e212cd9f25a0e9474e65f77f4adbf6f1"
TASK4_REPLAY_ORDER = [
    "89503cfb2b48a29f9996cf9dba2088d6b4a8d463",
    "08a2eedf67bcd6537dc048a77411cee5f21b1345",
    "706c5a4d0d034a8ab43914d45cef24f7f1d0fde2",
    "6aed1cc0f122cbe3aeba14203b11bcab2e915064",
    "e042ce38614967acd47cf70b00922e5d8d3ea308",
    "bffdd26edf190e6fa9798c3c8ff702baacfa4301",
    "f28a0f4b585899dae16bf181b9d036133ed35cc7",
    "8af9e3eefec781929fd16c788dba54d7f7cae254",
    "cdc12484f1326279c4fe215db453dd50b5c9e94c",
    "27567163b3759018af72030701dfb0f130ba146c",
    "366fb3f5a850abdd0f2e02ff014487deca1bc456",
    "cdb5c65bc8eca0d8fe37981ffc2e25e712a25ce4",
    "df34e9da62a19e7be209612da4c6fe6c9f022602",
    "c478db4d2f917a4216bee2827dcb6f2082702a1e",
    "e51e448669ac8a8e33dca12607382a1633f04b36",
    "f68510d9054119b0bd1de9db69411bfc3df3abbd",
    "817a5d0a6aef96027f3eda39a09b8648d058b5f0",
    "b22bdb8d310cca553218c706a5e61058838e94ee",
    "af19ce56b1a94792d084c2bb40ea98a8b570a80b",
    "50609240e290cb85b4d2024973b875cb5ffe642b",
    "bddbec2abb469700abbb5f603d48badae04102d9",
    "6e57ead9693ab16f8a6e9d3b4fabf87d1c0927a6",
    "c4d5ae2d4087f5a92290597562e93456c0d0b802",
    "f8d7c05fadcc0d405f0f5e89c2af3508a948c277",
    "34026f76276d2f4667652036e59751c7829b9bdd",
    "a41608712672b766f0855e658f7d09ffaf76cc25",
    "34f1c119e212d993f8685635ab44d92ca273e2e3",
    "c8fad50c49260feb0373aceb8a509a829e70c390",
    "363d464a85c8a6ce408a4af34963b3426a0fcba3",
    "a756c93e35b7d15037cfcb11de83d6198aa5f04c",
    "f5a7372e4c20d3fa1c66bacbebba2d51bf2956bb",
    "c893e264e99de1ee0293688ce59223d57f432a17",
    "38230a6c9fbe24382782cb52b4d3adc638f51cd0",
    "4e4a5d42c785e25f940531a9831ff2c591bfb412",
]

OWNER_ENUM = {
    "common-product",
    "package-shared",
    "package-macos",
    "package-windows",
    "ci-infrastructure",
    "test-evidence",
    "historical-drop",
    "reference-equivalent",
}
STRATEGY_ENUM = {
    "candidate",
    "merge",
    "cherry-pick",
    "path-extract",
    "reference-equivalent",
    "drop",
}
PRODUCT_OWNERS = {
    "common-product",
    "package-shared",
    "package-macos",
    "package-windows",
}


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def git_lines(repo: Path, *args: str) -> list[str]:
    result = run(repo, "git", *args)
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line]


def load_ledger() -> dict[str, object]:
    assert LEDGER.is_file(), f"missing migration ledger: {LEDGER}"
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def invoke(
    repo: Path,
    *,
    base: str,
    ledger: Path,
    product_paths: Path,
) -> subprocess.CompletedProcess[str]:
    assert CHECKER.is_file(), f"missing migration checker: {CHECKER}"
    return run(
        repo,
        sys.executable,
        str(CHECKER),
        "--repo",
        str(repo),
        "--base",
        base,
        "--ledger",
        str(ledger),
        "--product-paths",
        str(product_paths),
    )


def test_checked_in_ledger_has_locked_authorities_and_exact_enums() -> None:
    ledger = load_ledger()

    assert ledger["schema_version"] == 1
    assert ledger["base"] == BASE
    assert ledger["dmg_reference"] == DMG_REFERENCE
    assert ledger["windows_reference"] == WINDOWS_REFERENCE
    assert ledger["candidate_tip"] == PLANNED_CANDIDATE_TIP
    assert ledger["task4_replay_order"] == TASK4_REPLAY_ORDER
    assert set(ledger["owner_enum"]) == OWNER_ENUM
    assert set(ledger["strategy_enum"]) == STRATEGY_ENUM
    assert ledger["enforce_locked_commit_coverage"] is True
    assert ledger["common_baseline"] == "4ef56cef4c6eecc009e2284fe2f1df20664f357a"
    assert ledger["dmg_integration_reference"] == "403e1c3873d1679720c1403d7e38acd289804d69"
    assert ledger["windows_integration_reference"] == "56b402c63b22da81f906ff1f7398a90cfd17bd81"
    assert ledger["post_tip_commits"] == [
        "a6e8792b0c701287eecee97c692956e10a35b5d8",
        "11bcf8976dc3f9511b4d021da686b4236b52e68c",
        "ff25f03ef33b7943269f5f8b62066040277b2186",
        "0cf5c3c23e125e668bda27f8492e702e98bf6c3c",
        "83223adbcc56d4a960acc806d84a550f4509055b",
        "e9501893d88243105e87ae758c0574d3ee46e0e6",
        "6c7b59a1ac422d3dc5f59d4192bb238d8968293c",
        "fe5ab1a6845db6b6305dd8fbc916061082c70848",
    ]
    assert ledger["contract_bookkeeping_paths"] == [
        "docs/security/hermes-managed-download-origins.json",
        "docs/security/main-auth-voice-migration-ledger.json",
        "docs/security/main-auth-voice-product-paths.txt",
        "scripts/check_hermes_managed_downloads.py",
        "scripts/check_main_auth_voice_migration.py",
        "scripts/check_main_auth_voice_parity.py",
        "tests/test_hermes_managed_downloads.py",
        "tests/test_main_auth_voice_dependencies.py",
        "tests/test_main_auth_voice_migration.py",
        "tests/test_main_auth_voice_parity.py",
    ]


def test_every_locked_source_commit_has_one_owner_and_strategy() -> None:
    ledger = load_ledger()
    commits = ledger["commits"]
    assert isinstance(commits, list)

    actual = [entry["sha"] for entry in commits]
    assert len(actual) == len(set(actual)), "a commit is classified more than once"
    for entry in commits:
        assert entry["owner"] in OWNER_ENUM
        assert entry["strategy"] in STRATEGY_ENUM
        if entry["strategy"] == "path-extract":
            assert entry["paths"], f"path-extract lacks exhaustive paths: {entry['sha']}"

    expected = set(
        git_lines(REPO, "log", "--reverse", "--format=%H", "4ef56cef4c..403e1c3873")
        + git_lines(REPO, "log", "--reverse", "--format=%H", "4ef56cef4c..56b402c63b")
        + git_lines(
            REPO,
            "log",
            "--reverse",
            "--format=%H",
            f"ansatz/main..{ledger['candidate_tip']}",
        )
    )
    assert set(actual) == expected
    assert run(
        REPO,
        "git",
        "merge-base",
        "--is-ancestor",
        str(ledger["candidate_tip"]),
        "HEAD",
    ).returncode == 0


def test_product_path_manifest_equals_ledger_projection() -> None:
    ledger = load_ledger()
    path_owners = ledger["path_owners"]
    assert isinstance(path_owners, list)

    paths = [entry["path"] for entry in path_owners]
    assert len(paths) == len(set(paths)), "a path is listed under two owners"
    assert all(entry["owner"] in OWNER_ENUM for entry in path_owners)

    expected = sorted(
        entry["path"]
        for entry in path_owners
        if entry["owner"] in PRODUCT_OWNERS
    )
    assert PRODUCT_PATHS.is_file(), f"missing product path manifest: {PRODUCT_PATHS}"
    actual = [
        line
        for line in PRODUCT_PATHS.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert actual == sorted(actual)
    assert actual == expected
    assert all(not path.endswith("/") for path in actual), (
        "product ownership must use exact paths so test/CI descendants cannot be packaged"
    )
    assert all(
        not path.startswith((".github/", "tests/"))
        and (
            not path.startswith("docs/")
            or path == "docs/security/hermes-managed-download-origins.json"
        )
        and "/e2e/" not in path
        and ".test." not in path
        and ".spec." not in path
        for path in actual
    )


def test_current_candidate_has_one_owner_and_no_generated_or_secret_artifacts() -> None:
    result = invoke(
        REPO,
        base="ansatz/main",
        ledger=LEDGER,
        product_paths=PRODUCT_PATHS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_checker_itself_rejects_incomplete_locked_commit_coverage(
    tmp_path: Path,
) -> None:
    ledger = load_ledger()
    ledger["commits"] = ledger["commits"][1:]
    bad_ledger = tmp_path / "ledger.json"
    bad_ledger.write_text(json.dumps(ledger) + "\n", encoding="utf-8")

    result = invoke(
        REPO,
        base="ansatz/main",
        ledger=bad_ledger,
        product_paths=PRODUCT_PATHS,
    )

    assert result.returncode != 0
    assert "commit coverage" in result.stderr.lower()


def write_locked_fixture_contract(
    tmp_path: Path,
    *,
    base: str,
    owned_paths: list[str],
    post_tip_commits: list[str],
    post_tip_subjects: list[str] | None = None,
) -> tuple[Path, Path]:
    ledger, product = write_fixture_contract(
        tmp_path,
        base=base,
        owned_paths=owned_paths,
    )
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value.update(
        {
            "enforce_locked_commit_coverage": True,
            "common_baseline": base,
            "dmg_integration_reference": base,
            "windows_integration_reference": base,
            "candidate_tip": base,
            "contract_bookkeeping_paths": ["docs/security/migration-contract.json"],
            "post_tip_commits": post_tip_commits,
        }
    )
    if post_tip_subjects is not None:
        value["post_tip_subjects"] = post_tip_subjects
    ledger.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return ledger, product


def commit_fixture_path(tmp_path: Path, path: str, subject: str) -> str:
    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("candidate\n", encoding="utf-8")
    assert run(tmp_path, "git", "add", path).returncode == 0
    assert run(tmp_path, "git", "commit", "-qm", subject).returncode == 0
    return git_lines(tmp_path, "rev-parse", "HEAD")[0]


def test_locked_coverage_rejects_reused_planned_subject(tmp_path: Path) -> None:
    base = initialize_fixture_repo(tmp_path)
    path = "src/owned.py"
    subject = "fix: reconcile main packaging and voice baseline"
    commit_fixture_path(tmp_path, path, subject)
    ledger, product = write_locked_fixture_contract(
        tmp_path,
        base=base,
        owned_paths=[path],
        post_tip_commits=[],
        post_tip_subjects=[subject],
    )

    result = invoke(tmp_path, base=base, ledger=ledger, product_paths=product)

    assert result.returncode != 0
    assert "post_tip_subjects is forbidden" in result.stderr


def test_locked_coverage_accepts_exact_registered_post_tip_commit(
    tmp_path: Path,
) -> None:
    base = initialize_fixture_repo(tmp_path)
    path = "src/owned.py"
    sha = commit_fixture_path(tmp_path, path, "implementation commit")
    ledger, product = write_locked_fixture_contract(
        tmp_path,
        base=base,
        owned_paths=[path],
        post_tip_commits=[sha],
    )

    result = invoke(tmp_path, base=base, ledger=ledger, product_paths=product)

    assert result.returncode == 0, result.stdout + result.stderr


def test_locked_coverage_accepts_registered_commits_imported_by_merge(
    tmp_path: Path,
) -> None:
    base = initialize_fixture_repo(tmp_path)
    path = "src/auth.py"
    assert run(tmp_path, "git", "switch", "-qc", "auth-feature").returncode == 0
    source_sha = commit_fixture_path(tmp_path, path, "auth source")
    assert run(tmp_path, "git", "switch", "-").returncode == 0
    assert run(
        tmp_path,
        "git",
        "merge",
        "--no-ff",
        "auth-feature",
        "-m",
        "merge auth source",
    ).returncode == 0

    ledger, product = write_locked_fixture_contract(
        tmp_path,
        base=base,
        owned_paths=[path],
        post_tip_commits=[],
    )
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value["dmg_integration_reference"] = source_sha
    value["commits"] = [
        {
            "sha": source_sha,
            "owner": "common-product",
            "strategy": "merge",
            "paths": [],
        }
    ]
    ledger.write_text(json.dumps(value) + "\n", encoding="utf-8")

    result = invoke(tmp_path, base=base, ledger=ledger, product_paths=product)

    assert result.returncode == 0, result.stdout + result.stderr


def initialize_fixture_repo(tmp_path: Path) -> str:
    assert run(tmp_path, "git", "init", "-q").returncode == 0
    assert run(tmp_path, "git", "config", "user.email", "migration@example.invalid").returncode == 0
    assert run(tmp_path, "git", "config", "user.name", "Migration Test").returncode == 0
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    assert run(tmp_path, "git", "add", "base.txt").returncode == 0
    assert run(tmp_path, "git", "commit", "-qm", "base").returncode == 0
    return git_lines(tmp_path, "rev-parse", "HEAD")[0]


def write_fixture_contract(
    tmp_path: Path,
    *,
    base: str,
    owned_paths: list[str],
) -> tuple[Path, Path]:
    contract_dir = tmp_path.parent / f"{tmp_path.name}-contracts"
    contract_dir.mkdir()
    ledger = contract_dir / "ledger.json"
    product = contract_dir / "product.txt"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base": base,
                "dmg_reference": DMG_REFERENCE,
                "windows_reference": WINDOWS_REFERENCE,
                "candidate_tip": base,
                "owner_enum": sorted(OWNER_ENUM),
                "strategy_enum": sorted(STRATEGY_ENUM),
                "commits": [],
                "path_owners": [
                    {"path": path, "owner": "common-product"}
                    for path in owned_paths
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    product.write_text("".join(f"{path}\n" for path in sorted(owned_paths)), encoding="utf-8")
    return ledger, product


@pytest.mark.parametrize("state", ["committed", "staged", "unstaged", "untracked"])
def test_checker_inspects_all_candidate_states(tmp_path: Path, state: str) -> None:
    base = initialize_fixture_repo(tmp_path)
    path = "src/owned.py"
    candidate = tmp_path / path
    candidate.parent.mkdir()

    if state == "unstaged":
        candidate.write_text("first\n", encoding="utf-8")
        assert run(tmp_path, "git", "add", path).returncode == 0
        assert run(tmp_path, "git", "commit", "-qm", "owned").returncode == 0
        candidate.write_text("second\n", encoding="utf-8")
    else:
        candidate.write_text("candidate\n", encoding="utf-8")
        if state in {"committed", "staged"}:
            assert run(tmp_path, "git", "add", path).returncode == 0
        if state == "committed":
            assert run(tmp_path, "git", "commit", "-qm", "owned").returncode == 0

    ledger, product = write_fixture_contract(tmp_path, base=base, owned_paths=[path])
    result = invoke(tmp_path, base=base, ledger=ledger, product_paths=product)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "src/unowned.py",
        "apps/desktop/release/Hermes.dmg",
        "dist/Hermes.pkg",
        "dist/Hermes.exe",
        "dist/Hermes.msi",
        "dist/Hermes.zip",
        "apps/desktop/build/bootstrap/hermes-backend.tar.gz",
        "apps/desktop/build/bootstrap/git-bash-runtime.tar.xz",
        "apps/desktop/build/bootstrap/uv.gz",
        "apps/desktop/build/bootstrap/auth.whl",
        "apps/desktop/release/Hermes.dmg.blockmap",
        "apps/desktop/build/unexpected.txt",
        ".env",
        "credentials.json",
        "apps/desktop/build/logs/raw-session.log",
        "tests/.artifacts/keychain-session.txt",
    ],
)
def test_checker_rejects_unowned_generated_or_sensitive_paths(
    tmp_path: Path,
    path: str,
) -> None:
    base = initialize_fixture_repo(tmp_path)
    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("candidate\n", encoding="utf-8")
    ledger, product = write_fixture_contract(tmp_path, base=base, owned_paths=[])

    result = invoke(tmp_path, base=base, ledger=ledger, product_paths=product)

    assert result.returncode != 0
    assert path in result.stderr
