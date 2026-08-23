"""
Cron job scheduling system for Hermes Agent.

This module provides scheduled task execution, allowing the agent to:
- Run automated tasks on schedules (cron expressions, intervals, one-shot)
- Self-schedule reminders and follow-up tasks
- Execute tasks in isolated sessions (no prior context)

Cron jobs are executed automatically by the gateway daemon:
    hermes gateway install    # Install as a user service
    sudo hermes gateway install --system  # Linux servers: boot-time system service
    hermes gateway            # Or run in foreground

The gateway ticks the scheduler every 60 seconds. A file lock prevents
duplicate execution if multiple processes overlap.
"""

from importlib import import_module

__all__ = [
    "create_job",
    "get_job", 
    "list_jobs",
    "remove_job",
    "update_job",
    "pause_job",
    "resume_job",
    "trigger_job",
    "tick",
    "JOBS_FILE",
]

_EXPORTS = {
    "create_job": ("cron.jobs", "create_job"),
    "get_job": ("cron.jobs", "get_job"),
    "list_jobs": ("cron.jobs", "list_jobs"),
    "remove_job": ("cron.jobs", "remove_job"),
    "update_job": ("cron.jobs", "update_job"),
    "pause_job": ("cron.jobs", "pause_job"),
    "resume_job": ("cron.jobs", "resume_job"),
    "trigger_job": ("cron.jobs", "trigger_job"),
    "JOBS_FILE": ("cron.jobs", "JOBS_FILE"),
    "tick": ("cron.scheduler", "tick"),
}


def __getattr__(name: str):
    """Load public cron APIs only after the caller crosses its auth boundary."""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error

    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
