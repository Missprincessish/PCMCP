"""Screenshot adapters: the ONLY per-OS code in the swarm. One file per OS."""
from __future__ import annotations

import sys


def capture(target: dict, out_path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        from .macos import capture as impl
    elif sys.platform.startswith("win"):
        from .windows import capture as impl
    else:
        from .linux import capture as impl
    impl(target, out_path)
