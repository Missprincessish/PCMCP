"""macOS capture via the built-in `screencapture` tool.

Permissions: the process running PCMCP (Terminal/iTerm/Python) needs Screen Recording
(System Settings > Privacy & Security > Screen Recording). The 'app' target also needs
Accessibility to read the window bounds; without it we fall back to a full-screen capture.
"""
from __future__ import annotations

import subprocess
import time


def _app_bounds(name: str):
    script = (f'tell application "{name}" to activate\n'
              f'delay 0.6\n'
              f'tell application "System Events" to tell (first process whose name is "{name}")\n'
              f'  set {{x, y}} to position of front window\n'
              f'  set {{w, h}} to size of front window\n'
              f'end tell\n'
              f'return (x as text) & "," & (y as text) & "," & (w as text) & "," & (h as text)')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    try:
        return [int(float(v)) for v in r.stdout.strip().split(",")]
    except ValueError:
        return None


def capture(target: dict, out_path) -> None:
    kind = target.get("type", "full")
    cmd = ["screencapture", "-x"]  # -x: no shutter sound
    if kind == "region":
        cmd += ["-R", f"{int(target['x'])},{int(target['y'])},{int(target['w'])},{int(target['h'])}"]
    elif kind == "app":
        name = str(target["name"]).replace('"', "")
        b = _app_bounds(name)
        if b:
            cmd += ["-R", ",".join(map(str, b))]
        else:
            time.sleep(0.3)
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True, timeout=30)
