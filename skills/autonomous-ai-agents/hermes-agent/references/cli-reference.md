# Hermes CLI Reference

Live sources when anything looks stale: `ansatz --help`, `ansatz <command> --help`,
https://hermes-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
hermes [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
ansatz chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
ansatz setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
ansatz model                Interactive model/provider picker
ansatz fallback [add|remove|list]  Fallback provider chain
ansatz config [show|edit|get|set|unset|path|env-path|check|migrate]
ansatz login / logout       Remote-account sign-in / clear remote-account session
ansatz doctor [--fix]       Check dependencies and config
ansatz status [--all]       Component status
```

### Tools & Skills

```
ansatz tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

ansatz skills list|browse|search QUERY|inspect ID
ansatz skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
ansatz skills config        Enable/disable skills per platform
ansatz skills check|update|uninstall|publish PATH
ansatz skills tap add REPO  Add a GitHub repo as a skill source
ansatz bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
ansatz mcp add NAME (--url or --command) | remove | list | test NAME
ansatz mcp catalog | install NAME     Curated catalog install
ansatz mcp configure NAME             Toggle tool selection
ansatz mcp serve                      Run Hermes as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
ansatz gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `ansatz photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
ansatz sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
ansatz cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
ansatz webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
ansatz profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
ansatz profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
ansatz auth                 Interactive credential manager
ansatz auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
ansatz auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
ansatz desktop / gui        Native desktop app
ansatz dashboard            Web admin panel + embedded chat (--stop / --status)
ansatz proxy                OpenAI-compatible local proxy backed by an OAuth provider
ansatz portal               Quick setup / sign in via Nous Portal
ansatz kanban <verb>        Multi-agent work-queue board
ansatz project              Named multi-folder workspaces
ansatz skin list|use|set    Switch/tweak skins (see references/themes.md)
ansatz pets <verb>          Pet mascots (see references/petdex.md)
ansatz memory setup|status|off|reset   Memory provider
ansatz secrets bitwarden|onepassword   External secret stores
ansatz moa                  Mixture-of-Agents slots
ansatz hooks / security / backup / import / checkpoints / console
ansatz logs [-f] [errors]   View agent/error logs
ansatz send                 One-off message through a gateway platform
ansatz pairing / plugins / insights / journey / computer-use
ansatz acp                  ACP server (IDE integration)
ansatz completion bash|zsh|fish
ansatz update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `ansatz photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `ansatz config edit` · [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `ansatz tools list` · [Tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `ansatz skills browse` · [Skills catalog](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `ansatz model` · [Providers guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `ansatz config env-path` · [Env vars reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.hermes/logs/gateway.log` (or `ansatz logs`) |
| Sessions | `ansatz sessions browse` (reads state.db) |
