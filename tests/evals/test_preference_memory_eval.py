from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evals.preference_memory.dataset import DatasetError, load_dataset
from evals.preference_memory.graders import grade_response
from evals.preference_memory.report import (
    ResultSet,
    compare_variants,
    load_result_sets,
    render_html,
    render_markdown,
    summarize,
)
from evals.preference_memory.runner import (
    DEFAULT_DATASET,
    RunSettings,
    _audit_memory_provider,
    _count_message_metrics,
    _extract_memory_context,
    _isolated_hermes_environment,
    _load_current_config_overrides,
    _memory_marker_result,
    _run_full_history,
    _run_live_session,
    _run_native_memory,
    main as runner_main,
)


IDEAL_RESPONSES = DEFAULT_DATASET.parent.parent / "fixtures" / "ideal_responses.json"


def test_fixed_dataset_has_balanced_scope_pairs():
    dataset = load_dataset(DEFAULT_DATASET)

    assert len(dataset.cases) == 16
    categories = {case.category for case in dataset.cases}
    assert len(categories) == 8
    for category in categories:
        applicable = [
            case.applicable for case in dataset.cases if case.category == category
        ]
        assert applicable.count(True) == 1
        assert applicable.count(False) == 1


def test_distractor_materialization_is_deterministic_and_distance_exact():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]

    first = dataset.intervening_turns(case, 30, 42)
    second = dataset.intervening_turns(case, 30, 42)
    shifted = dataset.intervening_turns(case, 30, 44)

    assert len(first) == 30
    assert first == second
    assert first != shifted


def test_memory_recall_markers_accept_language_alternatives():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]

    assert _memory_marker_result(case, "use equation and keep one physical line")
    assert _memory_marker_result(case, "使用 equation 并保持一个物理行")
    assert not _memory_marker_result(case, "use equation")


def test_message_metrics_count_structured_profile_writes():
    metrics = _count_message_metrics(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "profile_remember"}},
                    {"function": {"name": "terminal"}},
                ],
            }
        ]
    )

    assert metrics == {"api_turns": 1, "tool_calls": 2, "memory_tool_calls": 1}


def test_live_session_raises_on_terminal_provider_failure(monkeypatch):
    closed = []

    class FailedAgent:
        def run_conversation(self, prompt, conversation_history):
            return {
                "final_response": "API call failed after 3 retries: HTTP 503",
                "messages": [{"role": "user", "content": prompt}],
                "failed": True,
                "error": "HTTP 503: temporarily unavailable",
                "failure_reason": "server_error",
            }

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "evals.preference_memory.runner._new_agent",
        lambda *args, **kwargs: FailedAgent(),
    )
    settings = RunSettings(
        protocol="full-history",
        variant="none",
        model="fake",
        provider="fake",
        memory_provider="profile_memory",
        temperature=0,
        turns_per_session=5,
        save_transcripts=False,
    )

    with pytest.raises(RuntimeError, match="server_error.*HTTP 503"):
        _run_live_session(
            settings,
            prompts=["hello"],
            session_id="failed-live-session",
            memory_enabled=False,
        )

    assert closed == [True]


def test_live_session_counts_profile_intent_gate_decisions(monkeypatch):
    class GateProvider:
        status = None

        def intent_gate_status(self):
            return self.status

    gate_provider = GateProvider()

    class MemoryManager:
        @staticmethod
        def get_provider(name):
            assert name == "profile_memory"
            return gate_provider

    class GatedAgent:
        _memory_manager = MemoryManager()
        session_prompt_tokens = 20
        session_completion_tokens = 10
        session_total_tokens = 30

        def run_conversation(self, prompt, conversation_history):
            gate_provider.status = {
                "skipped": prompt == "skip",
                "fail_open": prompt == "fail-open",
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            }
            return {
                "final_response": "answer",
                "messages": [
                    *conversation_history,
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "answer"},
                ],
            }

        def close(self):
            return None

    monkeypatch.setattr(
        "evals.preference_memory.runner._new_agent",
        lambda *args, **kwargs: GatedAgent(),
    )
    settings = RunSettings(
        protocol="native-memory",
        variant="control",
        model="fake",
        provider="fake",
        memory_provider="profile_memory",
        temperature=0,
        turns_per_session=5,
        save_transcripts=False,
        profile_intent_gate="on",
    )

    _responses, _messages, metrics = _run_live_session(
        settings,
        prompts=["retrieve", "skip", "fail-open"],
        session_id="gate-metrics",
        memory_enabled=True,
    )

    assert metrics["intent_gate_decisions"] == 3
    assert metrics["intent_gate_skips"] == 1
    assert metrics["intent_gate_fail_open"] == 1
    assert metrics["intent_gate_input_tokens"] == 30
    assert metrics["intent_gate_output_tokens"] == 6
    assert metrics["intent_gate_total_tokens"] == 36


def test_dataset_rejects_duplicate_case_ids(tmp_path: Path):
    raw = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    raw["cases"].append(raw["cases"][0])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate case ids"):
        load_dataset(path)


def test_latex_preference_grader_accepts_exact_workflow_style():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]
    response = (
        "我们假设观测值由真实信号和随机误差共同构成，即\n"
        "\\begin{equation}\n"
        "y_i=\\mu_i+\\varepsilon_i.\n"
        "\\end{equation}\n"
        "其中，$\\varepsilon_i\\sim\\mathcal{N}(0,\\sigma^2)$，且其均值为零。"
    )

    grade = grade_response(case, response)

    assert grade["score"] == 1.0
    assert grade["passed"] is True


def test_every_case_has_a_strictly_passing_calibration_response():
    dataset = load_dataset(DEFAULT_DATASET)
    responses = json.loads(IDEAL_RESPONSES.read_text(encoding="utf-8"))

    assert set(responses) == set(dataset.cases_by_id)
    failures = {}
    for case in dataset.cases:
        grade = grade_response(case, responses[case.case_id])
        if not grade["passed"]:
            failures[case.case_id] = [
                assertion["id"]
                for assertion in grade["assertions"]
                if not assertion["passed"]
            ]

    assert failures == {}


def test_latex_preference_grader_detects_wrapping_and_wrong_environment():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]
    response = "我们假设观测值由真实信号构成，\n" "并受到随机误差影响。\n" "\\[y_i=\\mu_i+\\varepsilon_i\\]"

    grade = grade_response(case, response)
    failed = {item["id"] for item in grade["assertions"] if not item["passed"]}

    assert grade["passed"] is False
    assert "latex_equation_environment" in failed
    assert "latex_forbidden_display_forms" in failed
    assert "latex_no_arbitrary_prose_wrap" in failed


def test_source_only_rejects_explanation_around_code_fence():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["citation_style_positive"]
    response = "源码如下：\n```latex\nPrior work confirms this \\autocite{smith2024}.\n```"

    grade = grade_response(case, response)
    source_assertion = next(
        item for item in grade["assertions"] if item["id"] == "source_only"
    )

    assert source_assertion["passed"] is False


def test_false_application_ignores_unrelated_negative_quality_failure():
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["client_email_negative"]
    response = "下午三点方便一起看测试结果吗？" + ("稍后聊。" * 30)
    grade = grade_response(case, response)
    result_set = ResultSet(
        label="scope-test",
        protocol="replay",
        variant="none",
        model="",
        provider="",
        dataset="workflow-prefeval@1.0-smoke",
        records=(
            {
                "case_id": case.case_id,
                "category": case.category,
                "applicable": False,
                "passed": grade["passed"],
                "score": grade["score"],
                "grade": grade,
                "distance": 0,
                "wall_s": 0,
                "error": None,
            },
        ),
        files=(),
    )

    summary = summarize(result_set)

    assert grade["passed"] is False
    assert summary["strict_pass_rate"] == 0.0
    assert summary["false_application"] == 0.0


def test_control_vs_structured_comparison_reports_deltas_and_partial_gates():
    dataset = load_dataset(DEFAULT_DATASET)
    responses = json.loads(IDEAL_RESPONSES.read_text(encoding="utf-8"))
    positive = dataset.cases_by_id["latex_source_positive"]
    negative = dataset.cases_by_id["latex_source_negative"]

    def record(case, response, *, recalled, wall_s):
        grade = grade_response(case, response)
        return {
            "case_id": case.case_id,
            "category": case.category,
            "applicable": case.applicable,
            "passed": grade["passed"],
            "score": grade["score"],
            "grade": grade,
            "memory_recalled": recalled,
            "distance": 30,
            "wall_s": wall_s,
            "error": None,
        }

    shared = {
        "protocol": "native-memory",
        "model": "test-model",
        "provider": "test-provider",
        "dataset": "workflow-prefeval@1.0-smoke",
        "files": (),
        "comparison_signature": "same-cases-seed-and-distances",
    }
    control = ResultSet(
        label="control-run",
        variant="control",
        records=(
            record(positive, "", recalled=False, wall_s=1.0),
            record(
                negative,
                responses[negative.case_id],
                recalled=None,
                wall_s=1.0,
            ),
        ),
        **shared,
    )
    structured = ResultSet(
        label="structured-run",
        variant="structured",
        records=(
            record(
                positive,
                responses[positive.case_id],
                recalled=True,
                wall_s=1.2,
            ),
            record(
                negative,
                responses[negative.case_id],
                recalled=None,
                wall_s=1.2,
            ),
        ),
        **shared,
    )

    comparison = compare_variants([control, structured])[0]

    assert comparison["preference_recall_delta"] == 1.0
    assert comparison["correct_application_delta"] == 1.0
    assert comparison["false_application_delta"] == 0.0
    assert comparison["p95_latency_ratio"] == pytest.approx(1.2)
    assert all(comparison["gates"].values())
    assert "Control vs structured" in render_markdown([control, structured])
    assert "A/B diagnostic" in render_html([control, structured])


def test_control_vs_structured_requires_identical_run_signature():
    shared = {
        "protocol": "native-memory",
        "model": "test-model",
        "provider": "test-provider",
        "dataset": "workflow-prefeval@1.0-smoke",
        "records": (),
        "files": (),
    }
    control = ResultSet(
        label="control",
        variant="control",
        comparison_signature="seed-1",
        **shared,
    )
    structured = ResultSet(
        label="structured",
        variant="structured",
        comparison_signature="seed-2",
        **shared,
    )

    assert compare_variants([control, structured]) == []


def test_extract_memory_context_prefers_api_sidecar():
    messages = [
        {
            "role": "user",
            "content": "probe",
            "api_content": "<memory-context>\nUse equation.\n</memory-context>\n\nprobe",
        }
    ]

    assert _extract_memory_context(messages) == "Use equation."


def test_full_history_uses_fixture_turns_but_counts_only_real_api_calls(monkeypatch):
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]
    distractors = dataset.intervening_turns(case, 2, 42)
    calls = []

    class FakeAgent:
        _api_call_count = 1
        session_prompt_tokens = 40
        session_completion_tokens = 10
        session_total_tokens = 50

        def run_conversation(self, prompt, conversation_history):
            calls.append((prompt, conversation_history))
            return {
                "final_response": "generated",
                "messages": [
                    *conversation_history,
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "generated"},
                ],
            }

        def close(self):
            return None

    monkeypatch.setattr(
        "evals.preference_memory.runner._new_agent",
        lambda *args, **kwargs: FakeAgent(),
    )
    settings = RunSettings(
        protocol="full-history",
        variant="none",
        model="fake",
        provider="fake",
        memory_provider="openviking",
        temperature=0,
        turns_per_session=5,
        save_transcripts=False,
    )

    outcome = _run_full_history(
        case,
        distractors,
        settings,
        namespace="full-history-test",
    )

    expected_history = []
    for turn in (*case.setup, *distractors):
        expected_history.extend(turn.as_messages())
    assert calls == [(case.probe, expected_history)]
    assert outcome["metrics"]["api_turns"] == 1
    assert outcome["metrics"]["total_tokens"] == 50


def test_native_memory_uses_fresh_sessions_and_chunks_distractors(monkeypatch):
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]
    distractors = dataset.intervening_turns(case, 7, 42)
    calls = []

    def fake_live_session(settings, *, prompts, session_id, memory_enabled):
        calls.append((tuple(prompts), session_id, memory_enabled))
        messages = []
        if "-probe-" in session_id:
            messages = [
                {
                    "role": "user",
                    "content": case.probe,
                    "api_content": (
                        "<memory-context>equation 物理行</memory-context>\n\n" + case.probe
                    ),
                }
            ]
        return (
            [f"answer:{prompt}" for prompt in prompts],
            messages,
            {
                "api_turns": len(prompts),
                "total_tokens": len(prompts) * 10,
            },
        )

    monkeypatch.setattr(
        "evals.preference_memory.runner._run_live_session", fake_live_session
    )
    settings = RunSettings(
        protocol="native-memory",
        variant="structured",
        model="fake",
        provider="fake",
        memory_provider="openviking",
        temperature=0,
        turns_per_session=3,
        save_transcripts=False,
    )

    outcome = _run_native_memory(
        case,
        distractors,
        settings,
        namespace="native-test",
    )

    assert [len(prompts) for prompts, _, _ in calls] == [1, 3, 3, 1, 1]
    assert all(memory_enabled for _, _, memory_enabled in calls)
    assert len({session_id for _, session_id, _ in calls}) == len(calls)
    assert calls[-1][0] == (case.probe,)
    assert outcome["memory_context"] == "equation 物理行"
    assert outcome["metrics"]["api_turns"] == 9


def test_native_fixture_mode_commits_fixed_noise_without_live_replies(monkeypatch):
    dataset = load_dataset(DEFAULT_DATASET)
    case = dataset.cases_by_id["latex_source_positive"]
    distractors = dataset.intervening_turns(case, 7, 42)
    live_calls = []
    fixture_calls = []

    def fake_live_session(settings, *, prompts, session_id, memory_enabled):
        live_calls.append((tuple(prompts), session_id))
        return ["answer" for _ in prompts], [], {"api_turns": len(prompts)}

    def fake_fixture_session(settings, *, turns, session_id):
        fixture_calls.append((turns, session_id))
        return [], {"api_turns": 0}

    monkeypatch.setattr(
        "evals.preference_memory.runner._run_live_session", fake_live_session
    )
    monkeypatch.setattr(
        "evals.preference_memory.runner._commit_fixture_session",
        fake_fixture_session,
    )
    settings = RunSettings(
        protocol="native-memory",
        variant="control",
        model="fake",
        provider="fake",
        memory_provider="openviking",
        temperature=0,
        turns_per_session=3,
        save_transcripts=False,
        native_distractor_mode="fixture",
    )

    _run_native_memory(case, distractors, settings, namespace="fixture-test")

    assert [prompts for prompts, _ in live_calls] == [
        (case.setup[0].user,),
        (case.probe,),
    ]
    assert [len(turns) for turns, _ in fixture_calls] == [3, 3, 1]
    assert len({session_id for _, session_id in fixture_calls}) == 3


def test_isolated_environment_writes_variant_and_restores_env(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/original-hermes-home")
    monkeypatch.setenv("OPENVIKING_AGENT", "original-agent")

    with _isolated_hermes_environment(
        memory_enabled=True,
        memory_provider="openviking",
        variant="structured",
        agent_namespace="eval-agent",
        model_config={"provider": "custom:test", "api_key": "test-only"},
        memory_provider_config={"recall_max_injected_chars": 1234},
    ) as home:
        config = json.loads((home / "config.yaml").read_text(encoding="utf-8"))
        assert os.environ["HERMES_HOME"] == str(home)
        assert os.environ["OPENVIKING_AGENT"] == "eval-agent"
        assert config["memory"]["provider"] == "openviking"
        assert config["memory"]["openviking"]["structured_preference_recall"] is True
        assert config["memory"]["openviking"]["recall_max_injected_chars"] == 1234
        assert config["model"]["provider"] == "custom:test"

    assert os.environ["HERMES_HOME"] == "/tmp/original-hermes-home"
    assert os.environ["OPENVIKING_AGENT"] == "original-agent"


def test_isolated_environment_writes_profile_intent_gate_override():
    with _isolated_hermes_environment(
        memory_enabled=True,
        memory_provider="profile_memory",
        variant="control",
        agent_namespace="profile-gate-eval",
        memory_provider_config={"intent_gate": True},
    ) as home:
        config = json.loads((home / "config.yaml").read_text(encoding="utf-8"))

    assert config["memory"]["profile_memory"]["intent_gate"] is True


def test_current_config_copy_isolates_profile_store_and_resolves_model_path(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        "hermes_cli.config.read_user_config_raw",
        lambda: {
            "model": {
                "default": "test-model",
                "provider": "custom:test",
                "api_key": "test-only",
            },
            "memory": {
                "profile_memory": {
                    "db_path": "$HERMES_HOME/real.sqlite3",
                    "snapshot_path": "$HERMES_HOME/profile/PROFILE.md",
                    "embedding_model_path": "$HERMES_HOME/models/profile.gguf",
                    "semantic_recall": True,
                }
            },
        },
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    model, memory, fingerprint, api_key, base_url = _load_current_config_overrides(
        "profile_memory"
    )

    assert model["provider"] == "custom:test"
    assert "api_key" not in model
    assert api_key == "test-only"
    assert base_url == ""
    assert "db_path" not in memory
    assert "snapshot_path" not in memory
    assert memory["embedding_model_path"] == str(
        (tmp_path / "models" / "profile.gguf").resolve()
    )
    assert memory["semantic_recall"] is True
    assert len(fingerprint) == 64


def test_profile_memory_audit_reads_items_and_closes_provider(monkeypatch):
    events = []

    class FakeProvider:
        def initialize(self, **kwargs):
            events.append(("initialize", kwargs))

        def handle_tool_call(self, name, args):
            events.append(("browse", name, args))
            return json.dumps({"items": [{"kind": "workflow_preference"}]})

        def shutdown(self):
            events.append(("shutdown",))

    monkeypatch.setattr(
        "plugins.memory.load_memory_provider",
        lambda *args, **kwargs: FakeProvider(),
    )
    settings = RunSettings(
        protocol="native-memory",
        variant="control",
        model="fake",
        provider="fake",
        memory_provider="profile_memory",
        temperature=0,
        turns_per_session=3,
        save_transcripts=True,
    )

    audit = _audit_memory_provider(settings, namespace="audit-test")

    assert audit["items"] == [{"kind": "workflow_preference"}]
    assert events[0][0] == "initialize"
    assert events[1] == ("browse", "profile_browse", {"status": "all"})
    assert events[-1] == ("shutdown",)


def test_replay_runner_and_report_end_to_end(tmp_path: Path):
    responses = {
        "latex_source_positive": (
            "模型设定如下：\n\\begin{equation}\ny_i=\\mu_i+\\varepsilon_i.\n"
            "\\end{equation}\n其中，$\\varepsilon_i\\sim\\mathcal{N}(0,\\sigma^2)$。"
        ),
        "latex_source_negative": ("样本均值是把样本中的数值相加后除以样本数量。" "它用一个数概括样本的平均水平。"),
    }
    response_path = tmp_path / "responses.json"
    response_path.write_text(
        json.dumps(responses, ensure_ascii=False), encoding="utf-8"
    )
    results_dir = tmp_path / "results"

    exit_code = runner_main(
        [
            "--protocol",
            "replay",
            "--responses",
            str(response_path),
            "--tasks",
            "latex_source_positive,latex_source_negative",
            "--distances",
            "0,30",
            "--label",
            "test-replay",
            "--results-dir",
            str(results_dir),
        ]
    )

    assert exit_code == 0
    result_file = next(results_dir.rglob("rep1.json"))
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert len(payload["records"]) == 4
    assert all(record["passed"] for record in payload["records"])

    result_sets = load_result_sets([str(result_file)])
    summary = summarize(result_sets[0])
    assert summary["strict_pass_rate"] == 1.0
    assert summary["correct_application"] == 1.0
    assert summary["false_application"] == 0.0
    assert set(summary["by_distance"]) == {0, 30}
    rendered = render_html(result_sets)
    assert "Workflow Preference Evaluation" in rendered
    assert "Retention curve" in rendered


def test_native_runner_applies_profile_intent_gate_override(monkeypatch, tmp_path):
    captured = []

    monkeypatch.setattr(
        "evals.preference_memory.runner._load_current_config_overrides",
        lambda memory_provider: (
            {},
            {"recall_limit": 3},
            "base-config-fingerprint",
            "",
            "",
        ),
    )

    def fake_run_record(dataset, case, settings, **kwargs):
        captured.append(settings)
        return {
            "score": 1.0,
            "passed": True,
            "memory_recalled": True,
            "wall_s": 0.0,
            "error": None,
        }

    monkeypatch.setattr("evals.preference_memory.runner.run_record", fake_run_record)
    results_dir = tmp_path / "results"

    exit_code = runner_main(
        [
            "--protocol",
            "native-memory",
            "--variant",
            "control",
            "--memory-provider",
            "profile_memory",
            "--profile-intent-gate",
            "on",
            "--model",
            "fake-model",
            "--provider",
            "fake-provider",
            "--tasks",
            "latex_source_positive",
            "--distances",
            "0",
            "--label",
            "gate-override",
            "--results-dir",
            str(results_dir),
        ]
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].isolated_memory_config == {
        "recall_limit": 3,
        "intent_gate": True,
    }
    result_file = next(results_dir.rglob("rep1.json"))
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["run"]["profile_intent_gate"] == "on"
