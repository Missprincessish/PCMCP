"""Letter S — screenshot. Per-OS capture lives in pcmcp_swarm/screenshot/<os>.py (the only per-OS code).

params.target: {"type": "full"} | {"type": "region", "x":..,"y":..,"w":..,"h":..} | {"type": "app", "name": "Finder"}
Output is written to params.out_path (inside the data dir's screenshots folder) and hashed.
"""
from __future__ import annotations

from pathlib import Path

from .common import Refuse, file_rec, qdir, run, stage
from ..screenshot import capture
from ..util import sha256_file

LETTER = "S"


def screenshot(order, mode):
    params = order["params"]
    out = Path(params["out_path"])
    shots_dir = Path(order["screenshots_dir"]).resolve()
    if shots_dir not in out.resolve().parents:
        raise Refuse("screenshots may only be written to the configured screenshots folder")
    if mode == "dry_run":
        return {"status": "would_do", "dst": str(out), "note": f"capture {params.get('target')}"}
    if out.exists():
        return {"status": "already_done", "dst": str(out), "files": [file_rec("screenshot", out, None, sha256_file(out))]}
    try:
        capture(params.get("target") or {"type": "full"}, out)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"capture failed ({e}). On macOS grant Screen Recording to the app running PCMCP "
                           "(System Settings > Privacy & Security > Screen Recording).")
    sha = sha256_file(out)
    if sha is None:
        raise RuntimeError("capture produced no file (on macOS, grant Screen Recording permission to your terminal)")
    return {"status": "done", "dst": str(out), "files": [file_rec("screenshot", out, None, sha)],
            "undo": {"kind": "stage_screenshot", "path": str(out), "sha256": sha}}


def undo_screenshot(order, mode):
    u = order["undo"]
    p = Path(u["path"])
    if mode != "dry_run" and p.exists():
        stage(p, qdir(order, "undo", p.name))
    return {"status": "done" if mode != "dry_run" else "would_do"}


if __name__ == "__main__":
    run(LETTER, {"screenshot": screenshot}, {"screenshot": undo_screenshot})
