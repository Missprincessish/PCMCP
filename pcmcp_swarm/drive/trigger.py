"""Text trigger watcher: Instinct texts Angie's Mac a secret code + a command; the bots do the work and the
results go back to Instinct by iMessage (a short summary plus attached .txt / .png files).

How the Mac sees the text: it reads the Messages database (~/Library/Messages/chat.db) read-only.

Text commands (one PER LINE: the line must contain ONLY the command; a command quoted inside a
normal sentence is NEVER executed):
    PCMCP-<secret> status                     live swarm status
    PCMCP-<secret> live                       listener health: uptime, poll rate, active jobs, last acted command + age, free disk
    PCMCP-<secret> screensaver                start the Mac screensaver (reversible action; verified launch only)
    PCMCP-<secret> inventory drive            read-only Google Drive inventory (attached .txt listing)
    PCMCP-<secret> inventory missing          compare iCloud with Documents and Downloads (read-only)
    PCMCP-<secret> inventory icloud           look-only iCloud Drive listing: names, sizes, downloaded/cloud-only
    PCMCP-<secret> screenshot                 screenshot of the screen (attached .png)
    PCMCP-<secret> task <name>                one named read-only task from text-tasks.json (attached .txt)
    PCMCP-<secret> browse <alias.index...>   browse approved folders by numbered child directories
    PCMCP-<secret> run <plan-hash-prefix>     start a plan Angie already SIGNED with her passphrase

SAFETY:
  - The secret is a doorbell, not an approval. Nothing a text can reach writes, moves or deletes, except `run`,
    and `run` only executes a plan carrying Angie's passphrase signature (verified against the root-owned pin),
    one-shot per approval, never after her `stop` or a rollback. There is no text command for approve.
  - iCloud is look-only (drive/icloud.py never opens, writes, moves or deletes). Tasks are fixed, read-only
    argv from the repo (drive/tasks.py); nothing from a text is passed to them.
  - Only the SHA-256 of the secret is stored (~/.pcmcp/trigger_code.sha256, mode 0600).
  - Each message is acted on at most once, only if new, incoming, from an allowed sender, and within the rate
    limit. Old texts are never replayed. Replies go only to allowed senders and never contain the code.
  - Everything is logged: logs/text_triggers.jsonl plus one folder per request (request, command record,
    outputs, reply) under data/text_requests/. Unrelated texts are never logged.
Permissions: Full Disk Access (read chat.db, list iCloud), Automation -> Messages (replies + attachments),
Screen Recording (screenshot).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import shutil
import threading
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import runs
from ..util import append_jsonl, atomic_write_json, now_iso, read_json, read_jsonl

CODE_RE = re.compile(rb"PCMCP-([A-Za-z0-9]{24,64})[ \t]+(status|inventory|screenshot|screensaver|live|task|browse|run)\b(?:[ \t]+([A-Za-z0-9_.\-]{1,64}))?[ \t]*[.!?]?", re.I)
READONLY_RE = re.compile(rb"PCMCP[ \t]+(status|inventory|screenshot|screensaver|live|task|browse|read)\b(?:[ \t]+([A-Za-z0-9_.\-]{1,64}))?[ \t]*[.!?]?", re.I)
APPLE_EPOCH = 978307200          # 2001-01-01 in Unix seconds
SAFE_COMMANDS = ("status", "inventory", "screenshot", "screensaver", "live", "task", "browse", "read", "run", "experiment")
HEX_RE = re.compile(r"^[0-9a-f]{8,64}$")


def defaults(base: Path) -> dict:
    return {"chat_db": str(Path.home() / "Library" / "Messages" / "chat.db"),
            "code_hash_file": str(base / "trigger_code.sha256"),
            "state_file": str(base / "data" / "state" / "trigger_watch.json"),
            "brain_name": "Instinct",        # label for the AI that texts commands (any AI can be the brain)
            "allowed_senders": [],           # REQUIRED, e.g. ["+15551234567"]; the watcher refuses to start without one
            "poll_seconds": 5, "max_age_seconds": 900, "rate_limit": {"max": 6, "window_seconds": 600},
            # Results back to Instinct: "messages" = iMessage/SMS reply via the Messages app; "none" = off;
            # "file" = append to reply_file (offline tests).
            "reply": "messages", "reply_file": str(base / "data" / "logs" / "text_replies.jsonl"),
            "icloud_root": str(Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"),
            "requests_dir": str(base / "data" / "text_requests"),
            # Messages can only attach files it can read; attachments are copied here first.
            "attach_staging_dir": str(Path.home() / "Pictures" / "PCMCP Outbox"),
            "attach_max_bytes": 50 * 1024 * 1024}


# ---------------------------------------------------------------- replies to Instinct

_SEND_TEXT_SCRIPT = '''on run argv
  set who to item 1 of argv
  set msg to item 2 of argv
  tell application "Messages"
    try
      set b to participant who of (1st account whose service type = iMessage)
    on error
      set b to participant who of (1st account whose service type = SMS)
    end try
    send msg to b
  end tell
end run'''

_SEND_FILE_SCRIPT = '''on run argv
  set who to item 1 of argv
  set thePath to item 2 of argv
  set theFile to POSIX file thePath as alias
  tell application "Messages"
    try
      set b to participant who of (1st account whose service type = iMessage)
    on error
      set b to participant who of (1st account whose service type = SMS)
    end try
    send theFile to b
  end tell
end run'''


def _attachment_recorded(t: dict, staged_name: str, from_unix: float = 0.0):
    """Was THIS staged file actually recorded as an outgoing attachment in the Messages database?
    Returns True/False, or None if we cannot verify (db unreadable). AppleScript success is NOT
    delivery; chat.db is the honest witness."""
    try:
        con = _open_db(t["chat_db"])
    except Exception:
        return None
    try:
        cut_unix = from_unix or (time.time() - 120)
        cut_apple_ns = (cut_unix - APPLE_EPOCH) * 1e9
        q = ("SELECT COUNT(*) FROM message m JOIN message_attachment_join maj ON maj.message_id = m.ROWID "
             "JOIN attachment a ON a.ROWID = maj.attachment_id "
             "WHERE m.is_from_me = 1 AND m.date > ? AND a.filename LIKE ?")
        n = con.execute(q, (cut_apple_ns, "%" + staged_name)).fetchone()[0]
        return n > 0
    except sqlite3.Error:
        return None
    finally:
        con.close()


def send_reply(t: dict, to: str, msg: str, attachments: list | None = None) -> dict:
    """Send a result back to Instinct: a short text plus optional files. Arguments go to osascript as argv
    (never pasted into the script). Files are copied to a staging folder Messages can read."""
    how = t.get("reply", "messages")
    allowed = {x.strip().lower() for x in t.get("allowed_senders", []) if x.strip()}
    if (to or "").lower() not in allowed:
        return {"sent": False, "why": "recipient is not in allowed_senders"}
    if how == "none" or not to:
        return {"sent": False, "why": "replies off" if how == "none" else "no sender"}
    msg = msg[:1500]
    files, skipped = [], []
    for f in attachments or []:
        f = Path(f)
        if not f.is_file():
            continue
        if f.stat().st_size > int(t["attach_max_bytes"]):
            skipped.append(f.name)
            continue
        files.append(f)
    if skipped:
        msg += f" (not attached, too large: {', '.join(skipped)}; kept on the Mac)"
    if how == "file":
        append_jsonl(Path(t["reply_file"]).expanduser(), {"at": now_iso(), "to": to, "text": msg,
                                                          "attachments": [str(f) for f in files]})
        return {"sent": True, "via": "file", "attachments": [f.name for f in files]}
    # Messages mode: send each staged FILE separately first (capturing per-file failures and
    # timeouts), then send the explanatory text. A failed attachment must never prevent the
    # text reply, and the text must never claim a file that was not sent. osascript success is
    # Messages *accepting* the send; actual arrival is only proven when the file shows up in chat.
    staged = []
    if files:
        sd = Path(t["attach_staging_dir"]).expanduser() / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        sd.mkdir(parents=True, exist_ok=True)
        for f in files:
            staged.append((str(shutil.copy2(f, sd / f.name)), sd))
    sent_files, failed, unverified = [], [], []
    for s, _ in staged:
        name = Path(s).name
        ok, why = False, None
        for attempt in (1, 2):                     # one bounded retry with the same alias-based script
            try:
                r = subprocess.run(["osascript", "-e", _SEND_FILE_SCRIPT, to, s], capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                why = "Messages send timed out after 120 s"
                continue
            except OSError as e:
                why = f"{type(e).__name__}"
                continue
            err = (r.stderr or "").strip()
            if r.returncode != 0 or err:
                why = err[:200] or f"osascript exited {r.returncode}"
                continue
            time.sleep(2 if attempt == 1 else 3)    # give Messages a moment to record the attachment
            seen = _attachment_recorded(t, name)
            if seen is True:
                ok = True
                break
            if seen is None:
                ok = True                            # could not verify; do not falsely claim failure either
                unverified.append(name)
                why = "sent (unverified: chat.db not readable by the watcher)"
                break
            why = (f"Messages reported the send as fine but no {name} attachment is recorded in chat.db; "
                   "NOT delivered")
        if ok:
            sent_files.append(name)
        else:
            failed.append((name, why or "send failed"))
    if failed:
        msg += ("; " if msg else "") + ("attachments NOT delivered: " + "; ".join(f"{n} ({why})" for n, why in failed)
                                        + "; copies kept on the Mac")
    if unverified:
        msg += ("; " if msg else "") + ("sent but UNVERIFIED (chat.db not readable): " + ", ".join(unverified))
    try:
        r = subprocess.run(["osascript", "-e", _SEND_TEXT_SCRIPT, to, msg], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"sent": False, "why": "text reply timed out; files may have been delivered already",
                "attachments": sent_files, "attachments_failed": [n for n, _ in failed]}
    except OSError as e:
        return {"sent": False, "why": f"text reply failed: {type(e).__name__}",
                "attachments": sent_files, "attachments_failed": [n for n, _ in failed]}
    if r.returncode != 0:
        return {"sent": False, "why": (r.stderr or "").strip()[:300] or "osascript failed",
                "hint": "allow Automation: System Settings > Privacy & Security > Automation > (your terminal) > Messages",
                "attachments": sent_files, "attachments_failed": [n for n, _ in failed]}
    out = {"sent": True, "via": "messages", "attachments": sent_files}
    if failed:
        out["attachments_failed"] = [n for n, _ in failed]
    return out


def status_text(st: dict | None, run_name: str, jobs: list | None = None) -> str:
    extra = ""
    if jobs:
        extra = " Text jobs running: " + "; ".join(jobs) + "."
    if not st:
        return f"PCMCP status: run {run_name} - nothing has run in it yet.{extra}"
    if st.get("mode") == "inventory":
        lead = st["leads"]["I"]
        return (f"PCMCP status: run {st['run']} inventory {st['state'].upper()} - {st['done']} items listed from "
                f"{lead['chunks']['done']} folders, {lead['chunks']['pending']} waiting, {st['bots_active']} bots active, "
                f"{st.get('per_minute_last_60s', 0)}/min, errors {st['errors']}, {st['elapsed_seconds']} s.{extra}")
    r = st.get("retries", {})
    return (f"PCMCP status: run {st['run']} {st['mode']} {st['state'].upper()} - {st['done']}/{st['planned']} done, "
            f"{st['bots_active']} bots active, {st.get('per_minute_last_60s', 0)}/min, errors {st['errors']}, "
            f"retries {r.get('rate_limit', 0)}+{r.get('transient', 0)}.{extra}")


def summary_text(sm: dict | None, run_name: str, exit_code: int) -> str:
    if not sm:
        return f"PCMCP run {run_name} ended (exit {exit_code}) without a summary; see the run folder."
    return (f"PCMCP run {sm['run']} {sm['state'].upper()}: {sm['operations_ok']}/{sm['operations_planned']} operations, "
            f"{sm['files_moved']} moved, {sm['folders_created']} folders created, errors {sm['errors']}, "
            f"{sm.get('average_moves_per_minute')} moves/min, {sm['wall_seconds']} s. Receipts chain "
            f"{'OK' if sm.get('receipt_chain_ok') else 'BROKEN'}. Evidence: run folder {sm['run']}.")


def tcfg(cfg: dict) -> dict:
    base = Path(cfg["_path"]).expanduser().parent
    out = defaults(base)
    out.update((cfg.get("drive") or {}).get("trigger") or {})
    return out


def setup(t: dict) -> str:
    """OWNER: make a new secret code. Prints it once; stores only its hash."""
    code = secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:32]
    p = Path(t["code_hash_file"]).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(hashlib.sha256(code.encode()).hexdigest() + "\n")
    os.replace(str(p) + ".tmp", p)
    return f"PCMCP-{code}"


def _code_hash(t: dict) -> str:
    p = Path(t["code_hash_file"]).expanduser()
    if not p.exists():
        raise FileNotFoundError("no trigger code yet: Angie runs `python3 -m pcmcp_swarm drive trigger-setup`")
    return p.read_text().strip()


def parse(blob: bytes, code_hash: str):
    """Find a valid trigger in a message's bytes (text or attributedBody). Returns (command, arg) or None.
    The secret is compared by hash, in constant time.
    A trigger only counts when ONE LINE of the message is EXACTLY the command (optional trailing
    punctuation allowed). A command QUOTED INSIDE A NORMAL SENTENCE IS NEVER EXECUTED."""
    for line in (blob or b"").splitlines():
        ls = line.strip()
        if not ls.upper().startswith(b"PCMCP-"):
            continue
        m = CODE_RE.fullmatch(ls)
        if m and secrets.compare_digest(hashlib.sha256(m.group(1)).hexdigest(), code_hash):
            cmd = m.group(2).decode().lower()
            arg = m.group(3).decode().lower() if m.group(3) and cmd not in ("status", "screenshot", "screensaver", "live") else None
            return cmd, arg
    return None


def _open_db(path: str):
    try:
        return sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        raise PermissionError(f"cannot read {path} ({e}). Grant Full Disk Access to the app running the watcher "
                              "(System Settings > Privacy & Security > Full Disk Access), then restart it.") from None


def _rows(con, after: int):
    q = ("SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date, h.id, m.service, "
         "(SELECT GROUP_CONCAT(DISTINCT c.service_name) FROM chat c "
         "JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID WHERE cmj.message_id = m.ROWID) "
         "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID WHERE m.ROWID > ? ORDER BY m.ROWID")
    return con.execute(q, (after,)).fetchall()


def _msg_time(apple_date) -> float:
    d = float(apple_date or 0)
    if d > 1e12:                     # nanoseconds since 2001 (modern macOS)
        d /= 1e9
    return d + APPLE_EPOCH


class Watcher:
    def __init__(self, cfg: dict, dcfg: dict, t: dict, log_path: Path, cli_module: str | None = None):
        self.cfg, self.dcfg, self.t = cfg, dcfg, t
        self.log_path = log_path
        self.code_hash = _code_hash(t)
        self.state_path = Path(t["state_file"]).expanduser()
        self.cli_module = cli_module or _current_cli_module()
        self.allowed = {s.strip().lower() for s in t.get("allowed_senders", []) if s.strip()}
        self.brain = str(t.get("brain_name") or "Instinct")     # PCMCP works with any AI; this only labels the evidence
        if not self.allowed:   # SAFETY: fail closed; never answer (or attach files to) an unknown number
            raise ValueError("drive.trigger.allowed_senders is empty; add Instinct's number to ~/.pcmcp/config.json first")
        self.running = []
        self.started_at = time.time()   # listener uptime reference (resets on each watcher restart)

    def _log(self, **f):
        append_jsonl(self.log_path, {"at": now_iso(), **f})

    def start_mark(self) -> int:
        st = read_json(self.state_path, {}) or {}
        if "last_rowid" in st:
            return int(st["last_rowid"])
        con = _open_db(self.t["chat_db"])
        mx = con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
        con.close()
        self._save(mx)                      # first start: never act on texts that arrived before the watcher
        return mx

    def _save(self, rowid: int):
        atomic_write_json(self.state_path, {"last_rowid": rowid, "updated_at": now_iso()})

    def _rate_ok(self) -> bool:
        rl = self.t["rate_limit"]
        cut = time.time() - rl["window_seconds"]
        recent = [r for r in read_jsonl(self.log_path) if r.get("acted") and r.get("unix", 0) >= cut]
        return len(recent) < rl["max"]

    def poll_once(self) -> list:
        last = self.start_mark()
        con = _open_db(self.t["chat_db"])
        try:
            rows = _rows(con, last)
        finally:
            con.close()
        done = []
        for rowid, text, body, from_me, date, sender, service, chat_services in rows:
            blob = (text or "").encode() + b"\n" + (body or b"")
            hit = parse(blob, self.code_hash)
            if not hit and service == "iMessage" and chat_services == "iMessage":
                # Only the explicitly allowlisted sender in a verified iMessage chat.
                # Never permit signed-plan execution without the original secret.
                if not from_me and (sender or "").strip().lower() in self.allowed:
                    command_text = (text or "").strip()
                    prefix = "PCMCP experiment "
                    if command_text.startswith(prefix):
                        hit = ("experiment", command_text[len(prefix):])
                    elif not command_text and prefix.encode() in (body or b""):
                        tail = body.split(prefix.encode(), 1)[1][:18000].decode("utf-8", errors="replace")
                        from .language_experiment import extract
                        hit = ("experiment", extract(tail))
                    else:
                        # Code-free command: the ENTIRE message must be the command (one line,
                        # optional trailing punctuation). Mentioning "PCMCP status" inside a
                        # normal sentence is chat, not a command.
                        candidates = [command_text] if command_text else (
                            body.decode("utf-8", errors="replace").strip().splitlines()
                            if (body or b"").startswith(b"PCMCP") else [])
                        for cand in candidates:
                            m = READONLY_RE.fullmatch(cand.strip().encode("utf-8", errors="replace"))
                            if m:
                                cmd = m.group(1).decode().lower()
                                arg = m.group(2).decode().lower() if m.group(2) and cmd not in ("status", "screenshot", "screensaver", "live") else None
                                hit = (cmd, arg)
                                break
            if hit:
                done.append(self._handle(rowid, hit, from_me, date, sender))
            self._save(rowid)               # each message is considered once, hit or not
        return done

    def _handle(self, rowid, hit, from_me, date, sender) -> dict:
        cmd, arg = hit
        base = {"rowid": rowid, "sender": sender, "command": cmd, "arg": arg, "unix": time.time()}
        age = time.time() - _msg_time(date)
        why = None
        if from_me:
            why = "sent from this account (only incoming texts count)"
        elif (sender or "").lower() not in self.allowed:
            why = f"sender {sender!r} is not in drive.trigger.allowed_senders"
        elif age > self.t["max_age_seconds"]:
            why = f"text is {int(age)} s old (max {self.t['max_age_seconds']})"
        elif cmd not in SAFE_COMMANDS:
            why = "not a safe command"
        elif cmd == "run" and not (arg and HEX_RE.match(arg)):
            why = "run needs a plan hash prefix (8+ hex characters)"
        elif cmd == "inventory" and (arg or "drive") not in ("drive", "icloud", "missing"):
            why = "inventory takes drive, icloud or missing"
        elif cmd == "read" and not arg:
            why = "read needs indexed file and chunk (try browse rescue)"
        elif cmd == "browse" and not arg:
            why = "browse needs an alias (try browse help)"
        elif cmd == "task" and not arg:
            why = "task needs a task name (see text-tasks.json)"
        elif cmd == "run" and not self._rate_ok():
            why = "rate limit reached"
        if why:
            rec = dict(base, acted=False, refused=why)
            # reply only to a trusted sender; never confirm anything to an unknown number
            if not from_me and (sender or "").lower() in self.allowed:
                rec["reply"] = send_reply(self.t, sender, f"PCMCP refused {cmd}: {why}")
            self._log(**rec)
            return rec
        req = self._request_dir(base)
        base["request_dir"] = req.name
        rec = self._act(base, req)
        if not rec.get("acted"):
            rec["reply"] = send_reply(self.t, sender, f"PCMCP refused {cmd}: {rec.get('refused')}")
        else:
            rec["reply"] = send_reply(self.t, sender, rec.pop("_reply_text"), rec.pop("_attach", []))
        clean = {k: v for k, v in rec.items() if not k.startswith("_")}
        runs.write_json(req / "reply.json", {"at": now_iso(), "reply": clean.get("reply"), "acted": clean.get("acted"),
                                              "refused": clean.get("refused")})
        self._log(**clean)
        return rec

    def live_text(self) -> dict:
        """Listener health snapshot. Age of the last acted command comes from the logged ISO stamp;
        if the log entry carries no timestamp we say so instead of inventing an age."""
        up = max(0, int(time.time() - self.started_at))
        jobs = [j["kind"] for j in self.running]
        last, age_txt = None, "time not recorded"
        for r in read_jsonl(self.log_path):
            if r.get("acted"):
                last = r
        if last:
            at = last.get("at")
            if at:
                try:
                    t0 = _dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                    age_txt = f"{max(0, int((time.time() - t0.timestamp())))} s ago"
                except ValueError:
                    age_txt = "time not recorded (unparseable stamp)"
            last_txt = f"{last.get('command')} {last.get('arg') or ''}".strip() + f" (text #{last.get('rowid')})"
        else:
            last_txt = "none"
        free = shutil.disk_usage(str(Path.home())).free
        text = (f"PCMCP live: listener up {up} s, polling every {self.t['poll_seconds']} s, "
                f"active text jobs: {len(jobs)}" + (f" ({', '.join(sorted(set(jobs)))})" if jobs else "") + ", "
                f"last acted command: {last_txt} {age_txt}, free disk: {free / 1e9:.1f} GB.")
        return {"uptime_seconds": up, "poll_seconds": self.t["poll_seconds"], "active_jobs": jobs,
                "last_acted": last_txt, "last_acted_age": age_txt, "free_disk_bytes": free, "text": text}

    def _request_dir(self, base) -> Path:
        """One evidence folder per coded text: request, command record, outputs, reply."""
        d = Path(self.t["requests_dir"]).expanduser() / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-text{base['rowid']}"
        d.mkdir(parents=True, exist_ok=True)
        runs.write_json(d / "request.json", {k: base[k] for k in ("rowid", "sender", "command", "arg")} | {"received_at": now_iso()})
        runs.record_command(d, Path(self.cfg["data_dir"]).expanduser() / "logs" / "text_commands.jsonl",
                            f"text_{base['command']}", ["text", base["command"], base["arg"] or ""],
                            f"{self.brain} via text #{base['rowid']}")
        return d

    def _cli(self) -> list:
        argv = [sys.executable, "-m", self.cli_module]
        if self.cfg.get("_path"):
            argv += ["--config", self.cfg["_path"]]
        return argv

    def _act(self, base, req: Path) -> dict:
        cmd, arg = base["command"], base["arg"]
        issuer = f"{self.brain} via text #{base['rowid']}"
        if cmd == "screenshot":
            from .. import screenshot
            out = req / "screenshot.png"
            try:
                screenshot.capture({"type": "full"}, out)
            except Exception as e:
                return dict(base, acted=False, refused=f"screenshot failed ({type(e).__name__}); grant Screen Recording to the watcher's app")
            if not out.exists():
                return dict(base, acted=False, refused="screenshot produced no file; grant Screen Recording to the watcher's app")
            return dict(base, acted=True, output=out.name, _reply_text="PCMCP screenshot attached.", _attach=[out])
        if cmd == "screensaver":
            # Bounded launch; a good open() means 'starting', not proof it is on screen.
            try:
                r = subprocess.run(["open", "-a", "/System/Library/CoreServices/ScreenSaverEngine.app"],
                                   capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                return dict(base, acted=False, refused="screensaver launch timed out (open did not return in 10 s)")
            except OSError as e:
                return dict(base, acted=False, refused=f"screensaver launch failed: {type(e).__name__}: {e}"[:250])
            if r.returncode != 0:
                why = (r.stderr or "").strip()[:200] or f"open exited {r.returncode}"
                return dict(base, acted=False, refused=f"screensaver launch failed: {why}")
            return dict(base, acted=True, _reply_text="PCMCP screensaver starting. (launch ok; actual display not verified from here)")
        if cmd == "live":
            snap = self.live_text()
            runs.write_json(req / "live.json", {k: v for k, v in snap.items() if k != "text"})
            return dict(base, acted=True, _reply_text=snap["text"])
        if cmd == "read":
            from . import browse
            try:
                message = browse.read_chunk(arg)
            except (ValueError, OSError, UnicodeError) as e:
                return dict(base, acted=False, refused=str(e)[:250])
            return dict(base, acted=True, _reply_text=message, _attach=[])
        if cmd == "browse":
            from . import browse
            try:
                if arg.startswith("findread."):
                    reply = browse.find_read(arg.removeprefix("findread."))
                    return dict(base, acted=True, _reply_text=reply, _attach=[])
                result = browse.run(arg, req / "browse.txt")
            except (ValueError, OSError) as e:
                return dict(base, acted=False, refused=str(e)[:250])
            listing = (req / "browse.txt").read_text(encoding="utf-8")
            # Send numbered entries directly in the iMessage text. Keep the full
            # listing on disk for long directories, but do not rely on attachments.
            if len(listing) > 1450:
                listing = listing[:1400].rsplit("\n", 1)[0] + "\n...more entries not shown (local listing saved)"
            return dict(base, acted=True, output="browse.txt", _reply_text=listing, _attach=[])
        if cmd == "experiment":
            from .language_experiment import validate
            try:
                cases = validate(arg)
            except ValueError as exc:
                return dict(base, acted=False, refused=str(exc))
            if any(job["kind"] == "language_experiment" for job in self.running):
                return dict(base, acted=False, refused="a language experiment is already running")
            spec = req / "language_cases.json"
            runs.write_json(spec, cases)
            log = (req / "language_results.txt").open("w", encoding="utf-8")
            runner = Path(__file__).with_name("language_experiment.py")
            try:
                proc = subprocess.Popen([sys.executable, "-B", str(runner), str(spec)],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            except OSError as exc:
                log.close()
                return dict(base, acted=False, refused=str(exc)[:250])
            self.running.append({"kind": "language_experiment", "rowid": base["rowid"], "to": base["sender"],
                                 "req": req, "proc": proc, "log": log})
            return dict(base, acted=True, _reply_text="PCMCP started the language experiment. Results will follow.")
        if cmd == "task" and arg == "language_baseline":
            if any(job["kind"] == arg for job in self.running):
                return dict(base, acted=False, refused="language baseline already running")
            log = (req / "language_baseline.txt").open("w", encoding="utf-8")
            runner = Path(__file__).with_name("language_baseline.py")
            try:
                proc = subprocess.Popen([sys.executable, "-B", str(runner)],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            except OSError as exc:
                log.close()
                return dict(base, acted=False, refused=str(exc)[:250])
            self.running.append({"kind": arg, "rowid": base["rowid"], "to": base["sender"],
                                 "req": req, "proc": proc, "log": log})
            return dict(base, acted=True, _reply_text="PCMCP started both language baselines. Results will follow.")
        if cmd == "task":
            from . import tasks
            known = tasks.load()
            t = known["tasks"].get(arg)
            if not t:
                return dict(base, acted=False, refused=f"unknown task {arg!r}; allowed: {', '.join(sorted(known['tasks']))}")
            res = tasks.run(t, req / f"task-{arg}.txt")
            msg = f"PCMCP task {arg} (exit {res['exit']}, {res['lines']} lines):\n{res['first_lines']}"
            return dict(base, acted=True, task=arg, exit=res["exit"], output=res["output_file"], _reply_text=msg,
                        _attach=[req / res["output_file"]])
        if cmd == "inventory" and arg == "missing":
            from . import compare_icloud
            job = {"kind": "inventory_missing", "to": base["sender"], "rowid": base["rowid"],
                   "req": req, "result": {}, "started": time.time()}
            def work():
                try:
                    home = Path.home()
                    job["result"] = compare_icloud.compare(
                        Path(self.t["icloud_root"]), [home / "Documents", home / "Downloads"],
                        req / "icloud_local_comparison.tsv")
                except Exception as e:
                    job["result"] = {"error": f"{type(e).__name__}: {e}"}
            job["thread"] = threading.Thread(target=work, daemon=True)
            job["thread"].start()
            self.running.append(job)
            return dict(base, acted=True, _reply_text="PCMCP iCloud/local comparison started (read-only). Results will follow.")
        if cmd == "inventory" and (arg or "drive") == "icloud":
            from . import icloud
            job = {"kind": "inventory_icloud", "to": base["sender"], "rowid": base["rowid"], "req": req, "result": {},
                   "started": time.time()}

            def work():
                try:
                    job["result"] = icloud.inventory(Path(self.t["icloud_root"]), req / "icloud_inventory.txt")
                except Exception as e:
                    job["result"] = {"error": f"{type(e).__name__}: {e}"}
            job["thread"] = threading.Thread(target=work, daemon=True)
            job["thread"].start()
            self.running.append(job)
            return dict(base, acted=True, _reply_text="PCMCP iCloud inventory started (look-only). I will text the listing.")
        if cmd == "inventory":
            log = open(req / "inventory.log", "w")
            p = subprocess.Popen(self._cli() + ["drive", "inventory", "--root", "root", "--issued-by", issuer],
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.running.append({"kind": "inventory_drive", "proc": p, "log": log, "to": base["sender"],
                                 "rowid": base["rowid"], "req": req, "started": time.time()})
            return dict(base, acted=True, pid=p.pid, _reply_text="PCMCP Drive inventory started (read-only). I will text the listing.")
        try:
            run_dir = runs.find_run(self.dcfg["runs_dir"], "latest")
        except FileNotFoundError as e:
            return dict(base, acted=False, refused=str(e))
        if cmd == "status":
            st = read_json(run_dir / "status.json")
            (req / "status.json").write_text(__import__("json").dumps(st or {}, indent=2))
            jobs = [f"{j['kind'].replace('_', ' ')} (text #{j['rowid']}, {int(time.time() - j.get('started', time.time()))} s)"
                    for j in self.running]
            return dict(base, acted=True, run=run_dir.name, _reply_text=status_text(st, run_dir.name, jobs))
        plans = [p for p in run_dir.glob("plan-*.json") if p.stem[5:].startswith(arg[:12]) or arg.startswith(p.stem[5:])]
        if len(plans) != 1:
            return dict(base, acted=False, refused=f"plan prefix {arg} matched {len(plans)} plans in run {run_dir.name} "
                                                   "(the plan must have been validated in this run first)")
        # A text never overrides Angie: not after a stop, not after a rollback, and one run per approval.
        from . import owner, planfile
        h = planfile.plan_hash(planfile.load_plan(plans[0]))
        st = read_json(run_dir / "status.json", {}) or {}
        used_path = run_dir / "text_used_approvals.jsonl"
        why = None
        if st.get("mode") == "run" and st.get("state") in ("running", "paused", "starting"):
            why = "a run is already in progress"
        elif runs.control_state(run_dir) == "stop":
            why = "Angie stopped this run; only she can resume it"
        elif (run_dir / "rollback_receipts.jsonl").exists() or (run_dir / "rollback_summary.json").exists():
            why = "this run was rolled back; a new inventory, plan and approval are needed"
        else:
            appr = owner.verify_approval(self.dcfg, run_dir, h)
            if not appr:
                why = f"plan {h[:12]} has no valid owner signature for this run (Angie must approve it with her passphrase)"
            elif any(u.get("signature_file") == appr["signature_file"] for u in read_jsonl(used_path)):
                why = "that approval was already used by a texted run; Angie must approve again"
            elif (read_json(run_dir / "summary.json", {}) or {}).get("plan_hash") == h:
                why = "this plan already ran in this run folder"
        if why:
            return dict(base, acted=False, refused=why)
        append_jsonl(used_path, {"at": now_iso(), "rowid": base["rowid"], "plan_hash": h,
                                 "signature_file": appr["signature_file"], "approved_at": appr["approved_at"]})
        log = open(run_dir / f"text-trigger-{base['rowid']}.log", "w")
        p = subprocess.Popen(self._cli() + ["drive", "run", "--plan", str(plans[0]), "--run", run_dir.name,
                                            "--no-clear-stop", "--issued-by", issuer],
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.running.append({"kind": "run", "proc": p, "run_dir": run_dir, "to": base["sender"], "rowid": base["rowid"],
                             "log": log, "req": req, "started": time.time()})
        return dict(base, acted=True, run=run_dir.name, plan_file=plans[0].name, pid=p.pid,
                    _reply_text=f"PCMCP run started for plan {plans[0].stem[5:13]} in run {run_dir.name}. I will text the result.")

    def check_finished(self) -> list:
        """When a texted job ends, send Instinct the result (summary + attached files)."""
        out, still = [], []
        for job in self.running:
            if job["kind"] in ("inventory_icloud", "inventory_missing"):
                if job["thread"].is_alive():
                    still.append(job)
                    continue
                res, code = job["result"], 0
                if "error" in res:
                    msg, files = f"PCMCP iCloud inventory failed: {res['error']}", []
                elif job["kind"] == "inventory_missing":
                    msg = (f"PCMCP iCloud/local comparison: {res['icloud_files']} iCloud files; "
                           f"{res['no_local_candidate']} with NO local name+size match; "
                           f"{res['candidate_unverified']} possible local matches (NOT verified identical); "
                           f"{res['unreadable']} unreadable locations. Google Drive check needed. Report attached.")
                    files = [job["req"] / res["listing"]]
                    runs.write_json(job["req"] / "icloud_comparison_summary.json", res)
                else:
                    c = res["counts"]
                    msg = (f"PCMCP iCloud inventory (look-only): {c['files']} files, {c['folders']} folders, "
                           f"{c['bytes_total'] / 1e9:.2f} GB; {c['downloaded']} downloaded, {c['cloud_only']} cloud-only"
                           + (", listing truncated" if res["truncated"] else "") + ". Listing attached.")
                    files = [job["req"] / res["listing"]]
                    runs.write_json(job["req"] / "icloud_summary.json", res)
            else:
                code = job["proc"].poll()
                if code is None:
                    still.append(job)
                    continue
                job["log"].close()
                msg, files = self._finish_proc(job, code)
            if job["kind"] == "language_experiment":
                chunks = [msg[i:i + 1300] for i in range(0, len(msg), 1300)]
                receipts = [send_reply(self.t, job["to"],
                                       f"Language experiment {job['rowid']} ({i}/{len(chunks)})\n{chunk}")
                            for i, chunk in enumerate(chunks, 1)]
                reply = {"sent": all(part["sent"] for part in receipts), "parts": receipts}
            else:
                reply = send_reply(self.t, job["to"], msg, files)
            rec = {"rowid": job["rowid"], "event": f"{job['kind']}_finished", "exit": code, "request_dir": job["req"].name,
                   "reply": reply}
            runs.write_json(job["req"] / "reply_final.json", rec)
            self._log(**rec)
            out.append(rec)
        self.running = still
        return out

    def _finish_proc(self, job, code):
        if job["kind"] == "language_experiment":
            log = (job["req"] / "language_results.txt").read_text(errors="replace")
            lines = [line for line in log.splitlines() if line.startswith("CASE ")]
            summary = "\n".join(lines) or log or "No case output was produced."
            return f"PCMCP language experiment finished (exit {code}).\n{summary}", []
        if job["kind"] == "language_baseline":
            lines = (job["req"] / "language_baseline.txt").read_text(errors="replace").splitlines()
            summary = "\n".join(line for line in lines if line.startswith("RESULT "))
            return f"PCMCP language baseline finished (exit {code}).\n{summary or 'No test summary produced.'}", []
        issuer = f"{self.brain} via text #{job['rowid']}"
        if job["kind"] == "run":
            sm = read_json(job["run_dir"] / "summary.json")
            fresh = sm and sm.get("command_id") and any(
                c.get("command_id") == sm["command_id"] and c.get("issued_by") == issuer
                for c in read_jsonl(job["run_dir"] / "commands.jsonl"))
            if fresh:
                return summary_text(sm, job["run_dir"].name, code), []
            tail = (job["run_dir"] / f"text-trigger-{job['rowid']}.log").read_text()[-300:].strip()
            return f"PCMCP run did not start (exit {code}): {tail.splitlines()[-1] if tail else 'no output'}", []
        # inventory_drive: find the run folder this text created, attach a tab-separated listing
        run_dir = None
        base = Path(self.dcfg["runs_dir"]).expanduser()
        for d in sorted(base.glob("*"), reverse=True) if base.exists() else []:
            if any(c.get("issued_by") == issuer and c.get("mode") == "inventory" for c in read_jsonl(d / "commands.jsonl")):
                run_dir = d
                break
        inv = read_json(run_dir / "inventory.json") if run_dir else None
        if code != 0 or not inv:
            tail = (job["req"] / "inventory.log").read_text()[-300:].strip()
            return f"PCMCP Drive inventory failed (exit {code}): {tail.splitlines()[-1] if tail else 'no output'}", []
        listing = job["req"] / "drive_inventory.txt"
        with open(listing, "w", encoding="utf-8") as f:
            f.write("id\tpath\tmimeType\tsize_bytes\tmodified\tmd5\n")
            for i in inv["items"]:
                f.write("\t".join(str(x if x is not None else "").replace("\t", " ").replace("\n", " ") for x in
                                  (i["id"], i["path"] or f"(outside My Drive)/{i['name']}", i["mimeType"], i["size"],
                                   i["modifiedTime"], i["md5Checksum"])) + "\n")
        c = inv["counts"]
        return (f"PCMCP Drive inventory (read-only) run {run_dir.name}: {c['files']} files, {c['folders']} folders, "
                f"{c['total_bytes'] / 1e9:.2f} GB. inventory_hash {inv['inventory_hash'][:16]}. Listing attached."), [listing]


def _current_cli_module() -> str:
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name = spec.name if spec else "pcmcp_swarm"
    return name[: -len(".__main__")] if name.endswith(".__main__") else name


def watch(w: Watcher, once: bool = False) -> None:
    w.start_mark()
    print(f"watching {w.t['chat_db']} every {w.t['poll_seconds']} s for PCMCP-<code> status|inventory|screenshot|task|run; "
          f"replies via {w.t.get('reply')}  (Ctrl-C to stop)")
    while True:
        for rec in w.poll_once():
            print(f"text #{rec['rowid']} {rec['command']} {rec.get('arg') or ''}: "
                  + ("ACTED" if rec.get("acted") else f"refused - {rec.get('refused')}"))
        for rec in w.check_finished():
            print(f"text #{rec['rowid']} {rec['event']} (exit {rec['exit']}); reply sent: {rec['reply'].get('sent')}")
        if once and not w.running:
            return
        time.sleep(w.t["poll_seconds"])
