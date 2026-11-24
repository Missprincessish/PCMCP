"""NOT IMPLEMENTED doorbell adapters."""
from __future__ import annotations

from . import DoorbellChannel


class GmailApi(DoorbellChannel):
    """NOT IMPLEMENTED. Gmail API + OAuth (gmail.readonly scope) replacement for gmail_imap.
    Would return the same ring dicts; switch by listing 'gmail_api' instead of 'gmail_imap' in config."""
    name = "gmail_api"

    def poll(self):
        raise NotImplementedError("doorbell channel 'gmail_api' is a stub and NOT IMPLEMENTED")
