<img width="512" height="279" alt="PCMCP logo" src="https://github.com/user-attachments/assets/712ad48c-ddf6-422b-ab1f-d522f1aa8c2c" />

# PCMCP

## Demo video
https://youtu.be/Aj-g2MU_Sqk

**Start on Mac. Continue on Windows. Send it back. Nothing gets lost.**

PCMCP is a WebMCP-powered cross-platform task handoff demo. A browser exposes WebMCP tools that let an agent send and receive a neutral task capsule so the same task state can move between computers without remote-controlling the other machine.

## What it does

- Sends a live task state from one computer to another.
- Rehydrates the same state on the destination.
- Lets the destination change the state and send it back.
- Uses real `document.modelContext.registerTool(...)` WebMCP tools.

## WebMCP tools

- `send_capsule` sends the current task state.
- `receive_capsule` retrieves the latest task state.

## Run locally

```bash
npm start
```

Then open `http://localhost:4173` on your browser.

## Live demo

https://finder-editor-truly-instructor.trycloudflare.com

## License

MIT
