import hashlib, json

class AuditLog:
    def __init__(self):
        self.events = []

    def append(self, event, payload):
        prev = self.events[-1]["hash"] if self.events else "GENESIS"
        body = json.dumps(
            {"seq": len(self.events)+1, "event": event, "payload": payload, "prev_hash": prev},
            sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(body.encode()).hexdigest()
        record = json.loads(body)
        record["hash"] = digest
        self.events.append(record)
        return record

    def verify(self):
        prev = "GENESIS"
        for e in self.events:
            body = json.dumps(
                {"seq": e["seq"], "event": e["event"], "payload": e["payload"], "prev_hash": prev},
                sort_keys=True, separators=(",", ":")
            )
            if hashlib.sha256(body.encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True
