"""`python3 -m pcmcp_swarm drive <command>` - the control interface Instinct drives (see CONTROL.md).

Every command is recorded (command_id, --issued-by, argv, parent process chain) in the run folder's
commands.jsonl BEFORE it does anything, and every receipt it causes carries that command_id.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

from . import check_pools, drive_cfg, inventory as inv_mod, owner, planfile, runs, swarm
from .api import DriveClient, authorize
from .. import config as config_mod
from ..util import append_jsonl, now_iso, read_json, read_jsonl


def _setup(args, mode, need_run=True, new_run=False, extra=None):
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    run_dir = runs.new_run(d["runs_dir"]) if new_run else (runs.find_run(d["runs_dir"], getattr(args, "run", None)) if need_run else None)
    glog = Path(cfg["data_dir"]).expanduser() / "logs" / "drive_commands.jsonl"
    cmd = runs.record_command(run_dir, glog, mode, ["python3", "-m", "pcmcp_swarm", *sys.argv[1:]] if not getattr(args, "_argv", None) else args._argv,
                              getattr(args, "issued_by", None), extra)
    return cfg, d, run_dir, cmd


def _fmap(d):
    fm = read_json(Path(d["folder_map"]).expanduser())
    if not fm:
        raise FileNotFoundError(f"folder map missing: {d['folder_map']}")
    return fm


def _inv(run_dir):
    inv = read_json(run_dir / "inventory.json")
    if not inv:
        raise FileNotFoundError(f"{run_dir.name} has no inventory.json; run `drive inventory` first")
    return inv


def _plan(args, run_dir):
    p = planfile.load_plan(Path(args.plan).expanduser())
    h = planfile.plan_hash(p)
    dst = run_dir / f"plan-{h[:12]}.json"
    if not dst.exists():
        shutil.copyfile(Path(args.plan).expanduser(), dst)
    return p, h


def _client(d, read_only=False, on_retry=None):
    return DriveClient(d, on_retry=on_retry, read_only=read_only)


def _print_validation(v):
    print(f"plan {v['plan_hash']}")
    print(f"files in scope: {v['stats']['files_in_scope']}  covered: {v['stats']['covered']}  to Review: {v['stats']['to_review']}  "
          f"folders to create: {v['stats']['folders_to_create']}  new folders declared: {v['stats']['new_folders_declared']}")
    for c, m in v["errors"][:200]:
        print(f"  ERROR {c}: {m}")
    if len(v["errors"]) > 200:
        print(f"  ... {len(v['errors']) - 200} more errors (see validation.json)")
    for c, m in v["warnings"][:50]:
        print(f"  warn  {c}: {m}")
    print("VALID - may proceed to dry run" if v["ok"] else f"INVALID - {len(v['errors'])} problem(s); this plan will refuse to run")


# ------------------------------------------------------------------ commands

def cmd_auth(args):
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    p = authorize(d, open_browser=not args.no_browser)
    print(f"signed in; token saved to {p} (mode 0600)")


def cmd_inventory(args):
    cfg, d, run_dir, cmd = _setup(args, "inventory", new_run=True, extra={"scope": args.root})
    retries = {"rate_limit": 0, "transient": 0}

    def on_retry(kind, n=1):
        retries[kind] = retries.get(kind, 0) + n
    client = _client(d, read_only=True, on_retry=on_retry)
    t0 = time.time()
    pool = int(d["pools"].get("I", 30))
    last = [0.0]

    def progress(snap, state="running"):
        if state == "running" and time.time() - last[0] < float(d["status_interval_seconds"]):
            return
        last[0] = time.time()
        runs.write_json(run_dir / "status.json", {
            "run": run_dir.name, "mode": "inventory", "state": state, "updated_at": now_iso(),
            "command_id": cmd["command_id"], "issued_by": cmd["issued_by"], "scope": args.root,
            "done": snap["items_listed"], "planned": None, "bots_active": snap["bots_active"],
            "bots_active_names": snap["bots_active_names"], "bots_total": pool + 1, "errors": snap["errors"],
            "refused": 0, "uncertain": 0, "retries": dict(retries), "per_minute_last_60s": snap["per_minute_last_60s"],
            "avg_per_minute": snap["avg_per_minute"], "elapsed_seconds": snap["elapsed_seconds"],
            "api_requests": client.requests_sent,
            "leads": {"I": {"pool": pool, "chunks": {"pending": snap["folders_waiting"], "running": snap["bots_active"],
                                                     "done": snap["folders_listed"], "failed": snap["errors"]},
                            "reassigned": 0, "items_planned": 0, "items_done": snap["items_listed"],
                            "sub_bots_used": snap["sub_bots_used"]}}})
    runs.event(run_dir, "inventory_started", command_id=cmd["command_id"], scope=args.root, lead="I", pool=pool)
    final = {}

    def prog(snap):
        final.update(snap)
        progress(snap)
    inv = inv_mod.export(client, args.root, pool=pool, progress=prog,
                         should_stop=lambda: runs.control_state(run_dir) == "stop")
    progress(final, state="done" if inv["complete"] else "incomplete")
    inv["command_id"] = cmd["command_id"]
    inv["retries"] = retries
    runs.write_json(run_dir / "inventory.json", inv)
    before = inv_mod.listing_of(inv)
    runs.write_json(run_dir / "before_listing.json", before)
    runs.event(run_dir, "inventory_exported", command_id=cmd["command_id"], counts=inv["counts"],
               inventory_hash=inv["inventory_hash"], seconds=round(time.time() - t0, 2), api_requests=client.requests_sent)
    c = inv["counts"]
    print(f"run: {run_dir.name}\nfolder: {run_dir}")
    print(f"inventory: {c['files']} files ({c['files_in_scope']} in scope), {c['folders']} folders, {c['total_bytes']:,} bytes")
    print(f"inventory_hash: {inv['inventory_hash']}\nwritten: {run_dir / 'inventory.json'} (metadata only; nothing was opened or changed)")
    cr = inv["crawl"]
    print(f"lead I: {len(cr['sub_bots_used'])}/{pool} sub-bots used, {cr['folders_listed']} folders, "
          f"{cr['elapsed_seconds']} s ({cr['avg_per_minute']} items/min), retries {retries}")
    if not inv["complete"]:
        print(f"INCOMPLETE: {len(inv['errors'])} folder(s) could not be listed (see inventory.json 'errors'); plans built on this inventory are refused")
        sys.exit(3)


def cmd_plan(args):
    if args.action == "folders":
        cfg, d, run_dir, cmd = _setup(args, "plan_folders", extra={"names": args.name})
        inv = _inv(run_dir)
        if not args.name:
            sys.exit("give at least one --name (a folder path such as PCMCP_TEST)")
        fm = _fmap(d)
        mp = {planfile.norm(f["path"]) for f in fm["folders"]}
        names = [planfile.norm(n) for n in args.name]
        plan = {"schema": "pcmcp.drive_plan", "schema_version": 1, "mode": "folders_only", "author": cmd["issued_by"],
                "written_at": now_iso(), "inventory_hash": inv["inventory_hash"], "scope_root_id": inv["scope_root_id"],
                "new_folders_declared": [n for n in names if n not in mp], "create_folders": names,
                "placements": {}, "archives": {}, "copies": {}, "stay": [], "tags": {}, "notes": {}}
        v = planfile.validate(plan, inv, fm)
        if not v["ok"]:
            _print_validation(v)
            sys.exit(2)
        h = planfile.plan_hash(plan)
        out = run_dir / f"plan-{h[:12]}.json"
        out.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        runs.event(run_dir, "folders_plan_written", command_id=cmd["command_id"], plan_hash=h, folders=names)
        print(f"folders-only plan written: {out}\nplan hash: {h}\nIt creates {names} and moves nothing.")
        print(f"Next: drive dryrun --plan '{out}'  ->  Angie: drive approve {h[:8]} --plan '{out}'  ->  "
              f"drive run --plan '{out}'  (or Instinct texts: PCMCP-<code> run {h[:8]})")
        return
    if args.action == "template":
        cfg, d, run_dir, cmd = _setup(args, "plan_template")
        inv = _inv(run_dir)
        t = {"schema": "pcmcp.drive_plan", "schema_version": 1, "author": "Instinct", "written_at": now_iso(),
             "inventory_hash": inv["inventory_hash"], "scope_root_id": inv["scope_root_id"],
             "new_folders_declared": [], "create_folders": [], "placements": {"Review": []}, "archives": {},
             "copies": {}, "stay": [], "tags": {}, "notes": {}}
        print(json.dumps(t, indent=2))
        return
    cfg, d, run_dir, cmd = _setup(args, f"plan_{args.action}", extra={"plan_file": args.plan})
    p, h = _plan(args, run_dir)
    inv = _inv(run_dir)
    if args.action == "show":
        print(planfile.render(p, inv))
        return
    v = planfile.validate(p, inv, _fmap(d))
    runs.write_json(run_dir / f"validation-{h[:12]}.json", dict(v, command_id=cmd["command_id"]))
    _write_skipped(run_dir, h, v.get("skipped", []))
    runs.event(run_dir, "plan_validated", command_id=cmd["command_id"], plan_hash=h, ok=v["ok"], errors=len(v["errors"]))
    _print_validation(v)
    if not v["ok"]:
        sys.exit(2)


def cmd_owner_setup(args):
    cfg = config_mod.load(args.config)
    fp = owner.setup(drive_cfg(cfg))
    print(f"owner approval key created. Fingerprint: {fp}\nWrite this fingerprint down; every approval and run prints it.")


def cmd_approve(args):
    """SAFETY: an approval is a signature made with Angie's passphrase-protected owner key.
    No flag, label or log line can stand in for it; `run` verifies the signature itself."""
    cfg, d, run_dir, cmd = _setup(args, "approve", extra={"plan_file": args.plan})
    if not sys.stdin.isatty():
        sys.exit("refused: approve needs the owner at an interactive terminal (stdin is not a TTY)")
    p, h = _plan(args, run_dir)
    if not h.startswith(args.hash_prefix) or len(args.hash_prefix) < 8:
        sys.exit("refused: give at least 8 characters of the plan hash shown by `drive plan show`")
    v = planfile.validate(p, _inv(run_dir), _fmap(d))
    if not v["ok"]:
        sys.exit("refused: the plan does not validate; it cannot be approved")
    print(planfile.render(p))
    print(f"\nOWNER: signing approval of plan {h} for run {run_dir.name} with key {owner.fingerprint(d)}.")
    print("Enter your owner-key passphrase only if you approve this plan.")
    rec = owner.sign_approval(d, run_dir, h, cmd["command_id"])
    rec.update(issued_by=cmd["issued_by"], tty=cmd["tty"], os_user=getpass.getuser(), host=socket.gethostname())
    append_jsonl(run_dir / "approvals.jsonl", rec)   # index only; `run` trusts the signature, not this line
    append_jsonl(Path(cfg["data_dir"]).expanduser() / "logs" / "approvals.jsonl", dict(rec, kind="drive"))
    print(f"approved (signed) {h} for run {run_dir.name}")


def _write_skipped(run_dir, h, skipped):
    """For Angie: the 3D assets attached to Synomize that the plan tried to move out; they stay put."""
    if not skipped:
        return None
    f = run_dir / f"skipped-kept-in-synomize-{h[:12]}.txt"
    f.write_text("These files stay where they are (3D assets attached to Synomize never leave Synomize).\n"
                 "The plan asked to move them; those moves are skipped.\n\n"
                 + "\n".join(f"{x['path']}\t{x['file_id']}" for x in skipped) + "\n", encoding="utf-8")
    print(f"{len(skipped)} move(s) skipped to keep Synomize 3D assets in Synomize; list: {f}")
    return f


def _approved(d, run_dir, h):
    return owner.verify_approval(d, run_dir, h)


def cmd_dryrun(args):
    cfg, d, run_dir, cmd = _setup(args, "dryrun", extra={"plan_file": args.plan})
    p, h = _plan(args, run_dir)
    client = _client(d, read_only=True)
    res = swarm.dry_run(client, p, _inv(run_dir), _fmap(d), d)
    _write_skipped(run_dir, h, res.get("skipped_kept_in_synomize", []))
    res["command_id"] = cmd["command_id"]
    res["api_requests"] = client.requests_sent
    runs.write_json(run_dir / "dryrun.json", res)
    runs.event(run_dir, "dry_run", command_id=cmd["command_id"], plan_hash=h, ok=res["ok"], blockers=len(res["blockers"]))
    print(f"DRY RUN plan {h}  (read-only; nothing changed)")
    if res["would"]:
        w = res["would"]
        print(f"would: create {w['create_folders']} folders (reuse {len(w['reuse_existing_folders'])}), move {w['moves']}, "
              f"copy {w['copies']}, archive {w['archive_moves']}; already in place {w['already_in_place']}")
    e = res["estimate"]
    print(f"estimate: {e['write_calls']} write calls in ~{e['batch_requests']} batch requests; >= {e['minutes_at_throttle']} min at {e['edits_per_minute_ceiling']}/min ceiling")
    for b in res["blockers"][:100]:
        print(f"  BLOCKER {b['code']}: {b['detail']}")
    for w_ in res["warnings"][:30]:
        print(f"  warn    {w_['code']}: {w_['detail']}")
    print("DRY RUN OK - ready for `drive run` once approved" if res["ok"] else "DRY RUN FOUND BLOCKERS - fix the plan; run will refuse")
    if not res["ok"]:
        sys.exit(2)


def _after(cfg, d, run_dir, inv, name, cmd, exclude_ids=None):
    client = _client(d, read_only=True)
    now_inv = inv_mod.export(client, inv["scope_arg"])
    ex = set(exclude_ids or [])
    if ex:  # exclude the holding folder and everything parked in it
        kids = {}
        for i in now_inv["items"]:
            for par in i["parents"]:
                kids.setdefault(par, []).append(i["id"])
        stack = list(ex)
        while stack:
            for k in kids.get(stack.pop(), []):
                if k not in ex:
                    ex.add(k)
                    stack.append(k)
    lst = inv_mod.listing_of(now_inv, ex)
    lst["command_id"] = cmd["command_id"]
    runs.write_json(run_dir / name, lst)
    return lst


def cmd_run(args):
    cfg, d, run_dir, cmd = _setup(args, "run", extra={"plan_file": args.plan})
    p, h = _plan(args, run_dir)
    inv, fm = _inv(run_dir), _fmap(d)
    problems = check_pools(d["pools"])
    if problems:
        sys.exit("refused: " + "; ".join(problems))
    v = planfile.validate(p, inv, fm)                        # never-move rules, check at run start
    if not v["ok"]:
        _print_validation(v)
        sys.exit("refused: plan does not validate")
    dr = read_json(run_dir / "dryrun.json") or {}
    if dr.get("plan_hash") != h or not dr.get("ok"):
        sys.exit("refused: no passing dry run for this exact plan; run `drive dryrun` first")
    appr = _approved(d, run_dir, h)
    if not appr:
        sys.exit(f"refused: plan {h[:12]} has no valid owner signature for this run (Angie: `drive approve {h[:8]} --plan ...`)")
    print(f"owner approval verified: signed by key {appr['verified_fingerprint']} (matches root-owned pin) at {appr['approved_at']}")
    shutil.copyfile(Path(args.plan).expanduser(), run_dir / "plan.json")
    if runs.control_state(run_dir) == "stop":                 # resuming after `drive stop`: clear the stop (a pause is kept)
        if args.no_clear_stop:
            sys.exit("refused: this run was stopped by the owner; a texted run cannot resume it")
        runs.set_control(run_dir, "go", cmd["command_id"])
    r = swarm.Runner(cfg, d, lambda cb: _client(d, on_retry=cb), run_dir, cmd, p, inv, fm)
    print(f"RUN {run_dir.name} plan {h[:12]}: {r.stats.planned} operations, pools {d['pools']}  (live view: drive status --watch)")
    st = r.execute()
    after = _after(cfg, d, run_dir, inv, "after_listing.json", cmd)
    before = read_json(run_dir / "before_listing.json")
    comp = inv_mod.compare(before, after)
    receipts = [x for x in read_jsonl(run_dir / "receipts.jsonl")]
    ok = [x for x in receipts if x.get("ok")]
    start = next((e["at"] for e in read_jsonl(run_dir / "events.jsonl") if e["event"] == "run_started"), None)
    moved = sum(1 for x in ok if x["action"] in ("move", "archive_move"))
    secs = st["elapsed_seconds"]
    summary = {
        "run": run_dir.name, "plan_hash": h, "command_id": cmd["command_id"], "issued_by": cmd["issued_by"],
        "owner_approval": appr,
        "skipped_kept_in_synomize": r.compiled["skipped"],
        "state": st["state"], "start_time": start, "end_time": now_iso(), "wall_seconds": secs,
        "files_moved": moved, "files_copied": sum(1 for x in ok if x["action"] == "copy"),
        "folders_created": sum(1 for x in ok if x["action"] == "create_folder"),
        "operations_ok": len(ok), "operations_planned": st["planned"], "errors": st["errors"], "refused": st["refused"],
        "uncertain": st["uncertain"], "retries": st["retries"],
        "average_operations_per_minute": round(len(ok) / secs * 60, 1) if secs else None,
        "average_moves_per_minute": round(moved / secs * 60, 1) if secs else None,
        "sub_bots_used": {k: v["sub_bots_used"] for k, v in st["leads"].items()},
        "sub_bots_used_total": sum(len(v["sub_bots_used"]) for v in st["leads"].values()),
        "chunks": {k: v["chunks"] for k, v in st["leads"].items()}, "api_requests": st["api_requests"],
        "receipts": len(receipts), "receipt_chain_ok": runs.verify_chain(run_dir / "receipts.jsonl")[0],
        "before_vs_after": {k: comp[k] for k in ("before_hash", "after_hash", "moved", "missing_after", "new_after", "bytes_changed")},
    }
    runs.write_json(run_dir / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("state", "wall_seconds", "operations_ok", "operations_planned", "errors", "retries",
                                              "average_moves_per_minute", "sub_bots_used_total")}, indent=2))
    if st["state"] != "done" or st["errors"]:
        sys.exit(3)


def cmd_control(args):
    cfg, d, run_dir, cmd = _setup(args, args.action)
    runs.set_control(run_dir, {"pause": "pause", "resume": "go", "stop": "stop"}[args.action], cmd["command_id"])
    print(f"{args.action}: written to {run_dir / 'control.json'} (sub-bots check it between chunks)")
    if args.action == "stop":
        print("in-flight chunks finish, then the run exits; re-run the same `drive run` command to resume")


def render_counter(s: dict) -> str:
    if s.get("mode") == "inventory":
        lead = s["leads"]["I"]
        return (f"PCMCP INVENTORY  run {s['run']}  state {s['state'].upper()}\n"
                f"command {s['command_id']} issued by {s['issued_by']}\n"
                f"listed {s['done']} items from {lead['chunks']['done']} folders ({lead['chunks']['pending']} folders waiting)\n"
                f"bots active {s['bots_active']}/{s['bots_total']}   items/min (last 60s) {s['per_minute_last_60s']}   avg/min {s['avg_per_minute']}\n"
                f"errors {s['errors']}   retries rate-limit {s['retries'].get('rate_limit', 0)} transient {s['retries'].get('transient', 0)}"
                f"   elapsed {s['elapsed_seconds']}s   sub-bots used {len(lead['sub_bots_used'])}")
    bar_n = 40
    frac = (s["done"] / s["planned"]) if s.get("planned") else 0
    bar = "#" * int(frac * bar_n) + "." * (bar_n - int(frac * bar_n))
    L = [f"PCMCP SWARM  run {s['run']}  mode {s['mode']}  state {s['state'].upper()}",
         f"command {s['command_id']} issued by {s['issued_by']}",
         f"[{bar}] {s['done']}/{s['planned']}  ({frac * 100:.1f}%)",
         f"bots active {s['bots_active']}/{s['bots_total']}   moves/min (last 60s) {s['per_minute_last_60s']}   avg/min {s['avg_per_minute']}",
         f"errors {s['errors']}   refused {s.get('refused', 0)}   retries rate-limit {s['retries'].get('rate_limit', 0)} transient {s['retries'].get('transient', 0)}"
         f"   elapsed {s['elapsed_seconds']}s"]
    for k, v in sorted(s["leads"].items()):
        c = v["chunks"]
        L.append(f"  lead {k}: pool {v['pool']:>3}  items {v['items_done']}/{v['items_planned']}  chunks "
                 f"pending {c['pending']} running {c['running']} done {c['done']} failed {c['failed']}  "
                 f"reassigned {v['reassigned']}  sub-bots used {len(v['sub_bots_used'])}")
    return "\n".join(L)


def cmd_status(args):
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    run_dir = runs.find_run(d["runs_dir"], args.run)
    while True:
        s = read_json(run_dir / "status.json")
        if not s:
            print(f"run {run_dir.name}: no status yet (nothing running). Files: {sorted(p.name for p in run_dir.iterdir())}")
            return
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            if args.watch:
                print("\033[2J\033[H", end="")
            print(render_counter(s))
        if not args.watch or s["state"] in ("done", "stopped"):
            return
        time.sleep(1)


def cmd_receipts(args):
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    run_dir = runs.find_run(d["runs_dir"], args.run)
    name = "rollback_receipts.jsonl" if args.rollback else "receipts.jsonl"
    rows = read_jsonl(run_dir / name)
    cmds = {c["command_id"]: c for c in read_jsonl(run_dir / "commands.jsonl")}
    ok, n, problem = runs.verify_chain(run_dir / name)
    if args.file or args.op:
        hits = [r for r in rows if (args.file and args.file in (r.get("file_id"), r.get("copy_id"), r.get("new_folder_id"))) or (args.op and r.get("op_id") == args.op)]
        for r in hits:
            print(json.dumps({"receipt": r, "caused_by_command": cmds.get(r["command_id"])}, indent=2))
        if not hits:
            print("no receipt for that file/op")
        return
    if args.json:
        print(json.dumps({"chain_ok": ok, "count": n, "problem": problem, "receipts": rows}, indent=2))
        return
    print(f"{name}: {n} receipts, hash chain {'OK' if ok else 'BROKEN: ' + str(problem)}")
    by_cmd = {}
    for r in rows:
        by_cmd.setdefault(r["command_id"], {"ok": 0, "failed": 0})["ok" if r.get("ok") else "failed"] += 1
    for cid, c in by_cmd.items():
        cm = cmds.get(cid, {})
        print(f"  {cid}  mode={cm.get('mode')}  issued_by={cm.get('issued_by')}  at={cm.get('issued_at')}  -> {c['ok']} ok, {c['failed']} failed")
    for r in rows[-args.tail:] if args.tail else []:
        print(f"  #{r['seq']} {r.get('op_id', r.get('undoes_seq'))} {r['action']} {r.get('file_name', r.get('path', ''))} "
              f"{r.get('old_folder', '')} -> {r.get('new_folder', '')} bot={r['bot']} ok={r['ok']}")


def cmd_rollback(args):
    cfg, d, run_dir, cmd = _setup(args, "rollback")
    inv = _inv(run_dir)
    if not (run_dir / "receipts.jsonl").exists():
        sys.exit("nothing to roll back: no receipts in this run")
    if not args.yes:
        typed = input(f"OWNER: roll back run {run_dir.name}? type the run name to confirm: ").strip()
        if typed != run_dir.name:
            sys.exit("not rolled back")
    runs.set_control(run_dir, "go", cmd["command_id"])
    rb = swarm.Rollback(cfg, d, lambda cb: _client(d, on_retry=cb), run_dir, cmd, inv)
    print(f"ROLLBACK {run_dir.name}: {len(rb.src) - len(rb.done_seqs)} receipts to reverse")
    st = rb.execute()
    holding = st.get("holding_folder_id")
    after = _after(cfg, d, run_dir, inv, "rollback_listing.json", cmd, exclude_ids=[holding] if holding and inv["scope_arg"] == "root" else None)
    before = read_json(run_dir / "before_listing.json")
    comp = inv_mod.compare(before, after)
    out = {"run": run_dir.name, "command_id": cmd["command_id"], "state": st["state"], "reversed_ok": st["done"],
           "errors": st["errors"], "retries": st["retries"], "wall_seconds": st["elapsed_seconds"],
           "holding_folder_id": holding, "byte_identical": comp["identical"] and not comp["bytes_changed"], "comparison": comp,
           "rollback_chain_ok": runs.verify_chain(run_dir / "rollback_receipts.jsonl")[0], "finished_at": now_iso()}
    runs.write_json(run_dir / "rollback_summary.json", out)
    print(json.dumps({k: out[k] for k in ("state", "reversed_ok", "errors", "byte_identical", "holding_folder_id")}, indent=2))
    print(f"before listing hash {comp['before_hash']}\nafter rollback hash {comp['after_hash']}")
    if not out["byte_identical"] or st["errors"]:
        sys.exit(3)


def cmd_pools(args):
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    from . import LETTER_JOBS, sub_bot_names
    for k, n in d["pools"].items():
        names = sub_bot_names(k, n)
        print(f"lead {k} ({LETTER_JOBS.get(k, '?')}): {n} sub-bots  {names[0]}..{names[-1]}")
    probs = check_pools(d["pools"])
    print("total bots:", sum(d["pools"].values()) + len(d["pools"]), "(cap 100)", "OK" if not probs else "; ".join(probs))


def cmd_fake_server(args):
    from .fake import FakeDrive, seed_demo
    fake = FakeDrive(rate_limit_every=args.rate_limit_every, latency_ms=args.latency_ms)
    test = seed_demo(fake, args.seed)
    url = fake.start(args.port)
    print(f"fake Drive at {url}  (test folder id {test}; {len(fake.files)} items)")
    print('point the swarm at it with config: {"drive": {"api_base": "%s", "auth": "none"}}' % url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


def cmd_trigger_setup(args):
    from . import trigger
    cfg = config_mod.load(args.config)
    code = trigger.setup(trigger.tcfg(cfg))
    print("New text trigger code (shown ONCE; only its hash is stored):\n\n  " + code + "\n")
    print("Give it to Instinct. Texts it can send:\n  " + "\n  ".join(code + " " + c for c in (
        "status", "inventory drive", "inventory icloud", "screenshot", "task <name>   (see text-tasks.json)",
        "run <plan-hash-8>   (only a plan Angie signed with her passphrase)")))
    print("A text can never approve a plan. Running `trigger-setup` again replaces the code.")


def cmd_trigger_watch(args):
    from . import trigger
    cfg = config_mod.load(args.config)
    d = drive_cfg(cfg)
    t = trigger.tcfg(cfg)
    w = trigger.Watcher(cfg, d, t, Path(cfg["data_dir"]).expanduser() / "logs" / "text_triggers.jsonl")
    try:
        trigger.watch(w, once=args.once)
    except KeyboardInterrupt:
        pass


def add_parser(sub):
    dp = sub.add_parser("drive", help="Swarm v2: Google Drive (see CONTROL.md)")
    ds = dp.add_subparsers(dest="drive_cmd", required=True)

    def p(name, fn, help_, run=True, plan=False):
        s = ds.add_parser(name, help=help_)
        s.add_argument("--issued-by", help="who issued this command (e.g. Instinct); recorded in the evidence trail")
        if run:
            s.add_argument("--run", help="run folder name or prefix (default: latest)")
        if plan:
            s.add_argument("--plan", required=True, help="path to the plan JSON file")
        s.set_defaults(fn=fn)
        return s

    s = p("auth", cmd_auth, "OWNER, once: sign in to Google in the browser", run=False)
    s.add_argument("--no-browser", action="store_true")
    s = p("inventory", cmd_inventory, "read-only export of every file's metadata; starts a new run folder", run=False)
    s.add_argument("--root", default="root", help="'root' = whole Drive, or a folder id (test folder)")
    s = p("plan", cmd_plan, "validate / show a plan file, or print an empty template")
    s.add_argument("action", choices=["validate", "show", "template", "folders"])
    s.add_argument("--name", action="append", help="folders: a folder path to create (repeatable)")
    s.add_argument("--plan", help="path to the plan JSON file (validate/show)")
    p("owner-setup", cmd_owner_setup, "OWNER, once: create the passphrase-protected approval key", run=False)
    s = p("approve", cmd_approve, "OWNER: sign approval of one exact plan hash for this run", plan=True)
    s.add_argument("hash_prefix")
    p("dryrun", cmd_dryrun, "simulate the plan against live Drive; changes nothing", plan=True)
    s = p("run", cmd_run, "execute an approved, dry-run-checked plan with the swarm", plan=True)
    s.add_argument("--no-clear-stop", action="store_true", help="refuse instead of clearing an owner `stop` (used by text triggers)")
    for a in ("pause", "resume", "stop"):
        s = p(a, cmd_control, f"{a} the running swarm safely")
        s.set_defaults(action=a)
    s = p("status", cmd_status, "live status / counter view")
    s.add_argument("--watch", action="store_true"); s.add_argument("--json", action="store_true")
    s = p("receipts", cmd_receipts, "read receipts, verify the hash chain, trace work to its command")
    s.add_argument("--json", action="store_true"); s.add_argument("--rollback", action="store_true")
    s.add_argument("--file", help="show the receipt(s) and causing command for one file id")
    s.add_argument("--op", help="show the receipt and causing command for one op id")
    s.add_argument("--tail", type=int, default=0)
    s = p("rollback", cmd_rollback, "OWNER: put every file back from the receipts, then prove it")
    s.add_argument("--yes", action="store_true")
    p("pools", cmd_pools, "show leads and sub-bot pools", run=False)
    p("trigger-setup", cmd_trigger_setup, "OWNER: create the secret text code for Instinct", run=False)
    s = p("trigger-watch", cmd_trigger_watch, "watch Messages for Instinct's coded texts and text back results (status, inventory, screenshot, task, signed run)", run=False)
    s.add_argument("--once", action="store_true", help="check once and exit")
    s = p("fake-server", cmd_fake_server, "offline rehearsal: start a local fake Drive", run=False)
    s.add_argument("--port", type=int, default=8799); s.add_argument("--seed", type=int, default=600)
    s.add_argument("--rate-limit-every", type=int, default=0); s.add_argument("--latency-ms", type=int, default=20)
