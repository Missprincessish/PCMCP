<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/46a77a06-cce8-4b0c-b13a-3c7477250b0f" />


# PCMCP

## STAY TUNED MORE COMING SOON!

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
