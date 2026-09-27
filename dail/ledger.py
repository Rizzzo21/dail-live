from collections import defaultdict
from .models import Transaction

class LedgerError(Exception):
    pass

class Ledger:
    def __init__(self, audit):
        self.balances = defaultdict(int)
        self.transactions = {}
        self.idempotency = {}
        self.audit = audit

    def credit(self, account, amount, kind="credit", idem=None):
        if amount <= 0:
            raise LedgerError("amount must be positive")
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
        return tx

    def transfer(self, source, destination, amount, kind="payment", idem=None):
        if amount <= 0:
            raise LedgerError("amount must be positive")
        if idem and idem in self.idempotency:
            return self.transactions[self.idempotency[idem]]
        if self.balances[source] < amount:
            raise LedgerError("insufficient funds")
        txid = f"tx_{len(self.transactions)+1:06d}"
        tx = Transaction(id=txid, kind=kind, from_account=source,
                         to_account=destination, amount=amount,
                         idempotency_key=idem or txid)
        self.balances[source] -= amount
        self.balances[destination] += amount
        self.transactions[txid] = tx
        self.idempotency[tx.idempotency_key] = txid
        self.audit.append("ledger.transfer", tx.model_dump())
        return tx

    def refund(self, txid):
        original = self.transactions[txid]
        if original.status == "refunded":
            return original
        self.transfer(original.to_account, original.from_account,
                      original.amount, kind="refund", idem=f"refund:{txid}")
        original.status = "refunded"
        self.audit.append("ledger.refund", {"txid": txid})
        return original
