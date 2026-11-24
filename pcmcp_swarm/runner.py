"""Runner: dispatches plan operations to bot programs, journals progress, resumes after a crash,
and drives undo. The runner is plumbing only; it makes no choices beyond the fixed phase order.

Journal (journal/<plan_hash>.jsonl): one "started" and one "done"/"failed" line per op_id.
Resume rule (hard rule 6): ops with a "done" line are skipped. Ops with only "started" are re-sent
to their bot, and the bot's intent file makes the re-run idempotent (see bots/common.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import health, plans as plans_mod
from .util import append_jsonl, atomic_write_json, now_iso, read_jsonl

BOT_MODULES = {"A": "pcmcp_swarm.bots.bot_a", "B": "pcmcp_swarm.bots.bot_b", "C": "pcmcp_swarm.bots.bot_c",
               "E": "pcmcp_swarm.bots.bot_e", "S": "pcmcp_swarm.bots.bot_s"}
PKG_PARENT = str(Path(__file__).resolve().parent.parent)


class NotApproved(Exception):
    pass


def call_bot(order: dict) -> dict:
    """Run one bot program with the stdin/stdout JSON contract."""
    letter = order["bot"][:1]
    env = dict(os.environ, PYTHONPATH=PKG_PARENT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, "-m", BOT_MODULES[letter]], input=json.dumps(order),
                       capture_output=True, text=True, env=env, cwd=PKG_PARENT, timeout=600)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"schema": "pcmcp.receipt", "schema_version": 1, "op_id": order.get("op_id"), "bot": order.get("bot"),
                "action": order.get("action"), "mode": order.get("mode"), "status": "error",
                "error": f"bot crashed (exit {r.returncode}): {r.stderr[-800:]}", "files": [],
                "started_at": now_iso(), "finished_at": now_iso()}


def base_order(paths, cfg, plan, op, mode) -> dict:
    return {"schema": "pcmcp.order", "schema_version": 1, "plan_hash": plan["plan_hash"], "op_id": op["op_id"],
            "bot": op["bot"], "action": op["action"], "params": op["params"], "mode": mode,
            "allowed_roots": [os.path.realpath(r) for r in cfg["allowed_roots"]],
            "quarantine_dir": str(paths["quarantine"]), "registry_path": str(paths["registry"]),
            "screenshots_dir": str(paths["screenshots"])}


def journal_state(paths, plan_hash) -> dict:
    state = {}
    for rec in read_jsonl(paths["journal"] / f"{plan_hash}.jsonl"):
        if rec.get("event") == "done" or state.get(rec["op_id"]) != "done":
            state[rec["op_id"]] = rec["event"]
    return state


def execute(paths, cfg, registry, plan, dry_run=True, log=print) -> dict:
    """Run a plan. dry_run=True (the default everywhere) changes nothing.

    SAFETY: a real run requires (1) the plan to validate against the registry and allowed roots and
    (2) an owner approval recorded for this exact plan_hash.
    """
    problems = plans_mod.validate(plan, registry, cfg["allowed_roots"])
    if problems:
        raise ValueError("plan failed validation: " + "; ".join(problems))
    h = plan["plan_hash"]
    if not dry_run and not plans_mod.is_approved(paths, h):
        raise NotApproved(f"plan {h[:12]} has no owner approval. Run: python3 -m pcmcp_swarm approve {h[:12]}")
    mode = "dry_run" if dry_run else "execute"
    jpath = paths["journal"] / f"{h}.jsonl"
    done_state = {} if dry_run else journal_state(paths, h)
    crash_after = int(os.environ.get("PCMCP_CRASH_AFTER_OPS", "0") or 0)  # test hook: simulate power loss
    counter = {"done": 0}
    lock = threading.Lock()
    receipts, halted = [], None

    def run_bot_queue(bot, ops):
        out = []
        for op in ops:
            if done_state.get(op["op_id"]) == "done":
                continue
            if not dry_run:
                append_jsonl(jpath, {"event": "started", "op_id": op["op_id"], "bot": bot, "at": now_iso()})
                health.heartbeat(paths, bot, f"op {op['op_id']}")
            rc = call_bot(base_order(paths, cfg, plan, op, mode))
            out.append(rc)
            if not dry_run:
                append_jsonl(paths["operations"], dict(rc, plan_hash=h))
                ok = rc.get("status") in ("done", "already_done")
                append_jsonl(jpath, {"event": "done" if ok else "failed", "op_id": op["op_id"], "bot": bot,
                                     "status": rc.get("status"), "at": now_iso()})
                health.record(paths, rc)
                if ok and crash_after:
                    with lock:
                        counter["done"] += 1
                        if counter["done"] >= crash_after:
                            os._exit(137)
        if not dry_run:
            health.heartbeat(paths, bot, "idle")
        return out

    for letter in plans_mod.PHASES:
        phase_ops = [op for op in plan["body"]["operations"] if op["letter"] == letter]
        if not phase_ops:
            continue
        queues = {}
        for op in phase_ops:
            queues.setdefault(op["bot"], []).append(op)
        log(f"phase {letter}: {len(phase_ops)} ops across {len(queues)} bots ({mode})")
        with ThreadPoolExecutor(max_workers=len(queues)) as pool:
            for res in pool.map(lambda kv: run_bot_queue(*kv), sorted(queues.items())):
                receipts.extend(res)
        bad = [r for r in receipts if r.get("status") in ("error", "refused")]
        if bad and not dry_run:
            # SAFETY: never continue into later (more destructive) phases after a failure.
            halted = f"halted after phase {letter}: {len(bad)} failed ops"
            log(halted)
            break

    result = {"schema": "pcmcp.run_result", "schema_version": 1, "plan_hash": h, "mode": mode, "finished_at": now_iso(),
              "halted": halted, "counts": _count([r.get("status") for r in receipts]), "receipts": receipts}
    if dry_run:
        atomic_write_json(paths["root"] / "dry_runs" / f"{h}.json", result)
    return result


def undo(paths, cfg, registry, plan, op_id=None, log=print) -> dict:
    """Reverse completed ops (newest first) using a bot of the SAME letter. Local CLI only."""
    h = plan["plan_hash"]
    done = {}
    for rec in read_jsonl(paths["operations"]):
        if rec.get("plan_hash") == h and rec.get("mode") == "execute" and rec.get("status") in ("done", "already_done") \
                and rec.get("undo"):
            done[rec["op_id"]] = rec
    undone = {r["op_id"] for r in read_jsonl(paths["operations"])
              if r.get("plan_hash") == h and r.get("mode") == "undo" and r.get("status") in ("done", "already_done")}
    ops_by_id = {op["op_id"]: op for op in plan["body"]["operations"]}
    order_ids = [op["op_id"] for op in plan["body"]["operations"]]
    targets = [op_id] if op_id else [i for i in reversed(order_ids) if i in done]
    receipts = []
    for oid in targets:
        if oid not in done:
            raise ValueError(f"op {oid} has no completed execute receipt to undo")
        if oid in undone:
            log(f"{oid}: already undone")
            continue
        op = ops_by_id[oid]
        order = base_order(paths, cfg, plan, op, "undo")
        order["undo"] = done[oid]["undo"]
        rc = call_bot(order)
        append_jsonl(paths["operations"], dict(rc, plan_hash=h))
        health.record(paths, rc)
        receipts.append(rc)
        log(f"undo {oid} by {op['bot']}: {rc.get('status')} {rc.get('error') or ''}")
        if rc.get("status") not in ("done", "already_done"):
            log("stopping undo at first failure")
            break
    return {"plan_hash": h, "receipts": receipts}


def report(paths, plan) -> dict:
    """Final report: what every bot did, hashes before/after, timing, errors, undo proof."""
    h = plan["plan_hash"]
    recs = [r for r in read_jsonl(paths["operations"]) if r.get("plan_hash") == h]
    per_bot = {}
    for r in recs:
        b = per_bot.setdefault(r.get("bot"), {"executed": 0, "undone": 0, "errors": [], "first": None, "last": None})
        if r.get("status") in ("done", "already_done"):
            b["executed" if r.get("mode") == "execute" else "undone"] += 1
        else:
            b["errors"].append({"op_id": r.get("op_id"), "status": r.get("status"), "error": r.get("error")})
        b["first"] = min(filter(None, [b["first"], r.get("started_at")]))
        b["last"] = max(filter(None, [b["last"], r.get("finished_at")]))
    return {"schema": "pcmcp.report", "schema_version": 1, "plan_hash": h, "generated_at": now_iso(),
            "approved": plans_mod.is_approved(paths, h), "summary": plan["body"]["summary"],
            "per_bot": dict(sorted(per_bot.items())),
            "operations": [{k: r.get(k) for k in ("op_id", "bot", "action", "mode", "status", "src", "dst", "files",
                                                   "started_at", "finished_at", "error")} for r in recs],
            "undo_events": [r.get("op_id") for r in recs if r.get("mode") == "undo"]}


def _count(items) -> dict:
    d = {}
    for i in items:
        d[i] = d.get(i, 0) + 1
    return d
