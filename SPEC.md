# PCMCP Swarm — Data Spec (schema version 1)

Everything the swarm knows lives in plain JSON / JSON Lines files described here. Bots hold no other
state. A rewrite in any language must be able to read these files and continue with no migration.
Every file carries `schema` and `schema_version`; readers must refuse versions they don't know.

## 1. Layout (all under `data_dir`)

| Path | Format | Written by | Purpose |
|---|---|---|---|
| `registry.json` | JSON `pcmcp.registry` | `init` | letter → job → bot names (source of truth) |
| `REGISTRY_KEY.md` | Markdown | `init` | human-readable key, generated from the registry |
| `plans/<plan_hash>.json` | JSON `{plan_hash, body: pcmcp.plan, meta}` | scan / check-in | exact operation list |
| `plans/<plan_hash>.txt` | text | scan / check-in | human-readable plan |
| `plans/<plan_hash>.classification.json` | JSON array | scan | per-file category + decision |
| `logs/approvals.jsonl` | JSONL | owner CLI only | `{plan_hash, approved_at, approved_by, os_user, host}` |
| `logs/operations.jsonl` | JSONL of `pcmcp.receipt` (+`plan_hash`) | runner | every executed / undone op |
| `logs/doorbell.jsonl` | JSONL | listener | every ring + verdict (text only as length + SHA-256) |
| `logs/checkin.jsonl` | JSONL | check-in | fetch failures, accepted/rejected batches, receipts posted |
| `journal/<plan_hash>.jsonl` | JSONL `{event: started\|done\|failed, op_id, bot, at}` | runner | crash-resume ledger |
| `health/<bot>.json` | JSON `pcmcp.health` | runner | per-bot meter |
| `quarantine/<plan_hash>/<op_id>/...` | files + `intent.json` | bots | staged deletes/overwrites/originals + pre-state |
| `state/used_batch_ids.jsonl` | JSONL `{batch_id, channel, at}` | check-in | replay protection |
| `state/pending_requests.jsonl` | JSONL | check-in | screenshot requests awaiting owner |
| `reports/<plan_hash>.json` | JSON `pcmcp.report` | runner | final report |
| `dry_runs/<plan_hash>.json` | JSON `pcmcp.run_result` | runner | dry-run receipts |
| `screenshots/<request_id>.png` | PNG | S bot | captures |

## 2. Canonical JSON and hashes

`canonical(x) = UTF-8( JSON with keys sorted, separators "," and ":", no ASCII escaping )`.
Used for `plan_hash = sha256_hex(canonical(plan.body))` and for batch signatures.
File hashes are lowercase hex SHA-256 of the raw bytes. `null` means "file did not exist".
Timestamps: UTC ISO-8601 `YYYY-MM-DDTHH:MM:SS.ffffffZ`. They are recorded, never used to decide.

## 3. Registry (`pcmcp.registry`)

```json
{"schema":"pcmcp.registry","schema_version":1,"fleet_cap":100,
 "letters":{"B":{"job":"copy_move","description":"...","actions":["copy","move"],"destructive":false,
                 "bots":["B","B1","B1B","B1B1"]}},
 "reserved":{"D":"..."}}
```
Rules: a letter's `job` never changes. A bot's letter is the first character of its name. Names follow
`L, L1, L1L, L1L1, …`. Total bots ≤ `fleet_cap`. Letters: A create, B copy/move, C delete (staged),
E archive, S screenshot. D reserved.

## 4. Signed instruction batch (`pcmcp.signed_batch`)

```json
{"schema":"pcmcp.signed_batch","schema_version":1,
 "batch":{"schema":"pcmcp.batch","schema_version":1,"batch_id":"unique-string",
          "kind":"scan|plan|execute|screenshot|status",
          "issued_at":"2026-11-09T18:00:00.000000Z","expires_at":"2026-11-09T19:00:00.000000Z",
          "params":{...},"operations":[...]},
 "signature":{"alg":"HMAC-SHA256","value":"<hex of HMAC-SHA256(secret, canonical(batch))>"}}
```
Accepted only if: signature verifies (constant-time) → schema/version known → `kind` known →
batch_id and every op_id match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` (no `..`) → not expired → lifetime ≤ `max_batch_lifetime_seconds` → `batch_id` never seen before → every operation
passes the registry check. The `batch_id` is recorded as used before anything runs.

| kind | params | effect |
|---|---|---|
| `status` | — | returns the health dashboard |
| `scan` | `root` | dry-run scan, saves a plan, **awaiting owner approval** |
| `plan` | `root` + `operations[]` | validates and saves a proposed plan, **awaiting owner approval** |
| `execute` | `plan_hash` | runs the plan only if the owner approved that exact hash |
| `screenshot` | `target` | queued for owner approval (default) or captured |

A batch can never approve anything. Approval exists only in the local CLI.

## 5. Bot contract (`pcmcp.order` → `pcmcp.receipt`)

`python3 -m pcmcp_swarm.bots.bot_<letter>` reads one order on stdin, writes one receipt on stdout.

Order: `{schema, schema_version, plan_hash, op_id, bot, action, params, mode: dry_run|execute|undo,
allowed_roots[], quarantine_dir, registry_path, screenshots_dir, undo?}`

Receipt: `{schema:"pcmcp.receipt", schema_version, plan_hash, op_id, bot, letter, action, mode,
status: would_do|done|already_done|refused|error, started_at, finished_at, src?, dst?,
files:[{role, path, sha256_before, sha256_after}], undo?:{kind, ...}, error?}`

The bot refuses (changes nothing) if its compiled letter ≠ its name's letter, the registry doesn't grant
the action, a path leaves `allowed_roots`, a path is a symlink, a source hash ≠ the approved
`expect_sha256`, or a destination exists and `overwrite` isn't set.

Actions and params:
- A `create_folder {path}` · `create_file {path, content | content_b64, overwrite?}`
- B `copy|move {src, dst, expect_sha256?, overwrite?}`
- C `delete {path, expect_sha256}` (required hash; file is staged, never destroyed)
- E `archive {dst_zip, sources:[{path, arcname, expect_sha256}], remove_originals}` (zip is deterministic)
- S `screenshot {target:{type: full|region|app,...}, out_path}`

## 6. Idempotency and resume

Before mutating, a bot writes `quarantine/<plan>/<op>/intent.json` with the pre-state hashes. If the run
dies, the runner re-sends every op whose journal has no `done`; the bot compares the world with its
intent and either finishes the op or reports `already_done`. Nothing is done twice.

## 7. Undo

Each `done` receipt carries an `undo` block. Undo runs newest-first, by a bot of the same letter, and
never hard-deletes: what it removes is staged under `quarantine/<plan>/<op>/undo/`. Undo refuses if a
file changed since the op or the original location is occupied. Quarantine is never purged automatically.
