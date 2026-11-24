"""Per-bot health meters + a dashboard that makes the problem bot obvious.

Each bot has health/<bot>.json: ops completed, errors, last error, heartbeat, recent finish times.
The dashboard compares each bot with its peers of the same letter and flags outliers.
"""
from __future__ import annotations

import statistics
import threading
from pathlib import Path

from .util import atomic_write_json, now_iso, parse_iso, read_json

_lock = threading.Lock()
RECENT = 20


def _path(paths, bot) -> Path:
    return paths["health"] / f"{bot}.json"


def _blank(bot):
    return {"schema": "pcmcp.health", "schema_version": 1, "bot": bot, "letter": bot[:1],
            "ops_completed": 0, "errors": 0, "refusals": 0, "last_error": None,
            "first_op_at": None, "last_heartbeat": None, "recent_finishes": []}


def heartbeat(paths, bot, note="working") -> None:
    with _lock:
        h = read_json(_path(paths, bot)) or _blank(bot)
        h["last_heartbeat"] = now_iso()
        h["last_activity"] = note
        atomic_write_json(_path(paths, bot), h)


def record(paths, receipt: dict) -> None:
    bot = receipt.get("bot") or "?"
    with _lock:
        h = read_json(_path(paths, bot)) or _blank(bot)
        t = now_iso()
        h["last_heartbeat"] = t
        h["first_op_at"] = h["first_op_at"] or receipt.get("started_at") or t
        st = receipt.get("status")
        if st in ("done", "already_done"):
            h["ops_completed"] += 1
            h["recent_finishes"] = (h["recent_finishes"] + [t])[-RECENT:]
        elif st == "refused":
            h["refusals"] += 1
            h["last_error"] = {"at": t, "op_id": receipt.get("op_id"), "error": receipt.get("error")}
        elif st == "error":
            h["errors"] += 1
            h["last_error"] = {"at": t, "op_id": receipt.get("op_id"), "error": receipt.get("error")}
        atomic_write_json(_path(paths, bot), h)


def _rates(h) -> tuple:
    """(overall ops/min, recent ops/min over the last RECENT finishes)."""
    fin = h.get("recent_finishes") or []
    overall = recent = 0.0
    if h.get("first_op_at") and fin:
        mins = max((parse_iso(fin[-1]) - parse_iso(h["first_op_at"])).total_seconds() / 60, 1 / 60)
        overall = h["ops_completed"] / mins
    if len(fin) >= 2:
        mins = max((parse_iso(fin[-1]) - parse_iso(fin[0])).total_seconds() / 60, 1 / 60)
        recent = (len(fin) - 1) / mins
    return overall, recent


def status(paths, registry: dict, cfg: dict) -> dict:
    stale_s = cfg["health"]["stale_heartbeat_seconds"]
    slow_ratio = cfg["health"]["slow_ratio"]
    now = parse_iso(now_iso())
    bots = []
    for letter, row in sorted(registry["letters"].items()):
        for b in row["bots"]:
            h = read_json(_path(paths, b)) or _blank(b)
            overall, recent = _rates(h)
            trend = "steady"
            if overall and recent > overall * 1.25:
                trend = "up"
            elif overall and recent < overall * 0.75:
                trend = "down"
            age = (now - parse_iso(h["last_heartbeat"])).total_seconds() if h["last_heartbeat"] else None
            bots.append({"bot": b, "letter": letter, "ops_completed": h["ops_completed"], "errors": h["errors"],
                         "refusals": h["refusals"], "last_error": h["last_error"],
                         "ops_per_min": round(overall, 1), "recent_ops_per_min": round(recent, 1), "trend": trend,
                         "last_heartbeat": h["last_heartbeat"], "heartbeat_age_s": None if age is None else int(age),
                         "busy": h.get("last_activity") not in (None, "idle"), "flags": []})
    # Compare each bot against peers of the same letter.
    for letter in {b["letter"] for b in bots}:
        peers = [b for b in bots if b["letter"] == letter]
        rates = [b["ops_per_min"] for b in peers if b["ops_completed"] >= 3]
        counts = [b["ops_completed"] for b in peers]
        med_rate = statistics.median(rates) if len(rates) >= 2 else None
        med_count = statistics.median(counts) if counts else 0
        for b in peers:
            if b["errors"]:
                b["flags"].append("ERRORS")
            if b["refusals"]:
                b["flags"].append("REFUSALS")
            if med_rate and b["ops_completed"] >= 3 and b["ops_per_min"] < med_rate * slow_ratio:
                b["flags"].append("SLOW")
            if med_count >= 4 and b["ops_completed"] < med_count * slow_ratio:
                b["flags"].append("BEHIND")
            if b["heartbeat_age_s"] is not None and b["heartbeat_age_s"] > stale_s and b.get("busy"):
                b["flags"].append("STALE")
    return {"schema": "pcmcp.status", "schema_version": 1, "generated_at": now_iso(), "bots": bots,
            "problem_bots": [b["bot"] for b in bots if b["flags"]]}


def render(st: dict) -> str:
    lines = [f"{'BOT':<6}{'OPS':>6}{'ERR':>5}{'REF':>5}{'OPM':>8}{'RECENT':>8}  {'TREND':<7}{'HB AGE':>8}  FLAGS"]
    for b in st["bots"]:
        age = "-" if b["heartbeat_age_s"] is None else f"{b['heartbeat_age_s']}s"
        flags = " ".join(b["flags"])
        mark = "  <== CHECK THIS BOT" if b["flags"] else ""
        lines.append(f"{b['bot']:<6}{b['ops_completed']:>6}{b['errors']:>5}{b['refusals']:>5}{b['ops_per_min']:>8}"
                     f"{b['recent_ops_per_min']:>8}  {b['trend']:<7}{age:>8}  {flags}{mark}")
        if b["last_error"]:
            lines.append(f"      last error: {b['last_error']['error']}")
    lines.append("")
    lines.append("problem bots: " + (", ".join(st["problem_bots"]) or "none"))
    return "\n".join(lines)
