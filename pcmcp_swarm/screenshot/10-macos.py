"""macOS capture via the built-in `screencapture` tool.

Permissions: the process running PCMCP (Terminal/iTerm/Python) needs Screen Recording
(System Settings > Privacy & Security > Screen Recording). The 'app' target also needs
Accessibility to read the window bounds; without it we fall back to a full-screen capture.
"""
from __future__ import annotations

import subprocess
import time


# The app name is data, never script text: it is handed to AppleScript as a command-line
# argument (`on run argv`), so quotes, backslashes and other characters in it cannot change
# the script. We still refuse values that are not a plausible app name.
_APP_BOUNDS_SCRIPT = (
    'on run argv\n'
    '  set appName to item 1 of argv\n'
    '  tell application appName to activate\n'
    '  delay 0.6\n'
    '  tell application "System Events" to tell (first process whose name is appName)\n'
    '    set {x, y} to position of front window\n'
    '    set {w, h} to size of front window\n'
    '  end tell\n'
    '  return (x as text) & "," & (y as text) & "," & (w as text) & "," & (h as text)\n'
    'end run')

MAX_APP_NAME = 100


def clean_app_name(name) -> str:
    """Return the app name unchanged if it is a plausible name; raise ValueError otherwise."""
    if not isinstance(name, str):
        raise ValueError("app name must be text")
    if not name.strip() or len(name) > MAX_APP_NAME:
        raise ValueError(f"app name must be 1 to {MAX_APP_NAME} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("app name must not contain control characters or line breaks")
    if name.startswith("-"):
        raise ValueError("app name must not start with '-'")
    return name


def _app_bounds(name: str):
    r = subprocess.run(["osascript", "-e", _APP_BOUNDS_SCRIPT, name],
                       capture_output=True, text=True, timeout=15)
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
        name = clean_app_name(target["name"])
        b = _app_bounds(name)
        if b:
            cmd += ["-R", ",".join(map(str, b))]
        else:
            time.sleep(0.3)
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True, timeout=30)
