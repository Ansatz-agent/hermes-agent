"""Deterministic helpers for the interactive CLI's user-profile browser.

The slash command must remain useful before the first model turn.  This module
therefore reads through the active memory provider's existing ``profile_browse``
tool contract, either via the live agent's ``MemoryManager`` or via a temporary
read-only provider instance.  It never initializes an agent or invokes an LLM.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping


PROFILE_BROWSE_TOOL = "profile_browse"


class ProfileBrowseError(RuntimeError):
    """A user-facing failure while reading the configured profile provider."""


def _parse_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        try:
            payload = json.loads(str(raw or ""))
        except (TypeError, ValueError) as exc:
            raise ProfileBrowseError(
                "The profile provider returned an invalid browse response."
            ) from exc
    if not isinstance(payload, dict):
        raise ProfileBrowseError(
            "The profile provider returned an invalid browse response."
        )
    if payload.get("error"):
        message = str(payload["error"])
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            rendered = ", ".join(str(value) for value in candidates)
            message = f"{message}: {rendered}"
        raise ProfileBrowseError(message)
    if not isinstance(payload.get("categories", []), list) or not isinstance(
        payload.get("items", []), list
    ):
        raise ProfileBrowseError(
            "The profile provider returned an invalid browse response."
        )
    return payload


def _live_memory_manager(agent: Any) -> Any:
    manager = getattr(agent, "_memory_manager", None) if agent is not None else None
    has_tool = getattr(manager, "has_tool", None)
    if manager is not None and callable(has_tool) and has_tool(PROFILE_BROWSE_TOOL):
        return manager
    return None


def read_profile_payload(
    *,
    agent: Any = None,
    session_id: str = "",
    scope: str = "",
) -> Dict[str, Any]:
    """Read the active profile tree without starting an agent or calling a model."""

    args = {"scope": scope} if scope else {}
    manager = _live_memory_manager(agent)
    if manager is not None:
        return _parse_payload(manager.handle_tool_call(PROFILE_BROWSE_TOOL, args))

    from hermes_cli.config import cfg_get, load_config
    from hermes_constants import get_hermes_home
    from plugins.memory import load_memory_provider

    config = load_config()
    provider_name = str(cfg_get(config, "memory", "provider", default="") or "").strip()
    if not provider_name:
        raise ProfileBrowseError(
            "No profile-memory provider is configured. "
            "Set memory.provider to profile_memory first."
        )

    provider = load_memory_provider(provider_name, register_skills=False)
    if provider is None:
        raise ProfileBrowseError(
            f"The configured memory provider '{provider_name}' could not be loaded."
        )
    try:
        schemas = provider.get_tool_schemas()
        names = {
            str(schema.get("name") or "")
            for schema in schemas
            if isinstance(schema, dict)
        }
        if PROFILE_BROWSE_TOOL not in names:
            raise ProfileBrowseError(
                f"The configured memory provider '{provider_name}' does not "
                "support profile browsing."
            )
        provider.initialize(
            session_id=session_id or "profile-cli-read",
            hermes_home=str(get_hermes_home()),
            platform="cli",
            agent_context="primary",
            read_only=True,
        )
        return _parse_payload(provider.handle_tool_call(PROFILE_BROWSE_TOOL, args))
    except ProfileBrowseError:
        raise
    except Exception as exc:
        raise ProfileBrowseError(
            f"The configured profile provider could not be read: {exc}"
        ) from exc
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


def _count_label(category: Mapping[str, Any]) -> str:
    active = int(category.get("active_items") or 0)
    candidate = int(category.get("candidate_items") or 0)
    parts = []
    if active:
        parts.append(f"{active} active")
    if candidate:
        parts.append(f"{candidate} candidate")
    return f"  [{' · '.join(parts)}]" if parts else ""


def format_profile_tree(categories: Iterable[Mapping[str, Any]]) -> List[str]:
    """Render ``profile://`` categories as a compact deterministic tree."""

    by_path: Dict[str, Mapping[str, Any]] = {}
    paths = set()
    for category in categories:
        uri = str(category.get("uri") or "")
        path = uri[len("profile://") :] if uri.startswith("profile://") else uri
        path = path.strip("/")
        if not path:
            continue
        by_path[path] = category
        parts = path.split("/")
        paths.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))

    if not paths:
        return ["profile://", "└── (empty)"]

    children: Dict[str, List[str]] = defaultdict(list)
    for path in paths:
        parent, _, name = path.rpartition("/")
        children[parent].append(name)
    for names in children.values():
        names.sort()

    lines = ["profile://"]

    def walk(parent: str, prefix: str) -> None:
        names = children.get(parent, [])
        for index, name in enumerate(names):
            last = index == len(names) - 1
            path = f"{parent}/{name}" if parent else name
            connector = "└── " if last else "├── "
            lines.append(
                f"{prefix}{connector}{name}/{_count_label(by_path.get(path, {}))}"
            )
            walk(path, prefix + ("    " if last else "│   "))

    walk("", "")
    return lines


def format_profile_directory(payload: Mapping[str, Any]) -> List[str]:
    """Format the complete directory view used by bare ``/profile``."""

    categories = payload.get("categories") or []
    active = sum(int(category.get("active_items") or 0) for category in categories)
    candidate = sum(
        int(category.get("candidate_items") or 0) for category in categories
    )
    lines = ["", "User preference profile", "─" * 40]
    lines.extend(format_profile_tree(categories))
    lines.extend(
        [
            "",
            f"{len(categories)} categories · {active} active · {candidate} candidate",
            "Use /profile <category> to show the preferences in one category.",
            "Use /profile runtime to show the active Hermes runtime profile.",
            "",
        ]
    )
    return lines


def format_profile_category(
    payload: Mapping[str, Any], requested_scope: str
) -> List[str]:
    """Format one category subtree and its atomic profile items."""

    if payload.get("scope_found") is False:
        return [
            "",
            f"Profile category not found: {requested_scope}",
            "Use /profile to inspect the available category paths.",
            "",
        ]

    resolved_scope = str(payload.get("resolved_scope") or requested_scope)
    lines = ["", f"User preferences · {resolved_scope}", "─" * 40]
    lines.extend(format_profile_tree(payload.get("categories") or []))
    items = payload.get("items") or []
    lines.extend(["", "Stored preferences:"])
    if not items:
        lines.append("  (no active or candidate items in this category)")
    for item in items:
        lines.extend(
            [
                f"  [{item.get('status', 'unknown')}] {item.get('item_id', '')}",
                f"    Category: {item.get('category', '')}",
                f"    Applies when: {item.get('applies_when', '')}",
                f"    Rule: {item.get('rule', '')}",
                f"    Confidence: {float(item.get('confidence') or 0.0):.2f}",
            ]
        )
    if payload.get("truncated"):
        lines.append("  … results truncated at the provider limit")
    lines.append("")
    return lines
