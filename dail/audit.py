import hashlib, json

# MEMORY CAP (2026-11-03): the audit log was append-only and unbounded —
# it grew forever in memory and reset to zero on every restart (342 -> 9
# events on the 2026-11-03 restart), and it is the largest strictly
# monotonic in-memory store in the app. 1000 events (~1 MB at ~1 KB each)
# keeps a useful verification window while bounding it. Postgres
# persistence of the full audit history remains a separate follow-up;
# this ring buffer is the safety valve, not a replacement for it.
AUDIT_MAX_EVENTS = 1000

class AuditLog:
    # MEMORY PHASE 2: optional Postgres backend. When a backend is
    # supplied (persist=callable taking a record, all=callable returning
    # chain-ordered records), every append is ALSO written to Postgres,
    # while the in-memory ring stays as a bounded read cache. Design
    # choice: ring-as-cache, not ring-replaced-by-DB, because verify()
    # and every existing caller read self.events, and the ring already
    # bounds RAM. A DB write failure must never break the caller, so
    # persist errors are swallowed; the ring remains the source of
    # truth for the current process, Postgres for restarts.
    def __init__(self, persist=None, all_records=None):
        self.events = []
        # Monotonic sequence: must NOT be len(events), which repeats
        # after the ring buffer trims old events.
        self._seq = 0
        self._persist = persist
        self._all_records = all_records

    def append(self, event, payload):
        prev = self.events[-1]["hash"] if self.events else "GENESIS"
        self._seq += 1
        body = json.dumps(
            {"seq": self._seq, "event": event, "payload": payload, "prev_hash": prev},
            sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(body.encode()).hexdigest()
        record = json.loads(body)
        record["hash"] = digest
        self.events.append(record)
        if len(self.events) > AUDIT_MAX_EVENTS:
            del self.events[:-AUDIT_MAX_EVENTS]
        if self._persist is not None:
            try:
                self._persist(record)
            except Exception:
                pass
        return record

    def load_from_backend(self):
        """Simulated-restart recovery: replace the ring with the newest
        AUDIT_MAX_EVENTS records from the backend and resume the
        sequence at the backend's max seq, so the chain continues
        without reusing sequence numbers. No-op without a backend."""
        if self._all_records is None:
            return
        try:
            rows = self._all_records()
        except Exception:
            return
        if not rows:
            return
        self.events = list(rows[-AUDIT_MAX_EVENTS:])
        self._seq = max(r["seq"] for r in rows)

    def verify_all(self):
        """Chain verification over the full backend history (when a
        backend is present); otherwise the ring verify(). Returns True
        for an empty history."""
        rows = None
        if self._all_records is not None:
            try:
                rows = self._all_records()
            except Exception:
                rows = None
        if rows is None:
            return self.verify()
        prev = "GENESIS"
        for e in rows:
            body = json.dumps(
                {"seq": e["seq"], "event": e["event"], "payload": e["payload"], "prev_hash": prev},
                sort_keys=True, separators=(",", ":")
            )
            if hashlib.sha256(body.encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True

    def verify(self):
        # Windowed chain: the first retained event's predecessor may have
        # been trimmed by the ring buffer, so its own hash is verified
        # against its stored prev_hash, and every later link is verified
        # against the previous retained record's hash.
        prev = "GENESIS"
        for i, e in enumerate(self.events):
            if i == 0:
                prev = e["prev_hash"]
            body = json.dumps(
                {"seq": e["seq"], "event": e["event"], "payload": e["payload"], "prev_hash": prev},
                sort_keys=True, separators=(",", ":")
            )
            if hashlib.sha256(body.encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True
