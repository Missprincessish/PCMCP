# PCMCP Deterministic Bot Swarm

Up to 100 small bots that do bulk file work for an orchestrator (Instinct). The bots are plain software:
no AI, no randomness. Each bot has one job letter for life, and every action is
**pre-approved by you, logged with SHA-256 hashes, and reversible**.

- Code: [`pcmcp_swarm/`](pcmcp_swarm/) (Python standard library only, no `pip install`)
- Data format: [`SPEC.md`](SPEC.md) (plain JSON, versioned)
- Task ledger: [`SWARM_TASKS.md`](SWARM_TASKS.md)

## Tested on

| Part | macOS 27.2 (arm64), Python 3.15 | Windows | Linux |
|---|---|---|---|
| Bots, registry, plans, undo, crash-resume, doorbell, signed check-in (`selftest`) | ✅ 57/57 | not yet run | not yet run |
| Gmail IMAP watcher | logic tested against a fake IMAP server only; **not yet run against real Gmail** | — | — |
| Screenshot bot | code path tested; real capture **failed without the Screen Recording permission** (see below) | untested adapter | untested adapter |

To test a VM, run one command and send the output:
```bash
python3 -m pcmcp_swarm.selftest
```

## 1. Setup (macOS)

You need Python 3.9+ (`python3 --version`). Run every command from the repo root:
```bash
cd ~/Documents/PCMCP_repo
```

Create your config, registry and check-in secret, and name the one folder the bots are allowed to touch.
Use a **copy** or a test corner of the Drive first:
```bash
python3 -m pcmcp_swarm init --allow-root "$HOME/Library/CloudStorage/GoogleDrive-YOU/My Drive/PCMCP-Test"
```
This writes:
- `~/.pcmcp/config.json`, the single config file (channels, paths, trigger, fleet size)
- `~/.pcmcp/checkin_secret`, the HMAC secret (file mode 600; never commit it)
- `~/.pcmcp/data/registry.json` + `REGISTRY_KEY.md`, plus logs, plans, quarantine and health

See who can do what at any time:
```bash
python3 -m pcmcp_swarm registry
```

## 2. Set the trigger string

Open `~/.pcmcp/config.json` and set:
```json
{ "doorbell": { "trigger": "your-unique-never-random-phrase" } }
```
Until you change it from the `CHANGE-ME…` default, no ring can ever be valid. Remember that **the trigger
is only a doorbell**. A ring can only make PCMCP check in for signed instructions. The listener logs
ring text only as a length and SHA-256, so the trigger never appears in a log.

## 3. Wire the check-in channel (how Instinct sends instructions)

The shipped channel is a **signed local inbox folder**. To let Instinct reach your Mac through Drive,
point the inbox and outbox at Drive-synced folders:
```bash
python3 -m pcmcp_swarm init --inbox "$HOME/Library/CloudStorage/GoogleDrive-YOU/My Drive/PCMCP/inbox" \
                            --outbox "$HOME/Library/CloudStorage/GoogleDrive-YOU/My Drive/PCMCP/outbox"
```
Give Instinct the contents of `~/.pcmcp/checkin_secret` out of band. Instinct builds a batch exactly as in
[SPEC.md §4](SPEC.md) and signs it. Unsigned, tampered, expired, replayed or registry-violating batches
are logged and rejected. A batch can only **propose**; it can never approve.

To produce a test batch yourself (the reference signer):
```bash
cat > /tmp/b.json <<'EOF'
{"batch_id":"test-001","kind":"status","issued_at":"2026-11-09T18:00:00.000000Z","expires_at":"2026-11-10T17:00:00.000000Z","params":{},"operations":[]}
EOF
python3 -m pcmcp_swarm sign /tmp/b.json
python3 -m pcmcp_swarm checkin
```

### Adding another check-in channel later
1. Create `pcmcp_swarm/checkin/<name>.py` with a class subclassing `CheckInChannel` that implements
   `fetch_instructions()` (returns `[(raw_bytes, {"ref": ...})]`) and `post_receipts(receipt, attachments)`.
2. Register it in `ADAPTERS()` in `pcmcp_swarm/checkin/__init__.py`.
3. Set `"checkin": {"channel": "<name>"}` in the config.

The adapter only moves bytes. It must **not** verify anything itself: every channel's bytes go through
the same `signing.verify()` in `checkin/handler.py`. Stubs (not implemented) already exist for
`https_endpoint`, `mcp_tool` and `email_attachment` in `checkin/stubs.py`. The channel that delivered
each batch is recorded in `logs/checkin.jsonl` and in every receipt (`delivered_by`).

## 4. Wire the Gmail doorbell (IMAP + app password)

1. Create a **dedicated** Gmail account used only for doorbell mail. An app password can read that
   whole mailbox, so never use your main account.
2. On that account, turn on 2-Step Verification, then create an app password (Google Account →
   Security → App passwords).
3. In Gmail, create a label such as `PCMCP-Doorbell` and a filter that applies it to the mail you expect.
4. Store the app password in the macOS Keychain (it prompts for the password, so nothing lands in shell history):
   ```bash
   security add-generic-password -s pcmcp-doorbell-imap -a doorbell.account@gmail.com -w
   ```
5. In `~/.pcmcp/config.json`:
   ```json
   {"doorbell": {"channels": ["local_spool", "gmail_imap"],
                 "gmail_imap": {"account": "doorbell.account@gmail.com", "label": "PCMCP-Doorbell",
                                "allowed_senders": ["the-address-instinct-sends-from@example.com"]}}}
   ```
To ring, send an email whose **subject** is exactly the trigger string. The watcher reads only the
From and Subject headers of unseen mail in that one label (`BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]`),
never bodies or attachments. A sender not on the allowlist is logged as `unexpected_sender`. The
watcher sits behind the doorbell channel interface; `gmail_api` (OAuth) is a stub you can swap in later.

## 5. Run the listener

```bash
python3 -m pcmcp_swarm listen
```
It polls every `poll_seconds` (default 30) and logs every ring to `~/.pcmcp/data/logs/doorbell.jsonl`.
On a valid ring it checks in once, acts on valid batches, writes receipts, and goes back to sleep.
Wakes are limited by `doorbell.rate_limit` (default 6 per 10 minutes).
To ring locally: `python3 -m pcmcp_swarm ring "<trigger>"`. Or enable `local_http` and run
`curl -X POST --data "<trigger>" http://127.0.0.1:8792/ring` (it binds to loopback only).

## 6. The open-box demo (acceptance test)

```bash
# 1. Dry-run scan: classify every file and print the exact plan with per-bot assignments. Changes nothing.
python3 -m pcmcp_swarm scan "/path/to/PCMCP-Test"

# 2. Review the plan, then approve that exact hash (you type its first 8 characters to confirm)
python3 -m pcmcp_swarm plan show latest
python3 -m pcmcp_swarm approve <hash-prefix>

# 3. Execute with logging, hashes, health meters and undo metadata
python3 -m pcmcp_swarm execute <hash-prefix> --for-real
python3 -m pcmcp_swarm status              # dashboard: problem bots are marked "<== CHECK THIS BOT"

# 4. Report, then prove one real rollback
python3 -m pcmcp_swarm report <hash-prefix>
python3 -m pcmcp_swarm undo <hash-prefix> --op <op_id>     # one op
python3 -m pcmcp_swarm undo <hash-prefix>                  # whole plan, newest first
```
What the scan does, with every rule fixed in `config.scan`:
- Exact duplicates (same SHA-256): the first path in sorted order is kept; the others are staged (C).
- Files older than `archive_before` (off by default; set e.g. `"2018-01-01"`): zipped per category
  into `_Archive/`, originals staged (E).
- Everything else: moved to `_Organized/<Category>/` (B), with folders created first (A).

If you interrupt `execute` (Ctrl-C, crash, power loss), just run the same command again. Finished ops
are skipped; half-finished ops are completed or recognized as done.

After a rollback, re-running the same approved plan does nothing, because its journal says every op is
done. To redo the work, scan again and approve the new plan.

## 7. Screenshot bot (letter S)

Screenshots are **queued for your approval by default** (`screenshot.requires_owner_approval`), because
the image goes back to a cloud assistant. When a signed `screenshot` batch arrives:
```bash
python3 -m pcmcp_swarm pending list
python3 -m pcmcp_swarm pending approve <request_id>   # captures, saves to data/screenshots, copies to outbox
```
Targets: `{"type":"full"}`, `{"type":"region","x":0,"y":0,"w":800,"h":600}`, `{"type":"app","name":"Finder"}`.
**macOS permission:** grant **Screen Recording** to the app running PCMCP (Terminal, iTerm, or the
launcher you use) under System Settings → Privacy & Security. The `app` target also needs
**Accessibility** to read window bounds; without it, it falls back to full screen.

**macOS Shortcut wiring (optional):** create a Shortcut with one *Run Shell Script* action:
`cd ~/Documents/PCMCP_repo && /opt/homebrew/bin/python3 -m pcmcp_swarm ring "<trigger>"`.
That rings the local doorbell from a keyboard shortcut, Siri, or a Shortcuts automation. The shell
script only rings the bell; actual instructions still have to arrive as signed batches.

## Safety summary

| Rule | Where it is enforced |
|---|---|
| No AI in bots | bots are fixed Python functions (`pcmcp_swarm/bots/`) |
| One letter per bot | compiled `LETTER` + name + registry check in `bots/common.py`; `registry.check_order` |
| Dry-run default, owner approval | `execute` without `--for-real` is a dry run; `runner.execute` refuses without an approval for the exact `plan_hash` |
| Hash-logged ops | every receipt in `logs/operations.jsonl` has `sha256_before`/`sha256_after` per file |
| Reversible | deletes, overwrites and archived originals are staged in `quarantine/`; nothing is ever hard-deleted |
| Crash-safe | `journal/` + per-op `intent.json`; fsync'd appends; atomic renames |
| Doorbell carries no authority | `daemon.py` only triggers `check_in`; `signing.verify` is the only gate |
| Halt on failure | the runner stops before later (more destructive) phases if any op fails |

Quarantine is never purged automatically. Purging it is a manual decision for you to make.
