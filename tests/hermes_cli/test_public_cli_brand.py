from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GOVERNED = (
    ROOT / "README.md",
    ROOT / "hermes_cli",
    ROOT / "gateway",
    ROOT / "scripts" / "install.sh",
    ROOT / "apps" / "desktop" / "src",
    ROOT / "apps" / "desktop" / "electron",
    ROOT / "ui-tui" / "src",
    ROOT / "website" / "docs",
    ROOT
    / "website"
    / "i18n"
    / "zh-Hans"
    / "docusaurus-plugin-content-docs"
    / "current",
    ROOT / "skills" / "autonomous-ai-agents" / "hermes-agent" / "references",
)
EXCLUDED_FILES = {
    ROOT / "hermes_cli" / "cli_identity.py",
    ROOT / "hermes_cli" / "entrypoints.py",
}
INTERNAL_COMPAT_COMMANDS = {
    ROOT / "hermes_cli" / "dashboard_procs.py": {
        '"hermes dashboard",',
        '"hermes serve",',
    },
    ROOT / "hermes_cli" / "main.py": {
        '"hermes dashboard",',
        '"hermes serve",',
    },
}
PUBLIC_SUBCOMMANDS = {
    "acp",
    "approvals",
    "auth",
    "backup",
    "bundles",
    "chat",
    "checkpoints",
    "claw",
    "completion",
    "computer-use",
    "config",
    "console",
    "cron",
    "curator",
    "dashboard",
    "debug",
    "desktop",
    "doctor",
    "dump",
    "egress",
    "fallback",
    "gateway",
    "gui",
    "help",
    "honcho",
    "hooks",
    "import",
    "import-agent",
    "insights",
    "journey",
    "kanban",
    "learning",
    "login",
    "logout",
    "logs",
    "lsp",
    "mcp",
    "memory",
    "memory-graph",
    "migrate",
    "moa",
    "model",
    "monitoring",
    "pairing",
    "pause",
    "pets",
    "photon",
    "plugins",
    "portal",
    "profile",
    "project",
    "prompt-size",
    "provider",
    "proxy",
    "resume",
    "secrets",
    "security",
    "send",
    "serve",
    "service",
    "sessions",
    "setup",
    "skin",
    "skills",
    "slack",
    "spotify",
    "status",
    "sync",
    "teams-pipeline",
    "tools",
    "uninstall",
    "update",
    "verify",
    "version",
    "webhook",
    "whatsapp",
    "whatsapp-cloud",
}
COMMAND = re.compile(
    r"(?<![\w-])hermes\s+(?=(?:--?[a-z][\w-]*|<|"
    + "|".join(sorted(map(re.escape, PUBLIC_SUBCOMMANDS), key=len, reverse=True))
    + r")\b)"
)
SLACK_PROTOCOL = re.compile(r"(?<![\w./~-])/hermes(?=\s)")


def _files():
    for path in GOVERNED:
        if path.is_file():
            yield path
            continue
        for child in path.rglob("*"):
            if child.suffix in {
                ".py",
                ".sh",
                ".ts",
                ".tsx",
                ".js",
                ".md",
                ".mdx",
                ".txt",
            }:
                if "test" not in child.name and child not in EXCLUDED_FILES:
                    yield child


def test_public_surfaces_do_not_recommend_legacy_cli():
    violations = []
    for path in _files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if line.strip() in INTERNAL_COMPAT_COMMANDS.get(path, set()):
                continue
            candidate = line.replace(
                "docker exec hermes ",
                "docker exec <container> ",
            )
            candidate = candidate.replace(
                "docker exec -u hermes ",
                "docker exec -u <user> ",
            )
            candidate = candidate.replace(
                "sudo -u hermes ",
                "sudo -u <user> ",
            )
            candidate = SLACK_PROTOCOL.sub("/<slash-command>", candidate)
            if COMMAND.search(candidate):
                relative = path.relative_to(ROOT)
                violations.append(f"{relative}:{line_number}:{line.strip()}")
    assert violations == []
