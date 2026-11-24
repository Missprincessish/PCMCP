"""Gmail doorbell over IMAP (Python stdlib imaplib) with an app password from the macOS Keychain.

USE A DEDICATED DOORBELL GMAIL ACCOUNT. An app password can read the whole mailbox, so it must only
ever unlock an inbox that holds nothing but doorbell mail.

What this reads: the FROM and SUBJECT headers of unseen messages in ONE label. Nothing else.
It never downloads a message body, never opens attachments, never follows links. The subject is
compared to the trigger string; that comparison is the only thing a message can influence.
"""
from __future__ import annotations

import imaplib
from email.header import decode_header, make_header
from email.parser import HeaderParser
from email.utils import parseaddr

from . import DoorbellChannel
from ..secret_store import get_secret


def _decode(v: str) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


class GmailImap(DoorbellChannel):
    name = "gmail_imap"
    imap_factory = imaplib.IMAP4_SSL  # swappable in tests

    def __init__(self, cfg):
        super().__init__(cfg)
        self.g = cfg["doorbell"]["gmail_imap"]
        if not self.g.get("account"):
            raise ValueError("doorbell.gmail_imap.account is empty (use a dedicated doorbell Gmail account)")

    def poll(self):
        password = get_secret(self.g["secret"], account=self.g["account"])
        rings = []
        m = self.imap_factory(self.g["host"])
        try:
            m.login(self.g["account"], password)
            typ, _ = m.select(f'"{self.g["label"]}"')
            if typ != "OK":
                raise RuntimeError(f"cannot open label {self.g['label']!r}")
            typ, data = m.uid("SEARCH", None, "UNSEEN")
            uids = (data[0] or b"").split() if typ == "OK" else []
            for uid in uids[:50]:  # bound the work per poll
                # BODY.PEEK[HEADER.FIELDS ...]: headers only, body never fetched.
                typ, msg = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                hdr_bytes = b""
                for part in msg or []:
                    if isinstance(part, tuple):
                        hdr_bytes += part[1]
                h = HeaderParser().parsestr(hdr_bytes.decode("utf-8", "replace"))
                sender = parseaddr(_decode(h.get("From", "")))[1].lower()
                subject = _decode(h.get("Subject", ""))[:512]
                m.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                rings.append({"channel": self.name, "sender": sender, "text": subject, "ref": f"uid:{uid.decode()}"})
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return rings

    def sender_allowed(self, sender: str) -> bool:
        allowed = [s.lower() for s in self.g.get("allowed_senders") or []]
        return not allowed or sender in allowed
