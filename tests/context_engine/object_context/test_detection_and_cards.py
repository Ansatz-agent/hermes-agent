import json
from dataclasses import replace

import pytest

from agent.context_engine import ContextDelta
from plugins.context_engine.object_context.cards import (
    CARD_CLOSE,
    CARD_OPEN,
    benefit_gate,
    build_card,
    parse_card_text,
    render_card,
)
from plugins.context_engine.object_context.detection import detect_delta_objects
from plugins.context_engine.object_context.extractors import extract_structure
from plugins.context_engine.object_context.models import ObjectRecord, ObjectType
from plugins.context_engine.object_context.renderer import apply_occurrence_cards


def _delta(content, *, role="user", name="", delta_id="turn:user:0"):
    message = {"role": role, "content": content, "timestamp": 1.0}
    if name:
        message.update({"name": name, "tool_name": name, "tool_call_id": "call-1"})
    return ContextDelta(
        delta_id=delta_id,
        kind="user" if role == "user" else "inference",
        conversation_id="conv",
        session_id="session",
        turn_id="turn",
        sequence=0,
        messages=(message,),
    )


@pytest.mark.parametrize(
    ("content", "expected_format"),
    [
        ('{"a": 1, "b": [2]}', "json"),
        ("alpha: 1\nbeta:\n  nested: true\n", "yaml"),
        ("<root><item>1</item><item>2</item></root>", "xml"),
        ("name,value\na,1\nb,2\n", "csv"),
    ],
)
def test_user_structured_payloads_are_first_class_objects(content, expected_format):
    [detected] = detect_delta_objects(_delta(content), min_tokens=1)
    assert detected.object_type == ObjectType.STRUCTURED_DATA
    assert detected.content == content
    assert detected.metadata["format"] == expected_format


def test_runtime_metadata_precedes_nested_parser_candidates():
    payload = '{"nested": "json"}\n' * 20
    [detected] = detect_delta_objects(
        _delta(payload, role="tool", name="read_file"), min_tokens=1
    )
    assert detected.object_type == ObjectType.FILE_CONTENT
    assert detected.whole_part is True
    assert detected.tool_call_id == "call-1"


def test_runtime_declared_code_artifact_log_and_generic_tool_result_types():
    code_delta = _delta([
        {
            "type": "input_file",
            "filename": "train.py",
            "text": "def train():\n    return 1\n",
        },
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    ])
    [code] = detect_delta_objects(code_delta, min_tokens=1)
    assert code.object_type == ObjectType.CODE
    assert code.language == "python"
    assert code.part_ordinal == 0

    [artifact] = detect_delta_objects(
        _delta("generated report body", role="tool", name="create_artifact"),
        min_tokens=1,
    )
    assert artifact.object_type == ObjectType.ARTIFACT

    log_text = "\n".join(f"INFO step={i} metric={i * 2}" for i in range(10))
    [log] = detect_delta_objects(
        _delta(log_text, role="tool", name="terminal"), min_tokens=1
    )
    assert log.object_type == ObjectType.LOG

    [generic] = detect_delta_objects(
        _delta("plain but bounded tool output", role="tool", name="custom_tool"),
        min_tokens=1,
    )
    assert generic.object_type == ObjectType.TOOL_RESULT


def test_all_natural_boundaries_and_retrieval_exclusion():
    code = "before\n```python\nclass A:\n    pass\n```\nafter"
    [code_object] = detect_delta_objects(_delta(code), min_tokens=1)
    assert code_object.object_type == ObjectType.CODE
    assert code_object.content == "class A:\n    pass\n"
    assert code[code_object.start : code_object.end].startswith("```python")

    table = "intro\n| a | b |\n|---|---|\n| 1 | 2 |\noutro"
    [table_object] = detect_delta_objects(_delta(table), min_tokens=1)
    assert table_object.object_type == ObjectType.TABLE
    assert table_object.metadata["format"] == "markdown"

    trace = (
        "Explanation before.\nTraceback (most recent call last):\n"
        '  File "demo.py", line 9, in <module>\n    run()\n'
        "ValueError: bad\nExplanation after.\n"
    )
    [trace_object] = detect_delta_objects(_delta(trace), min_tokens=1)
    assert trace_object.object_type == ObjectType.ERROR_TRACE
    assert "Explanation before" not in trace_object.content
    assert "Explanation after" not in trace_object.content

    retrieved = _delta(
        '{"retrieved_object": {"content": "x"}}', role="tool", name="retrieve_object"
    )
    assert detect_delta_objects(retrieved, min_tokens=1) == []


def test_ambiguous_prose_and_yaml_like_bullet_list_stay_raw():
    prose = "This is ordinary conversation prose, with commas; it is not a dataset."
    bullets = "- discuss the API\n- preserve ordinary prose\n- do not infer structure\n"
    assert detect_delta_objects(_delta(prose), min_tokens=1) == []
    assert detect_delta_objects(_delta(bullets), min_tokens=1) == []
    assert detect_delta_objects(_delta("```python\nmissing close"), min_tokens=1) == []


def test_generated_summaries_cards_and_synthetic_messages_are_not_redetected():
    summary = {
        "role": "user",
        "content": '{"large": [1, 2, 3]}',
        "timestamp": 2.0,
        "_compressed_summary": True,
    }
    synthetic = {
        "role": "user",
        "content": '{"large": [1, 2, 3]}',
        "timestamp": 3.0,
        "_thinking_prefill": True,
    }
    for message in (summary, synthetic):
        observed = ContextDelta(
            delta_id=f"excluded:{message['timestamp']}",
            kind="user",
            conversation_id="conv",
            session_id="session",
            turn_id="turn",
            sequence=0,
            messages=(message,),
        )
        assert detect_delta_objects(observed, min_tokens=1) == []

    card = _delta(
        '<OBJECT_CARD>{"schema_version":"1.0"}</OBJECT_CARD>',
        delta_id="card:user:0",
    )
    assert detect_delta_objects(card, min_tokens=1) == []


def _record(object_type, content, *, language="", metadata=None):
    return ObjectRecord(
        object_ref="object://obj_0123456789abcdef01234567@v1",
        object_id="obj_0123456789abcdef01234567",
        version=1,
        object_type=object_type,
        content=content,
        sha256="0" * 64,
        byte_size=len(content.encode()),
        char_count=len(content),
        token_count=100,
        conversation_id="conv",
        source_delta_id="delta",
        source_message_key="message",
        source_message_ordinal=0,
        source_part_ordinal=0,
        source_start=0,
        source_end=len(content),
        language=language,
        metadata=metadata or {},
    )


def test_type_specific_structural_extractors_are_deterministic():
    code = _record(
        ObjectType.CODE,
        "import os\nDEFAULT = 1\nclass Runner: pass\ndef main(x: int): return x\n",
        language="python",
    )
    code_index = extract_structure(code)
    assert code_index["classes"] == ["Runner"]
    assert code_index["functions"] == ["main(x: int)"]
    assert code_index["module_variables"] == ["DEFAULT"]
    assert code_index["imports"] == ["os"]
    assert code_index["entry_points"] == ["main()"]

    data = _record(
        ObjectType.STRUCTURED_DATA,
        json.dumps({"rows": [{"id": 1}], "enabled": True}),
        metadata={"format": "json"},
    )
    data_index = extract_structure(data)
    assert data_index["top_level_keys"] == ["rows", "enabled"]
    assert data_index["shape"] == {"type": "object", "key_count": 2}

    trace = _record(
        ObjectType.ERROR_TRACE,
        'Traceback (most recent call last):\n  File "a.py", line 2\nValueError: bad\n',
    )
    trace_index = extract_structure(trace)
    assert trace_index["exception_type"] == "ValueError"
    assert trace_index["exception_message"] == "bad"
    assert trace_index["stack_depth"] == 1

    log = _record(
        ObjectType.LOG,
        "Run started\nstage: train\nloss=1.2\ncheckpoint saved\n"
        "WARNING slow\nERROR failed\nRun finished\n",
    )
    log_index = extract_structure(log)
    assert log_index["stage_names"] == ["train"]
    assert "loss" in log_index["metric_names"]
    assert log_index["checkpoint_events"]
    assert log_index["start_markers"] == ["Run started"]
    assert log_index["end_markers"] == ["Run finished"]


def test_card_schema_is_stable_immutable_and_benefit_gated():
    record = replace(
        _record(ObjectType.CODE, "print('x')\n", language="python"),
        name="demo.py",
    )
    card = build_card(
        record,
        summary="Prints one directly specified value.",
        contains={"language": "python", "functions": []},
        origin={
            "role": "tool",
            "tool": "read_file",
            "operation": "read",
            "target": "demo.py",
        },
    )
    first = render_card(card)
    second = render_card(card)
    assert first == second
    assert first.startswith(CARD_OPEN) and first.endswith(CARD_CLOSE)
    payload = parse_card_text(first)
    assert payload["schema_version"] == "1.1"
    assert payload["object_ref"].endswith("@v1")
    assert payload["language"] == "python"
    assert payload["origin"] == {
        "operation": "read",
        "role": "tool",
        "target": "demo.py",
        "tool": "read_file",
    }
    assert "sha256" not in payload and "physical_path" not in payload

    assert benefit_gate(
        "x" * 5000,
        first,
        min_absolute_saving_tokens=10,
        min_relative_saving_ratio=0.1,
    )[0]
    assert not benefit_gate(
        "tiny",
        first,
        min_absolute_saving_tokens=10,
        min_relative_saving_ratio=0.1,
    )[0]


def test_card_without_semantic_summary_omits_field_but_keeps_origin():
    record = replace(
        _record(ObjectType.STRUCTURED_DATA, '{"key": "value"}'),
        name="request payload",
    )
    card = build_card(
        record,
        summary="",
        contains={"format": "json", "top_level_keys": ["key"]},
        origin={"role": "user"},
    )

    payload = parse_card_text(render_card(card))

    assert payload["schema_version"] == "1.1"
    assert "summary" not in payload
    assert payload["origin"] == {"role": "user"}
    assert payload["contains"]["top_level_keys"] == ["key"]


def test_renderer_replaces_only_exact_span_and_preserves_multimodal_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before <raw> after"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ],
            "timestamp": 1.0,
        }
    ]
    occurrence = {
        "message_ordinal": 0,
        "part_ordinal": 0,
        "span_start": 7,
        "span_end": 12,
        "object_ref": "object://obj_0123456789abcdef01234567@v1",
        "card_text": "<CARD>",
    }
    rendered = apply_occurrence_cards(messages, [occurrence])
    assert rendered[0]["content"][0]["text"] == "before <CARD> after"
    assert rendered[0]["content"][1] == messages[0]["content"][1]
    assert messages[0]["content"][0]["text"] == "before <raw> after"
