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
import secrets


KEY_PREFIX = "dail_sk_"


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
        self._restore()

    def _restore(self):
        if not self.store or not self.store.enabled:
            return
        try:
            for agent_id, key_hash in self.store.load_agent_keys():
                self._hashes[key_hash] = agent_id
        except Exception:
            pass  # table may not exist yet on very old databases; keys issue fresh

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

    def has_key(self, agent_id: str) -> bool:
        return agent_id in self._hashes.values()
