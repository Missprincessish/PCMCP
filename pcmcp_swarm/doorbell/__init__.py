"""Doorbell channels + rate limiter.

THE DOORBELL IS NOT A COMMAND. A ring carries no instructions and no authority. All a valid ring can
do is make PCMCP check in over the signed check-in channel. A spoofed ring therefore achieves, at
worst, one check-in that finds nothing valid and goes back to sleep. Every ring is logged.

To add a doorbell channel: write doorbell/<name>.py subclassing DoorbellChannel, add it to ADAPTERS,
and list it in config.doorbell.channels.
"""
from __future__ import annotations

import hmac
import time
from collections import deque


class DoorbellChannel:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def poll(self) -> list:
        """Return rings: dicts {channel, sender, text, ref}. `text` is the only content ever inspected."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def matches_trigger(text: str, trigger: str) -> bool:
    """Constant-time comparison of the ring text against the configured trigger."""
    if not trigger or trigger.startswith("CHANGE-ME"):
        return False  # an unset trigger can never match
    return hmac.compare_digest((text or "").strip().encode("utf-8"), trigger.strip().encode("utf-8"))


class RateLimiter:
    """Sliding window: at most max_wakes check-ins per window. Excess valid rings are logged, not acted on."""

    def __init__(self, max_wakes: int, window_seconds: int, clock=time.monotonic):
        self.max, self.window, self.clock = max_wakes, window_seconds, clock
        self.hits = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.hits and now - self.hits[0] > self.window:
            self.hits.popleft()
        if len(self.hits) >= self.max:
            return False
        self.hits.append(now)
        return True


def ADAPTERS():
    from .gmail_imap import GmailImap
    from .local import LocalHttp, LocalSpool
    from .stubs import GmailApi
    return {"local_spool": LocalSpool, "local_http": LocalHttp, "gmail_imap": GmailImap, "gmail_api": GmailApi}


def get_channels(cfg: dict) -> list:
    adapters = ADAPTERS()
    out = []
    for name in cfg["doorbell"]["channels"]:
        if name not in adapters:
            raise ValueError(f"unknown doorbell channel {name!r}; known: {sorted(adapters)}")
        out.append(adapters[name](cfg))
    return out
