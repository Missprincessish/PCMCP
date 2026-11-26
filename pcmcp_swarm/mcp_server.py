"""Minimal MCP server for the PCMCP swarm. Standard library only, stdio transport.

Protocol: Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0).
Run:  python3 -m pcmcp_swarm mcp        (or add PCMCP_CONFIG=... env)

Safety model (unchanged from the CLI):
  - swarm_scan / swarm_execute(dry) are PROPOSALS. A dry run changes nothing.
  - swarm_execute with for_real=true refuses unless config mcp.allow_real_execution is true,
    and the runner still refuses to run any plan that is not owner-approved for the exact hash.
  - swarm_approve refuses unless config mcp.allow_approval is true. Both flags default to false,
    so an MCP client out of the box can look, propose and dry-run, never destroy or approve.
  - swarm_screenshot refuses unless mcp.allow_screenshots is true, and returns the png inline
    so a vision-capable client can see the screen. Capture failures are reported, never faked.
  - swarm_meter is the fleet health meter: verdict per bot (OK/WARN/STALLED/BROKEN) with evidence
    and repair advice, straight from the run journal and health records. Read-only.
  - swarm_undo only restores from receipts/journal; it cannot delete anything.
  - Every tool call is logged to data/logs/mcp.jsonl. Reply text never contains secrets.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import health, plans as plans_mod
from . import registry as reg_mod
from . import runner, scanner
from .util import atomic_write_json, is_within, now_iso, read_json, read_jsonl

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

TOOLS = [
    {
        "name": "swarm_status",
        "description": "Health dashboard of the bot swarm: per-bot execution counts, errors, problem markers. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "swarm_plan_list",
        "description": "List saved plans (hash prefix, approved flag, operation count, root). Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "swarm_plan_show",
        "description": "Render one plan (operations, per-bot assignment) by hash prefix or 'latest'. Read-only.",
        "inputSchema": {"type": "object", "properties": {"ref": {"type": "string", "description": "plan hash prefix or 'latest'"}},
                        "required": ["ref"], "additionalProperties": False},
    },
    {
        "name": "swarm_scan",
        "description": "Dry-run scan of a folder inside allowed_roots; saves a plan AWAITING OWNER APPROVAL. Changes nothing. Returns the plan hash and counts.",
        "inputSchema": {"type": "object", "properties": {"root": {"type": "string", "description": "folder to scan (must be inside allowed_roots)"}},
                        "required": ["root"], "additionalProperties": False},
    },
    {
        "name": "swarm_execute",
        "description": "Dry-run a plan by default (nothing changes). for_real=true runs it, but ONLY if the owner approved that exact hash AND mcp.allow_real_execution is true.",
        "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}, "for_real": {"type": "boolean"}},
                        "required": ["ref"], "additionalProperties": False},
    },
    {
        "name": "swarm_undo",
        "description": "Reverse a plan (or one op) from its receipts. Restores originals and staged files; never deletes.",
        "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}, "op": {"type": "string", "description": "op_id to undo only that op (optional)"}},
                        "required": ["ref"], "additionalProperties": False},
    },
    {
        "name": "swarm_approve",
        "description": "Owner approval for one exact plan hash. REFUSED unless the owner set mcp.allow_approval=true in the config. There is no way for an MCP client to enable this itself.",
        "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}},
                        "required": ["ref"], "additionalProperties": False},
    },
    {
        "name": "swarm_screenshot",
        "description": "Capture a screenshot (full screen, region, or app window) and return it inline as PNG image content, so a vision-capable client can see the screen. REFUSED unless the owner set mcp.allow_screenshots=true in the config. Capture failures are reported honestly, never faked.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["full", "region", "app"], "description": "what to capture (default full)"},
            "name": {"type": "string", "description": "app name for target=app"},
            "x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"}, "h": {"type": "integer"},
        }, "additionalProperties": False},
    },
    {
        "name": "swarm_meter",
        "description": "Fleet health meter: verdict per bot (OK / WARN / STALLED / BROKEN) with evidence, so the client knows which arm of the swarm is broken and how to repair it. Reads the run journal and health records; changes nothing. BROKEN = failed ops (re-run the same plan to resume only unfinished ops); STALLED = started but never finished (crash); WARN = SLOW / BEHIND / REFUSALS flags vs peers.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class Gate(Exception):
    """A safety refusal the client should see as an error, not a crash."""


def _ctx():
    cfg = config_mod.load()
    return cfg, config_mod.data_paths(cfg)


def _require(cfg, flag: str) -> None:
    if not (cfg.get("mcp") or {}).get(flag):
        raise Gate(f"refused: mcp.{flag} is false. The owner must set it in the config; an MCP client cannot enable it.")


def _load_plan(paths, ref: str) -> dict:
    return plans_mod.load(paths, ref)


def tool_status(args):
    cfg, paths = _ctx()
    st = health.status(paths, reg_mod.load(paths["registry"]), cfg)
    return json.dumps(st, indent=2)


def tool_plan_list(args):
    _, paths = _ctx()
    out = []
    for p in sorted(paths["plans"].glob("*.json")):
        if p.name.endswith(".classification.json"):
            continue
        pl = read_json(p)
        if not pl:
            continue
        out.append({"plan_hash": pl["plan_hash"], "approved": plans_mod.is_approved(paths, pl["plan_hash"]),
                    "operations": pl["body"]["summary"].get("total_operations"), "root": pl["body"]["root"]})
    return json.dumps(out, indent=2)


def tool_plan_show(args):
    _, paths = _ctx()
    return plans_mod.render(_load_plan(paths, args["ref"]))


def tool_scan(args):
    cfg, paths = _ctx()
    reg = reg_mod.load(paths["registry"])
    root = os.path.realpath(args["root"])
    if not any(is_within(root, r) for r in cfg["allowed_roots"]):
        raise Gate(f"refused: {root} is not inside allowed_roots (the bot fleet only touches approved roots)")
    plan, classification = scanner.build_plan(root, cfg, reg, {"kind": "mcp_scan", "by": "mcp-client"})
    plans_mod.save(paths, plan)
    (paths["plans"] / f"{plan['plan_hash']}.txt").write_text(plans_mod.render(plan), encoding="utf-8")
    atomic_write_json(paths["plans"] / f"{plan['plan_hash']}.classification.json", classification)
    res = runner.execute(paths, cfg, reg, plan, dry_run=True, log=lambda *_: None)
    return (f"plan {plan['plan_hash']}\nPROPOSED, awaiting owner approval.\n"
            f"dry-run counts (nothing changed): {res['counts']}\n"
            f"approve gate: swarm_approve requires mcp.allow_approval=true, set by the owner in the config.")


def tool_execute(args):
    cfg, paths = _ctx()
    reg = reg_mod.load(paths["registry"])
    plan = _load_plan(paths, args["ref"])
    if not args.get("for_real"):
        res = runner.execute(paths, cfg, reg, plan, dry_run=True, log=lambda *_: None)
        return f"DRY RUN (nothing changed): {res['counts']}"
    _require(cfg, "allow_real_execution")
    res = runner.execute(paths, cfg, reg, plan, dry_run=False, log=lambda *_: None)
    rep = runner.report(paths, plan)
    atomic_write_json(paths["root"] / "reports" / f"{plan['plan_hash']}.json", rep)
    return f"result: {res['counts']}  halted: {res['halted']}\nreport: {paths['root'] / 'reports' / (plan['plan_hash'] + '.json')}"


def tool_undo(args):
    cfg, paths = _ctx()
    reg = reg_mod.load(paths["registry"])
    plan = _load_plan(paths, args["ref"])
    res = runner.undo(paths, cfg, reg, plan, op_id=args.get("op"), log=lambda *_: None)
    return f"undo receipts: {[(r.get('op_id'), r.get('status')) for r in res['receipts']]}"


def tool_approve(args):
    cfg, paths = _ctx()
    reg = reg_mod.load(paths["registry"])
    plan = _load_plan(paths, args["ref"])
    problems = plans_mod.validate(plan, reg, cfg["allowed_roots"])
    if problems:
        raise Gate("refusing to approve an invalid plan: " + "; ".join(problems))
    _require(cfg, "allow_approval")
    rec = plans_mod.record_approval(paths, plan["plan_hash"])
    return (f"approved {plan['plan_hash']} at {rec['approved_at']} (via MCP, gate mcp.allow_approval).\n"
            f"real execution still requires mcp.allow_real_execution and swarm_execute with for_real=true.")


def tool_screenshot(args):
    cfg, paths = _ctx()
    _require(cfg, "allow_screenshots")
    kind = args.get("target", "full")
    if kind not in ("full", "region", "app"):
        raise Gate("target must be full, region or app")
    if kind == "region" and not all(k in args for k in ("x", "y", "w", "h")):
        raise Gate("region needs x, y, w and h")
    if kind == "app" and not args.get("name"):
        raise Gate("app target needs a name")
    target = {"type": kind}
    if kind == "app":
        from .screenshot.macos import clean_app_name
        try:
            target["name"] = clean_app_name(args["name"])
        except ValueError as e:
            raise Gate(f"refused: {e}")
    if kind == "region":
        try:
            target.update({k: int(args[k]) for k in ("x", "y", "w", "h")})
        except (TypeError, ValueError):
            raise Gate("region x, y, w and h must be whole numbers")
    from . import screenshot
    out = paths["screenshots"] / f"mcp_{time.strftime('%Y%m%d_%H%M%S')}.png"
    screenshot.capture(target, out)          # failures propagate and get reported honestly
    data = base64.b64encode(out.read_bytes()).decode("ascii")
    return {"text": f"screenshot captured ({kind}): {out}", "image": (data, "image/png")}


def tool_meter(args):
    """Fleet health meter: honest verdicts from the journal + health records, never vibes."""
    cfg, paths = _ctx()
    reg = reg_mod.load(paths["registry"])
    st = health.status(paths, reg, cfg)

    # latest journal event per op id (evidence of failed or stalled work)
    latest = {}
    for jpath in paths["journal"].glob("*.jsonl"):
        for rec in read_jsonl(jpath):
            oid = rec.get("op_id")
            if rec.get("event") == "done" or latest.get(oid, ("", ))[0] != "done":
                latest[oid] = (rec.get("event"), rec.get("bot"), rec.get("status"), rec.get("at"))
    journal_evidence = {}
    done_now = {oid for oid, t in latest.items() if t[0] == "done"}
    for ev, bot, status, at in latest.values():
        if ev in ("failed", "started"):
            e = journal_evidence.setdefault(bot, [])
            if ev == "failed":
                e.append(f"failed op (last status {status!r}) at {at}")
            else:
                e.append(f"op started at {at} and never finished (crash or power loss)")

    LEVELS = ("OK", "WARN", "STALLED", "BROKEN")
    bots, arms = [], {}
    for sb in st["bots"]:
        level, evidence = 0, []
        jev = journal_evidence.get(sb["bot"], [])
        for e in jev:
            evidence.append(e)
            level = max(level, 3 if e.startswith("failed") else 2)
        if sb.get("last_error"):
            le = sb.get("last_error") or {}
            resolved = bool(le.get("op_id")) and le.get("op_id") in done_now
            if not resolved:
                level = max(level, 3)
                if not any(e.startswith("failed") for e in evidence):
                    evidence.append(f"last error at {le.get('at')}: {le.get('error')}")
            else:
                evidence.append(f"prior error on op {le.get('op_id')} was re-run and completed (resolved)")
        if "STALE" in sb["flags"]:
            level = max(level, 2)
            evidence.append(f"stale heartbeat ({sb['heartbeat_age_s']}s while busy)")
        warned = [f for f in sb["flags"] if f in ("SLOW", "BEHIND")]
        if "REFUSALS" in sb["flags"] and level == 0 and not jev:
            evidence.append(f"prior refusals ({sb['refusals']}) were re-run and resolved")
        if warned:
            level = max(level, 1)
            evidence.append("flags: " + ", ".join(warned))
        verdict = LEVELS[level]
        bots.append({"bot": sb["bot"], "letter": sb["letter"], "verdict": verdict,
                     "ops_completed": sb["ops_completed"], "evidence": evidence or ["all jobs completed, no errors, heartbeat fresh"]})
        arm = arms.setdefault(sb["letter"], {"total": 0, "problem": 0})
        arm["total"] += 1
        if verdict != "OK":
            arm["problem"] += 1
    for b in bots:
        if b["verdict"] == "BROKEN":
            b["repair"] = "re-run swarm_execute on the same plan: the journal resumes only the unfinished ops. bots are stateless programs, so a BROKEN verdict means the job needs a re-run (or an owner registry rebuild), not a healed process."
        elif b["verdict"] == "STALLED":
            b["repair"] = "re-run swarm_execute on the same plan to resume the unfinished op from the journal."

    fleet = "GREEN"
    if any(b["verdict"] in ("STALLED", "BROKEN") for b in bots):
        fleet = "RED"
    elif any(b["verdict"] == "WARN" for b in bots):
        fleet = "YELLOW"
    broken = [b["bot"] for b in bots if b["verdict"] != "OK"]
    return json.dumps({
        "schema": "pcmcp.meter", "schema_version": 1, "generated_at": now_iso(),
        "fleet": fleet,
        "fleet_advice": ("all arms complete their jobs" if fleet == "GREEN"
                         else f"instruct the owner: {len(broken)} bot(s) need attention: {', '.join(broken)}"),
        "arms": {L: f"{v['problem']}/{v['total']} problem bots" for L, v in sorted(arms.items())},
        "bots": bots,
    }, indent=2)


HANDLERS = {
    "swarm_status": (tool_status, {}),
    "swarm_meter": (tool_meter, {}),
    "swarm_plan_list": (tool_plan_list, {}),
    "swarm_plan_show": (tool_plan_show, {"ref"}),
    "swarm_scan": (tool_scan, {"root"}),
    "swarm_execute": (tool_execute, {"ref"}),
    "swarm_undo": (tool_undo, {"ref"}),
    "swarm_approve": (tool_approve, {"ref"}),
    "swarm_screenshot": (tool_screenshot, {}),
}


def _log_call(paths, name, ok, detail=""):
    try:
        from .util import append_jsonl
        append_jsonl(paths["root"] / "logs" / "mcp.jsonl",
                     {"at": now_iso(), "tool": name, "ok": ok, "detail": str(detail)[:400]})
    except Exception:
        pass


def handle(req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if "id" not in req:                       # notification: no response, ever
        return None
    if method == "initialize":
        want = req.get("params", {}).get("protocolVersion", "")
        version = want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False},
                             "logging": {},
                             "resources": {"listChanged": False}},
            "serverInfo": {"name": "pcmcp", "version": "1.2.3"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    if method == "prompts/get":
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown prompt"}}
    if method == "logging/setLevel":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "resources/list":
        _, paths = _ctx()
        resources = [{"uri": "pcmcp://registry", "name": "Bot registry key",
                      "description": "The signed manifest of bots, their job letters and public keys.",
                      "mimeType": "text/markdown"}]
        plans = sorted(paths["plans"].glob("*.json"))
        if plans:
            resources.append({"uri": "pcmcp://plans/latest", "name": "Latest plan",
                              "description": "The most recently saved plan, rendered.", "mimeType": "text/plain"})
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": resources}}
    if method == "resources/read":
        uri = req.get("params", {}).get("uri", "")
        _, paths = _ctx()
        try:
            if uri == "pcmcp://registry":
                text = paths["registry_key"].read_text(encoding="utf-8")
            elif uri == "pcmcp://plans/latest":
                plans = [p for p in sorted(paths["plans"].glob("*.json")) if not p.name.endswith(".classification.json")]
                if not plans:
                    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "no plans saved yet"}}
                text = plans_mod.render(read_json(plans[-1]) or {"plan_hash": "?"})
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": f"unknown resource: {uri}"}}
        except FileNotFoundError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": f"not available: {e}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}}
    if method == "tools/call":
        params = req.get("params", {})
        name, args = params.get("name"), params.get("arguments") or {}
        fn, required = HANDLERS.get(name, (None, ()))
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        missing = [k for k in required if k not in args]
        if missing:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"missing arguments: {missing}"}}
        try:
            text = fn(args)
            _, paths = _ctx()
            _log_call(paths, name, True)
            if isinstance(text, dict):                    # tool returns inline image content
                data, mime = text["image"]
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": str(text["text"])},
                    {"type": "image", "data": data, "mimeType": mime},
                ]}}
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(text)}]}}
        except Gate as e:
            _, paths = _ctx()
            _log_call(paths, name, False, e)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"REFUSED: {e}"}], "isError": True}}
        except runner.NotApproved as e:
            _, paths = _ctx()
            _log_call(paths, name, False, e)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"NOT APPROVED: {e}"}], "isError": True}}
        except Exception as e:                  # honest failure: report, don't fake
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}
        else:
            resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
