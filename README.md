<img width="512" height="279" alt="PCMCP logo" src="https://github.com/user-attachments/assets/712ad48c-ddf6-422b-ab1f-d522f1aa8c2c" />

# PCMCP

**Start on Mac. Continue anywhere. Send it back. Nothing gets lost.**

PCMCP is two things in one repo:

1. **The original WebMCP cross-platform task handoff demo** (browser MCP tools hand a task capsule between machines).
2. **PCMCP deterministic bots** — up to 100 deterministic, hash-logged, reversible file bots, plus an iMessage text trigger so a phone can ask a Mac for status, screenshots and more.

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
PCMCP-<secret> screensaver           start the Mac screensaver (reversible action; verified launch only)
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

Up to 100 bots: plain Python, no AI, no randomness. Every action is pre-approved by the owner, logged with SHA-256 hashes, and reversible (deletes and overwrites are staged, never destroyed). Doorbell + HMAC-signed instruction batches: the orchestrator can propose; only the local owner CLI can approve.

- Docs: [SWARM_README.md](SWARM_README.md) • Data format: [SPEC.md](SPEC.md) • Task ledger: [SWARM_TASKS.md](SWARM_TASKS.md) • Drive plan format: [DRIVE_PLAN_SPEC.md](DRIVE_PLAN_SPEC.md)
- Code: [`pcmcp_swarm/`](pcmcp_swarm/) (Python standard library only, no `pip install`)
- Selftest: `python3 -m pcmcp_swarm.selftest`

## MCP server

The swarm speaks Model Context Protocol, standard library only, no `pip install`:

```bash
python3 -m pcmcp_swarm mcp
```

Tools: `swarm_status`, `swarm_meter` (fleet health meter: per-bot verdict OK / WARN / STALLED / BROKEN with evidence and repair advice, so the client knows which arm of the swarm is broken), `swarm_plan_list`, `swarm_plan_show`, `swarm_scan` (proposes a plan, changes nothing), `swarm_execute` (dry-run by default), `swarm_undo` (restores), `swarm_approve` (refuses unless the owner sets `mcp.allow_approval: true` in the config), and `swarm_screenshot` (returns a PNG inline as MCP image content so a vision-capable client can see the screen; refuses unless `mcp.allow_screenshots: true`). Real execution additionally requires `mcp.allow_real_execution: true`. All gates default to false: out of the box an MCP client can look, propose and dry-run, never approve, destroy or photograph anything. A repaired bot returns to OK once its failed op is re-run and completed. Every call is logged to `logs/mcp.jsonl`. Also exposes the bot registry key as an MCP resource (`pcmcp://registry`), answers `prompts/list` and `logging/setLevel`, and speaks protocol versions 2024-11-05, 2025-03-26 and 2025-06-18. Conformance-tested against the documented client wire format: handshake, version fallback, notification silence, parse errors and every startup probe a real client sends.

## Built around Instinct

PCMCP is built for the new texting agents. I use Instinct daily: it texts my Mac a command (over iMessage), the deterministic swarm does the work, and results come back to the chat, screenshot or file attached. The agent never gets a shell, never gets approve rights, and can only propose work for the owner to approve locally. The trigger parser is hardened so a command quoted inside a normal sentence is never executed, and reply delivery is verified against the Messages database instead of trusting AppleScript's word for it.

## WebMCP handoff demo (the original PCMCP)

A browser exposes real `document.modelContext.registerTool(...)` WebMCP tools that let an agent send and receive a neutral task capsule, so the same task state can move between computers without remote-controlling the other machine.

```bash
npm start
```

Then open `http://localhost:4173` in your browser. (The live demo used a temporary Cloudflare tunnel; those URLs die by design, so run locally for a fresh one.)

## License

MIT

---

Built by **Angela Wilkinson** at **SynHumanity Labs** • [synomize.io](https://synomize.io)
