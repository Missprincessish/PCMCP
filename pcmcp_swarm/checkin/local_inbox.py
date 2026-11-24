"""Signed local inbox folder: the one shipped check-in channel.

Instinct (or anything holding the shared secret) drops signed batch files in inbox_dir. The folder
can be a Google Drive synced folder, which is how a cloud assistant with Drive access reaches the Mac.
Receipts are written to outbox_dir. Processed files move to inbox_dir/processed or inbox_dir/rejected.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import CheckInChannel
from ..util import is_within, now_iso, safe_id

MAX_BATCH_BYTES = 1024 * 1024


class LocalInbox(CheckInChannel):
    name = "local_inbox"

    def __init__(self, cfg):
        super().__init__(cfg)
        c = cfg["checkin"]["local_inbox"]
        self.inbox = Path(c["inbox_dir"]).expanduser()
        self.outbox = Path(c["outbox_dir"]).expanduser()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def fetch_instructions(self):
        items = []
        for p in sorted(self.inbox.glob("*.json")):
            if not p.is_file() or p.is_symlink():
                continue
            if p.stat().st_size > MAX_BATCH_BYTES:
                items.append((b"", {"ref": str(p), "path": str(p), "oversize": True}))
                continue
            items.append((p.read_bytes(), {"ref": str(p), "path": str(p)}))
        return items

    def acknowledge(self, meta, accepted):
        p = Path(meta["path"])
        if p.exists():
            dest = self.inbox / ("processed" if accepted else "rejected")
            dest.mkdir(exist_ok=True)
            shutil.move(str(p), str(dest / p.name))

    def post_receipts(self, receipt, attachments=None):
        stamp = now_iso().replace(":", "").replace(".", "")
        bid = receipt.get("batch_id")
        name = f"receipt_{bid if safe_id(bid) else 'local'}_{stamp}"
        out = self.outbox / f"{name}.json"
        files = []
        for a in attachments or []:
            a = Path(a)
            dest = self.outbox / f"{name}__{a.name}"
            if not is_within(dest, self.outbox):
                raise ValueError(f"attachment path escapes outbox: {dest}")
            shutil.copy2(a, dest)
            files.append(str(dest))
        receipt = dict(receipt, attachments=files)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(out)
        return str(out)
