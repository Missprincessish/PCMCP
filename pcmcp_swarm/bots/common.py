"""Bot harness shared by every bot program.

COMMAND-LINE CONTRACT (SPEC.md §5): a bot is a standalone program.
    python3 -m pcmcp_swarm.bots.bot_b  < order.json  > receipt.json
It reads ONE order JSON on stdin and writes ONE receipt JSON on stdout. Exit code 0 even on a
refused/failed op (the receipt says why); non-zero only if the harness itself crashed.

A bot holds no state of its own. Everything it needs is in the order; everything it did is in
the receipt plus the quarantine folder named in the order.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from .. import registry as reg_mod
from ..util import atomic_write_json, is_within, now_iso, read_json, sha256_file


class Refuse(Exception):
    """The order is not lawful or the world is not in the state the plan expected. Nothing was changed."""


def file_rec(role: str, path, before, after) -> dict:
    return {"role": role, "path": str(path), "sha256_before": before, "sha256_after": after}


def check_path(order: dict, path) -> Path:
    """SAFETY: every path a bot touches must be inside an allowed root (no ../, no symlink escape)."""
    p = Path(path)
    if not p.is_absolute():
        raise Refuse(f"path must be absolute: {path}")
    roots = order.get("allowed_roots") or []
    if not any(is_within(p if p.exists() else p.parent, r) for r in roots):
        raise Refuse(f"path outside allowed roots: {path}")
    if p.is_symlink():
        raise Refuse(f"refusing to operate on a symlink: {path}")
    return p


def qdir(order: dict, *sub) -> Path:
    """This op's private quarantine folder: <quarantine>/<plan_hash>/<op_id>/..."""
    base = Path(order["quarantine_dir"])
    d = base / order["plan_hash"] / order["op_id"]
    for s in sub:
        d = d / s
    if not is_within(d, base) or d.resolve() == base.resolve():
        raise Refuse(f"quarantine path escapes quarantine dir: {d}")
    return d


def load_intent(order: dict, sub="") -> dict | None:
    return read_json(qdir(order, sub, "intent.json") if sub else qdir(order, "intent.json"))


def save_intent(order: dict, intent: dict, sub="") -> None:
    """Written BEFORE any change. On a crash + resume, the bot reads it to know the true pre-state."""
    atomic_write_json(qdir(order, sub, "intent.json") if sub else qdir(order, "intent.json"), intent)


def stage(src: Path, dest: Path) -> str:
    """SAFETY: move a file into quarantine and prove the staged copy is byte-identical.

    Never deletes. Returns the sha256 of the staged file.
    """
    before = sha256_file(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if sha256_file(dest) == before:
            os.remove(src)  # identical copy already staged by an interrupted run
            return before
        raise Refuse(f"quarantine slot already occupied with different content: {dest}")
    shutil.move(str(src), str(dest))
    after = sha256_file(dest)
    if after != before:
        raise RuntimeError(f"staging corrupted file {src}: {before} != {after}")
    return after


def unstage(staged: Path, dest: Path, expect: str | None) -> None:
    """Put a quarantined file back. Refuses if the destination is occupied or hash differs."""
    if dest.exists():
        if expect and sha256_file(dest) == expect and not staged.exists():
            return  # already restored
        raise Refuse(f"cannot restore, destination occupied: {dest}")
    if not staged.exists():
        raise Refuse(f"staged file missing: {staged}")
    if expect and sha256_file(staged) != expect:
        raise Refuse(f"staged file hash changed: {staged}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(dest))


def run(letter: str, handlers: dict, undo_handlers: dict) -> None:
    raw = sys.stdin.read()
    started = now_iso()
    order = {}
    receipt = {"schema": "pcmcp.receipt", "schema_version": 1, "started_at": started}
    try:
        order = json.loads(raw)
        receipt.update({k: order.get(k) for k in ("plan_hash", "op_id", "bot", "action", "mode")})
        receipt["letter"] = letter
        bot, action, mode = order["bot"], order["action"], order.get("mode", "dry_run")

        # SAFETY (hard rule 2): the bot's own letter is compiled in. Double check against name + registry.
        if reg_mod.letter_of(bot) != letter:
            raise Refuse(f"this program is a letter-{letter} bot; it will not run as {bot}")
        reg = reg_mod.load(order["registry_path"])
        err = reg_mod.check_order(reg, bot, action)
        if err:
            raise Refuse(err)
        if mode not in ("dry_run", "execute", "undo"):
            raise Refuse(f"unknown mode {mode}")

        table = undo_handlers if mode == "undo" else handlers
        if action not in table:
            raise Refuse(f"letter {letter} has no {mode} handler for {action}")
        result = table[action](order, mode)
        receipt.update(result)
        receipt.setdefault("status", "would_do" if mode == "dry_run" else "done")
    except Refuse as e:
        receipt.update(status="refused", error=str(e))
    except Exception as e:  # noqa: BLE001 - every failure must become a receipt, never a silent crash
        receipt.update(status="error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=3))
    receipt["finished_at"] = now_iso()
    receipt.setdefault("files", [])
    sys.stdout.write(json.dumps(receipt, sort_keys=True))
    sys.stdout.flush()
