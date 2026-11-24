"""The swarm: leads command pools of sub-bots that work chunks in parallel.

A lead (A create, B copy/move, E archive) gets a work list from the compiled plan, splits it into
chunks (<= 100 calls each = one batch request), and hands chunks to its sub-bots (A1..An) running in
parallel. The lead tracks every chunk (pending / running / done / failed) and gives a failed chunk to
another sub-bot. Bots decide nothing: each item is executed exactly as the plan says, or refused and
logged if it falls outside the plan or breaks a hard rule.
"""
from __future__ import annotations

import collections
import math
import threading
import time
import traceback
from pathlib import Path

from . import FOLDER_MIME, sub_bot_names
from . import inventory as inv_mod, planfile, runs
from .api import TRANSIENT


def _all_transient(results) -> bool:
    """Every item still failing with a network / 5xx answer after retries: the chunk failed as a whole."""
    return bool(results) and all(s == 0 or s in TRANSIENT for s, _ in results)
from ..util import now_iso, read_json, read_jsonl


class Stopped(Exception):
    pass


class ChunkFailed(Exception):
    pass


class Stats:
    def __init__(self, leads: dict):
        self.lock = threading.Lock()
        self.planned = 0
        self.done = 0
        self.errors = 0
        self.refused = 0
        self.uncertain = 0
        self.retries = {"rate_limit": 0, "transient": 0}
        self.recent = collections.deque()
        self.active = set()
        self.started = time.time()
        self.leads = {k: {"pool": n, "chunks": {"pending": 0, "running": 0, "done": 0, "failed": 0},
                          "reassigned": 0, "items_planned": 0, "items_done": 0, "bots_used": set()} for k, n in leads.items()}

    def retry(self, kind, n=1):
        with self.lock:
            self.retries[kind] = self.retries.get(kind, 0) + n

    def item_done(self, letter, n=1):
        with self.lock:
            self.done += n
            self.leads[letter]["items_done"] += n
            t = time.time()
            for _ in range(n):
                self.recent.append(t)

    def per_minute(self):
        with self.lock:
            cut = time.time() - 60
            while self.recent and self.recent[0] < cut:
                self.recent.popleft()
            return len(self.recent)


class Engine:
    def __init__(self, cfg, dcfg, client_factory, run_dir: Path, command: dict, mode: str):
        self.cfg, self.dcfg, self.run_dir, self.command, self.mode = cfg, dcfg, run_dir, command, mode
        self.pools = {k: v for k, v in dcfg["pools"].items() if k in ("A", "B", "E")}   # I works in inventory.py
        self.stats = Stats(self.pools)
        self.client = client_factory(self.stats.retry)
        self.batch_size = min(100, int(dcfg["batch_size"]))
        self.max_attempts = int(dcfg["chunk_max_attempts"])
        self.chunks = {}
        self.chunk_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.state = "starting"
        self._ctl_cache = (0.0, "go")
        self.extra_status = {}
        self.started_at = now_iso()
        # keep chunk history from earlier sessions of this run (stop/resume, rollback): evidence is never overwritten
        self._prev_chunk_sessions = (read_json(run_dir / "chunks.json", {}) or {}).get("sessions", {})

    # ---------------------------------------------------------------- control + status
    def control(self) -> str:
        t, s = self._ctl_cache
        if time.time() - t > 0.4:
            s = runs.control_state(self.run_dir)
            self._ctl_cache = (time.time(), s)
        return s

    def wait_if_paused(self):
        while True:
            if self.stop_flag.is_set():
                raise Stopped()
            s = self.control()
            if s == "stop":
                self.stop_flag.set()
                raise Stopped()
            if s != "pause":
                return
            self.state = "paused"
            time.sleep(0.5)

    def status_doc(self) -> dict:
        st = self.stats
        with st.lock:
            leads = {k: {"pool": v["pool"], "chunks": dict(v["chunks"]), "reassigned": v["reassigned"],
                         "items_planned": v["items_planned"], "items_done": v["items_done"],
                         "sub_bots_used": sorted(v["bots_used"])} for k, v in st.leads.items()}
            elapsed = time.time() - st.started
            doc = {"run": self.run_dir.name, "mode": self.mode, "state": self.state, "updated_at": now_iso(),
                   "command_id": self.command["command_id"], "issued_by": self.command["issued_by"],
                   "elapsed_seconds": round(elapsed, 1), "bots_active": len(st.active), "bots_active_names": sorted(st.active),
                   "bots_total": sum(self.pools.values()) + len(self.pools),
                   "done": st.done, "planned": st.planned, "errors": st.errors, "refused": st.refused,
                   "uncertain": st.uncertain, "retries": dict(st.retries), "leads": leads,
                   "avg_per_minute": round(st.done / elapsed * 60, 1) if elapsed > 0 else 0.0,
                   "api_requests": self.client.requests_sent, **self.extra_status}
        doc["per_minute_last_60s"] = st.per_minute()
        return doc

    def _status_loop(self):
        iv = float(self.dcfg["status_interval_seconds"])
        while not self._status_done.is_set():
            self.write_status()
            self._status_done.wait(iv)

    def write_status(self):
        runs.write_json(self.run_dir / "status.json", self.status_doc())
        with self.chunk_lock:
            sessions = dict(self._prev_chunk_sessions)
            sessions[self.command["command_id"]] = {"mode": self.mode, "started_at": self.started_at, "updated_at": now_iso(), "chunks": self.chunks}
            runs.write_json(self.run_dir / "chunks.json", {"sessions": sessions})

    def _shot_loop(self):
        iv = float(self.dcfg.get("screenshot_interval_seconds") or 0)
        if iv <= 0:
            return
        from ..screenshot import capture
        n, warned = 0, False
        while not self._status_done.wait(iv):
            n += 1
            try:
                capture({"type": "full"}, self.run_dir / "screenshots" / f"shot-{n:04d}.png")
            except Exception as e:  # screenshots must never stop the run
                if not warned:
                    runs.event(self.run_dir, "screenshot_failed", error=str(e),
                               hint="grant Screen Recording to the app running PCMCP")
                    warned = True

    def start_background(self):
        self._status_done = threading.Event()
        self.state = "running"
        self._bg = [threading.Thread(target=self._status_loop, daemon=True), threading.Thread(target=self._shot_loop, daemon=True)]
        for t in self._bg:
            t.start()

    def stop_background(self, final_state: str):
        self.state = final_state
        self._status_done.set()
        for t in self._bg:
            t.join(timeout=5)
        self.write_status()

    # ---------------------------------------------------------------- the lead
    def run_lead(self, letter: str, items: list, worker, label: str = "") -> None:
        """Split items into chunks, run them on the lead's sub-bot pool in parallel, reassign failures."""
        if not items:
            return
        pool = int(self.pools[letter])
        names = sub_bot_names(letter, pool)
        # Even split into at least one chunk per sub-bot (each chunk <= batch_size calls).
        k = min(len(items), max(pool, math.ceil(len(items) / self.batch_size)))
        bounds = [round(i * len(items) / k) for i in range(k + 1)]
        base = len([c for c in self.chunks if c.startswith(letter)])
        # The lead deals chunks round-robin into each sub-bot's own queue; a sub-bot whose queue is
        # empty helps with leftovers (reassigned chunks first, then the longest other queue).
        own = {n: collections.deque() for n in names}
        retry_q = collections.deque()
        qlock = threading.Lock()
        for j in range(k):
            chunk = items[bounds[j]:bounds[j + 1]]
            cid = f"{letter}-{label}{base + j + 1:05d}"
            with self.chunk_lock:
                self.chunks[cid] = {"lead": letter, "status": "pending", "items": len(chunk), "dealt_to": names[j % pool],
                                    "attempts": 0, "bots": [], "error": None}
            with self.stats.lock:
                self.stats.leads[letter]["chunks"]["pending"] += 1
            own[names[j % pool]].append((cid, chunk))

        def next_chunk(name):
            with qlock:
                if own[name]:
                    return own[name].popleft()
                for idx, (cid, chunk, last) in enumerate(retry_q):   # a reassigned chunk goes to a DIFFERENT sub-bot
                    if last != name:
                        del retry_q[idx]
                        return cid, chunk
                donor = max(own, key=lambda n: len(own[n]))
                if own[donor]:
                    return own[donor].pop()
                if retry_q:                                           # only this bot is left to try it
                    cid, chunk, _ = retry_q.popleft()
                    return cid, chunk
                return None

        def queue_empty():
            with qlock:
                return not retry_q and not any(own.values())
        errors = []

        def set_status(cid, new, bot=None, err=None):
            with self.chunk_lock:
                c = self.chunks[cid]
                old = c["status"]
                c["status"] = new
                if bot:
                    c["bots"].append(bot)
                if new == "running":
                    c["attempts"] += 1
                if err:
                    c["error"] = err
            with self.stats.lock:
                ch = self.stats.leads[letter]["chunks"]
                ch[old] -= 1
                ch[new] += 1

        def sub_bot(name):
            while True:
                try:
                    self.wait_if_paused()
                except Stopped:
                    return
                self.state = "running"
                got = next_chunk(name)
                if got is None:
                    return
                cid, chunk = got
                set_status(cid, "running", bot=name)
                with self.stats.lock:
                    self.stats.active.add(name)
                try:
                    worker(chunk, name, cid)
                    set_status(cid, "done")
                    with self.stats.lock:
                        self.stats.leads[letter]["bots_used"].add(name)
                except Stopped:
                    set_status(cid, "pending")
                    return
                except Exception as e:  # a failed chunk goes back to the lead for another sub-bot
                    err = f"{type(e).__name__}: {e}"
                    runs.event(self.run_dir, "chunk_failed", chunk=cid, bot=name, error=err,
                               trace=traceback.format_exc(limit=3))
                    with self.chunk_lock:
                        attempts = self.chunks[cid]["attempts"]
                    if attempts < self.max_attempts:
                        set_status(cid, "pending", err=err)
                        with self.stats.lock:
                            self.stats.leads[letter]["reassigned"] += 1
                        with qlock:
                            retry_q.append((cid, chunk, name))
                        time.sleep(0.2)          # step back so a different sub-bot picks it up
                    else:
                        set_status(cid, "failed", err=err)
                        errors.append(cid)
                        with self.stats.lock:
                            self.stats.errors += len(chunk)
                finally:
                    with self.stats.lock:
                        self.stats.active.discard(name)

        threads = [threading.Thread(target=sub_bot, args=(n,), name=n, daemon=True) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # a sub-bot may exit just before another re-queues a failed chunk: finish leftovers
        i = 0
        while not queue_empty() and not self.stop_flag.is_set():
            t = threading.Thread(target=sub_bot, args=(names[i % pool],), daemon=True)
            t.start()
            t.join()
            i += 1
        if self.stop_flag.is_set():
            raise Stopped()
        if errors:
            runs.event(self.run_dir, "lead_finished_with_failed_chunks", lead=letter, chunks=errors)


# ==================================================================== RUN

class Runner(Engine):
    """Executes one approved, validated, dry-run-checked plan."""

    def __init__(self, cfg, dcfg, client_factory, run_dir, command, plan, inv, fmap):
        super().__init__(cfg, dcfg, client_factory, run_dir, command, "run")
        self.plan, self.inv, self.fmap = plan, inv, fmap
        self.compiled = planfile.compile_ops(plan, inv, fmap)
        self.items = {i["id"]: i for i in inv["items"]}
        mirror = Path(cfg["data_dir"]).expanduser() / "logs" / "drive_operations.jsonl"
        self.receipts = runs.ReceiptLog(run_dir, mirror=mirror)
        self.intents_path = run_dir / "intents.jsonl"
        prior = read_jsonl(run_dir / "receipts.jsonl")
        self.done_ops = {r["op_id"] for r in prior if r.get("ok")}
        self.intended = {r["op_id"] for r in read_jsonl(self.intents_path)}
        self.folder_ids = {"": inv["scope_root_id"]}
        for i in inv["items"]:
            if i["is_folder"] and i["in_scope"]:
                self.folder_ids.setdefault(i["path"], i["id"])
        for r in prior:
            if r.get("ok") and r["action"] == "create_folder":
                self.folder_ids[r["path"]] = r["new_folder_id"]
        tot = self.compiled["totals"]
        self.stats.planned = sum(tot.values())
        for k in ("A", "B", "E"):
            self.stats.leads[k]["items_planned"] = tot.get(k, 0)
        already = len(self.done_ops)
        self.stats.done = already
        self.ids_lock = threading.Lock()

    def _receipt(self, op, bot, chunk, ok, **fields):
        return self.receipts.write({
            "run": self.run_dir.name, "plan_hash": self.compiled["plan_hash"], "command_id": self.command["command_id"],
            "issued_by": self.command["issued_by"], "op_id": op["op_id"], "letter": op["letter"], "action": op["action"],
            "lead": op["letter"], "bot": bot, "chunk": chunk, "ok": ok, "at": now_iso(), **fields})

    def _intent(self, ops, bot, chunk):
        from ..util import append_jsonl
        for op in ops:
            append_jsonl(self.intents_path, {"op_id": op["op_id"], "bot": bot, "chunk": chunk, "at": now_iso()})

    def _fail_item(self, op, bot, chunk, status, body, refused=False):
        with self.stats.lock:
            if refused:
                self.stats.refused += 1
            else:
                self.stats.errors += 1
        self._receipt(op, bot, chunk, False, error_status=status, error=body,
                      file_id=op.get("file_id"), file_name=op.get("name"), refused=refused)

    # --- A: create folders
    def work_create(self, chunk, bot, cid):
        todo, calls = [], []
        for op in chunk:
            if op["op_id"] in self.done_ops:
                continue
            parent = self.folder_ids.get(op["parent_path"])
            if parent is None:
                self._fail_item(op, bot, cid, None, "parent folder id unknown (parent not created)", refused=True)
                continue
            if op["op_id"] in self.intended:       # resumed after a crash: did the create land?
                found = self.client.list_query(
                    "name='{}' and '{}' in parents and mimeType='{}' and trashed=false".format(
                        op["name"].replace("\\", "\\\\").replace("'", "\\'"), parent, FOLDER_MIME), "id,name,parents")
                if len(found) == 1:
                    with self.ids_lock:
                        self.folder_ids[op["path"]] = found[0]["id"]
                    self._receipt(op, bot, cid, True, path=op["path"], parent_id=parent, new_folder_id=found[0]["id"], recovered=True)
                    self.stats.item_done("A")
                    continue
            todo.append(op)
            calls.append({"method": "POST", "path": "/drive/v3/files", "params": {"fields": "id,name,parents"},
                          "body": {"name": op["name"], "mimeType": FOLDER_MIME, "parents": [parent]}})
        if not calls:
            return
        self._intent(todo, bot, cid)
        results, _ = self.client.batch(calls)
        if _all_transient(results):
            raise ChunkFailed(f"network/5xx failure for all {len(results)} items")
        for op, (status, body) in zip(todo, results):
            if status == 200:
                with self.ids_lock:
                    self.folder_ids[op["path"]] = body["id"]
                self._receipt(op, bot, cid, True, path=op["path"], parent_id=body.get("parents", [None])[0],
                              new_folder_id=body["id"], name=op["name"])
                self.stats.item_done("A")
            else:
                self._fail_item(op, bot, cid, status, body)

    # --- B/E: move, copy, archive_move
    def work_move(self, chunk, bot, cid):
        rules = self.fmap.get("never_move_rules", [])
        tags = self.compiled["tags"]
        todo, calls = [], []
        for op in chunk:
            if op["op_id"] in self.done_ops:
                continue
            it = self.items[op["file_id"]]
            broken = planfile.never_move_violation(it, op["to_path"], tags.get(op["file_id"], []), rules)  # check #2
            if broken:
                self._fail_item(op, bot, cid, None, f"never-move rule {broken} (sub-bot re-check)", refused=True)
                continue
            dest = self.folder_ids.get(op["to_path"])
            if dest is None:
                self._fail_item(op, bot, cid, None, f"destination {op['to_path']!r} has no folder id", refused=True)
                continue
            if op["action"] == "copy":
                if op["op_id"] in self.intended:
                    with self.stats.lock:
                        self.stats.uncertain += 1
                    self._receipt(op, bot, cid, False, uncertain=True, file_id=op["file_id"], file_name=op["name"],
                                  error="copy was sent before a crash and has no receipt; not repeated, check by hand")
                    continue
                calls.append({"method": "POST", "path": f"/drive/v3/files/{op['file_id']}/copy",
                              "params": {"fields": "id,name,parents,md5Checksum"},
                              "body": {"name": op["name"], "parents": [dest]}})
            else:
                remove = [p for p in op["from_parents"] if p != dest]
                params = {"addParents": dest, "fields": "id,name,parents,md5Checksum"}
                if remove:
                    params["removeParents"] = ",".join(remove)
                calls.append({"method": "PATCH", "path": f"/drive/v3/files/{op['file_id']}", "params": params, "body": {}})
            todo.append((op, dest))
        if not calls:
            return
        self._intent([o for o, _ in todo], bot, cid)
        results, _ = self.client.batch(calls)
        if _all_transient(results):
            raise ChunkFailed(f"network/5xx failure for all {len(results)} items")
        for (op, dest), (status, body) in zip(todo, results):
            it = self.items[op["file_id"]]
            if status == 200:
                fields = {"file_id": op["file_id"], "file_name": op["name"], "old_folder": op["from_path"],
                          "old_parent_ids": op["from_parents"], "new_folder": op["to_path"], "new_parent_id": dest,
                          "md5_before": it.get("md5Checksum"), "md5_after": body.get("md5Checksum"),
                          "is_folder": op["is_folder"]}
                if op["action"] == "copy":
                    fields["copy_id"] = body["id"]
                self._receipt(op, bot, cid, True, **fields)
                self.stats.item_done(op["letter"])
            else:
                self._fail_item(op, bot, cid, status, body)

    def execute(self) -> dict:
        ops = self.compiled["ops"]
        runs.event(self.run_dir, "run_started", command_id=self.command["command_id"], plan_hash=self.compiled["plan_hash"],
                   totals=self.compiled["totals"], pools=self.pools, already_done=len(self.done_ops))
        self.start_background()
        final = "done"
        try:
            for depth in sorted({o["depth"] for o in ops["A"]}):     # parents before children
                self.run_lead("A", [o for o in ops["A"] if o["depth"] == depth], self.work_create, label=f"d{depth}-")
            errs = []

            def lead(letter):
                try:
                    self.run_lead(letter, ops[letter], self.work_move)
                except Stopped:
                    pass
                except Exception as e:
                    errs.append(e)
            ts = [threading.Thread(target=lead, args=(k,)) for k in ("B", "E")]   # B and E leads in parallel
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if self.stop_flag.is_set():
                raise Stopped()
            if errs:
                raise errs[0]
        except (Stopped, KeyboardInterrupt):
            self.stop_flag.set()
            final = "stopped"
        finally:
            self.stop_background(final)
        runs.event(self.run_dir, "run_finished", state=final, command_id=self.command["command_id"])
        return self.status_doc()


# ==================================================================== ROLLBACK

class Rollback(Engine):
    """Reads the run's receipts and puts everything back. Nothing is deleted: copies and folders the
    swarm created are parked in a holding folder outside the target."""

    def __init__(self, cfg, dcfg, client_factory, run_dir, command, inv):
        super().__init__(cfg, dcfg, client_factory, run_dir, command, "rollback")
        self.inv = inv
        self.log = runs.ReceiptLog(run_dir, name="rollback_receipts.jsonl")
        self.done_seqs = {r["undoes_seq"] for r in read_jsonl(run_dir / "rollback_receipts.jsonl") if r.get("ok")}
        self.src = [r for r in read_jsonl(run_dir / "receipts.jsonl") if r.get("ok")]
        self.holding_id = None

    def _rr(self, r, bot, chunk, ok, **f):
        return self.log.write({"run": self.run_dir.name, "command_id": self.command["command_id"],
                               "issued_by": self.command["issued_by"], "undoes_seq": r["seq"],
                               "undoes_receipt_hash": r["receipt_hash"], "undoes_command_id": r["command_id"],
                               "action": "undo_" + r["action"], "bot": bot, "chunk": chunk, "ok": ok, "at": now_iso(), **f})

    def _holding(self):
        name = f"{self.dcfg['rollback_holding_folder']}_{self.run_dir.name}"
        parent = "root" if self.inv["scope_arg"] != "root" else self.inv["scope_root_id"]
        found = self.client.list_query(f"name='{name}' and '{parent}' in parents and mimeType='{FOLDER_MIME}' and trashed=false", "id")
        if found:
            return found[0]["id"]
        r = self.client.request("POST", "/drive/v3/files", {"fields": "id"},
                                {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}, write=True)
        runs.event(self.run_dir, "holding_folder_created", id=r["id"], name=name, parent=parent)
        return r["id"]

    def work(self, chunk, bot, cid):
        calls, todo = [], []
        for r in chunk:
            a = r["action"]
            if a in ("move", "archive_move"):
                params = {"addParents": ",".join(r["old_parent_ids"]), "fields": "id,parents,md5Checksum"}
                if r["new_parent_id"] not in r["old_parent_ids"]:
                    params["removeParents"] = r["new_parent_id"]
                calls.append({"method": "PATCH", "path": f"/drive/v3/files/{r['file_id']}", "params": params, "body": {}})
            elif a == "copy":
                calls.append({"method": "PATCH", "path": f"/drive/v3/files/{r['copy_id']}",
                              "params": {"addParents": self.holding_id, "removeParents": r["new_parent_id"], "fields": "id,parents"}, "body": {}})
            elif a == "create_folder":
                calls.append({"method": "PATCH", "path": f"/drive/v3/files/{r['new_folder_id']}",
                              "params": {"addParents": self.holding_id, "removeParents": r["parent_id"], "fields": "id,parents"}, "body": {}})
            else:
                continue
            todo.append(r)
        if not calls:
            return
        results, _ = self.client.batch(calls)
        if _all_transient(results):
            raise ChunkFailed("network/5xx failure for whole chunk")
        for r, (status, body) in zip(todo, results):
            letter = "E" if r["action"] == "archive_move" else "B"
            if status == 200:
                self._rr(r, bot, cid, True, target_id=body.get("id"), parents_now=body.get("parents"),
                         parked=r["action"] in ("copy", "create_folder"))
                self.stats.item_done(letter)
            else:
                with self.stats.lock:
                    self.stats.errors += 1
                self._rr(r, bot, cid, False, error_status=status, error=body)

    def execute(self) -> dict:
        todo = [r for r in self.src if r["seq"] not in self.done_seqs]
        files = [r for r in todo if r["action"] in ("move", "copy")]
        arch = [r for r in todo if r["action"] == "archive_move"]
        folders = [r for r in todo if r["action"] == "create_folder"]
        self.stats.planned = len(todo)
        self.stats.leads["B"]["items_planned"] = len(files) + len(folders)
        self.stats.leads["E"]["items_planned"] = len(arch)
        runs.event(self.run_dir, "rollback_started", command_id=self.command["command_id"], items=len(todo))
        self.start_background()
        final = "done"
        try:
            if any(r["action"] in ("copy", "create_folder") for r in todo):
                self.holding_id = self._holding()
                self.extra_status["holding_folder_id"] = self.holding_id
            errs = []

            def lead(letter, items):
                try:
                    self.run_lead(letter, items, self.work)
                except Stopped:
                    pass
                except Exception as e:
                    errs.append(e)
            ts = [threading.Thread(target=lead, args=("B", files)), threading.Thread(target=lead, args=("E", arch))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if self.stop_flag.is_set():
                raise Stopped()
            if errs:
                raise errs[0]
            # created folders are parked last, deepest first, once the files are back home
            folders.sort(key=lambda r: -r["path"].count("/"))
            self.run_lead("B", folders, self.work, label="fold-")
        except (Stopped, KeyboardInterrupt):
            self.stop_flag.set()
            final = "stopped"
        finally:
            self.stop_background(final)
        runs.event(self.run_dir, "rollback_finished", state=final)
        return self.status_doc()


# ==================================================================== DRY RUN

def dry_run(client, plan, inv, fmap, dcfg) -> dict:
    """Simulate the plan against the LIVE Drive with a read-only client. Changes nothing."""
    v = planfile.validate(plan, inv, fmap)
    comp = planfile.compile_ops(plan, inv, fmap) if v["ok"] else None
    blockers = [{"code": c, "detail": m} for c, m in v["errors"]]
    warnings = [{"code": c, "detail": m} for c, m in v["warnings"]]
    live = {}
    if comp:
        ids = sorted({o["file_id"] for k in ("B", "E") for o in comp["ops"][k]})
        items = {i["id"]: i for i in inv["items"]}
        dests = sorted({o["to_path"] for k in ("B", "E") for o in comp["ops"][k]} - {o["path"] for o in comp["ops"]["A"]})
        dest_ids = {}
        for d in dests:
            for i in inv["items"]:
                if i["is_folder"] and i["in_scope"] and i["path"] == d:
                    dest_ids[d] = i["id"]
        if "" in dests:
            dest_ids[""] = inv["scope_root_id"]
        check = ids + sorted(set(dest_ids.values()))
        for i in range(0, len(check), 100):
            calls = [{"method": "GET", "path": f"/drive/v3/files/{fid}",
                      "params": {"fields": "id,name,parents,md5Checksum,trashed,capabilities(canMoveItemWithinDrive,canAddChildren)"}}
                     for fid in check[i:i + 100]]
            res, _ = client.batch(calls)
            for fid, (st, body) in zip(check[i:i + 100], res):
                live[fid] = (st, body)
        for fid in ids:
            st, body = live[fid]
            if st != 200:
                blockers.append({"code": "MISSING", "detail": f"{fid} ({items[fid]['path']}): HTTP {st}"})
                continue
            if sorted(body.get("parents") or []) != sorted(items[fid]["parents"]):
                blockers.append({"code": "DRIFT", "detail": f"{items[fid]['path']}: moved since the inventory; re-export and re-plan"})
            if not (body.get("capabilities") or {}).get("canMoveItemWithinDrive", True):
                blockers.append({"code": "PERMISSION", "detail": f"{items[fid]['path']}: Drive will not let you move this item"})
        for d, fid in dest_ids.items():
            st, body = live[fid]
            if st != 200 or not (body.get("capabilities") or {}).get("canAddChildren", True):
                blockers.append({"code": "PERMISSION", "detail": f"destination {d or '(root)'!r}: cannot add items (HTTP {st})"})
        # name conflicts: Drive allows duplicate names; nothing is overwritten, but report them
        incoming = collections.defaultdict(list)
        for k in ("B", "E"):
            for o in comp["ops"][k]:
                incoming[o["to_path"]].append(o["name"])
        for d, names in incoming.items():
            existing = []
            if d in dest_ids:
                existing = [f["name"] for f in client.list_query(f"'{dest_ids[d]}' in parents and trashed=false", "id,name")]
            c = collections.Counter(names + existing)
            dup = sorted(n for n, k in c.items() if k > 1)
            if dup:
                warnings.append({"code": "NAME_CONFLICT", "detail": f"{d or '(root)'}: {len(dup)} names would appear more than once "
                                 f"(Drive keeps both; nothing is overwritten), e.g. {dup[:3]}"})
    writes = sum(comp["totals"].values()) if comp else 0
    epm = int(dcfg["edits_per_minute"])
    out = {"plan_hash": v["plan_hash"], "ok": not blockers, "blockers": blockers, "warnings": warnings,
           "skipped_kept_in_synomize": v.get("skipped", []),
           "validation": v["stats"], "checked_live_items": len(live),
           "would": ({"create_folders": comp["totals"]["A"], "reuse_existing_folders": comp["reused_folders"],
                      "moves": sum(1 for o in comp["ops"]["B"] if o["action"] == "move"),
                      "copies": sum(1 for o in comp["ops"]["B"] if o["action"] == "copy"),
                      "archive_moves": comp["totals"]["E"], "already_in_place": comp["unchanged"]} if comp else None),
           "estimate": {"write_calls": writes, "batch_requests": sum(math.ceil(n / 100) for n in (comp["totals"].values() if comp else [])),
                        "minutes_at_throttle": round(writes / epm, 2) if epm else None, "edits_per_minute_ceiling": epm},
           "changed_anything": False, "generated_at": now_iso()}
    return out
