import hashlib
import hmac
import re
import secrets

SAFE_ACCOUNT = "DAIL_SAFE"

# FIX 5 (2026-10-08): idempotency-key format shared with
# AgentWorld._require_idem (duplicated here to avoid a service->wallet
# import cycle). Keys must be 8..128 chars, alphanumeric plus : _ - .
_IDEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_\-\.]{6,126}[A-Za-z0-9]$")


def _require_withdraw_idem(key):
    cleaned = (key or "").strip() if isinstance(key, str) else ""
    if not cleaned:
        raise ValueError("idempotency_key is required for safe_withdraw")
    if not _IDEM_RE.match(cleaned):
        raise ValueError(
            "idempotency_key for safe_withdraw must be 8..128 chars, "
            "alphanumeric plus : _ - .")
    return cleaned


class SafeWallet:
    """Receive-first software safe for test-mode DAiL funds.

    The safe is deliberately not a real payment processor or bank account.
    Withdrawal secrets are stored only as SHA-256 hashes and are returned once.
    """

    def __init__(self, ledger, audit, admin_key=None, store=None):
        self.ledger = ledger
        self.audit = audit
        self.admin_key = admin_key
        self.store = store
        self.receive_address = f"dail-safe-{secrets.token_urlsafe(18)}"
        self._withdrawal_key_hash = None
        self._withdrawal_key_id = None
        self._key_version = 0
        # Withdrawal keys must survive restarts: the hash (never the raw key)
        # is persisted to KV. Without this, a deploy silently breaks
        # /safe/withdraw until an admin notices (2026-10-07).
        try:
            saved = (self.store.kv_get("safe_withdrawal_key", {}) or {}) if self.store else {}
            if saved.get("hash"):
                self._withdrawal_key_hash = saved["hash"]
                self._withdrawal_key_id = saved.get("key_id")
                self._key_version = int(saved.get("version", 0) or 0)
        except Exception:
            pass

    def _persist_key(self):
        if self.store:
            try:
                self.store.kv_set("safe_withdrawal_key", {
                    "hash": self._withdrawal_key_hash,
                    "key_id": self._withdrawal_key_id,
                    "version": self._key_version,
                })
            except Exception:
                pass

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

    def receive(self, agent_id, amount, provider, idem):
        # A safe deposit moves the agent's own DAIL into the safe — it never
        # creates DAIL. (The old pure-credit version was a minting hole.)
        tx = self.ledger.transfer(agent_id, SAFE_ACCOUNT, amount,
                                   kind="safe_receive", idem=idem)
        self.audit.append("safe.received", {
            "amount": amount,
            "provider": provider,
            "agent_id": agent_id,
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
        self._persist_key()
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
        self._persist_key()
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
        # FIX 5 (2026-10-08): withdrawals are money movement; the
        # idempotency key is mandatory and validated (blank keys rejected).
        idem = _require_withdraw_idem(idem)
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
