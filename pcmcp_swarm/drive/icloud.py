"""iCloud Drive: LOOK-ONLY listing. Names, sizes, dates, and whether each file is downloaded or in the cloud only.

SAFETY: this module only lists. It never opens a file (so nothing is downloaded or read), never writes,
moves, renames or deletes, and never follows symlinks. It uses os.scandir + lstat metadata only.
"Cloud only" = the file is a dataless placeholder (SF_DATALESS flag) or an old-style `.name.icloud` stub.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

SF_DATALESS = 0x40000000
DEFAULT_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inventory(root: Path, out_tsv: Path, max_entries: int = 500000) -> dict:
    root = Path(root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"iCloud Drive folder not found: {root}")
    counts = {"files": 0, "folders": 0, "links": 0, "unreadable": 0, "cloud_only": 0, "downloaded": 0,
              "bytes_total": 0, "bytes_downloaded": 0}
    truncated = False
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_tsv, "w", encoding="utf-8") as out:
        out.write("path\ttype\tsize_bytes\tmodified_utc\tstatus\n")
        stack, n = [root], 0
        while stack:
            d = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                counts["unreadable"] += 1
                out.write(f"{os.path.relpath(d, root)}\tfolder\t\t\tunreadable\n")
                continue
            for e in entries:
                if n >= max_entries:
                    truncated = True
                    stack = []
                    break
                n += 1
                rel = os.path.relpath(e.path, root).replace("\t", " ").replace("\n", " ")
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    counts["unreadable"] += 1
                    out.write(f"{rel}\t?\t\t\tunreadable\n")
                    continue
                if e.is_symlink():
                    counts["links"] += 1
                    out.write(f"{rel}\tlink\t\t{_iso(st.st_mtime)}\t-\n")
                elif e.is_dir(follow_symlinks=False):
                    counts["folders"] += 1
                    out.write(f"{rel}\tfolder\t\t{_iso(st.st_mtime)}\t-\n")
                    stack.append(e.path)
                else:
                    counts["files"] += 1
                    stub = e.name.startswith(".") and e.name.endswith(".icloud")
                    cloud = stub or bool(getattr(st, "st_flags", 0) & SF_DATALESS)
                    status = "cloud-only" if cloud else "downloaded"
                    counts["cloud_only" if cloud else "downloaded"] += 1
                    counts["bytes_total"] += st.st_size
                    if not cloud:
                        counts["bytes_downloaded"] += st.st_size
                    if stub:  # `.Name.ext.icloud` -> show the real name
                        rel = os.path.join(os.path.dirname(rel), e.name[1:-len(".icloud")])
                    out.write(f"{rel}\tfile\t{st.st_size}\t{_iso(st.st_mtime)}\t{status}\n")
    return {"root": str(root), "counts": counts, "truncated": truncated, "listing": out_tsv.name,
            "contents_opened": False, "changed_anything": False}
