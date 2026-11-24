"""The REGISTRY: single source of truth for "who is allowed to do what".

letter -> job description -> bot names holding that letter.
A letter, once assigned, never changes meaning. Bot names follow the ladder:
  round 1: A        round 2: A1      round 3: A1A      round 4: A1A1 ...
so a bot's job letter is always the first character of its name.
"""
from __future__ import annotations

import string

from .util import atomic_write_json, now_iso, read_json

FLEET_CAP = 100

# The permanent letter table. Never edit an existing row's meaning; only append new letters.
LETTERS = {
    "A": {"job": "create", "description": "Create a new file or folder.",
          "actions": ["create_folder", "create_file"], "destructive": False},
    "B": {"job": "copy_move", "description": "Copy or move a file. Overwrites are approval-gated and stage the old file.",
          "actions": ["copy", "move"], "destructive": False},
    "C": {"job": "delete", "description": "Delete a file by STAGING it into quarantine (reversible). Approval-gated.",
          "actions": ["delete"], "destructive": True},
    "E": {"job": "archive", "description": "Zip files into an archive; originals are staged into quarantine (reversible).",
          "actions": ["archive"], "destructive": True},
    "S": {"job": "screenshot", "description": "Capture a screenshot of the screen, a region, or an app and save it with its hash.",
          "actions": ["screenshot"], "destructive": False},
}
RESERVED = {"D": "Intentionally unassigned (skipped in the v2 build request). Do not reuse without an owner decision."}


def ladder_name(letter: str, round_no: int) -> str:
    """Round 1 -> 'A', 2 -> 'A1', 3 -> 'A1A', 4 -> 'A1A1', ..."""
    if round_no < 1:
        raise ValueError("round starts at 1")
    parts = []
    for i in range(round_no):
        parts.append(letter if i % 2 == 0 else "1")
    return "".join(parts)


def letter_of(bot_name: str) -> str:
    return bot_name[:1]


def build(fleet: dict) -> dict:
    """Deterministically build a registry from a fleet size map like {"A": 1, "B": 4}."""
    total = sum(int(n) for n in fleet.values())
    if total > FLEET_CAP:
        raise ValueError(f"fleet of {total} exceeds cap of {FLEET_CAP}")
    letters = {}
    for letter in sorted(LETTERS):
        n = int(fleet.get(letter, 0))
        letters[letter] = dict(LETTERS[letter], bots=[ladder_name(letter, r) for r in range(1, n + 1)])
    return {"schema": "pcmcp.registry", "schema_version": 1, "fleet_cap": FLEET_CAP,
            "generated_at": now_iso(), "letters": letters, "reserved": RESERVED}


def validate(reg: dict) -> list:
    """Return a list of problems; empty means the registry is sound."""
    problems = []
    seen = set()
    total = 0
    for letter, row in reg.get("letters", {}).items():
        if letter not in string.ascii_uppercase or len(letter) != 1:
            problems.append(f"bad letter {letter!r}")
        if letter in LETTERS and row.get("job") != LETTERS[letter]["job"]:
            problems.append(f"letter {letter} changed meaning ({row.get('job')} != {LETTERS[letter]['job']})")
        for b in row.get("bots", []):
            total += 1
            if letter_of(b) != letter:
                problems.append(f"bot {b} listed under {letter} but its name says {letter_of(b)}")
            if b in seen:
                problems.append(f"bot {b} listed twice")
            seen.add(b)
    if total > reg.get("fleet_cap", FLEET_CAP):
        problems.append(f"fleet {total} exceeds cap")
    return problems


def load(path) -> dict:
    reg = read_json(path)
    if reg is None:
        raise FileNotFoundError(f"registry missing at {path}; run `python3 -m pcmcp_swarm init`")
    problems = validate(reg)
    if problems:
        raise ValueError("registry invalid: " + "; ".join(problems))
    return reg


def save(path, reg: dict) -> None:
    problems = validate(reg)
    if problems:
        raise ValueError("refusing to save invalid registry: " + "; ".join(problems))
    atomic_write_json(path, reg)


def check_order(reg: dict, bot: str, action: str) -> str | None:
    """SAFETY: refuse any order whose action is outside the bot's letter. Returns an error or None."""
    letter = letter_of(bot)
    row = reg.get("letters", {}).get(letter)
    if row is None:
        return f"bot {bot}: letter {letter} not in registry"
    if bot not in row.get("bots", []):
        return f"bot {bot} does not hold letter {letter} in the registry"
    if action not in row.get("actions", []):
        return f"bot {bot} (letter {letter}={row['job']}) may not perform '{action}'"
    return None


def letter_for_action(action: str) -> str | None:
    for letter, row in LETTERS.items():
        if action in row["actions"]:
            return letter
    return None


def render_key(reg: dict) -> str:
    """Human-readable key for the registry file."""
    lines = ["# PCMCP Registry Key", "",
             "Generated from `registry.json` (the source of truth). Do not edit this file; edit the fleet in config and re-run `init`.",
             "", "| Letter | Job | What it does | Destructive | Bots holding it |", "|---|---|---|---|---|"]
    for letter, row in sorted(reg["letters"].items()):
        lines.append(f"| {letter} | {row['job']} | {row['description']} | {'yes (approval + quarantine)' if row['destructive'] else 'no'} | "
                     f"{', '.join(row['bots']) or '—'} |")
    for letter, why in sorted(reg.get("reserved", {}).items()):
        lines.append(f"| {letter} | (reserved) | {why} | — | — |")
    lines += ["", "Bot names: round 1 `A`, round 2 `A1`, round 3 `A1A`, round 4 `A1A1`. The first character is always the job letter.",
              f"Fleet cap: {reg.get('fleet_cap', FLEET_CAP)} bots.", ""]
    return "\n".join(lines)
