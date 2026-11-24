"""Run folders and the evidence trail.

One folder per run, named by timestamp, holds everything the demo needs:
  commands.jsonl     every command issued against this run (who, argv, parent process chain)
  inventory.json     read-only inventory export (the hand-off to Instinct)
  before_listing.json / after_listing.json / rollback_listing.json
  plan.json          the exact plan file that ran (copied in), approvals.jsonl
  dryrun.json, chunks.json, status.json, control.json, events.jsonl
  receipts.jsonl     one receipt per item, hash-chained (each line carries prev_hash + receipt_hash)
  summary.json, screenshots/

Chain of evidence: command record (command_id) -> receipts carrying that command_id -> receipt hash chain.
"""
from __future__ import annotations

import datetime as _dt
import getpass
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from ..util import append_jsonl, canonical_json, now_iso, read_json, read_jsonl, sha256_bytes

GENESIS = "0" * 64


def new_run(runs_dir: str) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    p = Path(runs_dir).expanduser() / stamp
    i = 1
    while p.exists():
        i += 1
        p = Path(runs_dir).expanduser() / f"{stamp}-{i}"
    (p / "screenshots").mkdir(parents=True)
    return p


def find_run(runs_dir: str, ref: str | None) -> Path:
    base = Path(runs_dir).expanduser()
    runs = sorted(d for d in base.glob("*") if d.is_dir()) if base.exists() else []
    if not runs:
        raise FileNotFoundError(f"no runs in {base}; start with `drive inventory`")
    if not ref or ref == "latest":
        return runs[-1]
    exact = [d for d in runs if d.name == ref]
    if exact:
        return exact[0]
    m = [d for d in runs if d.name.startswith(ref)]
    if len(m) != 1:
        raise FileNotFoundError(f"run {ref!r} matched {len(m)} runs in {base}")
    return m[0]


def _parent_chain(limit: int = 6) -> list:
    """Corroborating evidence of where a command came from: the parent process chain (pid, command)."""
    out, pid = [], os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            r = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            line = r.stdout.strip()
            if not line:
                break
            ppid, _, comm = line.partition(" ")
            out.append({"pid": pid, "command": comm.strip()})
            pid = int(ppid)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return out


def record_command(run_dir: Path | None, global_log: Path, mode: str, argv: list, issued_by: str | None,
                   extra: dict | None = None) -> dict:
    rec = {
        "command_id": "cmd-" + uuid.uuid4().hex[:16],
        "mode": mode,
        "argv": argv,
        "issued_by": issued_by or os.environ.get("PCMCP_ISSUER") or "unspecified",
        "issued_by_note": "self-declared via --issued-by / PCMCP_ISSUER; parent_process_chain is corroboration",
        "issued_at": now_iso(),
        "os_user": getpass.getuser(),
        "host": socket.gethostname(),
        "cwd": os.getcwd(),
        "tty": os.ttyname(0) if sys.stdin.isatty() else None,
        "parent_process_chain": _parent_chain(),
        "run": run_dir.name if run_dir else None,
        **(extra or {}),
    }
    if run_dir:
        append_jsonl(run_dir / "commands.jsonl", rec)
    append_jsonl(global_log, rec)
    return rec


def event(run_dir: Path, kind: str, **fields) -> None:
    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), "event": kind, **fields})


class ReceiptLog:
    """Hash-chained receipts. Thread-safe. A receipt's hash covers its content plus the previous hash,
    so editing or removing any line breaks every hash after it."""

    def __init__(self, run_dir: Path, mirror: Path | None = None, name: str = "receipts.jsonl"):
        self.path = run_dir / name
        self.mirror = mirror
        self.lock = threading.Lock()
        prev = read_jsonl(self.path)
        self.prev = prev[-1]["receipt_hash"] if prev else GENESIS
        self.count = len(prev)

    def write(self, receipt: dict) -> dict:
        with self.lock:
            r = dict(receipt, seq=self.count + 1, prev_hash=self.prev)
            r["receipt_hash"] = sha256_bytes(canonical_json(r))
            append_jsonl(self.path, r)
            if self.mirror:
                append_jsonl(self.mirror, dict(r, run=self.path.parent.name))
            self.prev = r["receipt_hash"]
            self.count += 1
            return r


def verify_chain(path: Path) -> tuple:
    """Returns (ok, n, problem)."""
    prev = GENESIS
    rows = read_jsonl(path)
    for i, r in enumerate(rows, 1):
        h = r.get("receipt_hash")
        body = {k: v for k, v in r.items() if k != "receipt_hash"}
        if r.get("prev_hash") != prev or r.get("seq") != i or sha256_bytes(canonical_json(body)) != h:
            return False, len(rows), f"chain breaks at line {i}"
        prev = h
    return True, len(rows), None


def write_json(path: Path, obj) -> None:
    from ..util import atomic_write_json
    atomic_write_json(path, obj)


def control_state(run_dir: Path) -> str:
    return (read_json(run_dir / "control.json", {}) or {}).get("state", "go")


def set_control(run_dir: Path, state: str, command_id: str) -> None:
    write_json(run_dir / "control.json", {"state": state, "set_at": now_iso(), "command_id": command_id})
