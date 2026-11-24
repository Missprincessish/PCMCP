"""Secret lookup. Secrets never live in the repo or in the config file itself.

spec forms (in config):
  {"type": "file", "path": "~/.pcmcp/checkin_secret"}
  {"type": "keychain", "service": "pcmcp-doorbell-imap", "account": "<user>"}   (macOS Keychain)
  {"type": "env", "name": "PCMCP_CHECKIN_SECRET"}
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def get_secret(spec: dict, account: str | None = None) -> str:
    kind = (spec or {}).get("type")
    if kind == "file":
        p = Path(spec["path"]).expanduser()
        if not p.exists():
            raise RuntimeError(f"secret file missing: {p}")
        return p.read_text(encoding="utf-8").strip()
    if kind == "env":
        v = os.environ.get(spec["name"], "")
        if not v:
            raise RuntimeError(f"env var {spec['name']} is empty")
        return v
    if kind == "keychain":
        if sys.platform != "darwin":
            raise RuntimeError("keychain secrets are macOS-only; use type=env or type=file on this OS")
        acct = spec.get("account") or account or ""
        r = subprocess.run(["security", "find-generic-password", "-s", spec["service"], "-a", acct, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"keychain item {spec['service']}/{acct} not found")
        return r.stdout.strip()
    raise RuntimeError(f"unknown secret type {kind!r}")
