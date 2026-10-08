from collections import defaultdict
from .models import Transaction

class LedgerError(Exception):
    pass

class Ledger:
    def __init__(self, audit, persist=None):
        self.balances = defaultdict(int)
        self.transactions = {}
        self.idempotency = {}
        self.audit = audit
        # persist(tx): write-through hook (WorldStore.record_tx). Called
        # synchronously after the in-memory commit; may raise -> fail closed.
        self.persist = persist

    def _committed(self, tx):
        if self.persist:
            self.persist(tx)
        return tx

    def _clean_idem(self, idem):
        # Defensive: a blank/whitespace idempotency key must never become a
        # real key. "   " is truthy as a string, so without this, unrelated
        # calls sharing a blank key would collide on e.g. "   :principal".
        if isinstance(idem, str):
            idem = idem.strip()
        return idem or None

    def credit(self, account, amount, kind="credit", idem=None):
        if amount <= 0:
            raise LedgerError("amount must be positive")
        idem = self._clean_idem(idem)
        if idem and idem in self.idempotency:
            return self.transactions[self.idempotency[idem]]
        txid = f"tx_{len(self.transactions)+1:06d}"
        tx = Transaction(id=txid, kind=kind, from_account="SYSTEM",
                         to_account=account, amount=amount,
                         idempotency_key=idem or txid)
        self.balances[account] += amount
        self.transactions[txid] = tx
        self.idempotency[tx.idempotency_key] = txid
        self.audit.append("ledger.credit", tx.model_dump())
        return self._committed(tx)

    def transfer(self, source, destination, amount, kind="payment", idem=None,
                 memo=""):
        if amount <= 0:
            raise LedgerError("amount must be positive")
        idem = self._clean_idem(idem)
        if idem and idem in self.idempotency:
            return self.transactions[self.idempotency[idem]]
        if self.balances[source] < amount:
            raise LedgerError("insufficient funds")
        txid = f"tx_{len(self.transactions)+1:06d}"
        tx = Transaction(id=txid, kind=kind, from_account=source,
                         to_account=destination, amount=amount,
                         idempotency_key=idem or txid, memo=memo or "")
        self.balances[source] -= amount
        self.balances[destination] += amount
        self.transactions[txid] = tx
        self.idempotency[tx.idempotency_key] = txid
        self.audit.append("ledger.transfer", tx.model_dump())
        return self._committed(tx)

    def refund(self, txid):
        original = self.transactions[txid]
        if original.status == "refunded":
            return original
        self.transfer(original.to_account, original.from_account,
                      original.amount, kind="refund", idem=f"refund:{txid}")
        original.status = "refunded"
        self.audit.append("ledger.refund", {"txid": txid})
        return original
