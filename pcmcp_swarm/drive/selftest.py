"""Swarm v2 acceptance tests 1-9. Drives the swarm ONLY through the terminal commands in CONTROL.md.

Offline (default): a local fake Drive with injected rate limits.
    python3 -m pcmcp_swarm.drive.selftest            [--keep]
Live: a copy-first test folder in Angie's real Drive (after `drive auth`). Never touches anything else.
    python3 -m pcmcp_swarm.drive.selftest --live --root <testFolderId>

NOTE: `sample_plan()` below is TEST FIXTURE code. It writes a deterministic sample plan by file
extension so the machinery can be exercised. It is not a classifier and the swarm never uses it;
the real plan is written by Instinct.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = []
ENTRY = "pcmcp_swarm.drive._testentry"   # offline; live mode switches to the production CLI
USED_COMMANDS = set()


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)


def cli(env, cfg, *args, expect=0, background=False):
    USED_COMMANDS.add(" ".join(a for a in args[:2] if not a.startswith("-")) if args[0] == "drive" else args[0])
    cmd = [sys.executable, "-m", ENTRY, "--config", str(cfg), *args, "--issued-by", "Instinct"]
    if background:
        return subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
    if expect is not None and r.returncode != expect:
        print(f"  ! {' '.join(args)} exited {r.returncode} (expected {expect})\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r


def approve_tty(env, cfg, prefix, pf, rn, passphrase="selftest-owner-pass", issuer=None):
    """Drive `drive approve` through a real pseudo-terminal. ssh-keygen gets the owner passphrase from a
    test-only SSH_ASKPASS helper (standing in for Angie typing it)."""
    import pty
    USED_COMMANDS.add("drive approve")
    ask = Path(env["PCMCP_TEST_TMP"]) / "askpass.sh"
    ask.write_text(f"#!/bin/sh\necho '{passphrase}'\n")
    ask.chmod(0o700)
    e = dict(env, SSH_ASKPASS=str(ask), SSH_ASKPASS_REQUIRE="force", DISPLAY="none")
    master, slave = pty.openpty()
    cmd = [sys.executable, "-m", ENTRY, "--config", str(cfg), "drive", "approve", prefix, "--plan", str(pf), *rn]
    if issuer:
        cmd += ["--issued-by", issuer]
    pr = subprocess.Popen(cmd, cwd=REPO, env=e, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    os.close(slave)
    out, _ = pr.communicate(timeout=120)
    os.close(master)
    return pr.returncode, out


def make_owner_key(tmp: Path, cfg_dict: dict):
    """Test owner key WITH a passphrase (Angie makes hers with `drive owner-setup`)."""
    key = tmp / "owner_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "selftest-owner-pass", "-C", "pcmcp-owner", "-f", str(key)], check=True)
    cfg_dict["drive"]["owner_key"] = str(key)
    cfg_dict["drive"]["owner_allowed_signers"] = str(tmp / "allowed_signers")
    from pcmcp_swarm.drive import owner
    owner.write_allowed_signers(cfg_dict["drive"])
    pin = tmp / "owner_key.pin"           # stands in for the root-owned pin; only honored for the loopback fake
    pin.write_text(owner.fingerprint(cfg_dict["drive"]) + "\n")
    return pin


# ------------------------------------------------------------------ TEST FIXTURE: sample plan (not a classifier)

EXT = {"Pics & Videos": {".jpg", ".jpeg", ".png", ".heic", ".mov", ".mp4", ".gif"},
       "College": {".pdf", ".doc", ".docx"}, "Financial": {".xlsx", ".xls", ".csv"}}
THREE_D = {".glb", ".gltf", ".fbx", ".obj", ".blend", ".usdz", ".stl"}


def sample_plan(inv, fmap, move_folder_name=None):
    items = [i for i in inv["items"] if i["in_scope"]]
    files = sorted((i for i in items if not i["is_folder"]), key=lambda i: i["id"])
    review = fmap["review_folder"]
    declared = ["3D assets/Models", f"{review}/Archive"]
    creates = [f["path"] for f in fmap["folders"]] + declared
    placements, archives, copies, tags = {}, {}, {}, {}
    n = 0
    stay = []
    for f in files:
        name = f["name"].lower()
        if (f["path"] or "").startswith("LEGAL PROOF - DO NOT TOUCH/"):
            stay.append(f["id"])          # LEGAL PROOF is a separate second plan
            continue
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if "synomize" in (f["path"] or "").lower() and ext in THREE_D:
            dest = "Projects/Synomize"; tags[f["id"]] = ["synomize-3d"]
        elif "synomize" in name:
            dest = "Projects/Synomize"; tags[f["id"]] = ["synomize"]
        elif re.search(r"(^|[^a-z])ash([^a-z]|$)", name):
            dest = "Projects/SynASH Intelligence"; tags[f["id"]] = ["ash"]
        elif ext in THREE_D:
            dest = "3D assets/Models"
        else:
            dest = next((d for d, exts in EXT.items() if ext in exts), None)
            if dest is None:
                n += 1
                if n % 3 == 0:
                    archives.setdefault(f"{review}/Archive", []).append(f["id"])
                    continue
                dest = review
        placements.setdefault(dest, []).append(f["id"])
    pdfs = [f["id"] for f in files if f["name"].lower().endswith(".pdf")][:6]
    if pdfs:
        copies["SynHumanity Labs"] = pdfs
    if move_folder_name:
        fol = [i for i in items if i["is_folder"] and i["name"] == move_folder_name]
        if fol:
            placements.setdefault(review, []).append(fol[0]["id"])
    return {"schema": "pcmcp.drive_plan", "schema_version": 1, "author": "selftest-fixture (stands in for Instinct)",
            "written_at": "2026-11-10T00:00:00Z", "inventory_hash": inv["inventory_hash"], "scope_root_id": inv["scope_root_id"],
            "new_folders_declared": declared, "create_folders": creates, "placements": placements, "archives": archives,
            "copies": copies, "stay": stay, "tags": tags, "notes": {}}


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--root", help="live: id of the copy-first test folder")
    ap.add_argument("--config", help="live: config file (default ~/.pcmcp/config.json)")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    if a.live and not a.root:
        sys.exit("--live needs --root <test folder id>")
    tmp = Path(tempfile.mkdtemp(prefix="pcmcp_drive_selftest_"))
    t0 = time.time()
    try:
        run(tmp, a)
        if not a.live:
            resilience(tmp / "resilience")
            text_triggers(tmp / "texts")
            folders_only(tmp / "folders")
            inventory_speed(tmp / "inv_speed")
    except Exception:
        import traceback
        traceback.print_exc()
        check("selftest completed without crashing", False)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed in {time.time() - t0:.1f}s")
    print("OVERALL: " + ("PASS" if passed == len(RESULTS) else "FAIL"))
    if not a.keep and not a.live:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"kept: {tmp}")
    sys.exit(0 if passed == len(RESULTS) else 1)


def run(tmp: Path, a):
    sys.path.insert(0, str(REPO))
    from pcmcp_swarm.drive import planfile, runs
    from pcmcp_swarm.util import read_json, read_jsonl
    env = dict(os.environ, PYTHONPATH=str(REPO), PCMCP_TEST_TMP=str(tmp))
    fake = None
    if a.live:
        global ENTRY
        ENTRY = "pcmcp_swarm"
        cfg = Path(a.config or Path.home() / ".pcmcp" / "config.json").expanduser()
        scope = a.root
    else:
        from pcmcp_swarm.drive.fake import FakeDrive, seed_demo
        fake = FakeDrive(rate_limit_every=37, latency_ms=30)
        scope = seed_demo(fake, 600)
        url = fake.start()
        cfg = tmp / "config.json"
        cd = {"data_dir": str(tmp / "data"), "drive": {
            "api_base": url, "auth": "none", "pools": {"A": 10, "B": 30, "E": 10}, "edits_per_minute": 30000,
            "backoff_base_seconds": 0.05, "backoff_max_seconds": 0.5, "status_interval_seconds": 0.25}}
        env["PCMCP_TEST_PIN_FILE"] = os.environ["PCMCP_TEST_PIN_FILE"] = str(make_owner_key(tmp, cd))
        cfg.write_text(json.dumps(cd))
        outside_before = {i: v for i, v in fake.listing().items() if i not in fake.descendants(scope)}
        test_before = fake.listing(scope)
        h0 = fake.state_hash()
        w0 = fake.c["writes_applied"]
    sys.path.insert(0, str(REPO))
    from pcmcp_swarm import config as config_mod
    from pcmcp_swarm.drive import drive_cfg
    if fake:
        from pcmcp_swarm.drive import _testentry
        _testentry.install()
    d = drive_cfg(config_mod.load(str(cfg)))
    fmap = read_json(Path(d["folder_map"]))

    # ---- 1. inventory
    r = cli(env, cfg, "drive", "inventory", "--root", scope)
    check("1a inventory command succeeded", r.returncode == 0, r.stdout.strip().splitlines()[0] if r.stdout else r.stderr[-300:])
    run_dir = runs.find_run(d["runs_dir"], "latest")
    inv = read_json(run_dir / "inventory.json")
    if fake:
        want = fake.descendants(scope)
        got = {i["id"] for i in inv["items"]}
        check("1b inventory covers 100% of the test folder", got == want and all(i["in_scope"] for i in inv["items"]), f"{len(got)}/{len(want)} items")
        check("1c inventory changed nothing (0 writes, state hash unchanged)", fake.c["writes_applied"] == w0 and fake.state_hash() == h0)
        r2 = cli(env, cfg, "drive", "inventory", "--root", "root")
        whole = read_json(runs.find_run(d["runs_dir"], "latest") / "inventory.json")
        check("1d whole-Drive inventory covers 100% of the Drive", len(whole["items"]) == len(fake.files) - 1, f"{len(whole['items'])}/{len(fake.files) - 1}")
        check("1e no file contents read: inventory flags contents_opened=false, 0 download attempts", whole["contents_opened"] is False and fake.c["forbidden_attempts"] == 0)
        # continue with the test-folder run (re-export so it is the latest run)
        r = cli(env, cfg, "drive", "inventory", "--root", scope)
        run_dir = runs.find_run(d["runs_dir"], "latest")
        inv = read_json(run_dir / "inventory.json")
    else:
        check("1b inventory listed the test folder", inv["counts"]["files_in_scope"] > 0, str(inv["counts"]))
    rn = ["--run", run_dir.name]

    # ---- 2. validator
    plan = sample_plan(inv, fmap, move_folder_name="misc" if fake else None)
    pf = tmp / "plan.json"
    pf.write_text(json.dumps(plan, indent=1))
    r = cli(env, cfg, "drive", "plan", "validate", "--plan", str(pf), *rn)
    check("2a complete sample plan validates", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else "")
    files = sorted(i["id"] for i in inv["items"] if i["in_scope"] and not i["is_folder"])
    R = fmap["review_folder"]
    victim = next(fid for fid in plan["placements"].get(R, []) if fid in files)

    def bad(name, mutate, code):
        p = copy.deepcopy(plan)
        mutate(p)
        f = tmp / f"bad-{name}.json"
        f.write_text(json.dumps(p))
        rr = cli(env, cfg, "drive", "plan", "validate", "--plan", str(f), *rn, expect=2)
        check(f"2{name} validator refuses: {code}", rr.returncode == 2 and code in rr.stdout, (rr.stdout.strip().splitlines() or [""])[-1])

    bad("b", lambda p: p["placements"][R].remove(victim), "MISSING_DESTINATION")

    def vv(p):
        p["placements"][R].remove(victim)
        p["placements"].setdefault("Projects/VectorVerse", []).append(victim)
        p["tags"][victim] = ["synomize"]
    bad("c", vv, "NEVER_MOVE:synomize-synatoms-never-into-vectorverse")

    def pac(p):
        p["placements"][R].remove(victim)
        p["placements"].setdefault("PAC", []).append(victim)
        p["tags"][victim] = ["ash"]
    bad("d", pac, "NEVER_MOVE:ash-never-into-pac")
    if fake:
        glb = next(i["id"] for i in inv["items"] if i["name"] == "tree_asset.glb")

        # 3D asset attached to Synomize: a direct move out is SKIPPED (stays put) and listed, not refused
        p3 = copy.deepcopy(plan)
        p3["placements"]["Projects/Synomize"].remove(glb)
        p3["placements"].setdefault("3D assets/Models", []).append(glb)
        p3["tags"].pop(glb, None)
        f3 = tmp / "skip-3d.json"
        f3.write_text(json.dumps(p3))
        rr = cli(env, cfg, "drive", "plan", "validate", "--plan", str(f3), *rn)
        c3 = planfile.compile_ops(p3, inv, fmap)
        skip_file = run_dir / f"skipped-kept-in-synomize-{planfile.plan_hash(p3)[:12]}.txt"
        check("2e Synomize 3D asset: the move out is skipped and listed for Angie (plan still valid, no op for it)",
              rr.returncode == 0 and "SKIPPED_KEPT_IN_SYNOMIZE" in rr.stdout and not any(o.get("file_id") == glb for k in c3["ops"] for o in c3["ops"][k])
              and skip_file.exists() and "tree_asset.glb" in skip_file.read_text())
        synfolder = next(i["id"] for i in inv["items"] if i["is_folder"] and i["name"] == "Synomize old export")

        def ride(p):   # the glb stays, but its folder is moved out: cannot be skipped, so refused
            p["placements"]["Projects/Synomize"].remove(glb)
            p["stay"].append(glb)
            p["placements"][R].append(synfolder)
        bad("e2", ride, "NEVER_MOVE:synomize-3d-assets-stay-in-synomize")
        legal_zip = next(i["id"] for i in inv["items"] if i["name"] == "Halo_2026-07-22.zip")

        def unlock(p):
            p["stay"].remove(legal_zip)
            p["placements"][R].append(legal_zip)
        bad("k", unlock, "NEVER_MOVE:legal-proof-locked")
        named = next(i["id"] for i in inv["items"] if i["name"] == "synomize_world_v3.json")

        def by_name(p):  # no tag: the name guard alone must catch it
            p["placements"]["Projects/Synomize"].remove(named)
            p["placements"].setdefault("Projects/VectorVerse", []).append(named)
            p["tags"].pop(named, None)
        bad("f", by_name, "NEVER_MOVE:synomize-synatoms-never-into-vectorverse")
    if fake:
        misc = next(i["id"] for i in inv["items"] if i["is_folder"] and i["name"] == "misc")
        named2 = next(i["id"] for i in inv["items"] if i["name"] == "synomize_world_v3.json")

        def smuggle(p):  # move folder misc into VectorVerse, then drop a Synomize file "into misc"
            p["placements"][R].remove(misc)
            p["placements"].setdefault("Projects/VectorVerse", []).append(misc)
            p["placements"]["Projects/Synomize"].remove(named2)
            p["placements"].setdefault("misc", []).append(named2)
            p["new_folders_declared"].append("misc")
        bad("j", smuggle, "FOLDER_BEING_MOVED")
    bad("g", lambda p: p.__setitem__("deletes", [victim]), "DELETE_FORBIDDEN")
    bad("h", lambda p: p["placements"][R].append({"action": "delete", "id": victim}), "DELETE_FORBIDDEN")
    bad("i", lambda p: (p["create_folders"].append("Projects/Undeclared Thing"),
                        p["placements"][R].remove(victim),
                        p["placements"].setdefault("Projects/Undeclared Thing", []).append(victim)), "UNDECLARED_FOLDER")

    # ---- 3. dry run + gates
    w1 = fake.c["writes_applied"] if fake else None
    r = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, expect=None)
    check("3a run refused before dry run / approval", r.returncode != 0 and "refused" in (r.stdout + r.stderr))
    r = cli(env, cfg, "drive", "dryrun", "--plan", str(pf), *rn)
    dr = read_json(run_dir / "dryrun.json")
    comp = planfile.compile_ops(plan, inv, fmap)
    check("3b dry run passes and reports exactly the compiled work", r.returncode == 0 and dr["would"]["create_folders"] == comp["totals"]["A"]
          and dr["would"]["moves"] + dr["would"]["copies"] == comp["totals"]["B"] and dr["would"]["archive_moves"] == comp["totals"]["E"],
          json.dumps(dr.get("would")))
    if fake:
        check("3c dry run changed nothing (0 writes, state hash unchanged)", fake.c["writes_applied"] == w1 and fake.state_hash() == h0)
    r = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, expect=None)
    check("3d run refused without owner approval", r.returncode != 0 and "no valid owner signature" in (r.stdout + r.stderr))

    # ---- 4/5. approve, run with live control (status read + pause/resume mid-run)
    ph = planfile.plan_hash(plan)
    if fake:
        from pcmcp_swarm.drive import owner
        r = cli(env, cfg, "drive", "approve", ph[:8], "--plan", str(pf), *rn, expect=None)          # no TTY
        r_yes = cli(env, cfg, "drive", "approve", ph[:8], "--plan", str(pf), "--yes", *rn, expect=None)
        rc_bad, _ = approve_tty(env, cfg, ph[:8], pf, rn, passphrase="wrong-guess")                # TTY but no passphrase
        check("4a0 approval refused without a TTY, with --yes, or with the wrong passphrase",
              r.returncode != 0 and r_yes.returncode != 0 and rc_bad != 0 and owner.verify_approval(d, run_dir, ph) is None)
        # forgery attempts: a hand-written approval line, and a signature from a key that is not the owner's
        from pcmcp_swarm.util import append_jsonl, canonical_json
        append_jsonl(run_dir / "approvals.jsonl", {"plan_hash": ph, "via": "typed-hash-on-tty", "issued_by": "angie", "approved_by": "owner-local"})
        evil = tmp / "evil_key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(evil)], check=True)
        (run_dir / "approvals").mkdir(exist_ok=True)
        fpay = run_dir / "approvals" / f"{ph[:16]}-cmd-forged.json"
        fpay.write_bytes(canonical_json({"plan_hash": ph, "run": run_dir.name, "namespace": owner.NAMESPACE, "approved_at": "x", "command_id": "cmd-forged", "key_fingerprint": "x"}))
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(evil), "-n", owner.NAMESPACE, str(fpay)], check=True, capture_output=True)
        r = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, expect=None)
        check("4a1 forged approvals refused: a JSON approval line and a non-owner signature do not unlock `run`",
              r.returncode != 0 and "no valid owner signature" in (r.stdout + r.stderr))
        # key-swap attempt: point allowed_signers at the attacker key and sign with it -> pin must refuse it
        real_allowed = Path(d["owner_allowed_signers"]).read_text()
        epub = (tmp / "evil_key.pub").read_text().split()
        Path(d["owner_allowed_signers"]).write_text(f'{owner.IDENTITY} namespaces="{owner.NAMESPACE}" {epub[0]} {epub[1]}\n')
        swapped = owner.verify_approval(d, run_dir, ph)
        r = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, expect=None)
        Path(d["owner_allowed_signers"]).write_text(real_allowed)
        check("4a2 swapping in another key (allowed_signers + its signature) is refused by the root-owned pin",
              swapped is None and r.returncode != 0)
        real_dcfg = dict(d, auth="oauth", api_base="https://www.googleapis.com")
        from pcmcp_swarm.drive import _testentry
        try:
            _testentry._test_pin_location(real_dcfg)
            refused = False
        except PermissionError:
            refused = True
        import importlib
        prod = importlib.reload(importlib.import_module("pcmcp_swarm.drive.owner"))
        check("4a3 production pin is the fixed root-owned path regardless of env; the test override refuses real Drive",
              prod.pin_location(real_dcfg) == (prod.PIN_PATH, True) and prod.pin_location(d) == (prod.PIN_PATH, True) and refused)
        _testentry.install()
        import inspect
        check("4a4 signing runs without ssh-agent (SSH_AUTH_SOCK removed)",
              "env=no_agent_env()" in inspect.getsource(owner.sign_approval) and "SSH_AUTH_SOCK" not in owner.no_agent_env())
        rc, out = approve_tty(env, cfg, ph[:8], pf, rn)
        from pcmcp_swarm.drive import owner
        v_ok = owner.verify_approval(d, run_dir, ph)
        good = sorted((run_dir / "approvals").glob(f"{ph[:16]}-cmd-*.json"))
        good = [g for g in good if "forged" not in g.name][0]
        orig = good.read_bytes()
        good.write_bytes(orig.replace(b'"approved_at":"', b'"approved_at":"X'))
        tampered = owner.verify_approval(d, run_dir, ph)
        good.write_bytes(orig)
        check("4a5 a signed approval payload edited after signing no longer verifies", tampered is None and v_ok is not None)
        check("4a owner approval verifies: signature good AND ssh-keygen's fingerprint equals the pin",
              rc == 0 and v_ok is not None and v_ok["verified_fingerprint"] == owner.fingerprint(d) == v_ok["pinned_fingerprint"], out[-300:] if rc else "")
    else:  # LIVE: Angie approves herself with her real owner key; forgery tests are covered offline
        from pcmcp_swarm.drive import owner
        print(f"\nANGIE: approve the sample plan for the live test (plan {ph}). Type your owner-key passphrase when asked.")
        subprocess.run([sys.executable, "-m", ENTRY, "--config", str(cfg), "drive", "approve", ph[:8], "--plan", str(pf), *rn], cwd=REPO, env=env)
        v_ok = owner.verify_approval(d, run_dir, ph)
        check("4a owner approval verifies against the root-owned pin", v_ok is not None)
    paused_ok = mid_status = False
    if fake:
        cli(env, cfg, "drive", "pause", *rn)            # paused before it starts: deterministic hold
    proc = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, background=True)
    if fake:
        end = time.time() + 60
        while time.time() < end and (read_json(run_dir / "status.json") or {}).get("state") != "paused":
            time.sleep(0.05)
        rs = cli(env, cfg, "drive", "status", "--json", *rn)
        try:
            mid_status = "bots_active" in json.loads(rs.stdout)
        except json.JSONDecodeError:
            pass
        w_p = fake.c["writes_applied"]
        time.sleep(1.5)
        s_ = read_json(run_dir / "status.json") or {}
        paused_ok = s_.get("state") == "paused" and s_.get("done") == 0 and fake.c["writes_applied"] == w_p and proc.poll() is None
        cli(env, cfg, "drive", "resume", *rn)
    out, _ = proc.communicate(timeout=900)
    summ = read_json(run_dir / "summary.json") or {}
    check("4b run finished (exit 0, state done)", proc.returncode == 0 and summ.get("state") == "done", out[-400:] if proc.returncode else "")
    if fake:
        check("4c live status readable mid-run (drive status --json)", mid_status)
        check("4d pause held the swarm still (0 writes while paused), resume finished it", paused_ok)
    rec = read_jsonl(run_dir / "receipts.jsonl")
    actions = {x["action"] for x in rec if x.get("ok")}
    check("4e every job type exercised (create, move, copy, archive)", {"create_folder", "move", "copy", "archive_move"} <= actions, str(sorted(actions)))
    used = summ.get("sub_bots_used", {})
    check("4f every lead used >= 2 sub-bots", all(len(used.get(k, [])) >= 2 for k in ("A", "B", "E")), json.dumps({k: len(v) for k, v in used.items()}))
    check("4g total sub-bots across the swarm >= 50", summ.get("sub_bots_used_total", 0) >= 50, str(summ.get("sub_bots_used_total")))
    check("5a zero real errors", summ.get("errors") == 0 and summ.get("refused") == 0, f"errors={summ.get('errors')} refused={summ.get('refused')}")
    if fake:
        check("5b rate limits happened and were logged as retries, not errors",
              fake.c["rate_limited"] > 0 and summ["retries"]["rate_limit"] == fake.c["rate_limited"] and summ["errors"] == 0,
              f"injected={fake.c['rate_limited']} logged retries={summ['retries']}")
        check("5c no batch over 100 calls; 0 delete/trash/download attempts", fake.c["max_batch_calls"] <= 100 and fake.c["forbidden_attempts"] == 0,
              f"max batch {fake.c['max_batch_calls']}")
        outside_now = {i: v for i, v in fake.listing().items() if i in outside_before}
        check("5d nothing outside the test folder was touched", outside_now == outside_before)
        bvsa = summ["before_vs_after"]
        check("5e after listing saved; before vs after shows the moves and no bytes changed",
              (run_dir / "after_listing.json").exists() and bvsa["moved"] > 0 and not bvsa["bytes_changed"], f"moved={bvsa['moved']}")

    # ---- 7. receipts + provenance (before rollback)
    planned = sum(comp["totals"].values())
    okrec = [x for x in rec if x.get("ok")]
    check("7a a receipt exists for 100% of planned operations", len({x["op_id"] for x in okrec}) == planned, f"{len(okrec)}/{planned}")
    need = {"file_id", "file_name", "old_folder", "new_folder", "bot", "at"}
    check("7b every move receipt has file id, name, old folder, new folder, bot, timestamp",
          all(need <= set(x) for x in okrec if x["action"] in ("move", "archive_move", "copy")))
    ok_chain, n_chain, _ = runs.verify_chain(run_dir / "receipts.jsonl")
    check("7c receipt hash chain verifies", ok_chain, f"{n_chain} receipts")
    tam = tmp / "tampered.jsonl"
    lines = (run_dir / "receipts.jsonl").read_text().splitlines()
    lines[len(lines) // 2] = lines[len(lines) // 2].replace('"ok": true', '"ok": false', 1)
    tam.write_text("\n".join(lines) + "\n")
    check("7d tampering with one receipt breaks the chain", not runs.verify_chain(tam)[0])
    cmds = {c["command_id"]: c for c in read_jsonl(run_dir / "commands.jsonl")}
    check("7e every receipt traces to a recorded `run` command issued by Instinct",
          all(cmds.get(x["command_id"], {}).get("mode") == "run" and cmds[x["command_id"]]["issued_by"] == "Instinct" for x in rec))
    one = okrec[len(okrec) // 2]
    rr = cli(env, cfg, "drive", "receipts", "--file", one["file_id"], *rn)
    check("7f `drive receipts --file` shows the receipt with the exact command that caused it",
          rr.returncode == 0 and one["command_id"] in rr.stdout and '"caused_by_command"' in rr.stdout)

    # ---- 6. rollback
    r = cli(env, cfg, "drive", "rollback", "--yes", *rn, expect=None)
    rb = read_json(run_dir / "rollback_summary.json") or {}
    check("6a rollback finished with 0 errors", r.returncode == 0 and rb.get("errors") == 0, r.stdout[-400:] if r.returncode else "")
    check("6b test folder is byte-identical to the pre-run state (listing hash + md5)", rb.get("byte_identical") is True,
          f"{rb.get('comparison', {}).get('before_hash', '')[:16]} vs {rb.get('comparison', {}).get('after_hash', '')[:16]}")
    if fake:
        check("6c independent check: fake Drive test-folder listing equals the original", fake.listing(scope) == test_before)
        hold = rb.get("holding_folder_id")
        parked = [f for f in fake.files.values() if hold in (f.get("parents") or [])]
        check("6d copies and created folders parked (not deleted) in a holding folder outside the target",
              hold and hold not in fake.descendants(scope) and len(parked) == comp["totals"]["A"] + len(plan["copies"].get("SynHumanity Labs", [])),
              f"{len(parked)} parked")
        check("6e rollback receipts chain verifies and trace to the rollback command", runs.verify_chain(run_dir / "rollback_receipts.jsonl")[0])

    # ---- 8. CONTROL.md covers everything used
    control = (REPO / "CONTROL.md").read_text()
    missing = sorted(c for c in USED_COMMANDS if c.split()[-1] not in control)
    check("8 CONTROL.md exists and documents every command this test used", not missing, f"used {sorted(USED_COMMANDS)}; missing {missing}")

    # ---- 9. speed
    secs, mpm = summ.get("wall_seconds"), summ.get("average_operations_per_minute")
    v1 = 1016 / 100 * 60
    check("9 wall-clock time and operations/minute reported", secs is not None and mpm is not None,
          f"{summ.get('operations_ok')} ops in {secs}s = {mpm} ops/min, {summ.get('average_moves_per_minute')} moves/min "
          f"(v1 single-bot local run: 1016 ops / 100 s = {v1:.0f} ops/min)")
    print(f"\nevidence folder: {run_dir}")
    if fake:
        print(f"fake Drive: {fake.c}")
        fake.stop()


def resilience(tmp: Path):
    """Extra: failed chunks are reassigned to another sub-bot; `drive stop` then the same `drive run` resumes
    with every operation done exactly once; rollback still byte-identical."""
    from pcmcp_swarm.drive import planfile, runs
    from pcmcp_swarm.drive.fake import FakeDrive, seed_demo
    from pcmcp_swarm import config as config_mod
    from pcmcp_swarm.drive import drive_cfg
    from pcmcp_swarm.util import read_json, read_jsonl
    tmp.mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(REPO), PCMCP_TEST_TMP=str(tmp))
    fake = FakeDrive(latency_ms=250, fail_first_batches=8)
    scope = seed_demo(fake, 400)
    url = fake.start()
    before = fake.listing(scope)
    cfg = tmp / "config.json"
    cd = {"data_dir": str(tmp / "data"), "drive": {
        "api_base": url, "auth": "none", "pools": {"A": 4, "B": 4, "E": 4}, "edits_per_minute": 30000, "max_retries": 1,
        "batch_size": 5, "backoff_base_seconds": 0.02, "backoff_max_seconds": 0.05, "status_interval_seconds": 0.2}}
    env["PCMCP_TEST_PIN_FILE"] = os.environ["PCMCP_TEST_PIN_FILE"] = str(make_owner_key(tmp, cd))
    cfg.write_text(json.dumps(cd))
    d = drive_cfg(config_mod.load(str(cfg)))
    cli(env, cfg, "drive", "inventory", "--root", scope)
    run_dir = runs.find_run(d["runs_dir"], "latest")
    inv = read_json(run_dir / "inventory.json")
    plan = sample_plan(inv, read_json(Path(d["folder_map"])))
    pf = tmp / "plan.json"
    pf.write_text(json.dumps(plan))
    rn = ["--run", run_dir.name]
    cli(env, cfg, "drive", "dryrun", "--plan", str(pf), *rn)
    approve_tty(env, cfg, planfile.plan_hash(plan)[:8], pf, rn)
    proc = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn, background=True)
    end = time.time() + 60
    while time.time() < end and ((read_json(run_dir / "status.json") or {}).get("leads", {}).get("B", {}).get("items_done", 0) < 20):
        time.sleep(0.05)
    cli(env, cfg, "drive", "stop", *rn)
    out, _ = proc.communicate(timeout=300)
    st = read_json(run_dir / "status.json") or {}
    check("X1 `drive stop` stopped the run part-way (exit 3, state stopped)",
          proc.returncode == 3 and st.get("state") == "stopped" and st.get("done", 0) < st.get("planned", 0),
          f"done {st.get('done')}/{st.get('planned')}")
    r = cli(env, cfg, "drive", "run", "--plan", str(pf), *rn)
    summ = read_json(run_dir / "summary.json") or {}
    rec = [x for x in read_jsonl(run_dir / "receipts.jsonl") if x.get("ok")]
    planned = sum(planfile.compile_ops(plan, inv, read_json(Path(d["folder_map"])))["totals"].values())
    ids = [x["op_id"] for x in rec]
    check("X2 re-running the same `drive run` resumed and finished; every op done exactly once",
          r.returncode == 0 and len(ids) == len(set(ids)) == planned, f"{len(set(ids))}/{planned}, dupes {len(ids) - len(set(ids))}")
    sessions = (read_json(run_dir / "chunks.json") or {}).get("sessions", {})
    first = sorted((s_ for s_ in sessions.values() if s_["mode"] == "run"), key=lambda s_: s_["started_at"])[0]["chunks"]
    moved_on = [c for c in first.values() if len(set(c["bots"])) > 1 and c["status"] == "done"]
    check("X3 failed chunks were reassigned to a different sub-bot and then succeeded (history kept across sessions)",
          fake.c.get("failed_batches", 0) == 8 and moved_on and len(sessions) == 2,
          f"injected 503 batches={fake.c.get('failed_batches')}, reassigned chunks={len(moved_on)}")
    cmds = {c["mode"] for c in read_jsonl(run_dir / "commands.jsonl")}
    two_runs = len({x["command_id"] for x in rec}) == 2
    check("X4 receipts show both run commands (before and after the stop)", two_runs and "stop" in cmds)
    r = cli(env, cfg, "drive", "rollback", "--yes", *rn)
    check("X5 rollback after a stopped+resumed run is byte-identical", r.returncode == 0 and fake.listing(scope) == before)
    fake.stop()


def text_triggers(tmp: Path):
    """Text trigger watcher against a stand-in Messages database (same tables/columns as chat.db)."""
    import sqlite3
    from pcmcp_swarm.drive import planfile, runs
    from pcmcp_swarm.drive.fake import FakeDrive, seed_demo
    from pcmcp_swarm.drive.trigger import APPLE_EPOCH
    from pcmcp_swarm import config as config_mod
    from pcmcp_swarm.drive import drive_cfg
    from pcmcp_swarm.util import read_json, read_jsonl
    tmp.mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(REPO), PCMCP_TEST_TMP=str(tmp))
    fake = FakeDrive(latency_ms=10)
    scope = seed_demo(fake, 120)
    url = fake.start()
    db = tmp / "chat.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
                      "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, is_from_me INTEGER, date INTEGER, handle_id INTEGER);"
                      "INSERT INTO handle VALUES (1, '+15550001111'), (2, '+15559998888');")
    con.commit()

    def text(body, sender=1, from_me=0, age=0, blob=None):
        d = int((time.time() - age - APPLE_EPOCH) * 1e9)
        cur = con.execute("INSERT INTO message (text, attributedBody, is_from_me, date, handle_id) VALUES (?,?,?,?,?)",
                          (body, blob, from_me, d, sender))
        con.commit()
        return cur.lastrowid
    cfg = tmp / "config.json"
    cd = {"data_dir": str(tmp / "data"), "drive": {
        "api_base": url, "auth": "none", "pools": {"A": 3, "B": 6, "E": 3}, "edits_per_minute": 30000,
        "backoff_base_seconds": 0.02, "backoff_max_seconds": 0.05, "status_interval_seconds": 0.2,
        "trigger": {"chat_db": str(db), "code_hash_file": str(tmp / "code.sha256"), "state_file": str(tmp / "watch_state.json"),
                    "allowed_senders": ["+15550001111"], "poll_seconds": 0.2, "max_age_seconds": 600,
                    "rate_limit": {"max": 50, "window_seconds": 600}, "reply": "file", "reply_file": str(tmp / "replies.jsonl"),
                    "icloud_root": str(tmp / "icloud"), "requests_dir": str(tmp / "requests")}}}
    env["PCMCP_TEST_FAKE_SCREENSHOT"] = "1"
    icl = tmp / "icloud"
    (icl / "Projects" / "Synomize").mkdir(parents=True)
    (icl / "Projects" / "Synomize" / "world.json").write_text("{}")
    (icl / "Photos").mkdir()
    (icl / "Photos" / "beach.jpg").write_bytes(b"jpg" * 1000)
    (icl / "Photos" / ".Big Movie.mov.icloud").write_bytes(b"stub")          # old-style cloud-only placeholder
    (icl / "locked.txt").write_text("private")
    os.chmod(icl / "locked.txt", 0)                                        # unreadable: listing must not need to open it
    os.symlink("/etc", icl / "link_out")

    def tree_state():
        out = {}
        for root_, dirs, fs in os.walk(icl):
            for n in dirs + fs:
                q = os.path.join(root_, n)
                st = os.lstat(q)
                out[os.path.relpath(q, icl)] = (st.st_size, st.st_mtime_ns, st.st_mode)
        return out
    env["PCMCP_TEST_PIN_FILE"] = os.environ["PCMCP_TEST_PIN_FILE"] = str(make_owner_key(tmp, cd))
    cfg.write_text(json.dumps(cd))
    d = drive_cfg(config_mod.load(str(cfg)))
    r = cli(env, cfg, "drive", "trigger-setup")
    code = re.search(r"(PCMCP-[A-Za-z0-9]{24,64})", r.stdout).group(1)
    stored = (tmp / "code.sha256").read_text()
    check("T0 trigger-setup stores only the code's hash (mode 0600), never the code",
          code[6:] not in stored and oct((tmp / "code.sha256").stat().st_mode & 0o777) == "0o600")
    old = text(f"{code} status")                                  # sent BEFORE the watcher ever started
    cli(env, cfg, "drive", "inventory", "--root", scope)
    run_dir = runs.find_run(d["runs_dir"], "latest")
    inv = read_json(run_dir / "inventory.json")
    plan = sample_plan(inv, read_json(Path(d["folder_map"])))
    pf = tmp / "plan.json"
    pf.write_text(json.dumps(plan))
    cli(env, cfg, "drive", "plan", "validate", "--plan", str(pf), "--run", run_dir.name)
    ph = planfile.plan_hash(plan)
    log = tmp / "data" / "logs" / "text_triggers.jsonl"
    watcher = cli(env, cfg, "drive", "trigger-watch", background=True)

    def wait_for(rowid, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            hit = [x for x in read_jsonl(log) if x.get("rowid") == rowid]
            if hit:
                return hit[-1]
            time.sleep(0.1)
        return None
    time.sleep(1.0)                                               # watcher records its start mark
    r1 = text(f"hey {code} status thanks")
    wrong_code = text("PCMCP-" + "x" * 32 + " status")
    wrong_sender = text(f"{code} status", sender=2)
    approve_try = text(f"{code} approve {ph[:8]}")
    s1, s_es = wait_for(r1), wait_for(wrong_sender)
    time.sleep(2.0)
    check("T1 coded `status` text from Instinct's number runs status", s1 and s1["acted"] and s1["command"] == "status")
    check("T2 old texts are never replayed when the watcher starts", not [x for x in read_jsonl(log) if x.get("rowid") == old])
    check("T3 wrong code is ignored", not [x for x in read_jsonl(log) if x.get("rowid") == wrong_code])
    check("T4 right code from an unknown sender is refused", s_es and not s_es["acted"] and "allowed_senders" in s_es["refused"])
    check("T5 a text cannot approve: `approve` is not a command, and no approval exists afterwards",
          not [x for x in read_jsonl(log) if x.get("rowid") == approve_try] and not list((run_dir / "approvals").glob("*.sig")) if (run_dir / "approvals").exists() else True)
    dr = text(f"{code} dryrun {ph[:8]}")                         # not a text command any more
    cli(env, cfg, "drive", "dryrun", "--plan", str(pf), "--run", run_dir.name)
    stale = text(f"{code} status", age=3600)
    mine = text(f"{code} status", from_me=1)
    blob = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + f"{code} status".encode() + b"\x86\x84\x02iI"
    hidden = text(None, blob=blob)
    w_st, w_mine, w_hid = wait_for(stale), wait_for(mine), wait_for(hidden)
    check("T7 only status and run exist: a coded `dryrun` text is ignored", not [x for x in read_jsonl(log) if x.get("rowid") == dr])
    check("T8 stale text (1 h old) refused", w_st and not w_st["acted"] and "old" in w_st["refused"])
    check("T9 text sent from Angie's own account refused (incoming only)", w_mine and not w_mine["acted"])
    check("T10 code inside the attributedBody blob (text column empty) is found", w_hid and w_hid["acted"])
    early_run = text(f"{code} run {ph[:8]}")                     # dry run passed, but Angie has not signed
    s_er = wait_for(early_run)
    check("T6 a coded `run` text before Angie's signed approval is refused",
          s_er and not s_er["acted"] and "no valid owner signature" in s_er["refused"] and not (run_dir / "receipts.jsonl").exists())
    rc, out = approve_tty(env, cfg, ph[:8], pf, ["--run", run_dir.name])
    go = text(f"{code} run {ph[:8]}")
    w_go = wait_for(go)
    end = time.time() + 120
    while time.time() < end and (read_json(run_dir / "summary.json") or {}).get("state") != "done":
        time.sleep(0.2)
    summ = read_json(run_dir / "summary.json") or {}
    cmds = {c["command_id"]: c for c in read_jsonl(run_dir / "commands.jsonl")}
    rec = [x for x in read_jsonl(run_dir / "receipts.jsonl") if x.get("ok")]
    check("T11 after Angie signs, a coded `run` text starts the approved run and it finishes",
          rc == 0 and w_go and w_go["acted"] and summ.get("state") == "done" and summ.get("errors") == 0, out[-200:] if rc else "")
    check("T12 every receipt traces to the `run` command issued 'Instinct via text #<n>'",
          rec and all(cmds[x["command_id"]]["issued_by"] == f"Instinct via text #{go}" for x in rec))
    again = [x for x in read_jsonl(log) if x.get("rowid") == go and "event" not in x]
    check("T13 each text is acted on once", len(again) == 1)
    end = time.time() + 30
    while time.time() < end and not any(x.get("event") == "run_finished" and x.get("rowid") == go for x in read_jsonl(log)):
        time.sleep(0.1)
    replies = read_jsonl(tmp / "replies.jsonl")
    to_instinct = [x for x in replies if x["to"] == "+15550001111"]
    texts_ = [x["text"] for x in to_instinct]
    check("T14 status result texted back to Instinct", any(t_.startswith("PCMCP status:") for t_ in texts_))
    check("T15 run start and run result texted back to Instinct (finished summary with counts)",
          any("run started" in t_ for t_ in texts_) and any(f"run {run_dir.name} DONE" in t_ and "Receipts chain OK" in t_ for t_ in texts_))
    check("T16 refused unsigned run explained to Instinct", any("refused run" in t_ and "owner signature" in t_ for t_ in texts_))
    check("T17 nothing texted to the unknown sender, and no reply ever contains the code",
          not [x for x in replies if x["to"] != "+15550001111"] and not any(code[6:] in x["text"] for x in replies))
    again1 = wait_for(text(f"{code} run {ph[:8]}"))
    check("T18 the same approval cannot start a second texted run (one-shot)",
          again1 and not again1["acted"] and "already used" in again1["refused"])
    cli(env, cfg, "drive", "rollback", "--yes", "--run", run_dir.name, expect=None)
    after_rb = wait_for(text(f"{code} run {ph[:8]}"))
    check("T19 after a rollback, a texted run is refused", after_rb and not after_rb["acted"] and "rolled back" in after_rb["refused"])
    cli(env, cfg, "drive", "stop", "--run", run_dir.name)
    after_stop = wait_for(text(f"{code} run {ph[:8]}"))
    check("T20 after Angie's `drive stop`, a texted run is refused (a text never clears a stop)",
          after_stop and not after_stop["acted"] and "stopped" in after_stop["refused"])
    r = cli(env, cfg, "drive", "run", "--plan", str(pf), "--run", run_dir.name, "--no-clear-stop", expect=None)
    check("T21 `drive run --no-clear-stop` refuses to resume an owner stop", r.returncode != 0 and "stopped by the owner" in (r.stdout + r.stderr))

    # ---- computer-use commands: screenshot, task, inventory icloud (look-only), inventory drive
    def final_for(rowid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            hit = [x for x in read_jsonl(log) if x.get("rowid") == rowid and str(x.get("event", "")).endswith("_finished")]
            if hit:
                return hit[-1]
            time.sleep(0.1)
        return None

    def reply_for(substr):
        return [x for x in read_jsonl(tmp / "replies.jsonl") if substr in x["text"]]
    shot = wait_for(text(f"{code} screenshot"))
    rs = reply_for("screenshot attached")
    check("T22 `screenshot` text: screen captured and the .png is attached to the reply",
          shot and shot["acted"] and rs and rs[-1]["attachments"] and rs[-1]["attachments"][0].endswith("screenshot.png")
          and Path(rs[-1]["attachments"][0]).read_bytes()[:4] == b"\x89PNG")
    tk = wait_for(text(f"{code} task uptime; rm -rf ~"))
    rt = reply_for("PCMCP task uptime")
    out_t = Path(rt[-1]["attachments"][0]).read_text() if rt and rt[-1]["attachments"] else ""
    check("T23 `task uptime` runs only the fixed read-only command (extra words ignored) and attaches the output",
          tk and tk["acted"] and tk["exit"] == 0 and out_t.startswith("$ /usr/bin/uptime") and "rm" not in out_t.splitlines()[0])
    bad_t = wait_for(text(f"{code} task delete_everything"))
    check("T24 a task not on the allowed list is refused", bad_t and not bad_t["acted"] and "unknown task" in bad_t["refused"])
    from pcmcp_swarm.drive import tasks as tasks_mod
    check("T25 the task guard rejects writing programs and write sub-commands",
          tasks_mod.check_task({"name": "x", "argv": ["/bin/rm", "-rf", "/"]}) and
          tasks_mod.check_task({"name": "x", "argv": ["/usr/bin/pmset", "sleepnow"]}) and
          tasks_mod.check_task({"name": "x", "argv": ["/bin/ls", "a;rm"]}) and not tasks_mod.load()["problems"])
    before_tree = tree_state()
    ic = wait_for(text(f"{code} inventory icloud"))
    icf = final_for(ic["rowid"]) if ic else None
    ri = reply_for("iCloud inventory (look-only)")
    listing = Path(ri[-1]["attachments"][0]).read_text() if ri and ri[-1]["attachments"] else ""
    check("T26 `inventory icloud` lists names, sizes, downloaded vs cloud-only, and attaches the listing",
          icf and "Photos/Big Movie.mov\tfile" in listing and "cloud-only" in listing and "beach.jpg\tfile\t3000" in listing
          and "1 cloud-only" in ri[-1]["text"])
    check("T27 iCloud is look-only: nothing changed, unreadable file still listed (never opened), symlink not followed",
          tree_state() == before_tree and "locked.txt\tfile" in listing and "link_out\tlink" in listing and "passwd" not in listing)
    os.chmod(icl / "locked.txt", 0o600)
    dv = wait_for(text(f"{code} inventory drive"))
    dvf = final_for(dv["rowid"]) if dv else None
    rd = reply_for("Drive inventory (read-only)")
    lines = Path(rd[-1]["attachments"][0]).read_text().splitlines() if rd and rd[-1]["attachments"] else []
    newest = runs.find_run(d["runs_dir"], "latest")
    inv2 = read_json(newest / "inventory.json") or {"items": []}
    check("T28 `inventory drive` runs the read-only inventory and attaches the full listing",
          dvf and len(lines) == len(inv2["items"]) + 1 and lines[0].startswith("id\tpath"))
    check("T29 the Drive inventory's command record says it was issued by Instinct via text",
          any(c["issued_by"] == f"Instinct via text #{dv['rowid']}" and c["mode"] == "inventory" for c in read_jsonl(newest / "commands.jsonl")))
    st_arg = wait_for(text(f"{code} inventory photos"))
    check("T30 commands with wrong arguments are refused (`inventory photos`)", st_arg and not st_arg["acted"] and "drive` or `icloud" in st_arg["refused"])
    reqs = [q for q in (tmp / "requests").iterdir() if q.is_dir()]
    complete = all((q / "request.json").exists() and (q / "commands.jsonl").exists() and (q / "reply.json").exists() for q in reqs)
    acted_rows = {x["rowid"] for x in read_jsonl(log) if x.get("acted")}
    check("T31 every acted text has its own logged request folder (request, command record, reply)",
          complete and acted_rows <= {read_json(q / "request.json")["rowid"] for q in reqs})
    from pcmcp_swarm.drive import trigger as trig
    empty = dict(trig.tcfg(config_mod.load(str(cfg))), allowed_senders=[])
    try:
        trig.Watcher(config_mod.load(str(cfg)), d, empty, tmp / "x.jsonl")
        closed = False
    except ValueError:
        closed = True
    check("T33 fail closed: the watcher refuses to start with no allowed sender, and replies never go to other numbers",
          closed and trig.send_reply(dict(empty, allowed_senders=["+15550001111"], reply="file"), "+19990000000", "x")["sent"] is False)
    w_b = trig.Watcher(config_mod.load(str(cfg)), d, dict(trig.tcfg(config_mod.load(str(cfg))), brain_name="SomeOtherAI"), tmp / "y.jsonl")
    check("T34 the brain is configurable (PCMCP works with any AI): evidence label follows drive.trigger.brain_name",
          w_b.brain == "SomeOtherAI" and trig.Watcher(config_mod.load(str(cfg)), d, trig.tcfg(config_mod.load(str(cfg))), tmp / "z.jsonl").brain == "Instinct")
    check("T32 no reply ever contains the code; nothing sent to unknown senders",
          not any(code[6:] in x["text"] for x in read_jsonl(tmp / "replies.jsonl"))
          and all(x["to"] == "+15550001111" for x in read_jsonl(tmp / "replies.jsonl")))
    watcher.terminate()
    watcher.wait(timeout=10)
    fake.stop()


def folders_only(tmp: Path):
    """B: a passphrase-gated folders-only plan creates PCMCP_TEST and moves nothing."""
    from pcmcp_swarm.drive import planfile, runs
    from pcmcp_swarm.drive.fake import FakeDrive, seed_demo
    from pcmcp_swarm import config as config_mod
    from pcmcp_swarm.drive import drive_cfg
    from pcmcp_swarm.util import read_json, read_jsonl
    tmp.mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(REPO), PCMCP_TEST_TMP=str(tmp))
    fake = FakeDrive(latency_ms=5)
    seed_demo(fake, 40)
    url = fake.start()
    cfg = tmp / "config.json"
    cd = {"data_dir": str(tmp / "data"), "drive": {"api_base": url, "auth": "none", "pools": {"A": 2, "B": 2, "E": 2},
          "edits_per_minute": 30000, "backoff_base_seconds": 0.02, "backoff_max_seconds": 0.05, "status_interval_seconds": 0.2}}
    env["PCMCP_TEST_PIN_FILE"] = os.environ["PCMCP_TEST_PIN_FILE"] = str(make_owner_key(tmp, cd))
    cfg.write_text(json.dumps(cd))
    d = drive_cfg(config_mod.load(str(cfg)))
    before = fake.listing()
    cli(env, cfg, "drive", "inventory", "--root", "root")
    run_dir = runs.find_run(d["runs_dir"], "latest")
    rn = ["--run", run_dir.name]
    r = cli(env, cfg, "drive", "plan", "folders", "--name", "PCMCP_TEST", *rn)
    pfile = Path(re.search(r"folders-only plan written: (.+)", r.stdout).group(1).strip())
    plan = json.loads(pfile.read_text())
    ph = planfile.plan_hash(plan)
    check("F1 `drive plan folders --name PCMCP_TEST` writes a valid folders-only plan (declares the new folder, moves nothing)",
          r.returncode == 0 and plan["mode"] == "folders_only" and plan["create_folders"] == ["PCMCP_TEST"]
          and plan["new_folders_declared"] == ["PCMCP_TEST"] and not plan["placements"])
    bad = dict(plan, placements={"Review - Not Sure": [next(iter(fake.files))]})
    bf = tmp / "bad-folders.json"
    bf.write_text(json.dumps(bad))
    rb = cli(env, cfg, "drive", "plan", "validate", "--plan", str(bf), *rn, expect=2)
    check("F2 a folders_only plan that tries to move something is refused", rb.returncode == 2 and "may only create folders" in rb.stdout)
    r_unsigned = cli(env, cfg, "drive", "run", "--plan", str(pfile), *rn, expect=None)
    cli(env, cfg, "drive", "dryrun", "--plan", str(pfile), *rn)
    r_unsigned2 = cli(env, cfg, "drive", "run", "--plan", str(pfile), *rn, expect=None)
    check("F3 not created without a dry run and Angie's passphrase signature", r_unsigned.returncode != 0 and r_unsigned2.returncode != 0
          and "no valid owner signature" in r_unsigned2.stdout + r_unsigned2.stderr
          and not [f for f in fake.files.values() if f["name"] == "PCMCP_TEST"])
    rc, _ = approve_tty(env, cfg, ph[:8], pfile, rn)
    r = cli(env, cfg, "drive", "run", "--plan", str(pfile), *rn)
    made = [f for f in fake.files.values() if f["name"] == "PCMCP_TEST"]
    rec = [x for x in read_jsonl(run_dir / "receipts.jsonl") if x.get("ok")]
    check("F4 after signing, the run creates exactly one PCMCP_TEST at the Drive root, with a receipt; nothing moved",
          rc == 0 and r.returncode == 0 and len(made) == 1 and made[0]["parents"] == [fake.root]
          and len(rec) == 1 and rec[0]["action"] == "create_folder"
          and all(fake.listing().get(i) == v for i, v in before.items()))
    rr = cli(env, cfg, "drive", "rollback", "--yes", *rn)
    rb_s = read_json(run_dir / "rollback_summary.json") or {}
    check("F5 rollback parks PCMCP_TEST in the holding folder (nothing deleted) and the Drive matches the before-state",
          rr.returncode == 0 and rb_s.get("byte_identical") and made[0]["id"] in fake.files)
    fake.stop()


def inventory_speed(tmp: Path):
    """Inventory lead I: many sub-bots crawl folders in parallel; same complete result as one-at-a-time; never silently partial."""
    from pcmcp_swarm.drive import runs, inventory as inv_mod, planfile
    from pcmcp_swarm.drive.api import DriveClient
    from pcmcp_swarm.drive.fake import FakeDrive
    from pcmcp_swarm import config as config_mod
    from pcmcp_swarm.drive import drive_cfg
    from pcmcp_swarm.util import read_json
    tmp.mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(REPO), PCMCP_TEST_TMP=str(tmp))
    fake = FakeDrive(latency_ms=40)
    # a wide, deep tree: 8 top folders x 8 sub x 4 subsub = 328 folders, 3 files each
    for a in range(8):
        fa = fake.add_folder(f"top{a}")
        for b in range(8):
            fb = fake.add_folder(f"sub{a}-{b}", fa)
            fake.add_file(f"f{a}-{b}.txt", fb, f"{a}{b}".encode())
            for c in range(4):
                fc = fake.add_folder(f"leaf{a}-{b}-{c}", fb)
                for k in range(3):
                    fake.add_file(f"x{a}{b}{c}{k}.pdf", fc, f"{a}{b}{c}{k}".encode())
    url = fake.start()
    cfg = tmp / "config.json"
    cd = {"data_dir": str(tmp / "data"), "drive": {"api_base": url, "auth": "none", "status_interval_seconds": 0.2,
          "reads_per_minute": 100000, "backoff_base_seconds": 0.02, "backoff_max_seconds": 0.05, "max_retries": 2}}
    cfg.write_text(json.dumps(cd))
    d = drive_cfg(config_mod.load(str(cfg)))
    n_items = len(fake.files) - 1
    t0 = time.time()
    seq = inv_mod.export(DriveClient(d, read_only=True), "root", pool=1)
    t_seq = time.time() - t0
    fake.max_concurrent = 0
    proc = cli(env, cfg, "drive", "inventory", "--root", "root", background=True)
    run_dir = None
    mid = None
    end = time.time() + 120
    while time.time() < end and proc.poll() is None:
        try:
            run_dir = runs.find_run(d["runs_dir"], "latest")
            st = read_json(run_dir / "status.json")
            if st and st.get("state") == "running" and st.get("done", 0) > 0 and mid is None:
                mid = st
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    out, _ = proc.communicate(timeout=120)
    run_dir = runs.find_run(d["runs_dir"], "latest")
    inv = read_json(run_dir / "inventory.json") or {}
    cr = inv.get("crawl", {})
    t_par = cr.get("elapsed_seconds", 999)
    check("I1 inventory lead I lists 100% of the Drive with many sub-bots working at once",
          proc.returncode == 0 and inv.get("complete") and inv["counts"]["items"] == n_items
          and len(cr.get("sub_bots_used", [])) >= 20 and fake.max_concurrent >= 10,
          f"{inv.get('counts', {}).get('items')}/{n_items} items, {len(cr.get('sub_bots_used', []))} sub-bots used, "
          f"{fake.max_concurrent} requests at once")
    check("I2 parallel inventory is much faster than one-at-a-time and gives the identical result",
          inv.get("inventory_hash") == seq["inventory_hash"] and t_par * 5 < t_seq,
          f"one bot {t_seq:.1f} s vs lead I {t_par:.1f} s ({t_seq / max(t_par, 0.01):.0f}x)")
    check("I3 live inventory progress in status.json while it runs (mode, items listed, bots active)",
          mid is not None and mid["mode"] == "inventory" and mid["bots_active"] >= 1 and "I" in mid["leads"],
          str({k: mid.get(k) for k in ("done", "bots_active", "per_minute_last_60s")}) if mid else "no mid-run status seen")
    bad_folder = next(f["id"] for f in fake.files.values() if f["name"] == "sub3-3")
    fake.fail_list_folders.add(bad_folder)
    r = cli(env, cfg, "drive", "inventory", "--root", "root", expect=None)
    run2 = runs.find_run(d["runs_dir"], "latest")
    inv2 = read_json(run2 / "inventory.json") or {}
    stub = {"schema": "pcmcp.drive_plan", "schema_version": 1, "author": "t", "inventory_hash": inv2.get("inventory_hash"),
            "scope_root_id": inv2.get("scope_root_id"), "create_folders": [], "placements": {}}
    v = planfile.validate(stub, inv2, read_json(Path(d["folder_map"])))
    fake.fail_list_folders.clear()
    fake.latency = 0.3
    proc = cli(env, cfg, "drive", "inventory", "--root", "root", background=True)
    end = time.time() + 30
    run3 = None
    while time.time() < end:
        r3 = runs.find_run(d["runs_dir"], "latest")
        if r3 not in (run_dir, run2) and (read_json(r3 / "status.json") or {}).get("done", 0) > 0:
            run3 = r3
            break
        time.sleep(0.05)
    if run3:
        cli(env, cfg, "drive", "stop", "--run", run3.name)
    out3, _ = proc.communicate(timeout=60)
    inv3 = read_json(run3 / "inventory.json") if run3 else {}
    check("I5 `drive stop` stops a running inventory; it is marked incomplete (never mistaken for a full one)",
          run3 and proc.returncode == 3 and inv3.get("complete") is False and any(e.get("stopped") for e in inv3.get("errors", [])))
    fake.latency = 0.04
    check("I4 a folder that can't be listed marks the inventory INCOMPLETE (exit 3) and plans on it are refused",
          r.returncode == 3 and inv2.get("complete") is False and any(c == "INVENTORY_INCOMPLETE" for c, _ in v["errors"]),
          (r.stdout.strip().splitlines() or [""])[-1])
    fake.stop()


if __name__ == "__main__":
    main()
