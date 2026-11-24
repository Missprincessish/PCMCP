# PCMCP Bot Swarm — Task Ledger

Source: `PCMCP_Bot_Swarm_Build_Prompt_v2.txt` (pasted 2026-11-09), plus Angie's answers on 2026-11-09:
check-in is a swappable `CheckInChannel` with a signed local inbox; Gmail uses IMAP + app password on a
**dedicated** account; only the owner approves, locally, bound to plan hash; Python stdlib; data
portable as versioned JSON; cross-OS selftest.
Branch: `swarm-v1`. The pre-swarm working tree was committed first as `a8a8cfb`.

Status key: [x] complete · [~] partial · [ ] not started · [?] needs investigation · [!] blocked

## Audit: build from scratch, or extend what exists?

Existing PCMCP (Node): `server.js` + `pcmcp-worker-runtime.js` (process "rockets" with fake, never
measured CPU/RAM meters), `public/` fleet dashboard, and `pcmcp-mcp-server.js` (`pcmcp_run` + Synomize
`world_*` tools). **None of it** does file operations, registry, hashing, approval, undo, doorbell or
signed check-in. Decision: build the swarm as a new, separate Python package `pcmcp_swarm/`, and leave
the Node files untouched (no duplicate architecture: the Node "rockets" launch arbitrary processes and are
not swarm bots). Connecting the Node dashboard and MCP server to the swarm is a follow-up (see the end).

## Tasks

[x] 1. Data spec + config
    Files: `SPEC.md`, `pcmcp_swarm/__init__.py` (SCHEMA_VERSION), `pcmcp_swarm/config.py` (`defaults()`, `load()`, `data_paths()`)
    Verification: selftest `init` check; all paths derive from the config folder, so the selftest touches nothing outside its temp dir.

[x] 2. Registry + naming ladder + human-readable key
    Files: `pcmcp_swarm/registry.py` (`LETTERS`, `ladder_name`, `build`, `validate`, `check_order`, `render_key`)
    Letters: A create, B copy/move, C delete (staged), E archive, **S screenshot (new letter, see caveat)**, D reserved.
    Verification: selftest: ladder A/A1/A1A/A1A1, fleet cap 100, B bot refused for delete, key file written.

[x] 3. Bot framework (stdin order → stdout receipt) and letters A, B, C, E
    Files: `pcmcp_swarm/bots/common.py` (`run`, `check_path`, `stage`, `unstage`, intent files), `bots/bot_a.py`, `bot_b.py`, `bot_c.py`, `bot_e.py`
    Verification: selftest: letter refusal, root escape refusal, overwrite refusal, changed-hash refusal, deterministic zip.

[x] 4. Plans, owner approval bound to plan hash
    Files: `pcmcp_swarm/plans.py` (`make`, `validate`, `record_approval`, `is_approved`, `render`), `__main__.py` `cmd_approve`
    Verification: selftest: execute refused without approval; tampered plan fails hash validation.

[x] 5. Dry-run scanner / classifier / per-bot plan
    Files: `pcmcp_swarm/scanner.py` (`discover`, `build_plan`, `deal`)
    Verification: same folder → same plan hash; dry run changes nothing; duplicates, old files and collisions handled.

[x] 6. Runner: phases, journal, crash-resume, halt-on-failure, undo, report
    Files: `pcmcp_swarm/runner.py` (`execute`, `undo`, `report`, `call_bot`, `journal_state`)
    Verification: selftest kills the run after 6 ops (exit 137), resumes, every op done exactly once; single-op and full rollback restore the drive byte-for-byte.

[x] 7. Health meters + dashboard
    Files: `pcmcp_swarm/health.py` (`record`, `heartbeat`, `status`, `render`)
    Verification: selftest: clean run has no problem bots; an injected erroring bot is flagged.

[x] 8. Check-in: `CheckInChannel` interface, signed local inbox, stubs, handler
    Files: `pcmcp_swarm/checkin/__init__.py`, `checkin/local_inbox.py`, `checkin/stubs.py` (https_endpoint, mcp_tool, email_attachment: NOT IMPLEMENTED), `checkin/handler.py`, `pcmcp_swarm/signing.py`, `pcmcp_swarm/secret_store.py`
    Verification: selftest: valid batches accepted; tampered, expired, unsigned, letter-violating and replayed batches rejected and moved aside; scan/plan only propose.

[x] 9. Doorbell: local spool + local HTTP + Gmail IMAP, rate limit, junk logging
    Files: `pcmcp_swarm/doorbell/__init__.py` (`matches_trigger`, `RateLimiter`), `doorbell/local.py`, `doorbell/gmail_imap.py`, `doorbell/stubs.py` (gmail_api: NOT IMPLEMENTED), `pcmcp_swarm/daemon.py`
    Verification: selftest: junk ignored, valid ring → check-in → "no instructions" → sleep, flood rate-limited, trigger never logged; Gmail adapter tested against a fake IMAP server (header-only PEEK, sender allowlist).
    Caveat: **not yet run against real Gmail**. `local_http` verified by hand (POST /ring on 127.0.0.1 → 202, ring queued); not in the selftest.

[~] 10. Screenshot bot (letter S), owner-approval queue
    Files: `pcmcp_swarm/bots/bot_s.py`, `pcmcp_swarm/screenshot/{__init__,macos,windows,linux}.py`, `checkin/handler.py` (`take_screenshot`, `pending`), `__main__.py` `cmd_pending`
    Verification: path-gate refusal and pending-approval flow pass. A real macOS capture **failed in this session** (`screencapture` exit 1); likely cause is that Screen Recording isn't granted to the process. The bot returns a clear error receipt. Windows/Linux adapters are untested.

[x] 11. CLI
    Files: `pcmcp_swarm/__main__.py` (init, keygen, status, scan, plan, approve, execute, undo, report, ring, listen, checkin, sign, pending, registry)
    Verification: exercised by selftest via subprocess; README's sign → checkin → status path run by hand.

[x] 12. Cross-OS selftest
    Files: `pcmcp_swarm/selftest.py`. Command: `python3 -m pcmcp_swarm.selftest`
    Verification: **57/57 PASS on macOS 27.2 arm64, Python 3.15** (2026-11-09; +2 path-traversal checks). Windows/Linux not run.

[x] 13. README + repo hygiene
    Files: `SWARM_README.md`, `.gitignore`, link added in `README.md`

[x] 14. Setup acceptance run on a real copy (NOT the final demo: Instinct will run the real one on the ~1 TB Drive)
    Folder: `~/Desktop/PCMCP_TEST` (Angie's copy of UMPI_FALL_2026; the only allowed root). Config/data: `~/.pcmcp/`.
    Plan `3a33d4916347…`: 1,009 files scanned (510 MB), 1,016 ops (A 7, B 257 across B/B1/B1B/B1B1, C 752 staged duplicates). Approved by Angie in chat ("fine if they break as long as the setup works"), recorded via `approve --yes`.
    Verification: executed 1016/1016 done, 0 errors, 0 refusals, 100 s; dashboard no problem bots; all 1,009 original byte-streams accounted for (folder or quarantine); 1016/1016 receipts carry before/after hashes; single-op rollback of `2026-27 UMPI IVR FILLABLE.pdf` (op a4d487178fa8-00264) restored it byte-identical (sha256 ce8df803…), repeat undo = no-op.
    Bug found and fixed: `plans.load` prefix lookup matched `.classification.json` siblings (`pcmcp_swarm/plans.py` `load`).
    Known plan-quality issues (not safety issues): saved course web pages get their assets scattered/deduped (907 of 1,026 files are inside 31 page folders); duplicate keeper picks first-alphabetical ("X 2.pdf" over "X.pdf"). Both await Angie's call. The rest of the plan is still applied in the test folder; full rollback available: `python3 -m pcmcp_swarm undo 3a33d4916347`.

[ ] 15. Scale-up to the ~1 TB Drive (Instinct's real run). Open items before it: (a) the scan SHA-256s every file, so a streaming Google Drive mount would download all ~1 TB; consider mirror mode or size+mtime pre-filter; (b) one subprocess per op (~10 ops/s per bot) means hours for millions of files, so raise the fleet and/or batch ops per bot call; (c) decide web-page-folder and duplicate-keeper rules.

## Open decisions / caveats (for Angie)
- **Letter S for screenshot** was assigned by Claude; D was left reserved because the build request skipped it. Letters never change meaning once used, so confirm before the real demo.
- **Screenshot approval:** defaults to owner approval (`screenshot.requires_owner_approval: true`) because images go to a cloud assistant. Not specified in the request.
- **Re-executing after a rollback** does nothing (the journal says done). A fresh scan + approval is required. Not specified in the request.
- **Quarantine retention:** never auto-purged. Purging policy is undecided.
- **Overwrites in scanner plans:** the scanner never plans overwrites (it picks a unique name instead); `overwrite: true` is only possible in an Instinct-proposed `plan` batch, which still needs your approval.

## Follow-ups (not part of this build)
- **Retire server.js** (Angie, 2026-11-09). Do NOT run it: it listens on the whole network (0.0.0.0, CORS `*`) and `POST /api/workers/launch` runs any command. The swarm does not use it.
- Pre-existing Node code has security problems flagged on commit: `server.js` listens on 0.0.0.0 with CORS `*` and `POST /api/workers/launch` runs any command (remote code execution on the LAN); static-file path check is weak; `pcmcp-mcp-server.js` `run_command`/`read_files` are unrestricted.
- Connect the Node dashboard / MCP server to the swarm (e.g. implement the `mcp_tool` check-in adapter, show `status --json` in the rocket dashboard).
