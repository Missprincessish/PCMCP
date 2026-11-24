"""Linux capture using whichever common tool is installed. UNTESTED until run on a Linux VM.
Tries: gnome-screenshot, scrot, ImageMagick `import`. 'region' needs scrot or import.
"""
from __future__ import annotations

import shutil
import subprocess


def capture(target: dict, out_path) -> None:
    out = str(out_path)
    region = target.get("type") == "region"
    if region:
        geo = f"{int(target['w'])}x{int(target['h'])}+{int(target['x'])}+{int(target['y'])}"
        if shutil.which("import"):
            return subprocess.run(["import", "-window", "root", "-crop", geo, out], check=True, timeout=30) and None
        if shutil.which("scrot"):
            a = f"{int(target['x'])},{int(target['y'])},{int(target['w'])},{int(target['h'])}"
            return subprocess.run(["scrot", "-a", a, out], check=True, timeout=30) and None
    for cmd in (["gnome-screenshot", "-f", out], ["scrot", out], ["import", "-window", "root", out]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=True, timeout=30)
            return
    raise RuntimeError("no screenshot tool found (install gnome-screenshot, scrot or imagemagick)")
