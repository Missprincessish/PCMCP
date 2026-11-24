"""PCMCP swarm command line.  Run from the repo root:  python3 -m pcmcp_swarm <command>

Owner-only actions (approve, execute --for-real, undo, pending approve) exist ONLY here, on the local
machine. No doorbell or check-in channel can reach them.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets as py_secrets
import sys
from pathlib import Path

from . import config as config_mod, health, plans as plans_mod, registry as reg_mod, runner, scanner, signing
from .util import atomic_write_json, now_iso, read_json


def _ctx(args):
    cfg = config_mod.load(args.config)
    return cfg, config_mod.data_paths(cfg)


def cmd_init(args):
    cpath = config_mod.config_path(args.config)
    user_cfg = read_json(cpath, {}) or {}
    if args.data_dir:
        user_cfg["data_dir"] = str(Path(args.data_dir).expanduser().resolve())
    roots = set(user_cfg.get("allowed_roots", []))
    roots.update(str(Path(r).expanduser().resolve()) for r in args.allow_root or [])
    user_cfg["allowed_roots"] = sorted(roots)
    if args.inbox:
        user_cfg.setdefault("checkin", {}).setdefault("local_inbox", {})["inbox_dir"] = str(Path(args.inbox).expanduser())
    if args.outbox:
        user_cfg.setdefault("checkin", {}).setdefault("local_inbox", {})["outbox_dir"] = str(Path(args.outbox).expanduser())
    atomic_write_json(cpath, user_cfg)
    cfg, paths = _ctx(args)
    for k in ("plans", "journal", "health", "quarantine", "screenshots"):
        paths[k].mkdir(parents=True, exist_ok=True)
    reg = reg_mod.build(cfg["fleet"])
    old = read_json(paths["registry"])
    if old and not args.force:
        print(f"registry exists at {paths['registry']} (use --force to rebuild from config.fleet)")
        reg = reg_mod.load(paths["registry"])
    else:
        reg_mod.save(paths["registry"], reg)
    paths["registry_key"].write_text(reg_mod.render_key(reg), encoding="utf-8")
    _keygen(cfg, force=False)
    print(f"config:   {cpath}\ndata dir: {paths['root']}\nregistry: {paths['registry']}\nkey:      {paths['registry_key']}")
    print(f"allowed roots: {cfg['allowed_roots'] or 'NONE (bots will refuse everything until you add one)'}")
    if cfg["doorbell"]["trigger"].startswith("CHANGE-ME"):
        print("NOTE: set doorbell.trigger in the config file; until then no ring can ever be valid.")


def _keygen(cfg, force):
    spec = cfg["checkin"]["secret"]
    if spec.get("type") != "file":
        return
    p = Path(spec["path"]).expanduser()
    if p.exists() and not force:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(py_secrets.token_hex(32) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    print(f"wrote new check-in secret to {p} (share it with the orchestrator out of band)")


def cmd_keygen(args):
    cfg, _ = _ctx(args)
    _keygen(cfg, force=args.force)


def cmd_status(args):
    cfg, paths = _ctx(args)
    st = health.status(paths, reg_mod.load(paths["registry"]), cfg)
    print(json.dumps(st, indent=2) if args.json else health.render(st))


def cmd_scan(args):
    cfg, paths = _ctx(args)
    reg = reg_mod.load(paths["registry"])
    root = os.path.realpath(args.root)
    from .util import is_within
    if not any(is_within(root, r) for r in cfg["allowed_roots"]):
        sys.exit(f"refused: {root} is not inside allowed_roots {cfg['allowed_roots']}")
    plan, classification = scanner.build_plan(root, cfg, reg, {"kind": "local_scan", "by": "owner-cli"})
    plans_mod.save(paths, plan)
    (paths["plans"] / f"{plan['plan_hash']}.txt").write_text(plans_mod.render(plan), encoding="utf-8")
    atomic_write_json(paths["plans"] / f"{plan['plan_hash']}.classification.json", classification)
    res = runner.execute(paths, cfg, reg, plan, dry_run=True, log=lambda *_: None)
    print(plans_mod.render(plan) if not args.quiet else f"plan {plan['plan_hash']}")
    print(f"\nDRY RUN (nothing changed): {res['counts']}")
    print(f"plan hash: {plan['plan_hash']}")
    print(f"approve with: python3 -m pcmcp_swarm approve {plan['plan_hash'][:12]}")


def cmd_plan(args):
    cfg, paths = _ctx(args)
    if args.action == "list":
        for p in sorted(paths["plans"].glob("*.json")):
            if p.name.endswith(".classification.json"):
                continue
            pl = read_json(p)
            print(f"{pl['plan_hash'][:12]}  approved={plans_mod.is_approved(paths, pl['plan_hash'])}  "
                  f"{pl['body']['summary'].get('total_operations')} ops  {pl['body']['root']}")
    else:
        print(plans_mod.render(plans_mod.load(paths, args.ref)))


def cmd_approve(args):
    """SAFETY: the owner approval gate. Applies only to the exact plan hash shown."""
    cfg, paths = _ctx(args)
    reg = reg_mod.load(paths["registry"])
    plan = plans_mod.load(paths, args.ref)
    problems = plans_mod.validate(plan, reg, cfg["allowed_roots"])
    if problems:
        sys.exit("refusing to approve an invalid plan: " + "; ".join(problems))
    h = plan["plan_hash"]
    print(f"plan {h}\nsummary: {json.dumps(plan['body']['summary'])}")
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("approval needs an interactive terminal (or --yes when you are scripting it yourself)")
        typed = input(f"Type the first 8 characters of the plan hash to approve ({h[:8]}): ").strip()
        if typed != h[:8]:
            sys.exit("not approved")
    rec = plans_mod.record_approval(paths, h)
    print(f"approved {h} at {rec['approved_at']}")
    if args.execute:
        _run_real(cfg, paths, reg, plan)


def _run_real(cfg, paths, reg, plan):
    res = runner.execute(paths, cfg, reg, plan, dry_run=False)
    print(f"result: {res['counts']}  halted: {res['halted']}")
    rep = runner.report(paths, plan)
    atomic_write_json(paths["root"] / "reports" / f"{plan['plan_hash']}.json", rep)
    print(f"report: {paths['root'] / 'reports' / (plan['plan_hash'] + '.json')}")


def cmd_execute(args):
    cfg, paths = _ctx(args)
    reg = reg_mod.load(paths["registry"])
    plan = plans_mod.load(paths, args.ref)
    if not args.for_real:
        res = runner.execute(paths, cfg, reg, plan, dry_run=True)
        print(f"DRY RUN (nothing changed): {res['counts']}. Add --for-real to execute an approved plan.")
        return
    try:
        _run_real(cfg, paths, reg, plan)
    except runner.NotApproved as e:
        sys.exit(str(e))


def cmd_undo(args):
    cfg, paths = _ctx(args)
    reg = reg_mod.load(paths["registry"])
    plan = plans_mod.load(paths, args.ref)
    res = runner.undo(paths, cfg, reg, plan, op_id=args.op)
    print(f"undo receipts: {[(r.get('op_id'), r.get('status')) for r in res['receipts']]}")


def cmd_report(args):
    cfg, paths = _ctx(args)
    plan = plans_mod.load(paths, args.ref)
    rep = runner.report(paths, plan)
    out = paths["root"] / "reports" / f"{plan['plan_hash']}.json"
    atomic_write_json(out, rep)
    if args.json:
        print(json.dumps(rep, indent=2))
        return
    print(f"REPORT {rep['plan_hash']}  approved={rep['approved']}")
    for bot, b in rep["per_bot"].items():
        print(f"  {bot:<5} executed={b['executed']:<4} undone={b['undone']:<3} errors={len(b['errors'])}  {b['first']} -> {b['last']}")
    print(f"  undo events: {len(rep['undo_events'])}\nfull report (all hashes): {out}")


def cmd_ring(args):
    cfg, _ = _ctx(args)
    from .doorbell.local import spool_ring
    print(f"rang local spool: {spool_ring(cfg, args.text)}")


def cmd_listen(args):
    cfg, paths = _ctx(args)
    from .daemon import listen
    listen(paths, cfg, once=args.once)


def cmd_checkin(args):
    cfg, paths = _ctx(args)
    from .checkin.handler import check_in
    print(check_in(paths, cfg, wake_reason="manual"))


def cmd_sign(args):
    """Reference signer (for testing, and as the spec for the orchestrator's own signer)."""
    cfg, _ = _ctx(args)
    from .secret_store import get_secret
    batch = read_json(args.file)
    batch.setdefault("schema", signing.BATCH_SCHEMA)
    batch.setdefault("schema_version", 1)
    env = signing.sign(batch, get_secret(cfg["checkin"]["secret"]))
    out = Path(args.out or (cfg["checkin"]["local_inbox"]["inbox_dir"] + f"/{batch['batch_id']}.json")).expanduser()
    atomic_write_json(out, env)
    print(f"signed batch written to {out}")


def cmd_pending(args):
    cfg, paths = _ctx(args)
    from .checkin import get_channel
    from .checkin.handler import pending, take_screenshot
    items = pending(paths)
    if args.action == "list":
        for p in items:
            print(f"{p['request_id']}  {p['kind']}  {p['target']}  {p['at']}")
        if not items:
            print("no pending requests")
        return
    match = [p for p in items if p["request_id"] == args.id]
    if not match:
        sys.exit(f"no pending request {args.id}")
    receipt, att = take_screenshot(paths, cfg, reg_mod.load(paths["registry"]), match[0]["target"], args.id)
    receipt.update(schema="pcmcp.batch_receipt", schema_version=1, batch_id=args.id, kind="screenshot", at=now_iso())
    print(f"{receipt['status']}: {receipt.get('screenshot')}  -> {get_channel(cfg).post_receipts(receipt, att)}")


def cmd_registry(args):
    cfg, paths = _ctx(args)
    reg = reg_mod.load(paths["registry"])
    print(reg_mod.render_key(reg))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m pcmcp_swarm", description="PCMCP deterministic bots")
    ap.add_argument("--config", help="config file (default $PCMCP_CONFIG or ~/.pcmcp/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create config, data dir, registry, key and check-in secret")
    s.add_argument("--allow-root", action="append", help="folder the bots may touch (repeatable)")
    s.add_argument("--data-dir"); s.add_argument("--inbox"); s.add_argument("--outbox")
    s.add_argument("--force", action="store_true", help="rebuild registry from config.fleet")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("keygen"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_keygen)
    s = sub.add_parser("status", help="health dashboard"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("scan", help="dry-run scan a folder and propose a plan (changes nothing)")
    s.add_argument("root"); s.add_argument("--quiet", action="store_true"); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("plan"); s.add_argument("action", choices=["show", "list"]); s.add_argument("ref", nargs="?", default="latest")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("approve", help="OWNER: approve one exact plan hash")
    s.add_argument("ref"); s.add_argument("--yes", action="store_true"); s.add_argument("--execute", action="store_true")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("execute", help="dry-run a plan; --for-real runs an approved plan")
    s.add_argument("ref"); s.add_argument("--for-real", action="store_true"); s.set_defaults(fn=cmd_execute)
    s = sub.add_parser("undo", help="OWNER: reverse a plan (or one op) from its receipts")
    s.add_argument("ref"); s.add_argument("--op"); s.set_defaults(fn=cmd_undo)
    s = sub.add_parser("report"); s.add_argument("ref"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("ring", help="ring the local doorbell"); s.add_argument("text"); s.set_defaults(fn=cmd_ring)
    s = sub.add_parser("listen", help="run the doorbell listener"); s.add_argument("--once", action="store_true"); s.set_defaults(fn=cmd_listen)
    s = sub.add_parser("checkin", help="check in once now, without a doorbell"); s.set_defaults(fn=cmd_checkin)
    s = sub.add_parser("sign", help="sign a batch JSON with the shared secret (reference signer)")
    s.add_argument("file"); s.add_argument("--out"); s.set_defaults(fn=cmd_sign)
    s = sub.add_parser("pending", help="OWNER: list/approve pending screenshot requests")
    s.add_argument("action", choices=["list", "approve"]); s.add_argument("id", nargs="?"); s.set_defaults(fn=cmd_pending)
    s = sub.add_parser("registry", help="print the registry key"); s.set_defaults(fn=cmd_registry)
    s = sub.add_parser("mcp", help="run the MCP server (stdio) for agent clients: propose-only until the owner enables config gates")
    s.set_defaults(fn=lambda args: (_ for _ in ()).throw(SystemExit(_mcp_main())))
    from .drive import cli as drive_cli
    drive_cli.add_parser(sub)
    args = ap.parse_args(argv)
    if argv is not None:
        args._argv = ["python3", "-m", "pcmcp_swarm", *argv]
    from .drive.api import DriveError, ForbiddenOperation
    try:
        args.fn(args)
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError, DriveError, ForbiddenOperation) as e:
        sys.exit(f"error: {e}")


def _mcp_main():
    from .mcp_server import serve
    return serve()


if __name__ == "__main__":
    main()
