"""Cross-OS self test.  ONE command, prints PASS/FAIL per check and a final verdict:

    python3 -m pcmcp_swarm.selftest            (add --keep to leave the temp folder for inspection)
    python3 -m pcmcp_swarm.selftest --with-screenshot   (also captures one real screenshot)

Builds a messy test folder in a temp dir, then runs the whole open-box demo against it:
scan (dry run) -> refuse without approval -> approve -> execute with a simulated crash -> resume ->
verify hashes -> roll back one op -> roll back everything -> verify the folder is byte-identical
to the start. Then exercises the doorbell, signed batches, replay/expiry/tamper rejection, rate
limiting, the Gmail adapter (with a fake IMAP server) and the screenshot gate.
Touches nothing outside its temp folder.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def snapshot(root: Path) -> dict:
    from pcmcp_swarm.util import sha256_file
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


def make_fixture(root: Path):
    files = {
        "Q3 report.pdf": b"%PDF quarterly", "notes.txt": b"meeting notes", "budget.xlsx": b"xlsx-bytes",
        "deck.pptx": b"pptx-bytes", "photo 1.JPG": b"jpegdata1", "photo 2.png": b"pngdata2",
        "song.mp3": b"mp3data", "clip.mov": b"movdata", "script.py": b"print('hi')\n", "random.xyz": b"???",
        "Old Stuff/taxes 2014.pdf": b"%PDF old taxes", "Old Stuff/scan.jpg": b"old scan",
        "Downloads/report.pdf": b"%PDF another report", "Work/report.pdf": b"%PDF yet another report",
        "Work/notes copy.txt": b"meeting notes",          # exact duplicate of notes.txt
        "Work/Deep/nested/dup.png": b"pngdata2",          # exact duplicate of photo 2.png
        "Work/résumé ünïcode.docx": b"docx-unicode",
        "Work/.DS_Store": b"ignored",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    old = _dt.datetime(2014, 6, 1, tzinfo=_dt.timezone.utc).timestamp()
    for rel in ("Old Stuff/taxes 2014.pdf", "Old Stuff/scan.jpg"):
        os.utime(root / rel, (old, old))


def run_cli(env, *args, expect_ok=True):
    r = subprocess.run([sys.executable, "-m", "pcmcp_swarm", *args], capture_output=True, text=True, env=env,
                       cwd=str(Path(__file__).resolve().parent.parent))
    return r


def main():
    keep = "--keep" in sys.argv
    with_shot = "--with-screenshot" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="pcmcp_selftest_"))
    print(f"PCMCP selftest on {platform.system()} {platform.release()} / Python {platform.python_version()}")
    print(f"temp folder: {tmp}\n")
    try:
        _run(tmp, with_shot)
    except Exception:
        check("selftest ran to completion", False, traceback.format_exc())
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed on {platform.system()} {platform.machine()}")
    print("OVERALL: " + ("PASS" if passed == len(RESULTS) else "FAIL"))
    sys.exit(0 if passed == len(RESULTS) else 1)


def _run(tmp: Path, with_shot: bool):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pcmcp_swarm import config as config_mod, plans as plans_mod, registry as reg_mod, runner, scanner, signing
    from pcmcp_swarm.checkin.handler import check_in, pending
    from pcmcp_swarm.daemon import poll_once
    from pcmcp_swarm.doorbell import RateLimiter, get_channels
    from pcmcp_swarm.doorbell.local import spool_ring
    from pcmcp_swarm.secret_store import get_secret
    from pcmcp_swarm.util import atomic_write_json, now_iso, read_jsonl, sha256_file

    drive = tmp / "drive"
    make_fixture(drive)
    cfgfile = tmp / "pcmcp" / "config.json"
    trigger = "selftest-doorbell-7f3a9c"
    atomic_write_json(cfgfile, {
        "doorbell": {"trigger": trigger, "channels": ["local_spool"], "rate_limit": {"max_wakes": 3, "window_seconds": 600}},
        "scan": {"archive_before": "2016-01-01"},
        "fleet": {"A": 1, "B": 4, "C": 1, "E": 1, "S": 1},
    })
    env = dict(os.environ, PCMCP_CONFIG=str(cfgfile))
    r = run_cli(env, "init", "--allow-root", str(drive))
    check("init creates config, registry, key, secret", r.returncode == 0, r.stderr)
    cfg = config_mod.load(str(cfgfile))
    paths = config_mod.data_paths(cfg)
    reg = reg_mod.load(paths["registry"])

    # ---- 1. registry + naming ladder ----
    check("ladder names A, A1, A1A, A1A1", [reg_mod.ladder_name("A", i) for i in (1, 2, 3, 4)] == ["A", "A1", "A1A", "A1A1"])
    check("registry has B, B1, B1B, B1B1 for fleet B=4", reg["letters"]["B"]["bots"] == ["B", "B1", "B1B", "B1B1"])
    check("registry refuses a B bot doing a delete", reg_mod.check_order(reg, "B1", "delete") is not None)
    check("registry key file written", paths["registry_key"].exists())
    try:
        reg_mod.build({"B": 101})
        check("fleet cap of 100 enforced", False)
    except ValueError:
        check("fleet cap of 100 enforced", True)

    # ---- 2. dry-run scan is deterministic and changes nothing ----
    before = snapshot(drive)
    p1, _ = scanner.build_plan(str(drive), cfg, reg, {"kind": "selftest"})
    p2, _ = scanner.build_plan(str(drive), cfg, reg, {"kind": "selftest"})
    check("same folder -> same plan hash (determinism)", p1["plan_hash"] == p2["plan_hash"])
    plans_mod.save(paths, p1)
    plan = p1
    dr = runner.execute(paths, cfg, reg, plan, dry_run=True, log=lambda *_: None)
    check("dry run reports would_do for every op", dr["counts"].get("would_do") == len(plan["body"]["operations"]), dr["counts"])
    check("dry run changed nothing on disk", snapshot(drive) == before)
    s = plan["body"]["summary"]
    check("scan found 2 duplicates, 2 old files, organizes the rest",
          s["duplicates"] == 2 and s["to_archive"] == 2 and s["to_organize"] == 13, s)
    check("work dealt across all 4 B bots", set(b for b in s["by_bot"] if b.startswith("B")) == {"B", "B1", "B1B", "B1B1"})

    # ---- 3. approval gate ----
    try:
        runner.execute(paths, cfg, reg, plan, dry_run=False, log=lambda *_: None)
        check("execute without approval is refused", False)
    except runner.NotApproved:
        check("execute without approval is refused", True)
    check("snapshot unchanged after refused execute", snapshot(drive) == before)
    r = run_cli(env, "approve", plan["plan_hash"][:12], "--yes")
    check("owner approves exact plan hash", r.returncode == 0 and plans_mod.is_approved(paths, plan["plan_hash"]), r.stderr)
    tampered = json.loads(json.dumps(plan))
    tampered["body"]["operations"][3]["params"]["dst"] = str(drive / "evil.txt")
    check("tampered plan fails validation (approval is bound to the hash)",
          any("plan_hash" in p for p in plans_mod.validate(tampered, reg, cfg["allowed_roots"])))

    # ---- 4. crash mid-run, then resume ----
    env_crash = dict(env, PCMCP_CRASH_AFTER_OPS="6")
    r = run_cli(env_crash, "execute", plan["plan_hash"][:12], "--for-real")
    check("simulated power loss mid-run (exit 137)", r.returncode == 137, f"rc={r.returncode} {r.stderr[-300:]}")
    j = read_jsonl(paths["journal"] / f"{plan['plan_hash']}.jsonl")
    done_before = {x["op_id"] for x in j if x["event"] == "done"}
    check("journal shows partial progress", 0 < len(done_before) < len(plan["body"]["operations"]), len(done_before))
    r = run_cli(env, "execute", plan["plan_hash"][:12], "--for-real")
    check("resume completes the plan", r.returncode == 0 and "halted: None" in r.stdout, r.stdout[-400:] + r.stderr[-400:])
    j = read_jsonl(paths["journal"] / f"{plan['plan_hash']}.jsonl")
    done_counts = {}
    for x in j:
        if x["event"] == "done":
            done_counts[x["op_id"]] = done_counts.get(x["op_id"], 0) + 1
    check("every op done exactly once (no duplicated work)",
          len(done_counts) == len(plan["body"]["operations"]) and set(done_counts.values()) == {1}, done_counts)

    # ---- 5. outcome + hashes ----
    after = snapshot(drive)
    org = [k for k in after if k.startswith("_Organized/")]
    check("13 files now under _Organized/", len(org) == 13, org)
    check("2 archives under _Archive/", len([k for k in after if k.startswith("_Archive/")]) == 2)
    check("duplicates gone from the drive", "Work/notes copy.txt" not in after and "Work/Deep/nested/dup.png" not in after)
    check("name collision resolved deterministically",
          {"_Organized/Documents/report.pdf", "_Organized/Documents/report (2).pdf"} <= set(after), sorted(org))
    staged = [p for p in paths["quarantine"].rglob("*") if p.is_file() and p.name != "intent.json"]
    check("deleted + archived originals are staged in quarantine (4 files)", len(staged) == 4, [p.name for p in staged])
    ops_log = [x for x in read_jsonl(paths["operations"]) if x.get("plan_hash") == plan["plan_hash"] and x.get("mode") == "execute"]
    complete = all(x.get("files") and all("sha256_before" in f and "sha256_after" in f for f in x["files"])
                   and x.get("op_id") and x.get("bot") and x.get("started_at") for x in ops_log
                   if x["action"] != "create_folder")
    check("every file op logged with before/after SHA-256, bot, op ID, time", complete)
    orig_hashes = set(before.values())
    import zipfile, hashlib
    have = set(after.values()) | {sha256_file(p) for p in staged}
    for z in (drive / "_Archive").glob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            have |= {hashlib.sha256(zf.read(n)).hexdigest() for n in zf.namelist()}
    check("every original byte-stream still accounted for (drive, archive or quarantine)", orig_hashes <= have)

    # ---- 6. health ----
    from pcmcp_swarm import health
    st = health.status(paths, reg, cfg)
    check("health dashboard: ops counted, no problem bots", sum(b["ops_completed"] for b in st["bots"]) >= len(plan["body"]["operations"])
          and not st["problem_bots"], st["problem_bots"])
    hp = paths["health"] / "B1.json"
    h = json.loads(hp.read_text())
    h["errors"], h["last_error"] = 3, {"at": now_iso(), "op_id": "x", "error": "simulated disk error"}
    atomic_write_json(hp, h)
    st = health.status(paths, reg, cfg)
    check("an erroring bot is flagged on the dashboard", "B1" in st["problem_bots"])
    h["errors"], h["last_error"] = 0, None
    atomic_write_json(hp, h)

    # ---- 7. undo: one op, then the whole plan ----
    move_op = next(op for op in plan["body"]["operations"] if op["action"] == "move")
    r = run_cli(env, "undo", plan["plan_hash"][:12], "--op", move_op["op_id"])
    src = Path(move_op["params"]["src"])
    check("single-op rollback puts the file back byte-identical",
          sha256_file(src) == move_op["params"]["expect_sha256"] and not Path(move_op["params"]["dst"]).exists(), r.stdout + r.stderr)
    r = run_cli(env, "undo", plan["plan_hash"][:12])
    final = snapshot(drive)
    check("full rollback restores the drive byte-for-byte", final == before,
          f"missing={set(before) - set(final)} extra={set(final) - set(before)}")
    r = run_cli(env, "report", plan["plan_hash"][:12], "--json")
    rep = json.loads(r.stdout) if r.returncode == 0 else {}
    check("final report lists per-bot work and undo events", rep.get("per_bot") and len(rep.get("undo_events", [])) > 0)

    # ---- 8. bot-level safety refusals ----
    def order(bot, action, params, mode="execute"):
        return {"plan_hash": "selftest", "op_id": f"t-{bot}-{action}", "bot": bot, "action": action, "params": params,
                "mode": mode, "allowed_roots": [os.path.realpath(drive)], "quarantine_dir": str(paths["quarantine"]),
                "registry_path": str(paths["registry"]), "screenshots_dir": str(paths["screenshots"])}
    rc = runner.call_bot(order("B", "delete", {"path": str(drive / "notes.txt"), "expect_sha256": "x"}))
    check("bot refuses an action outside its letter", rc["status"] == "refused" and drive.joinpath("notes.txt").exists(), rc)
    rc = runner.call_bot(order("B", "copy", {"src": str(drive / "notes.txt"), "dst": str(tmp / "outside.txt")}))
    check("bot refuses a path outside allowed roots", rc["status"] == "refused" and not (tmp / "outside.txt").exists(), rc)
    rc = runner.call_bot(order("B", "copy", {"src": str(drive / "notes.txt"), "dst": str(drive / "budget.xlsx")}))
    check("copy refuses to overwrite without approval", rc["status"] == "refused" and sha256_file(drive / "budget.xlsx") == before["budget.xlsx"], rc)
    rc = runner.call_bot(order("C", "delete", {"path": str(drive / "notes.txt"), "expect_sha256": "0" * 64}))
    check("delete refuses when the file changed since approval", rc["status"] == "refused" and drive.joinpath("notes.txt").exists(), rc)
    rc = runner.call_bot(order("S", "screenshot", {"target": {"type": "full"}, "out_path": str(drive / "x.png")}))
    check("screenshot bot only writes into the screenshots folder", rc["status"] == "refused", rc)

    # ---- 9. archive determinism ----
    from pcmcp_swarm.bots.bot_e import _write_zip
    a, b = tmp / "z1.zip", tmp / "z2.zip"
    _write_zip(a, [("x.txt", str(drive / "notes.txt"))])
    _write_zip(b, [("x.txt", str(drive / "notes.txt"))])
    check("archives are byte-reproducible", sha256_file(a) == sha256_file(b))

    # ---- 10. doorbell ----
    channels = get_channels(cfg)
    limiter = RateLimiter(3, 600)
    quiet = lambda *_: None
    spool_ring(cfg, "hello this is junk")
    res = poll_once(paths, cfg, channels, limiter, quiet)
    check("junk ring logged and ignored (no check-in)", res["valid_rings"] == 0 and res["checkin"] is None)
    spool_ring(cfg, trigger)
    res = poll_once(paths, cfg, channels, limiter, quiet)
    check("valid ring wakes, checks in, finds nothing, sleeps", res["checkin"] == {"accepted": 0, "rejected": 0})
    for _ in range(3):
        spool_ring(cfg, trigger)
    poll_once(paths, cfg, channels, limiter, quiet)
    verdicts = [x["verdict"] for x in read_jsonl(paths["doorbell"])]
    check("ring flood is rate-limited", "rate_limited" in verdicts, verdicts)
    check("every ring logged, trigger never logged in clear",
          len(verdicts) == 5 and trigger not in paths["doorbell"].read_text())

    # ---- 11. signed batches through the check-in channel ----
    secret = get_secret(cfg["checkin"]["secret"])
    inbox = Path(cfg["checkin"]["local_inbox"]["inbox_dir"])
    outbox = Path(cfg["checkin"]["local_inbox"]["outbox_dir"])

    def batch(bid, kind, params=None, ops=None, life=3600, issued_offset=0):
        now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=issued_offset)
        f = "%Y-%m-%dT%H:%M:%S.%fZ"
        return {"schema": "pcmcp.batch", "schema_version": 1, "batch_id": bid, "kind": kind,
                "issued_at": now.strftime(f), "expires_at": (now + _dt.timedelta(seconds=life)).strftime(f),
                "params": params or {}, "operations": ops or []}

    def drop(name, env_obj):
        (inbox / f"{name}.json").write_text(json.dumps(env_obj), encoding="utf-8")

    good = signing.sign(batch("b-scan-1", "scan", {"root": str(drive)}), secret)
    drop("1-good", good)
    bad = signing.sign(batch("b-tamper", "scan", {"root": str(drive)}), secret)
    bad["batch"]["params"]["root"] = "/"
    drop("2-tampered", bad)
    drop("3-expired", signing.sign(batch("b-old", "status", life=10, issued_offset=-3600), secret))
    drop("4-unsigned", {"schema": "pcmcp.signed_batch", "batch": batch("b-nosig", "status")})
    wrong_letter = [{"op_id": "x1", "bot": "B", "action": "delete", "params": {"path": str(drive / "notes.txt"), "expect_sha256": "0"}}]
    drop("5-letter", signing.sign(batch("b-letter", "plan", {"root": str(drive)}, wrong_letter), secret))
    drop("5b-traversal", signing.sign(batch("../../evil", "status"), secret))
    evil_op = [{"op_id": "../../x", "bot": "A", "action": "create_folder", "params": {"path": str(drive / "z")}}]
    drop("5c-evilop", signing.sign(batch("b-evilop", "plan", {"root": str(drive)}, evil_op), secret))
    drop("6-status", signing.sign(batch("b-status", "status"), secret))
    drop("7-shot", signing.sign(batch("b-shot", "screenshot", {"target": {"type": "full"}}), secret))
    drop("8-exec", signing.sign(batch("b-exec", "execute", {"plan_hash": p1["plan_hash"]}), secret))
    res = check_in(paths, cfg, "selftest")
    log = read_jsonl(paths["checkin"])
    rej = {x["ref"].split("/")[-1].split("\\")[-1]: x["reason"] for x in log if x.get("event") == "batch_rejected"}
    check("batch_id with path traversal rejected", "unsafe batch_id" in rej.get("5b-traversal.json", ""), rej)
    check("op_id with path traversal rejected", "unsafe op_id" in rej.get("5c-evilop.json", ""), rej)
    check("valid signed batches accepted (scan, status, screenshot, execute)", res["accepted"] == 4, res)
    check("tampered batch rejected (bad signature)", rej.get("2-tampered.json") == "bad signature", rej)
    check("expired batch rejected", rej.get("3-expired.json") == "expired", rej)
    check("unsigned batch rejected", "unsigned" in rej.get("4-unsigned.json", ""), rej)
    check("batch ordering a bot outside its letter rejected", "may not perform" in rej.get("5-letter.json", ""), rej)
    receipts = [json.loads(p.read_text()) for p in sorted(outbox.glob("receipt_*.json")) if "__" not in p.name]
    by_kind = {r.get("kind"): r for r in receipts if r.get("batch_id")}
    check("scan batch only PROPOSES a plan (awaiting owner approval)",
          by_kind.get("scan", {}).get("status") == "awaiting_owner_approval" and snapshot(drive) == before)
    check("screenshot request waits for owner approval", by_kind.get("screenshot", {}).get("status") == "pending_owner_approval"
          and len(pending(paths)) == 1)
    check("execute batch runs only an approved plan (already-complete plan is idempotent)",
          by_kind.get("execute", {}).get("status") in ("executed",))
    drop("9-replay", good)
    check_in(paths, cfg, "selftest")
    log = read_jsonl(paths["checkin"])
    check("replayed batch rejected", any(x.get("event") == "batch_rejected" and "replay" in x.get("reason", "") for x in log))
    check("rejected batches moved aside, never re-read", not list(inbox.glob("*.json")))
    # Re-running the approved plan via batch must not change the restored drive? It re-executes
    # done ops as skips; the drive was rolled back, but the journal says done -> nothing re-runs.
    check("execute after rollback does not redo journaled ops", snapshot(drive) == before)

    # ---- 12. Gmail adapter against a fake IMAP server ----
    from pcmcp_swarm.doorbell import gmail_imap as gi
    fetched = []

    class FakeImap:
        def __init__(self, host): pass
        def login(self, u, p): self.user = u
        def select(self, label): return "OK", [b"2"]
        def uid(self, cmd, *a):
            if cmd == "SEARCH":
                return "OK", [b"11 12"]
            if cmd == "FETCH":
                fetched.append(a[1])
                hdr = {b"11": b"From: Owner <owner@example.com>\r\nSubject: " + trigger.encode() + b"\r\n\r\n",
                       b"12": b"From: spam@evil.test\r\nSubject: " + trigger.encode() + b"\r\n\r\n"}[a[0]]
                return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM SUBJECT)] {40}", hdr), b")"]
            return "OK", [b""]
        def logout(self): pass

    gcfg = json.loads(json.dumps(cfg))
    gcfg["doorbell"]["gmail_imap"].update(account="doorbell@example.com", allowed_senders=["owner@example.com"],
                                          secret={"type": "env", "name": "PCMCP_SELFTEST_IMAP"})
    os.environ["PCMCP_SELFTEST_IMAP"] = "app-password"
    g = gi.GmailImap(gcfg)
    g.imap_factory = FakeImap
    rings = g.poll()
    check("gmail adapter fetches headers only, never bodies",
          fetched and all("PEEK[HEADER.FIELDS (FROM SUBJECT)]" in f for f in fetched))
    check("gmail adapter returns rings with sender + subject", [r["sender"] for r in rings] == ["owner@example.com", "spam@evil.test"])
    check("unexpected gmail sender is rejected", g.sender_allowed("owner@example.com") and not g.sender_allowed("spam@evil.test"))

    # ---- 13. optional real screenshot ----
    if with_shot:
        from pcmcp_swarm.checkin.handler import take_screenshot
        rec, att = take_screenshot(paths, cfg, reg, {"type": "full"}, "selftest-shot")
        check("real screenshot captured and hashed", rec["status"] == "done" and rec["sha256"], rec)


if __name__ == "__main__":
    main()
