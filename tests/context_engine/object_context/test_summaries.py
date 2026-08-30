from types import SimpleNamespace

from agent.model_metadata import estimate_tokens_rough
from plugins.context_engine.object_context.models import ObjectRecord, ObjectType
from plugins.context_engine.object_context.summaries import BoundedSummaryGenerator


def _record(object_type=ObjectType.CODE):
    return ObjectRecord(
        object_ref="object://obj_0123456789abcdef01234567@v1",
        object_id="obj_0123456789abcdef01234567",
        version=1,
        object_type=object_type,
        content="def run():\n    return 1\n",
        sha256="0" * 64,
        byte_size=24,
        char_count=24,
        token_count=8,
        conversation_id="conv",
        source_delta_id="delta",
        source_message_key="message",
        source_message_ordinal=0,
        source_part_ordinal=0,
        source_start=0,
        source_end=24,
        language="python",
        name="demo.py",
    )


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_summary_call_is_type_specific_and_has_hard_output_cap():
    captured = {}

    def call(**kwargs):
        captured.update(kwargs)
        return _response("word " * 200)

    generator = BoundedSummaryGenerator(max_tokens=16, call=call)
    summary, fallback = generator.generate(
        engine=SimpleNamespace(
            model="main", provider="test", base_url="", api_key="", api_mode=""
        ),
        record=_record(),
        contains={"functions": ["run()"]},
    )

    assert fallback is False
    assert estimate_tokens_rough(summary) <= 16
    assert captured["max_tokens"] == 16
    prompt = captured["messages"][0]["content"]
    assert "Object type: code" in prompt
    assert "Do not enumerate symbols" in prompt


def test_summary_failure_uses_deterministic_supported_fallback():
    def fail(**kwargs):
        raise RuntimeError("offline")

    generator = BoundedSummaryGenerator(max_tokens=16, call=fail)
    summary, fallback = generator.generate(
        engine=SimpleNamespace(),
        record=_record(),
        contains={"functions": ["run()"]},
    )
    assert fallback is True
    assert summary == "Python code object named demo.py."
    assert estimate_tokens_rough(summary) <= 16


def test_summary_that_repeats_structural_index_is_rejected():
    def call(**kwargs):
        return _response("Contains Alpha, beta, and gamma.")

    generator = BoundedSummaryGenerator(max_tokens=32, call=call)
    summary, fallback = generator.generate(
        engine=SimpleNamespace(),
        record=_record(ObjectType.STRUCTURED_DATA),
        contains={"top_level_keys": ["Alpha", "beta", "gamma"]},
    )
    assert fallback is True
    assert "Alpha" not in summary


def test_summary_auxiliary_input_is_redacted_but_exact_record_is_unchanged():
    secret = "not-a-real-key-object-context-1234567890"
    record = _record()
    record = record.__class__(**{
        **record.__dict__,
        "content": f"API_KEY={secret}\ndef run(): return 1\n",
    })
    captured = {}

    def call(**kwargs):
        captured.update(kwargs)
        return _response("Uses a configured API credential before running.")

    generator = BoundedSummaryGenerator(max_tokens=24, call=call)
    summary, fallback = generator.generate(
        engine=SimpleNamespace(), record=record, contains={"functions": ["run()"]}
    )

    assert fallback is False
    assert summary
    assert secret not in captured["messages"][0]["content"]
    assert record.content.startswith(f"API_KEY={secret}")
