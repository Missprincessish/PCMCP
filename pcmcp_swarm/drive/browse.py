"""Read-only, indexed folder browsing for trusted PCMCP text sender.
Only explicitly listed roots are exposed. Child traversal uses directory indexes,
not arbitrary paths. No file contents are opened.
"""
import os
import re
from pathlib import Path
import zipfile
from datetime import datetime

ROOTS = {
    "downloads": Path.home() / "Downloads",
    "documents": Path.home() / "Documents",
    "rescue": Path.home() / "iCloud_Rescue_2026-11-06",
    "icloud": Path.home() / "Library/Mobile Documents/com~apple~CloudDocs",
}
MAX_DEPTH = 8
MAX_ROWS = 150

def _dirs(path):
    return sorted((x for x in path.iterdir() if x.is_dir() and not x.is_symlink() and not x.name.startswith(".")), key=lambda x:x.name.casefold())

def run(token, output):
    if token.startswith("files."):
        parts = token[6:].split(".")
        page = 0
        if parts[-1].startswith("p") and parts[-1][1:].isdigit():
            page = int(parts.pop()[1:])
        folder_token = ".".join(parts)
        if page > 10000: raise ValueError("page out of range")
        folder = _resolve_folder(folder_token)
        files = sorted((x for x in folder.iterdir() if x.is_file() and not x.is_symlink() and not x.name.startswith(".")), key=lambda x:x.name.casefold())
        # Stable 20-file pages keep each response comfortably below iMessage's limit.
        per_page = 20
        start = page * per_page
        if start >= len(files) and page: raise ValueError("page out of range")
        lines = [f"FILES {folder_token}: {len(files)} total; page {page+1}"]
        for i in range(start, min(start+per_page, len(files))):
            f = files[i]
            lines.append(f"{i}: {f.name} [{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d}]")
        if start+per_page < len(files):
            lines.append(f"NEXT: PCMCP browse files.{folder_token}.p{page+1}")
        output.write_text("\n".join(lines)+"\n", encoding="utf-8")
        return f"{len(files)} files, page {page+1}"
    if token.startswith("find."):
        parts = token.split(".")
        if len(parts) != 3 or parts[1] not in ROOTS or not parts[2].isalnum():
            raise ValueError("use find.<downloads|documents|rescue|icloud>.<keyword>")
        root = ROOTS[parts[1]].resolve(strict=True)
        matches = []
        import os
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(current)/d).is_symlink()]
            for name in dirs + files:
                if parts[2].casefold() not in name.casefold(): continue
                item = Path(current)/name
                if item.is_symlink(): continue
                try:
                    st = item.stat()
                    matches.append(f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d} {st.st_size}B {item.relative_to(root)}")
                except OSError: continue
                if len(matches) >= 200: break
            if len(matches) >= 200: break
        lines = [f"Matches for {parts[2]} in {parts[1]} (up to 200):", *matches]
        output.write_text("\n".join(lines)+"\n", encoding="utf-8")
        return f"{len(matches)} matches"
    if token.startswith("zip."):
        parts = token[4:].split(".")
        if len(parts) < 2 or parts[0] not in ROOTS or len(parts) > MAX_DEPTH + 2:
            raise ValueError("zip requires alias and numbered ZIP index")
        zip_idx = parts[-1]
        if not zip_idx.isdigit():
            raise ValueError("ZIP index must be numeric")
        folder_token = ".".join(parts[:-1])
        folder = _resolve_folder(folder_token)
        zips = sorted((x for x in folder.iterdir() if x.is_file() and not x.is_symlink() and x.suffix.lower() == ".zip"), key=lambda x:x.name.casefold())
        if int(zip_idx) >= len(zips):
            raise ValueError("ZIP index not found")
        target = zips[int(zip_idx)]
        with zipfile.ZipFile(target) as z:
            infos = z.infolist()
            lines = [f"ZIP: {target}", f"Entries: {len(infos)}", "Names and uncompressed sizes only; nothing extracted:"]
            lines.extend(f"{i.file_size} bytes  {i.filename}" for i in infos[:1000])
            if len(infos) > 1000: lines.append(f"... {len(infos)-1000} more entries")
        output.write_text("\n".join(lines)+"\n", encoding="utf-8")
        return f"PCMCP ZIP {target.name}: {len(infos)} entries. Read-only listing attached."
    if token == "help":
        msg = "PCMCP browse aliases: " + ", ".join(ROOTS) + ". Send browse <alias>, then browse <alias.0> to open directory 0, etc. Use PCMCP browse findread.<folder-token>.<keyword> to find a unique filename and read its first chunk."
        output.write_text(msg + "\n")
        return msg
    parts = token.split(".")
    if parts[0] not in ROOTS or len(parts)>MAX_DEPTH+1:
        raise ValueError("invalid alias or depth; send browse help")
    root = ROOTS[parts[0]].resolve(strict=True)
    path = root
    for idx in parts[1:]:
        if not idx.isdigit() or len(idx)>5:
            raise ValueError("directory indexes must be numbers")
        dirs = _dirs(path)
        i = int(idx)
        if i >= len(dirs):
            raise ValueError("directory index not found; browse parent again")
        path = dirs[i]
        if not path.resolve().is_relative_to(root):
            raise ValueError("path outside approved root")
    return _list_folder(path, token, output)

TEXT_EXTS = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".html", ".css", ".csv", ".log", ".sh", ".swift", ".c", ".h", ".cpp", ".rs"}
CHUNK_SIZE = 1050

def read_chunk(token):
    # read.<alias>[.<folder-index>...].<file-index>.<chunk-index>
    parts = token.split(".")
    if len(parts) < 3 or parts[0] not in ROOTS or not all(x.isdigit() for x in parts[1:]):
        raise ValueError("use read.<alias>[.folder-index].file-index.chunk-index")
    if len(parts) > MAX_DEPTH + 3:
        raise ValueError("too many directory levels")
    folder = _resolve_folder(".".join(parts[:-2]))
    files = sorted((x for x in folder.iterdir() if x.is_file() and not x.is_symlink() and not x.name.startswith(".")), key=lambda x:x.name.casefold())
    index, chunk = int(parts[-2]), int(parts[-1])
    if index >= len(files) or chunk > 10000:
        raise ValueError("file index or chunk out of range")
    message, total = _file_chunk(files[index], chunk)
    if chunk + 1 < total:
        message += f"\nNEXT: PCMCP read {token.rsplit('.', 1)[0]}.{chunk + 1}"
    return message


def _file_chunk(f, chunk):
    if f.suffix.lower() not in TEXT_EXTS or f.stat().st_size > 2_000_000:
        raise ValueError("only supported text/code files up to 2 MB; no binary or private config")
    if f.name.lower() in {".env", "config.json", "credentials.json", "secrets.json"}:
        raise ValueError("protected configuration file")
    data = f.read_bytes()
    if b"\0" in data[:4096]:
        raise ValueError("binary file refused")
    text = data.decode("utf-8-sig", errors="replace")
    total = max(1, (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE)
    if chunk >= total:
        raise ValueError(f"chunk out of range (0-{total-1})")
    modified = datetime.fromtimestamp(f.stat().st_mtime)
    header = f"{f.name} | chunk {chunk + 1}/{total} | modified {modified:%Y-%m-%d}"
    start = chunk * CHUNK_SIZE
    return header + "\n" + text[start:start + CHUNK_SIZE], total


def find_read(argument):
    """Return a preview only when one visible filename matches in the chosen folder."""
    folder_token, separator, keyword = argument.rpartition(".")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", keyword):
        raise ValueError("use findread.<folder-token>.<filename-keyword>")
    folder = _resolve_folder(folder_token).resolve(strict=True)
    remaining_depth = MAX_DEPTH - folder_token.count(".")
    needle = keyword.casefold()
    match = None

    def on_error(error):
        raise error

    for current, dirs, files in os.walk(folder, onerror=on_error):
        parent = Path(current)
        if len(parent.relative_to(folder).parts) >= remaining_depth:
            dirs.clear()
        else:
            dirs[:] = [name for name in dirs if not name.startswith(".") and not (parent / name).is_symlink()]
        for name in files:
            if name.startswith(".") or needle not in name.casefold():
                continue
            candidate = parent / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if match is not None:
                raise ValueError("Multiple matching files. Narrow the folder or keyword.")
            match = candidate
    if match is None:
        raise ValueError("No matching file in this folder.")
    if match.resolve(strict=True) != match:
        raise ValueError("File changed during search. Retry.")
    preview, _ = _file_chunk(match, 0)
    location = str(match.relative_to(folder))
    if len(location) > 210:
        location = "…" + location[-209:]
    return f"{location}\n{preview}"


def _resolve_folder(token):
    parts = token.split(".")
    if parts[0] not in ROOTS or len(parts) > MAX_DEPTH+1:
        raise ValueError("invalid alias or depth")
    root = ROOTS[parts[0]].resolve(strict=True)
    path = root
    for idx in parts[1:]:
        if not idx.isdigit() or len(idx)>5: raise ValueError("directory indexes must be numbers")
        dirs = _dirs(path)
        if int(idx) >= len(dirs): raise ValueError("directory index not found")
        path = dirs[int(idx)]
        if not path.resolve().is_relative_to(root): raise ValueError("outside approved root")
    return path

def _list_folder(path, token, output):
    lines = [f"Folder: {path}", "Directories (send browse alias.index):"]
    dirs = _dirs(path)
    for i, child in enumerate(dirs[:MAX_ROWS]):
        lines.append(f"DIR {token}.{i} {datetime.fromtimestamp(child.stat().st_mtime):%Y-%m-%d} {child.name}")
    if len(dirs)>MAX_ROWS:
        lines.append(f"... {len(dirs)-MAX_ROWS} additional directories not shown")
    files = sorted((x for x in path.iterdir() if x.is_file() and not x.is_symlink() and not x.name.startswith(".")),key=lambda x:x.name.casefold())
    lines.append(f"Files: {len(files)}; directories: {len(dirs)}")
    zips = sorted((f for f in files if f.suffix.lower() == ".zip"), key=lambda f:f.name.casefold())
    for i, z in enumerate(zips[:MAX_ROWS]):
        lines.append(f"ZIP zip.{token}.{i}  {z.name} (send zip.{token}.{i} for contents)")
    for f in files[:MAX_ROWS]:
        try: size=f.stat().st_size
        except OSError: size=-1
        lines.append(f"FILE {token}.{files.index(f)} {size} bytes {datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d} {f.name}")
    if len(files)>MAX_ROWS:
        lines.append(f"... {len(files)-MAX_ROWS} additional files not shown")
    output.write_text("\n".join(lines)+"\n",encoding="utf-8")
    return f"PCMCP browse {token}: {len(dirs)} folders, {len(files)} files. Listing attached."
