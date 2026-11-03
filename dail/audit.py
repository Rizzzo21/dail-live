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
    def __init__(self):
        self.events = []
        # Monotonic sequence: must NOT be len(events), which repeats
        # after the ring buffer trims old events.
        self._seq = 0

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
        return record

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
