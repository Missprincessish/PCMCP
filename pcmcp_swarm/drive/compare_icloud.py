"""Read-only comparison of iCloud entries with Documents and Downloads.

Never opens file contents or trusts iCloud's download-state labels.
"""
from __future__ import annotations
import os
from collections import defaultdict
from pathlib import Path

def _walk(root):
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            yield ("unreadable", str(folder), None, None)
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    name = entry.name
                    if name.startswith(".") and name.endswith(".icloud"):
                        name = name[1:-len(".icloud")]
                    yield ("file", entry.path, name, st.st_size)
            except OSError:
                yield ("unreadable", entry.path, None, None)

def compare(icloud_root, local_roots, output, max_entries=1000000):
    icloud_root = Path(icloud_root).expanduser()
    if not icloud_root.is_dir():
        raise FileNotFoundError(str(icloud_root))
    local_roots = [Path(p).expanduser() for p in local_roots]
    missing_roots = [str(p) for p in local_roots if not p.is_dir()]
    if missing_roots:
        raise FileNotFoundError("Local root missing: " + ", ".join(missing_roots))
    # Name+size is only a candidate match, never a verified identical copy.
    local = defaultdict(list)
    unreadable = 0
    local_count = 0
    for root in local_roots:
        for kind, path, name, size in _walk(root):
            if kind == "unreadable":
                unreadable += 1
            else:
                local[(name.casefold(), size)].append(path)
                local_count += 1
    count = 0
    no_candidate = 0
    candidates = 0
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as out:
        out.write("status\tsize_bytes\ticloud_path\tlocal_candidate_paths\n")
        for kind, path, name, size in _walk(icloud_root):
            if kind == "unreadable":
                unreadable += 1
                continue
            count += 1
            if count > max_entries:
                raise RuntimeError("Inventory limit reached; report incomplete")
            hits = local.get((name.casefold(), size), [])
            status = "CANDIDATE_UNVERIFIED" if hits else "NO_LOCAL_CANDIDATE"
            if hits:
                candidates += 1
            else:
                no_candidate += 1
            clean = lambda s: str(s).replace("\t", " ").replace("\n", " ")
            out.write(f"{status}\t{size}\t{clean(path)}\t{'; '.join(map(clean, hits[:5]))}\n")
    return {"icloud_files": count, "local_files": local_count, "no_local_candidate": no_candidate,
            "candidate_unverified": candidates, "unreadable": unreadable,
            "listing": output.name, "contents_opened": False, "changed_files": False}
