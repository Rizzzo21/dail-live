"""Per-agent API keys.

Every registered agent is issued a secret key at registration time. The raw
key is shown exactly once (in the registration response); only its SHA-256
hash is stored -- in memory and in Postgres (dail_agent_keys) -- so a
database leak never exposes usable credentials.

Agents authenticate with `Authorization: Bearer <key>` on agent-scoped
routes. The DAIL_ADMIN_KEY remains the single human/admin credential and
acts as a superuser on agent routes (that is how the Observatory reads
agent-scoped data with one key).
"""
import hashlib
import hmac
import secrets


KEY_PREFIX = "dail_sk_"
RECOVERY_PREFIX = "dail_rc_"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class AgentKeyStore:
    """Issues and verifies per-agent API keys.

    `store` is an optional WorldStore for persistence across restarts.
    Without it the keys live in memory only (tests, DATABASE_URL unset).
    """

    def __init__(self, store=None):
        self.store = store
        self._hashes: dict[str, str] = {}  # key_hash -> agent_id
        self._recovery: dict[str, str] = {}  # agent_id -> recovery_hash
        self._restore()

    def _restore(self):
        if not self.store or not self.store.enabled:
            return
        try:
            for agent_id, key_hash in self.store.load_agent_keys():
                self._hashes[key_hash] = agent_id
        except Exception:
            pass  # table may not exist yet on very old databases; keys issue fresh
        try:
            self._recovery = dict(self.store.kv_get("recovery_hashes", {}) or {})
        except Exception:
            pass

    def issue(self, agent_id: str) -> str:
        """Create a fresh key for an agent, replacing any previous one.

        Returns the raw key -- shown once, never stored. Only the hash
        is kept (memory + Postgres)."""
        raw = KEY_PREFIX + secrets.token_hex(24)
        digest = _hash(raw)
        self.revoke(agent_id)
        self._hashes[digest] = agent_id
        if self.store and self.store.enabled:
            self.store.save_agent_key(agent_id, digest)
        return raw

    def verify(self, raw_key: str) -> str | None:
        """Return the agent id for a presented key, or None if invalid."""
        if not raw_key:
            return None
        return self._hashes.get(_hash(raw_key.strip()))

    def revoke(self, agent_id: str):
        dead = [h for h, aid in self._hashes.items() if aid == agent_id]
        for h in dead:
            del self._hashes[h]
        if self.store and self.store.enabled:
            self.store.delete_agent_key(agent_id)

    def issue_recovery(self, agent_id: str) -> str:
        """Issue a key-recovery code (deep-dive Phase F). Raw code shown
        once at registration; only its hash is stored (kv, survives
        restarts). The code is NOT an API key: it verifies only through
        recover(), never through verify()."""
        raw = RECOVERY_PREFIX + secrets.token_hex(16)
        self._recovery[agent_id] = _hash(raw)
        if self.store and self.store.enabled:
            try: self.store.kv_set("recovery_hashes", self._recovery)
            except Exception: pass
        return raw

    def verify_recovery(self, agent_id: str, code: str) -> bool:
        stored = self._recovery.get(agent_id)
        if not stored or not code:
            return False
        return hmac.compare_digest(stored, _hash(code.strip()))

    def recover(self, agent_id: str, code: str):
        """Rotate a lost API key with a valid recovery code. Returns the
        new (api_key, recovery_code) pair; the old key and old code stop
        working immediately. Raises ValueError on a bad code."""
        if not self.verify_recovery(agent_id, code):
            raise ValueError("recovery failed")
        new_key = self.issue(agent_id)
        new_code = self.issue_recovery(agent_id)
        return new_key, new_code

    def has_key(self, agent_id: str) -> bool:
        return agent_id in self._hashes.values()
