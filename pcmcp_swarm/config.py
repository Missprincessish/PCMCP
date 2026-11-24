"""Single config file for the swarm. Every channel setting lives here (owner requirement).

Location: --config flag, else $PCMCP_CONFIG, else ~/.pcmcp/config.json.
The config file and the secret live OUTSIDE the repo.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

from .util import read_json

DEFAULT_CONFIG_PATH = Path.home() / ".pcmcp" / "config.json"

def defaults(base: Path) -> dict:
    """Defaults; every path lives under `base` (the folder holding the config file)."""
    return {
        "schema_version": 1,
        # Registry, logs, plans, quarantine, health all live together here.
        "data_dir": str(base / "data"),
        # Bots may only touch paths inside one of these roots. Empty = nothing allowed.
        "allowed_roots": [],
        "doorbell": {
            # The trigger string. MUST be changed. It is a doorbell only: it carries no authority.
            "trigger": "CHANGE-ME-TO-A-UNIQUE-TRIGGER",
            "channels": ["local_spool"],          # any of: local_spool, local_http, gmail_imap, gmail_api(stub)
            "poll_seconds": 30,
            "rate_limit": {"max_wakes": 6, "window_seconds": 600},
            "local_spool": {"dir": str(base / "doorbell_spool")},
            "local_http": {"port": 8792},         # always bound to 127.0.0.1 only
            "gmail_imap": {
                "host": "imap.gmail.com",
                "account": "",                    # DEDICATED doorbell account, never the main one
                "label": "PCMCP-Doorbell",
                "allowed_senders": [],            # empty = any sender may ring (still only a ring)
                "secret": {"type": "keychain", "service": "pcmcp-doorbell-imap"},
            },
        },
        "checkin": {
            "channel": "local_inbox",             # local_inbox | https_endpoint(stub) | mcp_tool(stub) | email_attachment(stub)
            "local_inbox": {
                "inbox_dir": str(base / "checkin" / "inbox"),
                "outbox_dir": str(base / "checkin" / "outbox"),
            },
            # HMAC-SHA256 shared secret. Never stored in the repo.
            "secret": {"type": "file", "path": str(base / "checkin_secret")},
            "max_batch_lifetime_seconds": 86400,
        },
        "scan": {
            "organized_dir": "_Organized",
            "archive_dir": "_Archive",
            "archive_before": None,               # "YYYY-MM-DD": files last modified before this are archived. None = off
            "delete_exact_duplicates": True,      # duplicates are STAGED (reversible), and only after approval
            "categories": {
                "Documents": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".pages", ".md"],
                "Spreadsheets": [".xls", ".xlsx", ".csv", ".numbers", ".ods"],
                "Presentations": [".ppt", ".pptx", ".key", ".odp"],
                "Images": [".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".tif", ".tiff", ".bmp", ".svg"],
                "Video": [".mp4", ".mov", ".avi", ".mkv", ".m4v"],
                "Audio": [".mp3", ".wav", ".m4a", ".aac", ".flac"],
                "Archives": [".zip", ".tar", ".gz", ".7z", ".rar"],
                "Code": [".py", ".js", ".ts", ".html", ".css", ".json", ".sh"],
            },
            "uncategorized": "Other",
            "ignore_names": [".DS_Store", "desktop.ini", "Thumbs.db", "Icon\r"],
        },
        "fleet": {"A": 1, "B": 4, "C": 1, "E": 1, "S": 1},
        "screenshot": {"requires_owner_approval": True},
        "health": {"stale_heartbeat_seconds": 120, "slow_ratio": 0.5},
        # Swarm v2: Google Drive. See CONTROL.md and DRIVE_PLAN_SPEC.md.
        "drive": {
            "api_base": "https://www.googleapis.com",       # the fake Drive server overrides this for offline tests
            "auth": "oauth",                                # oauth | none (none = fake server only)
            "oauth_client_file": str(base / "drive_oauth_client.json"),   # downloaded from Google Cloud Console
            "token_file": str(base / "drive_token.json"),                  # written by `drive auth`, mode 0600
            "scope": "https://www.googleapis.com/auth/drive",             # moving existing files needs full Drive scope
            "folder_map": str(Path(__file__).resolve().parent.parent / "folder-map.json"),
            "runs_dir": None,                               # None = <data_dir>/drive_runs
            # Leads (A create, B copy/move, E archive) each command a pool of sub-bots. Leads + sub-bots <= 100.
            "pools": {"A": 10, "B": 30, "E": 10, "I": 30},     # I = read-only inventory lead
            "batch_size": 100,                              # Google's max calls per batch request
            "edits_per_minute": 6000,                       # throttle ceiling, under Google's ~6,500/min
            "reads_per_minute": 12000,                      # read throttle (inventory crawl, dry run checks)
            "max_retries": 8,                               # per item, for 403-rate-limit / 429 / 5xx
            "backoff_base_seconds": 1.0,
            "backoff_max_seconds": 64.0,
            "chunk_max_attempts": 3,                        # a failed chunk is reassigned to another sub-bot
            "status_interval_seconds": 1.0,
            "screenshot_interval_seconds": 0,               # 0 = off; needs Screen Recording permission
            "rollback_holding_folder": "_PCMCP_Rollback_Holding",
            # Owner approval signing key (passphrase-protected, created by `drive owner-setup`).
            "owner_key": str(base / "owner_approval_key"),
            "owner_allowed_signers": str(base / "owner_allowed_signers"),
        },
    }


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("PCMCP_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def load(explicit: str | None = None) -> dict:
    cp = config_path(explicit)
    cfg = _merge(defaults(cp.parent), read_json(cp, {}) or {})
    cfg["_path"] = str(config_path(explicit))
    return cfg


def data_paths(cfg: dict) -> dict:
    d = Path(cfg["data_dir"]).expanduser()
    return {
        "root": d,
        "registry": d / "registry.json",
        "registry_key": d / "REGISTRY_KEY.md",
        "operations": d / "logs" / "operations.jsonl",
        "doorbell": d / "logs" / "doorbell.jsonl",
        "approvals": d / "logs" / "approvals.jsonl",
        "checkin": d / "logs" / "checkin.jsonl",
        "plans": d / "plans",
        "journal": d / "journal",
        "health": d / "health",
        "quarantine": d / "quarantine",
        "screenshots": d / "screenshots",
        "used_batches": d / "state" / "used_batch_ids.jsonl",
        "pending": d / "state" / "pending_requests.jsonl",
    }
