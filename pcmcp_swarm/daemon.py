"""The doorbell listener loop.

poll every doorbell channel -> log every ring with a verdict -> if at least one ring is valid and
the rate limiter allows, perform ONE check-in -> go back to sleep.

Verdicts: valid | junk_trigger | unexpected_sender | rate_limited | channel_error.
The ring text is never logged in clear (it might be the trigger); only its length and SHA-256.
"""
from __future__ import annotations

import time

from .checkin.handler import check_in
from .doorbell import RateLimiter, get_channels, matches_trigger
from .util import append_jsonl, now_iso, sha256_bytes


def evaluate(ring: dict, cfg: dict, limiter: RateLimiter, sender_ok) -> str:
    if not matches_trigger(ring.get("text", ""), cfg["doorbell"]["trigger"]):
        return "junk_trigger"
    if not sender_ok(ring):
        return "unexpected_sender"
    if not limiter.allow():
        return "rate_limited"
    return "valid"


def poll_once(paths, cfg, channels, limiter, log=print) -> dict:
    valid = 0
    for ch in channels:
        try:
            rings = ch.poll()
        except Exception as e:  # noqa: BLE001
            append_jsonl(paths["doorbell"], {"at": now_iso(), "channel": ch.name, "verdict": "channel_error",
                                             "error": f"{type(e).__name__}: {e}"})
            log(f"doorbell channel {ch.name} error: {e}")
            continue
        sender_ok = (lambda r, ch=ch: ch.sender_allowed(r.get("sender", ""))) if hasattr(ch, "sender_allowed") \
            else (lambda r: True)
        for r in rings:
            verdict = evaluate(r, cfg, limiter, sender_ok)
            text = (r.get("text") or "").encode("utf-8")
            append_jsonl(paths["doorbell"], {"at": now_iso(), "channel": r.get("channel"), "sender": r.get("sender"),
                                             "ref": r.get("ref"), "verdict": verdict,
                                             "text_len": len(text), "text_sha256": sha256_bytes(text)})
            log(f"ring via {r.get('channel')} from {r.get('sender')}: {verdict}")
            if verdict == "valid":
                valid += 1
    result = {"valid_rings": valid, "checkin": None}
    if valid:
        # Many valid rings in one poll coalesce into one check-in.
        result["checkin"] = check_in(paths, cfg, wake_reason=f"{valid} valid ring(s)")
        log(f"check-in: {result['checkin']}; back to sleep")
    return result


def listen(paths, cfg, once=False, log=print) -> None:
    channels = get_channels(cfg)
    rl = cfg["doorbell"]["rate_limit"]
    limiter = RateLimiter(int(rl["max_wakes"]), int(rl["window_seconds"]))
    log(f"PCMCP listening on {[c.name for c in channels]} (poll {cfg['doorbell']['poll_seconds']}s). Ctrl-C to stop.")
    try:
        while True:
            poll_once(paths, cfg, channels, limiter, log)
            if once:
                return
            time.sleep(int(cfg["doorbell"]["poll_seconds"]))
    finally:
        for c in channels:
            c.close()
