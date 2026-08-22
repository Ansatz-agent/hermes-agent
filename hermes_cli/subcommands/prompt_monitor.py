"""``hermes prompt-monitor`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_prompt_monitor_parser(
    subparsers, *, cmd_prompt_monitor: Callable
) -> None:
    """Attach the live finalized-request viewer to ``subparsers``."""

    parser = subparsers.add_parser(
        "prompt-monitor",
        help="Print every finalized LLM prompt from a separate terminal",
        description=(
            "Follow redacted snapshots of finalized main-agent and auxiliary "
            "LLM request bodies. Capture is disabled by default; enable "
            "logging.prompt_monitor.enabled in config.yaml first."
        ),
    )
    parser.add_argument(
        "-n",
        "--existing",
        type=int,
        default=0,
        metavar="COUNT",
        help="Print the newest COUNT retained snapshots before following",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print retained snapshots and exit (latest one when -n is omitted)",
    )
    parser.add_argument(
        "--source",
        choices=("all", "main", "auxiliary"),
        default="all",
        help="Show all calls (default), main-agent calls, or auxiliary calls",
    )
    parser.add_argument(
        "--session",
        metavar="ID",
        help="Only show session IDs containing this substring",
    )
    parser.add_argument(
        "--task",
        metavar="NAME",
        help="Only show auxiliary/main task names containing this substring",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one compact JSON record per captured request",
    )
    parser.set_defaults(func=cmd_prompt_monitor)


__all__ = ["build_prompt_monitor_parser"]
