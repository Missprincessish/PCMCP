"""The check-in handshake: fetch -> verify -> act -> receipt. Channel-agnostic.

SAFETY: this is the single gate between "bytes from somewhere" and "the swarm does something".
  - signature, expiry, replay and registry checks happen in signing.verify() for EVERY channel
  - a batch can never approve anything: scan/plan only PROPOSE; execute runs only an owner-approved plan
  - the batch_id is burned before acting, so a crash can't turn into a replay
"""
from __future__ import annotations

import os

from .. import health, plans as plans_mod, registry as reg_mod, runner, scanner, signing
from ..secret_store import get_secret
from ..util import append_jsonl, atomic_write_json, is_within, now_iso, read_jsonl, sha256_bytes, sha256_file
from . import get_channel


def _log(paths, **kw):
    append_jsonl(paths["checkin"], dict(at=now_iso(), **kw))


def check_in(paths, cfg, wake_reason: str) -> dict:
    registry = reg_mod.load(paths["registry"])
    try:
        channel = get_channel(cfg)
        items = channel.fetch_instructions()
    except Exception as e:  # missing / broken check-in response: log, reject, sleep
        _log(paths, event="checkin_failed", reason=wake_reason, error=f"{type(e).__name__}: {e}")
        return {"accepted": 0, "rejected": 0, "error": str(e)}
    if not items:
        _log(paths, event="no_instructions", reason=wake_reason, channel=channel.name)
        return {"accepted": 0, "rejected": 0}
    secret = get_secret(cfg["checkin"]["secret"])
    accepted = rejected = 0
    for raw, meta in items:
        try:
            if meta.get("oversize"):
                raise signing.Rejected("oversize batch")
            batch = signing.verify(raw, secret, paths["used_batches"], registry,
                                   cfg["checkin"]["max_batch_lifetime_seconds"])
        except signing.Rejected as e:
            rejected += 1
            _log(paths, event="batch_rejected", channel=channel.name, ref=meta.get("ref"), reason=str(e),
                 raw_sha256=sha256_bytes(raw))
            channel.acknowledge(meta, accepted=False)
            channel.post_receipts({"schema": "pcmcp.batch_receipt", "schema_version": 1, "status": "rejected",
                                   "reason": str(e), "ref": meta.get("ref"), "at": now_iso()})
            continue
        signing.mark_used(paths["used_batches"], batch["batch_id"], channel.name)
        channel.acknowledge(meta, accepted=True)
        _log(paths, event="batch_accepted", channel=channel.name, batch_id=batch["batch_id"], kind=batch["kind"])
        accepted += 1
        try:
            receipt, attachments = process(paths, cfg, registry, batch)
        except Exception as e:  # noqa: BLE001
            receipt, attachments = {"status": "error", "error": f"{type(e).__name__}: {e}"}, []
        receipt.update(schema="pcmcp.batch_receipt", schema_version=1, batch_id=batch["batch_id"],
                       kind=batch["kind"], delivered_by=channel.name, at=now_iso())
        where = channel.post_receipts(receipt, attachments)
        _log(paths, event="receipt_posted", batch_id=batch["batch_id"], status=receipt.get("status"), to=where)
    return {"accepted": accepted, "rejected": rejected}


def process(paths, cfg, registry, batch) -> tuple:
    kind, params = batch["kind"], batch.get("params") or {}
    source = {"batch_id": batch["batch_id"], "kind": kind}
    if kind == "status":
        return {"status": "ok", "health": health.status(paths, registry, cfg)}, []

    if kind == "scan":
        root = os.path.realpath(str(params.get("root", "")))
        if not any(is_within(root, r) for r in cfg["allowed_roots"]):
            return {"status": "refused", "error": f"root not in allowed_roots: {root}"}, []
        plan, classification = scanner.build_plan(root, cfg, registry, source)
        return _proposed(paths, plan, classification)

    if kind == "plan":
        ops = batch.get("operations") or []
        for op in ops:
            op.setdefault("letter", reg_mod.letter_for_action(op["action"]))
        plan = plans_mod.make(os.path.realpath(str(params.get("root", ""))), ops, source)
        problems = plans_mod.validate(plan, registry, cfg["allowed_roots"])
        if problems:
            return {"status": "refused", "error": "; ".join(problems)}, []
        return _proposed(paths, plan, None)

    if kind == "execute":
        plan = plans_mod.load(paths, str(params.get("plan_hash", "")))
        if not plans_mod.is_approved(paths, plan["plan_hash"]):
            return {"status": "not_approved", "plan_hash": plan["plan_hash"],
                    "note": "the owner must approve this exact plan hash locally"}, []
        res = runner.execute(paths, cfg, registry, plan, dry_run=False, log=lambda *_: None)
        rep = runner.report(paths, plan)
        rpath = paths["root"] / "reports" / f"{plan['plan_hash']}.json"
        atomic_write_json(rpath, rep)
        return {"status": "executed" if not res["halted"] else "halted", "plan_hash": plan["plan_hash"],
                "counts": res["counts"], "halted": res["halted"], "health": health.status(paths, registry, cfg)}, [rpath]

    if kind == "screenshot":
        target = params.get("target") or {"type": "full"}
        if cfg["screenshot"]["requires_owner_approval"]:
            append_jsonl(paths["pending"], {"request_id": batch["batch_id"], "kind": "screenshot",
                                            "target": target, "at": now_iso()})
            return {"status": "pending_owner_approval", "request_id": batch["batch_id"],
                    "note": "owner runs: python3 -m pcmcp_swarm pending approve " + batch["batch_id"]}, []
        return take_screenshot(paths, cfg, registry, target, batch["batch_id"])

    return {"status": "refused", "error": f"unhandled kind {kind}"}, []


def _proposed(paths, plan, classification):
    plans_mod.save(paths, plan)
    text_path = paths["plans"] / f"{plan['plan_hash']}.txt"
    text_path.write_text(plans_mod.render(plan), encoding="utf-8")
    att = [text_path]
    if classification is not None:
        cpath = paths["plans"] / f"{plan['plan_hash']}.classification.json"
        atomic_write_json(cpath, classification)
        att.append(cpath)
    return {"status": "awaiting_owner_approval", "plan_hash": plan["plan_hash"], "summary": plan["body"]["summary"],
            "note": "nothing has been changed; the owner must approve this exact plan hash locally"}, att


def take_screenshot(paths, cfg, registry, target, request_id):
    bots = registry["letters"]["S"]["bots"]
    if not bots:
        return {"status": "refused", "error": "no S bots in registry"}, []
    out = paths["screenshots"] / f"{request_id}.png"
    order = {"schema": "pcmcp.order", "schema_version": 1, "plan_hash": "screenshots", "op_id": request_id,
             "bot": bots[0], "action": "screenshot", "params": {"target": target, "out_path": str(out)},
             "mode": "execute", "allowed_roots": [], "quarantine_dir": str(paths["quarantine"]),
             "registry_path": str(paths["registry"]), "screenshots_dir": str(paths["screenshots"])}
    rc = runner.call_bot(order)
    append_jsonl(paths["operations"], dict(rc, plan_hash="screenshots"))
    health.record(paths, rc)
    ok = rc.get("status") in ("done", "already_done")
    return {"status": "done" if ok else rc.get("status"), "screenshot": str(out) if ok else None,
            "sha256": sha256_file(out) if ok else None, "error": rc.get("error")}, ([out] if ok else [])


def pending(paths) -> list:
    done = {r.get("op_id") for r in read_jsonl(paths["operations"]) if r.get("plan_hash") == "screenshots"}
    return [p for p in read_jsonl(paths["pending"]) if p["request_id"] not in done]
