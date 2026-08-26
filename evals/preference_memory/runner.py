"""Run PrefEval-style work-preference evaluations through real Hermes agents.

Protocols
---------
replay
    Grade already-collected responses.  No provider or network is required.
full-history
    Put setup + deterministic distractor exchanges in ``conversation_history``
    and issue only the final probe.  This measures long-context preference
    following, not persistent memory.
native-memory
    Send setup and distractor turns through real ``AIAgent`` sessions, close
    those sessions so the configured memory provider can commit them, and run
    the probe in a fresh session.  This measures the product memory path.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from .dataset import EvalCase, EvalDataset, Turn, load_dataset
    from .graders import grade_response
except ImportError:  # Direct ``python evals/preference_memory/runner.py``.
    from dataset import EvalCase, EvalDataset, Turn, load_dataset
    from graders import grade_response


DEFAULT_DATASET = EVAL_DIR / "datasets" / "workflow_prefeval_v1.json"
DEFAULT_RESULTS = EVAL_DIR / "results"
_MEMORY_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>\s*(.*?)\s*</\s*memory-context\s*>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class RunSettings:
    protocol: str
    variant: str
    model: str
    provider: str
    memory_provider: str
    temperature: float
    turns_per_session: int
    save_transcripts: bool
    isolated_model_config: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    isolated_memory_config: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    config_sha256: str = ""
    api_key: str = field(default="", repr=False, compare=False)
    base_url: str = ""
    native_distractor_mode: str = "live"
    profile_intent_gate: str = "inherit"


def _slug(value: str, *, maximum: int = 80) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.") or "run"
    if len(cleaned) <= maximum:
        return cleaned
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    return f"{cleaned[: maximum - 11]}-{digest}"


def _parse_distances(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "distances must be comma-separated integers"
        ) from exc
    if not values or any(value < 0 for value in values):
        raise argparse.ArgumentTypeError("distances must contain non-negative integers")
    return tuple(dict.fromkeys(values))


def _git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if value is None else str(value)


def _extract_memory_context(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        for key in ("api_content", "content"):
            text = _message_text(message.get(key))
            match = _MEMORY_CONTEXT_RE.search(text)
            if match:
                return match.group(1).strip()
    return ""


def _count_message_metrics(messages: list[dict[str, Any]]) -> dict[str, int]:
    api_turns = 0
    tool_calls = 0
    memory_tool_calls = 0
    for message in messages or []:
        if message.get("role") != "assistant":
            continue
        api_turns += 1
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tool_calls += 1
            function = tool_call.get("function") or {}
            if function.get("name") in {
                "memory",
                "profile_remember",
                "viking_remember",
            }:
                memory_tool_calls += 1
    return {
        "api_turns": api_turns,
        "tool_calls": tool_calls,
        "memory_tool_calls": memory_tool_calls,
    }


def _memory_marker_result(case: EvalCase, context: str) -> bool | None:
    if not case.expected_memory_markers:
        return None
    lowered = context.casefold()
    return all(
        any(marker.casefold() in lowered for marker in alternatives)
        for alternatives in case.expected_memory_markers
    )


def _raise_for_failed_agent_result(result: dict[str, Any]) -> None:
    """Turn a terminal provider failure into an invalid eval record."""
    if result.get("failed") is not True:
        return
    detail = _message_text(result.get("error")) or _message_text(
        result.get("final_response")
    )
    reason = _message_text(result.get("failure_reason"))
    prefix = (
        f"live model call failed ({reason})" if reason else "live model call failed"
    )
    raise RuntimeError(f"{prefix}: {detail or 'unknown provider error'}")


def _load_replay_responses(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot load replay responses from {path}: {exc}") from exc
    if isinstance(raw, dict) and "responses" in raw:
        raw = raw["responses"]
    if not isinstance(raw, dict):
        raise SystemExit(
            "replay response file must be an object or contain a responses object"
        )
    responses = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SystemExit("all replay response keys and values must be strings")
        responses[key] = value
    return responses


def _load_current_config_overrides(
    memory_provider: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    """Copy live routing into the disposable eval home without persisting it.

    The native protocol changes ``HERMES_HOME`` for store isolation. Without
    this copy, a configured custom model route and a profile provider's local
    embedding model disappear with the old home, producing an infrastructure
    failure rather than a memory measurement. Database and snapshot paths are
    intentionally excluded so the benchmark never reads or mutates the user's
    real profile store.
    """
    try:
        from hermes_cli.config import read_user_config_raw
        from hermes_constants import get_hermes_home

        config = read_user_config_raw() or {}
        current_home = Path(get_hermes_home()).expanduser().resolve()
    except Exception as exc:
        raise RuntimeError(
            f"could not read the current Hermes configuration: {exc}"
        ) from exc

    model = config.get("model")
    raw_model_config = dict(model) if isinstance(model, dict) else {}
    api_key = str(raw_model_config.get("api_key") or "").strip()
    base_url = str(
        raw_model_config.get("base_url") or raw_model_config.get("url") or ""
    ).strip()
    model_config = dict(raw_model_config)
    model_config.pop("api_key", None)
    memory = config.get("memory")
    provider_config: dict[str, Any] = {}
    if isinstance(memory, dict):
        raw_provider = memory.get(memory_provider)
        if isinstance(raw_provider, dict):
            provider_config = dict(raw_provider)
    provider_config.pop("db_path", None)
    provider_config.pop("snapshot_path", None)
    model_path = provider_config.get("embedding_model_path")
    if isinstance(model_path, str) and model_path.strip():
        resolved = model_path.replace("${HERMES_HOME}", str(current_home)).replace(
            "$HERMES_HOME", str(current_home)
        )
        provider_config["embedding_model_path"] = str(
            Path(resolved).expanduser().resolve()
        )

    fingerprint_payload = json.dumps(
        {"model": raw_model_config, "memory_provider": provider_config},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    return model_config, provider_config, fingerprint, api_key, base_url


@contextlib.contextmanager
def _isolated_hermes_environment(
    *,
    memory_enabled: bool,
    memory_provider: str,
    variant: str,
    agent_namespace: str,
    model_config: dict[str, Any] | None = None,
    memory_provider_config: dict[str, Any] | None = None,
) -> Iterator[Path]:
    """Create a disposable Hermes home while preserving sourced credentials."""
    temp_root = Path(tempfile.mkdtemp(prefix="workflow-prefeval-home-"))
    hermes_home = temp_root / ".hermes"
    hermes_home.mkdir(parents=True)
    config: dict[str, Any] = {
        "memory": {
            "memory_enabled": False,
            "user_profile_enabled": False,
            "provider": memory_provider if memory_enabled else "",
        },
        "agent": {"background_review": {"enabled": False}},
    }
    if model_config:
        config["model"] = dict(model_config)
    if memory_provider_config:
        config["memory"][memory_provider] = dict(memory_provider_config)
    if memory_provider == "openviking":
        openviking = dict(config["memory"].get("openviking") or {})
        openviking.update(
            {
                "structured_preference_recall": variant == "structured",
                "preference_recall_limit": 3,
            }
        )
        config["memory"]["openviking"] = openviking
    # JSON is valid YAML and avoids adding a data-format dependency to the eval.
    (hermes_home / "config.yaml").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    old_home = os.environ.get("HERMES_HOME")
    old_agent = os.environ.get("OPENVIKING_AGENT")
    os.environ["HERMES_HOME"] = str(hermes_home)
    if memory_provider == "openviking":
        os.environ["OPENVIKING_AGENT"] = agent_namespace
    try:
        yield hermes_home
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home
        if old_agent is None:
            os.environ.pop("OPENVIKING_AGENT", None)
        else:
            os.environ["OPENVIKING_AGENT"] = old_agent
        shutil.rmtree(temp_root, ignore_errors=True)


def _new_agent(settings: RunSettings, *, session_id: str, memory_enabled: bool):
    from run_agent import AIAgent

    return AIAgent(
        base_url=settings.base_url or None,
        api_key=settings.api_key or None,
        model=settings.model,
        provider=settings.provider,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=not memory_enabled,
        skip_background_review=True,
        enabled_toolsets=["memory"] if memory_enabled else [],
        max_iterations=20,
        session_id=session_id,
        platform="cli",
        request_overrides={"temperature": settings.temperature},
    )


def _run_live_session(
    settings: RunSettings,
    *,
    prompts: list[str],
    session_id: str,
    memory_enabled: bool,
) -> tuple[list[str], list[dict[str, Any]], dict[str, int]]:
    agent = _new_agent(settings, session_id=session_id, memory_enabled=memory_enabled)
    provider = None
    if memory_enabled:
        memory_manager = getattr(agent, "_memory_manager", None)
        provider = (
            memory_manager.get_provider(settings.memory_provider)
            if memory_manager is not None
            else None
        )
        if provider is None:
            agent.close()
            raise RuntimeError(
                f"memory provider {settings.memory_provider!r} did not activate; "
                "check its configuration and dependencies"
            )
        # OpenViking can register successfully while its server is unavailable.
        # That state must abort an eval instead of being scored as poor recall.
        ensure_client = getattr(provider, "_ensure_client", None)
        if settings.memory_provider == "openviking" and (
            not callable(ensure_client) or ensure_client() is None
        ):
            agent.close()
            raise RuntimeError(
                "OpenViking memory provider activated but is not reachable; "
                "start or configure the service before running native-memory"
            )
    history: list[dict[str, Any]] = []
    responses: list[str] = []
    last_messages: list[dict[str, Any]] = []
    totals = {
        "api_turns": 0,
        "tool_calls": 0,
        "memory_tool_calls": 0,
        "intent_gate_decisions": 0,
        "intent_gate_skips": 0,
        "intent_gate_fail_open": 0,
        "intent_gate_input_tokens": 0,
        "intent_gate_output_tokens": 0,
        "intent_gate_total_tokens": 0,
    }
    try:
        for prompt in prompts:
            result = agent.run_conversation(prompt, conversation_history=history)
            _raise_for_failed_agent_result(result)
            response = _message_text(result.get("final_response"))
            last_messages = result.get("messages") or []
            history = last_messages
            responses.append(response)
            current = _count_message_metrics(last_messages)
            # ``last_messages`` contains the whole current session, so replace
            # rather than add; the final snapshot already counts earlier turns.
            totals.update(current)
            gate_status_fn = getattr(provider, "intent_gate_status", None)
            gate_status = gate_status_fn() if callable(gate_status_fn) else None
            if isinstance(gate_status, dict):
                totals["intent_gate_decisions"] += 1
                totals["intent_gate_skips"] += int(bool(gate_status.get("skipped")))
                totals["intent_gate_fail_open"] += int(
                    bool(gate_status.get("fail_open"))
                )
                totals["intent_gate_input_tokens"] += int(
                    gate_status.get("input_tokens") or 0
                )
                totals["intent_gate_output_tokens"] += int(
                    gate_status.get("output_tokens") or 0
                )
                totals["intent_gate_total_tokens"] += int(
                    gate_status.get("total_tokens") or 0
                )
        totals.update(
            {
                "prompt_tokens": int(getattr(agent, "session_prompt_tokens", 0) or 0),
                "completion_tokens": int(
                    getattr(agent, "session_completion_tokens", 0) or 0
                ),
                "total_tokens": int(getattr(agent, "session_total_tokens", 0) or 0),
            }
        )
        return responses, last_messages, totals
    finally:
        agent.close()


def _commit_fixture_session(
    settings: RunSettings,
    *,
    turns: tuple[Turn, ...],
    session_id: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Commit a fixed completed transcript without generating distractor replies.

    This preserves the real provider's session-end extraction and durable
    storage path while avoiding paid model calls for assistant text that is
    already part of the fixed PrefEval fixture.
    """
    agent = _new_agent(settings, session_id=session_id, memory_enabled=True)
    memory_manager = getattr(agent, "_memory_manager", None)
    provider = (
        memory_manager.get_provider(settings.memory_provider)
        if memory_manager is not None
        else None
    )
    if provider is None:
        agent.close()
        raise RuntimeError(
            f"memory provider {settings.memory_provider!r} did not activate; "
            "check its configuration and dependencies"
        )
    messages: list[dict[str, str]] = []
    for turn in turns:
        messages.extend(turn.as_messages())
    try:
        agent.shutdown_memory_provider(messages)
        return messages, {
            "api_turns": 0,
            "tool_calls": 0,
            "memory_tool_calls": 0,
            "intent_gate_decisions": 0,
            "intent_gate_skips": 0,
            "intent_gate_fail_open": 0,
            "intent_gate_input_tokens": 0,
            "intent_gate_output_tokens": 0,
            "intent_gate_total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    finally:
        agent.close()


def _merge_metrics(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value or 0)


def _audit_memory_provider(settings: RunSettings, *, namespace: str) -> dict[str, Any]:
    """Read generated profile items before the disposable eval home is removed."""
    if settings.memory_provider != "profile_memory":
        return {}
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("profile_memory", register_skills=False)
    if provider is None:
        return {"error": "profile_memory could not be loaded for audit"}
    try:
        provider.initialize(
            session_id=f"{namespace}-audit",
            hermes_home=os.environ.get("HERMES_HOME", ""),
            agent_context="primary",
            read_only=True,
        )
        raw = provider.handle_tool_call("profile_browse", {"status": "all"})
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"error": "profile_browse returned a non-JSON audit result"}
        return (
            parsed if isinstance(parsed, dict) else {"error": "invalid profile audit"}
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


def _run_full_history(
    case: EvalCase,
    distractors: tuple[Turn, ...],
    settings: RunSettings,
    *,
    namespace: str,
) -> dict[str, Any]:
    history: list[dict[str, str]] = []
    for turn in (*case.setup, *distractors):
        history.extend(turn.as_messages())
    with _isolated_hermes_environment(
        memory_enabled=False,
        memory_provider=settings.memory_provider,
        variant=settings.variant,
        agent_namespace=namespace,
        model_config=settings.isolated_model_config,
        memory_provider_config=settings.isolated_memory_config,
    ):
        agent = _new_agent(
            settings, session_id=f"{namespace}-probe", memory_enabled=False
        )
        try:
            started = time.monotonic()
            result = agent.run_conversation(case.probe, conversation_history=history)
            _raise_for_failed_agent_result(result)
            elapsed = time.monotonic() - started
            final = _message_text(result.get("final_response"))
            messages = result.get("messages") or []
            metrics = _count_message_metrics(messages)
            # The history contains fixture assistant messages that were never
            # generated by an API call.  Keep tool counts from the transcript,
            # but report the real final-turn model-call count from AIAgent.
            metrics["api_turns"] = int(getattr(agent, "_api_call_count", 0) or 0)
            metrics.update(
                {
                    "prompt_tokens": int(
                        getattr(agent, "session_prompt_tokens", 0) or 0
                    ),
                    "completion_tokens": int(
                        getattr(agent, "session_completion_tokens", 0) or 0
                    ),
                    "total_tokens": int(getattr(agent, "session_total_tokens", 0) or 0),
                }
            )
            return {
                "final_response": final,
                "messages": messages,
                "memory_context": "",
                "metrics": metrics,
                "wall_s": round(elapsed, 3),
                "setup_responses": [],
                "setup_messages": [],
                "memory_audit": {},
            }
        finally:
            agent.close()


def _run_native_memory(
    case: EvalCase,
    distractors: tuple[Turn, ...],
    settings: RunSettings,
    *,
    namespace: str,
) -> dict[str, Any]:
    total_metrics: dict[str, int] = {}
    setup_responses: list[str] = []
    started = time.monotonic()
    with _isolated_hermes_environment(
        memory_enabled=True,
        memory_provider=settings.memory_provider,
        variant=settings.variant,
        agent_namespace=namespace,
        model_config=settings.isolated_model_config,
        memory_provider_config=settings.isolated_memory_config,
    ):
        responses, setup_messages, metrics = _run_live_session(
            settings,
            prompts=[turn.user for turn in case.setup],
            session_id=f"{namespace}-setup-{uuid.uuid4().hex[:8]}",
            memory_enabled=True,
        )
        setup_responses.extend(responses)
        _merge_metrics(total_metrics, metrics)

        for start in range(0, len(distractors), settings.turns_per_session):
            chunk = distractors[start : start + settings.turns_per_session]
            noise_session_id = f"{namespace}-noise-{start}-{uuid.uuid4().hex[:8]}"
            if settings.native_distractor_mode == "fixture":
                _, metrics = _commit_fixture_session(
                    settings,
                    turns=chunk,
                    session_id=noise_session_id,
                )
            else:
                _, _, metrics = _run_live_session(
                    settings,
                    prompts=[turn.user for turn in chunk],
                    session_id=noise_session_id,
                    memory_enabled=True,
                )
            _merge_metrics(total_metrics, metrics)

        probe_responses, probe_messages, metrics = _run_live_session(
            settings,
            prompts=[case.probe],
            session_id=f"{namespace}-probe-{uuid.uuid4().hex[:8]}",
            memory_enabled=True,
        )
        _merge_metrics(total_metrics, metrics)
        memory_context = _extract_memory_context(probe_messages)
        memory_audit = _audit_memory_provider(settings, namespace=namespace)
        return {
            "final_response": probe_responses[-1] if probe_responses else "",
            "messages": probe_messages,
            "memory_context": memory_context,
            "metrics": total_metrics,
            "wall_s": round(time.monotonic() - started, 3),
            "setup_responses": setup_responses,
            "setup_messages": setup_messages,
            "memory_audit": memory_audit,
        }


def _misconfiguration_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "no llm provider configured",
            "authentication",
            "api key",
            "did not activate",
            "not reachable",
        )
    )


def run_record(
    dataset: EvalDataset,
    case: EvalCase,
    settings: RunSettings,
    *,
    distance: int,
    rep: int,
    seed: int,
    label: str,
    replay_responses: dict[str, str] | None = None,
) -> dict[str, Any]:
    distractors = dataset.intervening_turns(case, distance, seed + rep)
    namespace = _slug(
        f"wpref-{label}-{settings.variant}-{case.case_id}-d{distance}-r{rep}",
        maximum=62,
    )
    base: dict[str, Any] = {
        "case_id": case.case_id,
        "category": case.category,
        "preference_form": case.preference_form,
        "applicable": case.applicable,
        "distance": distance,
        "rep": rep,
        "protocol": settings.protocol,
        "variant": settings.variant,
        "tags": list(case.tags),
    }
    try:
        if settings.protocol == "replay":
            if replay_responses is None:
                raise ValueError("replay protocol requires collected responses")
            response_key = f"{case.case_id}@{distance}"
            if response_key in replay_responses:
                final = replay_responses[response_key]
            elif case.case_id in replay_responses:
                final = replay_responses[case.case_id]
            else:
                raise KeyError(
                    f"no replay response for {response_key!r} or {case.case_id!r}"
                )
            outcome = {
                "final_response": final,
                "messages": [],
                "memory_context": "",
                "metrics": {},
                "wall_s": 0.0,
                "setup_responses": [],
                "setup_messages": [],
                "memory_audit": {},
            }
        elif settings.protocol == "full-history":
            outcome = _run_full_history(
                case,
                distractors,
                settings,
                namespace=namespace,
            )
        else:
            outcome = _run_native_memory(
                case,
                distractors,
                settings,
                namespace=namespace,
            )

        grade = grade_response(case, outcome["final_response"])
        context = outcome["memory_context"]
        base.update(
            {
                "final_response": outcome["final_response"],
                "score": grade["score"],
                "passed": grade["passed"],
                "grade": grade,
                "memory_recalled": (
                    _memory_marker_result(case, context)
                    if settings.protocol == "native-memory"
                    else None
                ),
                "memory_context_chars": len(context),
                "memory_audit_item_count": len(
                    (outcome.get("memory_audit") or {}).get("items") or []
                ),
                "wall_s": outcome["wall_s"],
                "error": None,
                **outcome["metrics"],
            }
        )
        if settings.save_transcripts:
            base["messages"] = outcome["messages"]
            base["memory_context"] = context
            base["setup_responses"] = outcome["setup_responses"]
            base["setup_messages"] = outcome.get("setup_messages") or []
            base["memory_audit"] = outcome.get("memory_audit") or {}
    except Exception as exc:
        if settings.protocol != "replay" and _misconfiguration_error(exc):
            raise SystemExit(
                f"ABORT: harness/provider configuration failed for {case.case_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        base.update(
            {
                "final_response": "",
                "score": 0.0,
                "passed": False,
                "grade": None,
                "memory_recalled": None,
                "memory_context_chars": 0,
                "wall_s": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return base


def _build_manifest(
    dataset: EvalDataset,
    settings: RunSettings,
    *,
    label: str,
    rep: int,
    seed: int,
    distances: tuple[int, ...],
    case_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_revision": _git_revision(),
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "path": str(dataset.source_path),
            "sha256": dataset.sha256,
        },
        "run": {
            "label": label,
            "rep": rep,
            "seed": seed,
            "protocol": settings.protocol,
            "variant": settings.variant,
            "model": settings.model,
            "provider": settings.provider,
            "base_url": settings.base_url,
            "memory_provider": settings.memory_provider,
            "temperature": settings.temperature,
            "turns_per_session": settings.turns_per_session,
            "native_distractor_mode": settings.native_distractor_mode,
            "profile_intent_gate": settings.profile_intent_gate,
            "isolated_config_sha256": settings.config_sha256,
            "distances": list(distances),
            "case_ids": case_ids,
            "save_transcripts": settings.save_transcripts,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--protocol",
        choices=("replay", "full-history", "native-memory"),
        default="replay",
    )
    parser.add_argument(
        "--responses", type=Path, help="JSON responses for replay protocol"
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument(
        "--base-url",
        default="",
        help="temporary live-provider endpoint override; never stores credentials",
    )
    parser.add_argument("--memory-provider", default="openviking")
    parser.add_argument(
        "--variant", choices=("none", "control", "structured"), default="none"
    )
    parser.add_argument("--distances", type=_parse_distances, default=(10,))
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--tasks", default="", help="comma-separated case ids; default all"
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--turns-per-session", type=int, default=5)
    parser.add_argument(
        "--native-distractor-mode",
        choices=("live", "fixture"),
        default="live",
        help="native-memory noise: generate live replies or commit fixed fixture turns",
    )
    parser.add_argument(
        "--profile-intent-gate",
        choices=("inherit", "on", "off"),
        default="inherit",
        help="override profile_memory's opt-in pre-retrieval intent gate",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--save-transcripts", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    if args.reps < 1:
        parser.error("--reps must be at least 1")
    if args.turns_per_session < 1:
        parser.error("--turns-per-session must be at least 1")
    if args.protocol == "replay" and not args.responses:
        parser.error("--responses is required for replay protocol")
    if args.protocol != "replay" and (not args.model or not args.provider):
        parser.error("--model and --provider are required for live protocols")
    if args.protocol == "native-memory" and args.variant == "none":
        parser.error("native-memory requires --variant control or structured")
    if args.protocol != "native-memory" and args.variant != "none":
        parser.error("--variant is only meaningful for native-memory")
    if args.profile_intent_gate != "inherit" and (
        args.protocol != "native-memory" or args.memory_provider != "profile_memory"
    ):
        parser.error(
            "--profile-intent-gate on/off requires native-memory with "
            "--memory-provider profile_memory"
        )

    dataset = load_dataset(args.dataset)
    requested = [item.strip() for item in args.tasks.split(",") if item.strip()]
    cases = dataset.select(requested)
    replay_responses = (
        _load_replay_responses(args.responses) if args.responses else None
    )
    isolated_model_config: dict[str, Any] = {}
    isolated_memory_config: dict[str, Any] = {}
    config_sha256 = ""
    api_key = ""
    base_url = ""
    if args.protocol != "replay":
        try:
            (
                isolated_model_config,
                isolated_memory_config,
                config_sha256,
                api_key,
                base_url,
            ) = _load_current_config_overrides(args.memory_provider)
        except RuntimeError as exc:
            parser.error(str(exc))
    if args.profile_intent_gate != "inherit":
        isolated_memory_config["intent_gate"] = args.profile_intent_gate == "on"
        config_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "base_config_sha256": config_sha256,
                    "profile_intent_gate": args.profile_intent_gate,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    settings = RunSettings(
        protocol=args.protocol,
        variant=args.variant,
        model=args.model,
        provider=args.provider,
        memory_provider=args.memory_provider,
        temperature=args.temperature,
        turns_per_session=args.turns_per_session,
        save_transcripts=args.save_transcripts,
        isolated_model_config=isolated_model_config,
        isolated_memory_config=isolated_memory_config,
        config_sha256=config_sha256,
        api_key=api_key,
        base_url=args.base_url.strip() or base_url,
        native_distractor_mode=args.native_distractor_mode,
        profile_intent_gate=args.profile_intent_gate,
    )

    model_slug = _slug(args.model or "offline")
    out_dir = (
        args.results_dir.expanduser().resolve()
        / _slug(args.label)
        / args.protocol
        / args.variant
        / model_slug
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    for rep in range(1, args.reps + 1):
        out_path = out_dir / f"rep{rep}.json"
        if out_path.exists() and not args.overwrite:
            print(f"{out_path} exists; skipping (use --overwrite to replace)")
            continue
        records = []
        for distance in args.distances:
            for case in cases:
                print(f"[rep{rep} d={distance}] {case.case_id} ...", flush=True)
                record = run_record(
                    dataset,
                    case,
                    settings,
                    distance=distance,
                    rep=rep,
                    seed=args.seed,
                    label=args.label,
                    replay_responses=replay_responses,
                )
                print(
                    f"  score={record['score']:.3f} pass={record['passed']} "
                    f"recall={record.get('memory_recalled')} "
                    f"wall={record.get('wall_s', 0):.1f}s error={record.get('error')}",
                    flush=True,
                )
                records.append(record)

        payload = _build_manifest(
            dataset,
            settings,
            label=args.label,
            rep=rep,
            seed=args.seed,
            distances=args.distances,
            case_ids=[case.case_id for case in cases],
        )
        payload["records"] = records
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
