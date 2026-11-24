"""Signed instruction batches: the ONLY authority in the system.

The doorbell carries nothing. A channel carries bytes. Only a batch whose HMAC-SHA256
signature verifies, that has not expired, whose batch_id was never used before, and whose
every operation passes the registry check is acted on. Everything else is logged and dropped.
See SPEC.md §4 for the exact wire format.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from . import registry as reg_mod
from .util import safe_id, append_jsonl, canonical_json, now_iso, parse_iso, read_jsonl

BATCH_SCHEMA = "pcmcp.batch"
KINDS = {"scan", "plan", "execute", "screenshot", "status"}


def sign(batch: dict, secret: str) -> dict:
    sig = hmac.new(secret.encode("utf-8"), canonical_json(batch), hashlib.sha256).hexdigest()
    return {"schema": "pcmcp.signed_batch", "schema_version": 1, "batch": batch,
            "signature": {"alg": "HMAC-SHA256", "value": sig}}


class Rejected(Exception):
    pass


def verify(raw: bytes, secret: str, used_ids_path, registry: dict, max_lifetime_s: int) -> dict:
    """SAFETY: full validation of an incoming batch. Returns the batch or raises Rejected(reason).

    Order matters: the signature is checked before anything inside the batch is trusted.
    """
    try:
        env = json.loads(raw.decode("utf-8"))
    except Exception:
        raise Rejected("malformed: not JSON")
    if not isinstance(env, dict) or env.get("schema") != "pcmcp.signed_batch":
        raise Rejected("malformed: not a pcmcp.signed_batch envelope")
    batch, sig = env.get("batch"), (env.get("signature") or {})
    if not isinstance(batch, dict) or sig.get("alg") != "HMAC-SHA256" or not isinstance(sig.get("value"), str):
        raise Rejected("unsigned or malformed signature")
    expected = hmac.new(secret.encode("utf-8"), canonical_json(batch), hashlib.sha256).hexdigest()
    # Constant-time compare: no timing oracle on the signature.
    if not hmac.compare_digest(expected, sig["value"]):
        raise Rejected("bad signature")

    # --- From here the batch is authentic. Now check it is current, unique and lawful. ---
    if batch.get("schema") != BATCH_SCHEMA or batch.get("schema_version") != 1:
        raise Rejected("unsupported batch schema/version")
    bid = batch.get("batch_id")
    if not safe_id(bid):
        raise Rejected("missing or unsafe batch_id (allowed: letters, digits, . _ -; max 128)")
    if batch.get("kind") not in KINDS:
        raise Rejected(f"unknown kind {batch.get('kind')!r}")
    try:
        issued, expires = parse_iso(batch["issued_at"]), parse_iso(batch["expires_at"])
    except Exception:
        raise Rejected("missing/invalid issued_at or expires_at")
    now = parse_iso(now_iso())
    if expires <= now:
        raise Rejected("expired")
    if (expires - issued).total_seconds() > max_lifetime_s:
        raise Rejected("lifetime exceeds max_batch_lifetime_seconds")
    used = {r.get("batch_id") for r in read_jsonl(used_ids_path)}
    if bid in used:
        raise Rejected("replay: batch_id already used")
    for op in batch.get("operations", []) or []:
        err = check_op_shape(op, registry)
        if err:
            raise Rejected(f"operation rejected: {err}")
    return batch


def mark_used(used_ids_path, batch_id: str, channel: str) -> None:
    """Burn the batch_id BEFORE acting, so a crash mid-run can never cause a replay to be accepted."""
    append_jsonl(used_ids_path, {"batch_id": batch_id, "channel": channel, "at": now_iso()})


def check_op_shape(op: dict, registry: dict) -> str | None:
    if not isinstance(op, dict):
        return "operation is not an object"
    for k in ("op_id", "bot", "action", "params"):
        if k not in op:
            return f"operation missing {k}"
    if not safe_id(op["op_id"]):
        return f"unsafe op_id {op['op_id']!r}"
    letter = reg_mod.letter_for_action(op["action"])
    if letter is None:
        return f"unknown action {op['action']!r}"
    if op.get("letter") not in (None, letter):
        return f"op {op['op_id']}: letter {op.get('letter')} does not match action {op['action']} ({letter})"
    return reg_mod.check_order(registry, op["bot"], op["action"])
