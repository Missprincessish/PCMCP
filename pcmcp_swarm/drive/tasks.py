"""Named read-only terminal tasks for text triggers.

SAFETY: a task is a FIXED argv from text-tasks.json (in the repo). Nothing from a text is passed to it; there
is no shell. Every task must use a program on READ_ONLY below, with arguments that keep it read-only; any
other task is refused when the list is loaded. To add a task, add it to text-tasks.json (and, for a new
program, to READ_ONLY here after checking it cannot write).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..util import read_json

TASKS_FILE = Path(__file__).resolve().parents[2] / "text-tasks.json"

# program -> rule for its arguments. "any" = the program has no write/delete mode at all.
# A list = the first argument must be one of these (read-only sub-commands).
READ_ONLY = {
    "/bin/df": "any", "/bin/ps": "any", "/usr/bin/uptime": "any", "/usr/bin/sw_vers": "any",
    "/usr/bin/vm_stat": "any", "/bin/ls": "any", "/usr/bin/du": "any",
    "/usr/bin/pmset": ["-g"], "/usr/sbin/scutil": ["--nwi", "--dns", "--proxy"],
    "/usr/sbin/diskutil": ["list", "info"], "/usr/bin/brctl": ["status"],
    "/usr/sbin/system_profiler": ["SPHardwareDataType", "SPStorageDataType", "SPPowerDataType"],
}
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def check_task(t: dict) -> str | None:
    argv = t.get("argv")
    if not NAME_RE.match(str(t.get("name", ""))):
        return f"bad task name {t.get('name')!r}"
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return f"{t.get('name')}: argv must be a list of strings"
    rule = READ_ONLY.get(argv[0])
    if rule is None:
        return f"{t['name']}: program {argv[0]} is not on the read-only list"
    if rule != "any" and (len(argv) < 2 or argv[1] not in rule):
        return f"{t['name']}: {argv[0]} is only allowed with {rule}"
    if any(ch in a for a in argv for ch in (";", "|", "&", "`", "$(", ">", "<")):
        return f"{t['name']}: shell characters are not allowed"
    return None


def load(path: Path = TASKS_FILE) -> dict:
    data = read_json(path) or {}
    out, problems = {}, []
    for t in data.get("tasks", []):
        err = check_task(t)
        if err:
            problems.append(err)
        else:
            out[t["name"]] = t
    return {"tasks": out, "problems": problems}


def run(task: dict, out_txt: Path) -> dict:
    home = str(Path.home())
    argv = [a.replace("{HOME}", home) for a in task["argv"]]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=int(task.get("timeout", 60)), stdin=subprocess.DEVNULL)
        code, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = 124, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timed out"
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(f"$ {' '.join(argv)}\n(exit {code})\n\n{out}" + (f"\n--- stderr ---\n{err}" if err else ""), encoding="utf-8")
    return {"exit": code, "lines": out.count("\n"), "first_lines": "\n".join(out.splitlines()[:6]), "output_file": out_txt.name}
