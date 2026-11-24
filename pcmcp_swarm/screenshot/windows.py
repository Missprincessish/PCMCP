"""Windows capture via PowerShell + System.Drawing (built into Windows). UNTESTED until run on a Windows VM.
Supports 'full' and 'region'. 'app' falls back to full screen.
"""
from __future__ import annotations

import subprocess


def capture(target: dict, out_path) -> None:
    if target.get("type") == "region":
        x, y, w, h = (int(target[k]) for k in ("x", "y", "w", "h"))
        rect = f"$b = New-Object System.Drawing.Rectangle({x},{y},{w},{h})"
    else:
        rect = "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds"
    path = str(out_path).replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; " + rect + "; "
          "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
          "$g = [System.Drawing.Graphics]::FromImage($bmp); "
          "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
          f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=30)
