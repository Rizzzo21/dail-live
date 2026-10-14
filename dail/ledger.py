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

    def debit(self, account, amount, kind="debit", idem=None, memo="",
              allow_negative=False):
        """Reverse a credit: pull `amount` back out of `account`.

        Used by the refund rail (bnty_0162/bnty_0131). Idempotent on `idem`
        like credit/transfer, so a retried refund never double-debits. Fails
        closed with the same insufficient-funds error as transfer unless the
        caller explicitly allows a negative balance (audited admin clawback).
        """
        if amount <= 0:
            raise LedgerError("amount must be positive")
        idem = self._clean_idem(idem)
        if idem and idem in self.idempotency:
            return self.transactions[self.idempotency[idem]]
        if not allow_negative and self.balances[account] < amount:
            err = LedgerError("insufficient funds")
            err.required = amount
            err.available = self.balances[account]
            raise err
        txid = f"tx_{len(self.transactions)+1:06d}"
        tx = Transaction(id=txid, kind=kind, from_account=account,
                         to_account="SYSTEM", amount=amount,
                         idempotency_key=idem or txid, memo=memo or "")
        self.balances[account] -= amount
        self.transactions[txid] = tx
        self.idempotency[tx.idempotency_key] = txid
        self.audit.append("ledger.debit", tx.model_dump())
        return self._committed(tx)

    def transfer(self, source, destination, amount, kind="payment", idem=None,
                 memo=""):
        if amount <= 0:
            raise LedgerError("amount must be positive")
        idem = self._clean_idem(idem)
        if idem and idem in self.idempotency:
            return self.transactions[self.idempotency[idem]]
        if self.balances[source] < amount:
            # bnty_0168 safe subset: attach the numbers additively. The
            # message stays the legacy string; the API layer surfaces
            # required/available alongside it.
            err = LedgerError("insufficient funds")
            err.required = amount
            err.available = self.balances[source]
            raise err
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
