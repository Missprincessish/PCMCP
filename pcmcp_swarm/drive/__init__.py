"""PCMCP Swarm v2: Google Drive.

The bots have no brains. Instinct writes the plan; this package validates it and executes it exactly.
Nothing here classifies files, chooses destinations, downloads file bytes, or deletes anything.
"""
from __future__ import annotations

from pathlib import Path

PLAN_SCHEMA = "pcmcp.drive_plan"
INVENTORY_SCHEMA = "pcmcp.drive_inventory"
FOLDER_MIME = "application/vnd.google-apps.folder"
LETTER_JOBS = {"A": "create", "B": "copy_move", "E": "archive", "I": "inventory"}   # A/B/E keep their v1 meanings; I = read-only inventory
FLEET_CAP = 100


def drive_cfg(cfg: dict) -> dict:
    d = dict(cfg["drive"])
    if not d.get("runs_dir"):
        d["runs_dir"] = str(Path(cfg["data_dir"]).expanduser() / "drive_runs")
    return d


def check_pools(pools: dict) -> list:
    """Leads + sub-bots must fit the fleet cap; only letters A, B, E, I exist in v2; every lead needs >= 1 sub-bot."""
    problems = []
    for letter, n in pools.items():
        if letter not in LETTER_JOBS:
            problems.append(f"pool letter {letter!r} is not a v2 job (A create, B copy/move, E archive, I inventory; no delete)")
        if not isinstance(n, int) or n < 1:
            problems.append(f"pool {letter} must be a whole number >= 1")
    total = sum(n for n in pools.values() if isinstance(n, int)) + len(pools)
    if total > FLEET_CAP:
        problems.append(f"{total} bots (leads + sub-bots) exceeds the fleet cap of {FLEET_CAP}")
    return problems


def sub_bot_names(letter: str, n: int) -> list:
    return [f"{letter}{i}" for i in range(1, n + 1)]
