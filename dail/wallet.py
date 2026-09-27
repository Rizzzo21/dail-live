import hashlib
import hmac
import secrets

SAFE_ACCOUNT = "DAIL_SAFE"


class SafeWallet:
    """Receive-first software safe for test-mode DAiL funds.

    The safe is deliberately not a real payment processor or bank account.
    Withdrawal secrets are stored only as SHA-256 hashes and are returned once.
    """

    def __init__(self, ledger, audit, admin_key=None):
        self.ledger = ledger
        self.audit = audit
        self.admin_key = admin_key
        self.receive_address = f"dail-safe-{secrets.token_urlsafe(18)}"
        self._withdrawal_key_hash = None
        self._withdrawal_key_id = None
        self._key_version = 0

    @property
    def balance(self):
        return self.ledger.balances[SAFE_ACCOUNT]

    def info(self):
        return {
            "account": SAFE_ACCOUNT,
            "receive_address": self.receive_address,
            "balance": self.balance,
            "currency": "DAIL",
            "receive_only_by_default": True,
            "withdrawal_key_configured": self._withdrawal_key_hash is not None,
            "key_version": self._key_version,
            "real_funds": False,
        }

    def receive(self, amount, provider, idem):
        tx = self.ledger.credit(SAFE_ACCOUNT, amount, kind="safe_receive", idem=idem)
        self.audit.append("safe.received", {
            "amount": amount,
            "provider": provider,
            "transaction": tx.id,
        })
        return tx

    def create_withdrawal_key(self, admin_key):
        if not self.admin_key or not hmac.compare_digest(admin_key or "", self.admin_key):
            raise PermissionError("admin_key_invalid")
        raw = "dail_wd_" + secrets.token_urlsafe(32)
        self._withdrawal_key_hash = hashlib.sha256(raw.encode()).hexdigest()
        self._withdrawal_key_id = f"wdkey_{secrets.token_hex(6)}"
        self._key_version += 1
        self.audit.append("safe.withdrawal_key.created", {
            "key_id": self._withdrawal_key_id,
            "version": self._key_version,
        })
        return {
            "key_id": self._withdrawal_key_id,
            "key_version": self._key_version,
            "withdrawal_key": raw,
            "warning": "Store this key securely. It is shown only once.",
        }

    def revoke_withdrawal_key(self, admin_key):
        if not self.admin_key or not hmac.compare_digest(admin_key or "", self.admin_key):
            raise PermissionError("admin_key_invalid")
        old_id = self._withdrawal_key_id
        self._withdrawal_key_hash = None
        self._withdrawal_key_id = None
        self.audit.append("safe.withdrawal_key.revoked", {"key_id": old_id})
        return {"revoked": True, "key_id": old_id}

    def withdraw(self, amount, destination, idem, withdrawal_key):
        if not self._withdrawal_key_hash:
            raise PermissionError("withdrawal_key_not_configured")
        supplied_hash = hashlib.sha256((withdrawal_key or "").encode()).hexdigest()
        if not hmac.compare_digest(supplied_hash, self._withdrawal_key_hash):
            raise PermissionError("withdrawal_key_invalid")
        if not destination or len(destination) > 200:
            raise ValueError("invalid_destination")
        tx = self.ledger.transfer(
            SAFE_ACCOUNT,
            f"withdrawal:{destination}",
            amount,
            kind="safe_withdrawal",
            idem=idem,
        )
        self.audit.append("safe.withdrawn", {
            "amount": amount,
            "destination": destination,
            "transaction": tx.id,
            "key_id": self._withdrawal_key_id,
        })
        return tx
