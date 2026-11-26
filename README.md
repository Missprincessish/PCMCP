<p align="center"><img src="https://raw.githubusercontent.com/Missprincessish/PCMCP/main/assets/pcmcp-3.gif" width="320" alt="PCMCP animated logo: two keycaps and a curved PCMCP smile"></p>

# PCMCP

**Start on Mac. Continue anywhere. Send it back. Nothing gets lost.**

[![PyPI](https://img.shields.io/pypi/v/pcmcp)](https://pypi.org/project/pcmcp/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Quick start

```bash
pip install pcmcp        # or: uvx pcmcp mcp   (no install)
pcmcp mcp                # MCP server over stdio
```

No dependencies beyond Python 3.9+. Out of the box an MCP client can look, propose and dry-run. It cannot approve, delete or take screenshots until you turn those gates on in your config.

### Connect it to your MCP client

PCMCP is a standard MCP server (stdio). Add it with the config format your client documents:

| Client | How |
|---|---|
| Claude Code | `claude mcp add --transport stdio pcmcp -- pcmcp mcp` ([docs](https://code.claude.com/docs/en/mcp)) |
| Claude Desktop | Settings, Developer, Edit Config, then add `{"mcpServers": {"pcmcp": {"command": "pcmcp", "args": ["mcp"]}}}` to `claude_desktop_config.json` and fully quit and reopen the app ([docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers)) |
| Cursor | same `mcpServers` block in `~/.cursor/mcp.json` or `.cursor/mcp.json` ([docs](https://cursor.com/docs/mcp)) |
| VS Code (Copilot) | workspace-root `.mcp.json` or Copilot user `mcp-config.json`: `{"mcpServers": {"pcmcp": {"type": "stdio", "command": "pcmcp", "args": ["mcp"]}}}` ([docs](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)) |
| Codex | `~/.codex/config.toml`: `[mcp_servers.pcmcp]` with `command = "pcmcp"` and `args = ["mcp"]` ([docs](https://developers.openai.com/codex/mcp)) |
| Anything else | run `pcmcp mcp` as a stdio server, or use the HTTP mode below |

Using `uvx` instead of an installed command: `"command": "uvx", "args": ["pcmcp", "mcp"]`.

If your app says it can't find `pcmcp`, put the full path in `command` instead. Run `which pcmcp` (or `which uvx`) in a terminal to get it. Some apps don't see your terminal's PATH.

### Remote clients: Streamable HTTP

For clients that connect to a URL instead of launching a command:

```bash
export PCMCP_HTTP_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
pcmcp mcp --http --port 8765      # http://127.0.0.1:8765/mcp
```

Safe by default: binds to 127.0.0.1 only, every request needs `Authorization: Bearer <token>`, Host and Origin headers are checked, no sessions, and the same owner gates apply as over stdio. `--allow-host` and `--allow-origin` add extra accepted Host and Origin values. The Origin check only applies when a request carries an Origin header (browsers send one; most non-browser clients and some proxies or tunnels do not), so it protects against web pages, not against other programs. The bearer token is the real access control. Anyone with the URL and token can call the tools, so keep the token secret and use TLS. Check it with `python3 -m pcmcp_swarm.mcp_http_selftest`.


PCMCP is two things in one repo:

1. **The original WebMCP cross-platform task handoff demo** (browser MCP tools hand a task capsule between machines).
2. **PCMCP deterministic bots** — up to 100 deterministic, hash-logged file bots with undo, plus an iMessage text trigger so a phone can ask a Mac for status, screenshots and more.

## What actually works (tested, no hand-waving)

| Feature | Status |
|---|---|
| WebMCP capsule handoff demo (`send_capsule` / `receive_capsule`) | works; run locally with `npm start` |
| Swarm selftest (bots, registry, plans, approval, undo, crash-resume, doorbell, signed check-in) | 57/57 on macOS (arm64), Python 3.15 |
| iMessage text trigger → screenshot, attached back to the chat as `.png` | verified live: attachment confirmed present in the Messages database, not just "sent" |
| iMessage text trigger → screensaver start | verified live (bounded launch, honest failure reporting) |
| Text trigger commands: `status`, `live`, `inventory`, `task`, `browse`, `run` | implemented and unit-tested |
| Trigger parser: a command quoted inside a normal sentence is never executed | 10/10 unit tests; line-anchored full-match parsing |
| Cross-platform selftest | 57/57 on macOS (arm64) and Linux, verified from a fresh public-repo download; Windows not yet run |

## The iMessage text trigger

The Mac reads the Messages database read-only. A text from an allowed sender can do:

```text
PCMCP-<secret> status                live swarm status
PCMCP-<secret> live                  listener health: uptime, poll rate, active jobs, last acted command + age, free disk
PCMCP-<secret> screensaver           start the Mac screensaver (verified launch only)
PCMCP-<secret> screenshot            screenshot of the screen, attached back as .png
PCMCP-<secret> inventory drive        read-only Google Drive inventory (attached .txt listing)
PCMCP-<secret> inventory icloud      look-only iCloud Drive listing (names, sizes, downloaded/cloud-only)
PCMCP-<secret> inventory missing     compare iCloud with Documents and Downloads (read-only)
PCMCP-<secret> task <name>           one named read-only task (attached .txt)
PCMCP-<secret> browse <alias.index>  browse approved folders by numbered child directories
PCMCP-<secret> run <plan-prefix>     start a plan the owner already signed with a passphrase
```

Safety rules that matter:

- **The secret is a doorbell, not an approval.** There is no text command for approve.
- One command per line: a command quoted inside a normal sentence is never executed.
- The Mac stores only the SHA-256 of the secret; replies never contain it; each message acts at most once, only if new, incoming, from an allowed sender, inside the rate limit.
- Nothing a text can reach writes, moves or deletes — except `run`, which executes only a plan signed with the owner passphrase, one shot per approval, never after her stop or a rollback.

See the docstring of `pcmcp_swarm/drive/trigger.py` for the full safety model.

## Deterministic bot swarm

Up to 100 bots: plain Python, no AI, no randomness. File bots preview changes with a dry run. Deletes and overwrites are staged first, and undo can restore them. Doorbell + HMAC-signed instruction batches: the orchestrator can propose; only the local owner CLI can approve.

- Docs: [SWARM_README.md](SWARM_README.md) • Data format: [SPEC.md](SPEC.md) • Task ledger: [SWARM_TASKS.md](SWARM_TASKS.md) • Drive plan format: [DRIVE_PLAN_SPEC.md](DRIVE_PLAN_SPEC.md)
- Code: [`pcmcp_swarm/`](pcmcp_swarm/) (Python standard library only, no dependencies)
- Selftest: `python3 -m pcmcp_swarm.selftest`

## MCP server

The swarm speaks Model Context Protocol, standard library only, no third-party dependencies:

```bash
pcmcp mcp                          # same as: python3 -m pcmcp_swarm mcp
```

Tools: `swarm_status`, `swarm_meter` (fleet health meter: per-bot verdict OK / WARN / STALLED / BROKEN with evidence and repair advice, so the client knows which arm of the swarm is broken), `swarm_plan_list`, `swarm_plan_show`, `swarm_scan` (proposes a plan, changes nothing), `swarm_execute` (dry-run by default), `swarm_undo` (restores), `swarm_approve` (refuses unless the owner sets `mcp.allow_approval: true` in the config), and `swarm_screenshot` (returns a PNG inline as MCP image content so a vision-capable client can see the screen; refuses unless `mcp.allow_screenshots: true`). Real execution additionally requires `mcp.allow_real_execution: true`. All gates default to false: out of the box an MCP client can look, propose and dry-run, never approve, destroy or photograph anything. A repaired bot returns to OK once its failed op is re-run and completed. Every call is logged to `logs/mcp.jsonl`. Also exposes the bot registry key as an MCP resource (`pcmcp://registry`), answers `prompts/list` and `logging/setLevel`, and speaks protocol versions 2024-11-05, 2025-03-26 and 2025-06-18. Conformance-tested against the documented client wire format: handshake, version fallback, notification silence, parse errors and every startup probe a real client sends.

## Built around Instinct

PCMCP is built for the new texting agents. I use Instinct daily: it texts my Mac a command (over iMessage), the deterministic swarm does the work, and results come back to the chat, screenshot or file attached. The text trigger and MCP server expose fixed commands and tools. MCP approval, real execution and screenshots are separate owner-controlled settings, all off by default. The trigger parser is hardened so a command quoted inside a normal sentence is never executed, and reply delivery is verified against the Messages database instead of trusting AppleScript's word for it.

## WebMCP handoff demo (the original PCMCP)

A browser exposes real `document.modelContext.registerTool(...)` WebMCP tools that let an agent send and receive a neutral task capsule, so the same task state can move between computers without remote-controlling the other machine.

```bash
npm start
```

Then open `http://localhost:4173` in your browser. (The live demo used a temporary Cloudflare tunnel; those URLs die by design, so run locally for a fresh one.)

## License

MIT

---

Built by **Angie Wilkinson** at **SynHumanity Labs** • [synomize.io](https://synomize.io)
<!-- mcp-name: io.github.Missprincessish/pcmcp -->
