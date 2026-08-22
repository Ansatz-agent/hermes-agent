"""Live viewer for snapshots captured by :mod:`agent.prompt_monitor`."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from agent.prompt_monitor import (
    PROMPT_MONITOR_GLOB,
    load_prompt_monitor_settings,
    prompt_monitor_directory,
)
from hermes_constants import display_hermes_home


def _snapshot_paths(directory: Path) -> list[Path]:
    try:
        return sorted(directory.glob(PROMPT_MONITOR_GLOB))
    except OSError:
        return []


def _read_snapshot(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"prompt-monitor: could not read {path}: {exc}", file=sys.stderr)
        return None


def _matches(
    record: dict[str, Any],
    *,
    source: str,
    session: Optional[str],
    task: Optional[str],
) -> bool:
    if source != "all" and record.get("source") != source:
        return False
    if session and session not in str(record.get("session_id") or ""):
        return False
    if task and task not in str(record.get("task") or ""):
        return False
    return True


def _filtered_records(
    paths: Iterable[Path],
    *,
    source: str,
    session: Optional[str],
    task: Optional[str],
) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        record = _read_snapshot(path)
        if record is not None and _matches(
            record, source=source, session=session, task=task
        ):
            records.append((path, record))
    return records


def _print_snapshot(path: Path, record: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        return

    source = str(record.get("source") or "llm")
    task = str(record.get("task") or "conversation")
    session = str(record.get("session_id") or "-")
    provider = str(record.get("provider") or "-")
    model = str(record.get("model") or "-")
    attempt = record.get("attempt")
    captured_at = str(record.get("captured_at") or "-")
    attempt_text = "-" if attempt is None else str(attempt)
    print()
    print("=" * 88)
    print(
        f"LLM PROMPT  {captured_at}  source={source}  task={task}  "
        f"attempt={attempt_text}"
    )
    print(f"session={session}  provider={provider}  model={model}")
    print(f"snapshot={path}")
    print("-" * 88)
    request = record.get("request")
    body = request.get("body") if isinstance(request, dict) else request
    print(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    print("=" * 88, flush=True)


def monitor_prompts(
    *,
    existing: int = 0,
    once: bool = False,
    source: str = "all",
    session: Optional[str] = None,
    task: Optional[str] = None,
    json_output: bool = False,
    poll_interval: float = 0.25,
) -> int:
    """Print retained snapshots and optionally follow newly captured prompts."""

    settings = load_prompt_monitor_settings()
    if not settings.enabled:
        print("Prompt monitor capture is disabled for the active Hermes profile.")
        print("Enable it with:")
        print("  hermes config set logging.prompt_monitor.enabled true")
        return 2

    directory = prompt_monitor_directory()
    initial_paths = _snapshot_paths(directory)
    seen = set(initial_paths)
    records = _filtered_records(
        initial_paths, source=source, session=session, task=task
    )
    count = max(0, int(existing))
    if once and count == 0:
        count = 1
    if count:
        for path, record in records[-count:]:
            _print_snapshot(path, record, json_output=json_output)

    if once:
        if not records:
            print(f"No matching prompt snapshots in {directory}")
        return 0

    relative_dir = f"{display_hermes_home()}/logs/prompt-monitor"
    print(f"Watching {relative_dir} for finalized LLM prompts (Ctrl+C to stop).")
    if settings.include_auxiliary:
        print("Sources: main + auxiliary")
    else:
        print("Sources: main only (logging.prompt_monitor.include_auxiliary=false)")
    print("Snapshots are redacted best-effort and retained as private local files.")

    try:
        while True:
            current = _snapshot_paths(directory)
            for path in current:
                if path in seen:
                    continue
                seen.add(path)
                record = _read_snapshot(path)
                if record is None or not _matches(
                    record, source=source, session=session, task=task
                ):
                    continue
                _print_snapshot(path, record, json_output=json_output)
            time.sleep(max(0.05, poll_interval))
    except KeyboardInterrupt:
        print("\n--- prompt monitor stopped ---")
        return 0


__all__ = ["monitor_prompts"]
