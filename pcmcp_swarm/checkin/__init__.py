"""Check-in channels: the swappable plug PCMCP uses to fetch instructions and return receipts.

The rest of PCMCP talks ONLY to CheckInChannel. Channels move bytes; they never grant authority.
Every batch from every channel goes through the same signing.verify() in checkin/handler.py.

To add a channel: write checkin/<name>.py with a class subclassing CheckInChannel, register it in
ADAPTERS below, and set config.checkin.channel = "<name>". Nothing else changes.
"""
from __future__ import annotations


class CheckInChannel:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def fetch_instructions(self) -> list:
        """Return a list of (raw_bytes, meta_dict). meta must include 'ref' (where it came from)."""
        raise NotImplementedError

    def post_receipts(self, receipt: dict, attachments: list | None = None) -> str:
        """Deliver a receipt (and optional files) back to the orchestrator. Returns where it went."""
        raise NotImplementedError

    def acknowledge(self, meta: dict, accepted: bool) -> None:
        """Optional: tell the channel an item was consumed so it is not fetched again."""


def ADAPTERS():
    from .local_inbox import LocalInbox
    from .stubs import EmailAttachment, HttpsEndpoint, McpTool
    return {"local_inbox": LocalInbox, "https_endpoint": HttpsEndpoint, "mcp_tool": McpTool,
            "email_attachment": EmailAttachment}


def get_channel(cfg: dict) -> CheckInChannel:
    name = cfg["checkin"]["channel"]
    adapters = ADAPTERS()
    if name not in adapters:
        raise ValueError(f"unknown check-in channel {name!r}; known: {sorted(adapters)}")
    return adapters[name](cfg)
