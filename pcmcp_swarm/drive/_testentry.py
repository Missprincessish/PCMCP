"""OFFLINE SELFTEST ENTRY POINT ONLY.  python3 -m pcmcp_swarm.drive._testentry <same args as pcmcp_swarm>

Replaces the owner-key pin location with $PCMCP_TEST_PIN_FILE so the selftest can sign approvals with a
throwaway key. It refuses (raises) for any config that is not the loopback fake Drive with auth "none",
so it can never be used against real Google Drive. Production (`python3 -m pcmcp_swarm`) never reads it.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from . import owner


def _test_pin_location(dcfg: dict) -> tuple:
    host = urllib.parse.urlparse(dcfg.get("api_base", "")).hostname
    test = os.environ.get("PCMCP_TEST_PIN_FILE")
    if not test or dcfg.get("auth") != "none" or host not in ("127.0.0.1", "localhost"):
        raise PermissionError("test pin override refused: only the loopback fake Drive (auth none) may use it")
    return Path(test), False


def _fake_capture(target, out_path) -> None:
    """Selftest only: never capture Angie's real screen; write a tiny valid PNG instead."""
    import base64
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))


def install() -> None:
    owner.pin_location = _test_pin_location
    if os.environ.get("PCMCP_TEST_FAKE_SCREENSHOT"):
        from .. import screenshot
        screenshot.capture = _fake_capture


if __name__ == "__main__":
    install()
    from ..__main__ import main
    main()
