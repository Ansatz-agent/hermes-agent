from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.memory.profile_memory import ProfileMemoryProvider, _normalize_scope
from plugins.memory.profile_memory.store import ProfileStore


class FakeEmbedder:
    model_id = "fake-bge"

    @staticmethod
    def embed(text: str, *, is_query: bool = False):
        value = text.casefold()
        if any(token in value for token in ("latex", "equation", "方程", "公式")):
            return [1.0, 0.0, 0.0]
        if any(token in value for token in ("python", "test", "测试")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def close(self):
        pass


class FakeLlm:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.parsed, Exception):
            raise self.parsed
        return SimpleNamespace(
            parsed=self.parsed,
            provider="active-provider",
            model="active-main-model",
            usage=SimpleNamespace(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
            ),
        )


def make_provider(tmp_path: Path, *, llm=None, config=None):
    store = ProfileStore(tmp_path / "profile.sqlite3")
    provider = ProfileMemoryProvider(
        llm=llm,
        config={
            "score_threshold": 0.5,
            "recall_limit": 3,
            "inferred_activation_sessions": 2,
            **(config or {}),
        },
        store=store,
        embedder=FakeEmbedder(),
    )
    provider.initialize("session-1", hermes_home=str(tmp_path), agent_context="primary")
    return provider, store


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("artifacts/latex/equations", "artifacts/latex/equations"),
        ("工作流/LaTeX 排版", "工作流/latex-排版"),
        ("../private", ""),
        ("a//b", ""),
        ("https://example.test", ""),
    ],
)
def test_scope_is_open_vocabulary_but_path_safe(raw, expected):
    normalized, error = _normalize_scope(raw)
    assert normalized == expected
    assert bool(error) is (not bool(expected))


def test_explicit_atomic_items_are_stored_embedded_and_recalled(tmp_path):
    provider, store = make_provider(tmp_path)

    first = json.loads(
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": "artifacts/latex/source-layout",
                "applies_when": "writing LaTeX source for the user",
                "rule": "Do not insert arbitrary source line breaks.",
                "evidence": "以后写 LaTeX 时不要随意换行。",
            },
        )
    )
    second = json.loads(
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": "artifacts/latex/equations",
                "applies_when": "writing displayed LaTeX equations",
                "rule": "Use the equation environment for displayed equations.",
                "evidence": "以后公式使用 equation 环境。",
            },
        )
    )
    provider.handle_tool_call(
        "profile_remember",
        {
            "kind": "workflow_preference",
            "scope": "engineering/python/tests",
            "applies_when": "adding Python regression tests",
            "rule": "Prefer behavior-contract tests.",
            "evidence": "写 Python 测试时优先验证行为契约。",
        },
    )

    assert first["profile_status"] == "active"
    assert second["profile_status"] == "active"
    assert first["category"] == (
        "profile://preferences/workflow/artifacts/latex/source-layout"
    )
    assert store.stats() == {
        "active": 3,
        "candidate": 0,
        "superseded": 0,
        "revoked": 0,
        "evidence": 3,
    }
    context = provider.prefetch("请给我写一段包含展示公式的 LaTeX")
    assert "equation environment" in context
    assert "arbitrary source line breaks" in context
    assert "behavior-contract" not in context
    assert context.count("\n- [id=") <= 3
    assert provider.recall_status().count >= 1
    assert (tmp_path / "profile_memory" / "PROFILE.md").exists()


def test_categories_are_normalized_nodes_and_user_can_browse_a_subtree(tmp_path):
    provider, store = make_provider(tmp_path)
    for scope, rule in (
        ("artifacts/latex/source-layout", "Keep LaTeX source paragraphs intact."),
        ("artifacts/latex/equations", "Use equation for displayed formulas."),
        ("engineering/python/tests", "Test behavior contracts."),
    ):
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": scope,
                "applies_when": f"working in {scope}",
                "rule": rule,
                "evidence": f"Remember {rule}",
            },
        )

    paths = {node["path"]: node for node in store.list_scope_nodes()}
    latex = paths["preferences/workflow/artifacts/latex"]
    assert latex["subtree_active_count"] == 2
    assert (
        paths["preferences/workflow/artifacts/latex/equations"]["parent_id"]
        == latex["scope_id"]
    )
    assert paths["preferences/workflow/artifacts/latex/equations"]["active_count"] == 1
    assert paths["preferences/workflow/engineering/python/tests"]["active_count"] == 1

    result = json.loads(
        provider.handle_tool_call(
            "profile_browse",
            {"scope": "profile://preferences/workflow/artifacts/latex"},
        )
    )
    assert result["requested_scope"] == (
        "profile://preferences/workflow/artifacts/latex"
    )
    assert result["resolved_scope"] == (
        "profile://preferences/workflow/artifacts/latex"
    )
    assert result["scope_found"] is True
    assert {item["rule"] for item in result["items"]} == {
        "Keep LaTeX source paragraphs intact.",
        "Use equation for displayed formulas.",
    }
    assert any(
        category["uri"] == "profile://preferences/workflow/artifacts/latex/equations"
        for category in result["categories"]
    )
    short_result = json.loads(
        provider.handle_tool_call("profile_browse", {"scope": "latex"})
    )
    assert short_result["resolved_scope"] == (
        "profile://preferences/workflow/artifacts/latex"
    )
    assert {item["item_id"] for item in short_result["items"]} == {
        item["item_id"] for item in result["items"]
    }
    assert any(
        schema["name"] == "profile_browse" for schema in provider.get_tool_schemas()
    )

    snapshot = (tmp_path / "profile_memory" / "PROFILE.md").read_text(encoding="utf-8")
    assert "## Category tree" in snapshot
    assert "profile://preferences/workflow/artifacts/latex/equations" in snapshot


def test_short_browse_scope_reports_ambiguous_category_matches(tmp_path):
    provider, _store = make_provider(tmp_path)
    for scope in ("artifacts/latex", "projects/paper/latex"):
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": scope,
                "applies_when": f"working in {scope}",
                "rule": f"Rule for {scope}.",
                "evidence": f"Remember the rule for {scope}.",
            },
        )

    result = json.loads(provider.handle_tool_call("profile_browse", {"scope": "latex"}))

    assert result["error"].startswith("profile category is ambiguous")
    assert result["candidates"] == [
        "profile://preferences/workflow/artifacts/latex",
        "profile://preferences/workflow/projects/paper/latex",
    ]


def test_read_only_initialization_skips_local_embedding_model(monkeypatch, tmp_path):
    def fail_if_loaded(**_kwargs):
        raise AssertionError("read-only profile browsing must not load BGE")

    monkeypatch.setattr(
        "plugins.memory.profile_memory.LocalBgeEmbedder", fail_if_loaded
    )
    provider = ProfileMemoryProvider(
        config={
            "db_path": str(tmp_path / "profile.sqlite3"),
            "semantic_recall": True,
        }
    )

    provider.initialize(
        "profile-cli-read",
        hermes_home=str(tmp_path),
        agent_context="primary",
        read_only=True,
    )
    result = json.loads(provider.handle_tool_call("profile_browse", {}))

    assert result["resolved_scope"] == "profile://"
    assert result["scope_found"] is True
    assert {category["uri"] for category in result["categories"]} >= {
        "profile://identity",
        "profile://preferences/interaction",
        "profile://preferences/workflow",
    }
    provider.shutdown()


def test_profile_kinds_are_placed_under_stable_directory_roots(tmp_path):
    provider, store = make_provider(tmp_path)
    identity = json.loads(
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "identity",
                "scope": "professional/role",
                "applies_when": "adapting technical depth",
                "rule": "The user develops AI agents.",
                "evidence": "我在开发 AI agent。",
            },
        )
    )
    interaction = json.loads(
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "interaction_preference",
                "scope": "communication/answer-structure",
                "applies_when": "answering implementation questions",
                "rule": "Lead with the exact conclusion.",
                "evidence": "以后先给我准确结论。",
            },
        )
    )

    assert identity["category"] == "profile://identity/professional/role"
    assert interaction["category"] == (
        "profile://preferences/interaction/communication/answer-structure"
    )
    paths = {node["path"] for node in store.list_scope_nodes()}
    assert "identity/professional/role" in paths
    assert "preferences/interaction/communication/answer-structure" in paths

    filtered = json.loads(
        provider.handle_tool_call("profile_browse", {"kind": "interaction_preference"})
    )
    assert [item["item_id"] for item in filtered["items"]] == [interaction["item_id"]]


def test_directory_route_filters_unrelated_workflow_branches_before_item_ranking(
    tmp_path,
):
    provider, _store = make_provider(tmp_path)
    for scope, rule in (
        ("artifacts/latex/equations", "Use convention alpha."),
        ("engineering/python/tests", "Use convention beta."),
    ):
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": scope,
                "applies_when": "creating an artifact",
                "rule": rule,
                "evidence": f"Always {rule}",
            },
        )

    context = provider.prefetch("请写一个 LaTeX 展示公式")
    assert "convention alpha" in context
    assert "convention beta" not in context
    assert (
        "category=profile://preferences/workflow/artifacts/latex/equations" in context
    )


def _store_latex_equation_preference(provider):
    provider.handle_tool_call(
        "profile_remember",
        {
            "kind": "workflow_preference",
            "scope": "artifacts/latex/equations",
            "applies_when": "writing displayed equations in the current paper",
            "rule": "Use the equation environment.",
            "evidence": "当前论文的独立公式始终使用 equation 环境。",
        },
    )


def test_intent_gate_skips_before_profile_item_retrieval(tmp_path):
    llm = FakeLlm(
        {
            "decision": "skip",
            "confidence": 0.97,
            "reason": "The current turn explicitly excludes the paper workflow.",
        }
    )
    provider, store = make_provider(tmp_path, llm=llm, config={"intent_gate": True})
    _store_latex_equation_preference(provider)

    def fail_if_items_are_retrieved(*_args, **_kwargs):
        raise AssertionError("intent gate must run before item retrieval")

    store.list_items = fail_if_items_are_retrieved
    context = provider.prefetch("现在不是在写论文，也不要公式或源码。请用两句普通中文解释样本均值。")

    assert context == ""
    assert provider.recall_status() is None
    assert provider.intent_gate_status() == {
        "decision": "skip",
        "confidence": 0.97,
        "minimum_skip_confidence": 0.85,
        "skipped": True,
        "fail_open": False,
        "reason": "The current turn explicitly excludes the paper workflow.",
        "provider": "active-provider",
        "model": "active-main-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert llm.calls[0]["schema_name"] == "profile_memory_retrieval_intent"
    assert llm.calls[0]["purpose"] == "profile-memory retrieval intent gate"


def test_intent_gate_retrieves_when_profile_may_help(tmp_path):
    llm = FakeLlm(
        {
            "decision": "retrieve",
            "confidence": 0.99,
            "reason": "A paper LaTeX workflow preference may apply.",
        }
    )
    provider, _store = make_provider(tmp_path, llm=llm, config={"intent_gate": True})
    _store_latex_equation_preference(provider)

    context = provider.prefetch("请为当前论文写一个 LaTeX 展示公式。")

    assert "Use the equation environment" in context
    assert provider.intent_gate_status()["skipped"] is False
    assert provider.recall_status().count == 1


def test_intent_gate_low_confidence_skip_fails_open(tmp_path):
    llm = FakeLlm(
        {
            "decision": "skip",
            "confidence": 0.60,
            "reason": "Possibly self-contained, but uncertain.",
        }
    )
    provider, _store = make_provider(
        tmp_path,
        llm=llm,
        config={
            "intent_gate": True,
            "intent_gate_min_skip_confidence": 0.85,
        },
    )
    _store_latex_equation_preference(provider)

    context = provider.prefetch("请为当前论文写一个 LaTeX 展示公式。")

    assert "Use the equation environment" in context
    assert provider.intent_gate_status()["decision"] == "skip"
    assert provider.intent_gate_status()["skipped"] is False


def test_intent_gate_failure_fails_open(tmp_path):
    llm = FakeLlm(RuntimeError("temporary gate failure"))
    provider, _store = make_provider(tmp_path, llm=llm, config={"intent_gate": True})
    _store_latex_equation_preference(provider)

    context = provider.prefetch("请为当前论文写一个 LaTeX 展示公式。")

    assert "Use the equation environment" in context
    assert provider.intent_gate_status()["fail_open"] is True
    assert provider.intent_gate_status()["decision"] == "retrieve"


def test_intent_gate_is_disabled_by_default(tmp_path):
    llm = FakeLlm(AssertionError("disabled gate must not call the LLM"))
    provider, _store = make_provider(tmp_path, llm=llm)
    _store_latex_equation_preference(provider)

    context = provider.prefetch("请为当前论文写一个 LaTeX 展示公式。")

    assert "Use the equation environment" in context
    assert provider.intent_gate_status() is None
    assert llm.calls == []


def test_legacy_flat_scope_database_is_migrated_without_duplicate_items(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE profile_items (
            item_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            applies_when TEXT NOT NULL,
            rule TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            explicit INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            supersedes_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO profile_items
            (item_id, kind, scope, applies_when, rule, status, confidence,
             explicit, evidence_count, content_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', 1.0, 1, 0, ?, ?, ?)
        """,
        (
            "a" * 32,
            "workflow_preference",
            "artifacts/latex/equations",
            "writing displayed equations",
            "Use equation.",
            "legacy-hash",
            time.time(),
            time.time(),
        ),
    )
    conn.execute(
        """
        INSERT INTO profile_items
            (item_id, kind, scope, applies_when, rule, status, confidence,
             explicit, evidence_count, content_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'candidate', 0.8, 0, 0, ?, ?, ?)
        """,
        (
            "b" * 32,
            "workflow_preference",
            "preferences/workflow/artifacts/latex/equations",
            "writing displayed equations",
            "Use equation.",
            "second-legacy-hash",
            time.time(),
            time.time(),
        ),
    )
    conn.commit()
    conn.close()

    store = ProfileStore(db_path)
    migrated = store.get_item("a" * 32)
    assert migrated["scope"] == "preferences/workflow/artifacts/latex/equations"
    assert migrated["scope_id"]
    assert store.get_scope_node(migrated["scope_id"])["path"] == migrated["scope"]

    same = store.upsert_item(
        kind="workflow_preference",
        scope="artifacts/latex/equations",
        applies_when="writing displayed equations",
        rule="Use equation.",
        confidence=1.0,
        explicit=True,
        evidence_ids=[],
    )
    assert same["item_id"] == "a" * 32
    assert store.stats()["active"] == 1
    assert store.stats()["superseded"] == 1
    store.close()


def test_inferred_item_needs_distinct_sessions_to_activate(tmp_path):
    store = ProfileStore(tmp_path / "profile.sqlite3")
    first_evidence = store.add_evidence(
        session_id="s1",
        turn_index=1,
        user_text="first observation",
        source_type="main_model_extraction",
    )
    item = store.upsert_item(
        kind="interaction_preference",
        scope="communication/answer-structure",
        applies_when="answering a complex question",
        rule="Lead with the concrete outcome.",
        confidence=0.8,
        explicit=False,
        evidence_ids=[first_evidence],
        activation_sessions=2,
    )
    assert item["status"] == "candidate"

    repeated_same_session = store.add_evidence(
        session_id="s1",
        turn_index=2,
        user_text="second observation",
        source_type="main_model_extraction",
    )
    item = store.upsert_item(
        kind=item["kind"],
        scope=item["scope"],
        applies_when=item["applies_when"],
        rule=item["rule"],
        confidence=0.8,
        explicit=False,
        evidence_ids=[repeated_same_session],
        activation_sessions=2,
    )
    assert item["status"] == "candidate"

    second_session = store.add_evidence(
        session_id="s2",
        turn_index=1,
        user_text="third observation",
        source_type="main_model_extraction",
    )
    item = store.upsert_item(
        kind=item["kind"],
        scope=item["scope"],
        applies_when=item["applies_when"],
        rule=item["rule"],
        confidence=0.8,
        explicit=False,
        evidence_ids=[second_session],
        activation_sessions=2,
    )
    assert item["status"] == "active"
    assert item["evidence_count"] == 3


def test_main_model_extraction_uses_user_turns_and_writes_atomic_items(tmp_path):
    llm = FakeLlm(
        {
            "items": [
                {
                    "operation": "add",
                    "kind": "workflow_preference",
                    "scope": "artifacts/latex/source-layout",
                    "applies_when": "writing LaTeX source",
                    "rule": "Do not insert arbitrary source line breaks.",
                    "durability": "explicit",
                    "confidence": 1.0,
                    "evidence_turns": [1],
                },
                {
                    "operation": "add",
                    "kind": "workflow_preference",
                    "scope": "artifacts/latex/equations",
                    "applies_when": "writing displayed LaTeX equations",
                    "rule": "Use the equation environment.",
                    "durability": "explicit",
                    "confidence": 1.0,
                    "evidence_turns": [1],
                },
            ]
        }
    )
    provider, store = make_provider(tmp_path, llm=llm)
    messages = [
        {
            "role": "user",
            "content": "以后给我写 LaTeX 时不要随意换行，而且展示公式用 equation 环境。",
        },
        {"role": "assistant", "content": "I will also invent an unrelated preference."},
    ]

    provider.on_session_end(messages)
    provider.on_session_end(messages)

    assert len(llm.calls) == 1
    input_text = llm.calls[0]["input"][0]["text"]
    assert "不要随意换行" in input_text
    assert "invent an unrelated preference" not in input_text
    assert "existing_scope_paths" in json.loads(input_text)
    assert llm.calls[0]["schema_name"] == "profile_memory_extraction"
    assert store.stats()["active"] == 2
    assert store.stats()["evidence"] == 1
    assert {item["scope"] for item in store.list_items()} == {
        "preferences/workflow/artifacts/latex/source-layout",
        "preferences/workflow/artifacts/latex/equations",
    }


def test_supersede_marks_old_item_and_recall_uses_only_replacement(tmp_path):
    provider, store = make_provider(tmp_path)
    old = json.loads(
        provider.handle_tool_call(
            "profile_remember",
            {
                "kind": "workflow_preference",
                "scope": "artifacts/latex/equations",
                "applies_when": "writing display equations",
                "rule": "Use displaymath.",
                "evidence": "之前我说用 displaymath。",
            },
        )
    )
    provider.handle_tool_call(
        "profile_remember",
        {
            "kind": "workflow_preference",
            "scope": "artifacts/latex/equations",
            "applies_when": "writing display equations",
            "rule": "Use the equation environment instead.",
            "evidence": "纠正一下，以后改用 equation 环境。",
            "supersedes_id": old["item_id"],
        },
    )

    assert store.get_item(old["item_id"])["status"] == "superseded"
    context = provider.prefetch("写一个 LaTeX 展示公式")
    assert "equation environment instead" in context
    assert "Use displaymath" not in context


def test_failed_extraction_is_nonfatal_and_can_be_retried(tmp_path):
    llm = FakeLlm(RuntimeError("temporary main-model failure"))
    provider, store = make_provider(tmp_path, llm=llm)
    messages = [{"role": "user", "content": "以后回答都先给结论。"}]

    provider.on_session_end(messages)
    provider.on_session_end(messages)

    assert len(llm.calls) == 2
    assert store.stats()["active"] == 0


def test_snapshot_is_audit_only_not_recall_source(tmp_path):
    provider, _store = make_provider(tmp_path)
    provider.handle_tool_call(
        "profile_remember",
        {
            "kind": "workflow_preference",
            "scope": "artifacts/latex/equations",
            "applies_when": "writing LaTeX equations",
            "rule": "Use equation.",
            "evidence": "以后用 equation。",
        },
    )
    snapshot = tmp_path / "profile_memory" / "PROFILE.md"
    snapshot.unlink()

    assert "Use equation" in provider.prefetch("写 LaTeX 公式")
    assert not snapshot.exists()
