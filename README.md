<img width="1536" height="1024" alt="PCMCP" src="https://github.com/user-attachments/assets/46a77a06-cce8-4b0c-b13a-3c7477250b0f" />

# PCMCP

**Start on Mac. Continue on Windows. Send it back. Nothing gets lost.**

PCMCP is a WebMCP-powered cross-platform task handoff demo. It moves a neutral task capsule between browsers/computers without remote-controlling the destination machine.

> **September 2026 update:** We just discovered that the README had been missing the single Cloudflare tunnel command required to reproduce the original two-computer demo. 😅 Sorry to everyone who cloned PCMCP and only found the localhost setup. The cross-computer handoff from the demo was real, and the current repo still supports it. The missing setup is documented below.

## Demo

https://youtu.be/Aj-g2MU_Sqk

The original demo shows a Mac sending the capsule to a Windows 10 PC and receiving it back.

## What works today

- Live task-state handoff between two computers through one shared PCMCP server.
- Mac → Windows → Mac round-trip demonstrated.
- Destination edits appear back on the other computer in about a second under normal conditions.
- Browser UI works without WebMCP for manual testing.
- When WebMCP is available, PCMCP exposes:
  - `send_capsule`
  - `receive_capsule`
- No remote desktop is involved.

### Important scope note

PCMCP currently **transfers task state**. The platform selector labels the intended destination, but PCMCP does not itself execute programs or commands on the destination OS.

## One-computer quickstart

Requirements: Node.js.

```bash
git clone https://github.com/Missprincessish/PCMCP.git
cd PCMCP
npm start
```

Open:

```text
http://localhost:4173
```

## Two-computer setup

This is the step that was accidentally missing from the README.

### 1. Start PCMCP on the host computer

```bash
npm start
```

PCMCP listens on port `4173`.

### 2. Create a Cloudflare Quick Tunnel

With `cloudflared` installed:

```bash
cloudflared tunnel --url http://localhost:4173
```

Cloudflare will print a temporary `https://....trycloudflare.com` URL.

### 3. Open PCMCP on both computers

**Host computer:**

```text
http://localhost:4173
```

**Second computer:**

Open the temporary `trycloudflare.com` URL produced above.

Both browsers now connect to the same capsule. PCMCP polls for state changes roughly every 700 ms, so sending/updating the capsule on one computer appears on the other almost immediately.

> Quick Tunnel URLs are ephemeral. The old URL previously listed here expired, which is why it no longer worked.

## WebMCP

PCMCP uses `document.modelContext.registerTool(...)` when the browser exposes WebMCP.

In a WebMCP-enabled environment:

- `send_capsule` sends the current task state.
- `receive_capsule` retrieves the latest state.

A normal browser can still use the PCMCP interface manually, but the WebMCP tools themselves require browser/runtime support for WebMCP.

## Current architecture

The demo intentionally stays tiny:

```text
Computer A browser
        │
        ▼
PCMCP Node server
        │
        ▼
Cloudflare tunnel
        │
        ▼
Computer B browser
```

Both computers read and update the same capsule on the host server.

## Current limitations

This is still a prototype:

- Capsule state is currently in memory and resets when the Node server restarts.
- The current demo uses one shared capsule rather than isolated user sessions.
- The demo server is not intended to be exposed as a production public relay.
- PCMCP transports state; destination-side execution/orchestration belongs to higher-level tooling built around it.

## If you cloned PCMCP before this update

**You probably did not have broken code. You were missing an embarrassingly important line of documentation.** 😂

Pull the latest README and use:

```bash
cloudflared tunnel --url http://localhost:4173
```

after `npm start`.

If you already have the same code revision, you do not need to reclone just to reproduce the original two-computer handoff.

## License

MIT
