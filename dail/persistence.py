"""Write-through persistence for money-critical world state.

Money must survive restarts. Balances are *derived* from the ledger, so the
ledger's transaction log is the source of truth: every credit/transfer is
inserted as it happens, and on startup the log is replayed to rebuild
balances. Agents and escrow orders are persisted alongside (an order's escrow
funds live in the ledger, but the order metadata must survive too, otherwise
funds get stuck).

Non-money state (services, bulletins, notifications, profiles) stays
in-memory for now; losing it on restart is annoying but never loses funds.

Disabled when DATABASE_URL is unset: everything stays in-memory, exactly as
before.
"""
import json
import os
import threading
from datetime import datetime, timezone

try:
    from sqlalchemy import create_engine, text
    _HAVE_SA = True
except Exception:
    _HAVE_SA = False


def _now():
    return datetime.now(timezone.utc).isoformat()


class WorldStore:
    def __init__(self, database_url=None):
        self.database_url = database_url or os.getenv("DATABASE_URL", "")
        # SQLAlchemy 2.x wants postgresql://, Render/Railway hand out postgres://
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        self.engine = None
        self._lock = threading.Lock()
        if self.database_url and _HAVE_SA:
            self.engine = create_engine(self.database_url, pool_pre_ping=True)
            self._init_tables()

    @property
    def enabled(self):
        return self.engine is not None

    def _init_tables(self):
        # Each DDL statement in its own transaction: on PostgreSQL a failed
        # statement aborts the whole transaction, so table creation must
        # never share a transaction with a migration.
        stmts = [
            """CREATE TABLE IF NOT EXISTS dail_agents (
                id VARCHAR(255) PRIMARY KEY, name VARCHAR(255) NOT NULL,
                goal TEXT NOT NULL DEFAULT '', spending_limit INTEGER NOT NULL DEFAULT 10000,
                approval_limit INTEGER NOT NULL DEFAULT 2500,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at VARCHAR(64) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_ledger_tx (
                txid VARCHAR(64) PRIMARY KEY, kind VARCHAR(64) NOT NULL,
                from_account VARCHAR(255) NOT NULL, to_account VARCHAR(255) NOT NULL,
                amount INTEGER NOT NULL, idempotency_key VARCHAR(255) UNIQUE NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'posted',
                created_at VARCHAR(64) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_orders (
                order_id VARCHAR(64) PRIMARY KEY, data TEXT NOT NULL,
                updated_at VARCHAR(64) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_services (
                service_id VARCHAR(64) PRIMARY KEY, data TEXT NOT NULL,
                updated_at VARCHAR(64) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_bulletins (
                bulletin_id VARCHAR(64) PRIMARY KEY, data TEXT NOT NULL,
                updated_at VARCHAR(64) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_kv (
                key VARCHAR(128) PRIMARY KEY, value TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dail_agent_keys (
                agent_id VARCHAR(255) PRIMARY KEY, key_hash VARCHAR(64) NOT NULL,
                created_at VARCHAR(64) NOT NULL)""",
        ]
        for s in stmts:
            try:
                with self.engine.begin() as c:
                    c.execute(text(s))
            except Exception:
                pass  # table probably exists; keep going

    # ---- writes (fail closed: money must never silently diverge) ----
    def save_agent(self, agent):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_agents (id, name, goal, spending_limit, approval_limit, status, created_at)
                   VALUES (:id, :name, :goal, :sl, :al, :status, :now)
                   ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, goal=EXCLUDED.goal,
                     spending_limit=EXCLUDED.spending_limit, approval_limit=EXCLUDED.approval_limit,
                     status=EXCLUDED.status"""),
                {"id": agent.id, "name": agent.name, "goal": agent.goal,
                 "sl": agent.spending_limit, "al": agent.approval_limit,
                 "status": agent.status, "now": _now()})

    def record_tx(self, tx):
        """Persist one ledger transaction. Called synchronously on every
        credit/transfer; raises on failure so money can't diverge."""
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_ledger_tx (txid, kind, from_account, to_account, amount, idempotency_key, status, created_at)
                   VALUES (:txid, :kind, :frm, :to, :amt, :idem, :status, :now)
                   ON CONFLICT (txid) DO NOTHING"""),
                {"txid": tx.id, "kind": tx.kind, "frm": tx.from_account,
                 "to": tx.to_account, "amt": tx.amount,
                 "idem": tx.idempotency_key, "status": tx.status, "now": _now()})

    def recent_ledger_txs(self, limit=60):
        """Newest ledger transactions for the public activity feed.
        Projection of persisted state; returns [] when persistence is off."""
        if not self.enabled:
            return []
        with self._lock, self.engine.begin() as c:
            rows = c.execute(text(
                """SELECT txid, kind, from_account, to_account, amount,
                          idempotency_key, created_at
                   FROM dail_ledger_tx
                   ORDER BY created_at DESC, txid DESC
                   LIMIT :lim"""), {"lim": int(limit)}).fetchall()
        return [{"txid": r[0], "kind": r[1], "from_account": r[2],
                 "to_account": r[3], "amount": r[4],
                 "idempotency_key": r[5], "created_at": r[6]}
                for r in rows]

    def save_order(self, order):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_orders (order_id, data, updated_at)
                   VALUES (:oid, :data, :now)
                   ON CONFLICT (order_id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at"""),
                {"oid": order["id"], "data": json.dumps(order), "now": _now()})

    def save_service(self, service):
        """Persist a marketplace service listing (upsert by id)."""
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_services (service_id, data, updated_at)
                   VALUES (:sid, :data, :now)
                   ON CONFLICT (service_id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at"""),
                {"sid": service["id"], "data": json.dumps(service), "now": _now()})

    def save_bulletin(self, bulletin):
        """Persist a marketing bulletin (upsert by id)."""
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_bulletins (bulletin_id, data, updated_at)
                   VALUES (:bid, :data, :now)
                   ON CONFLICT (bulletin_id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at"""),
                {"bid": bulletin["id"], "data": json.dumps(bulletin), "now": _now()})

    def delete_bulletin(self, bulletin_id):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text("DELETE FROM dail_bulletins WHERE bulletin_id=:bid"),
                      {"bid": bulletin_id})

    def kv_set(self, key, value):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_kv (key, value) VALUES (:k, :v)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"""),
                {"k": key, "v": json.dumps(value)})

    # ---- agent API keys (hashes only; raw keys are never stored) ----
    def save_agent_key(self, agent_id, key_hash):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_agent_keys (agent_id, key_hash, created_at)
                   VALUES (:aid, :kh, :now)
                   ON CONFLICT (agent_id) DO UPDATE SET key_hash=EXCLUDED.key_hash,
                     created_at=EXCLUDED.created_at"""),
                {"aid": agent_id, "kh": key_hash, "now": _now()})

    def load_agent_keys(self):
        if not self.enabled:
            return []
        with self.engine.begin() as c:
            return c.execute(text("SELECT agent_id, key_hash FROM dail_agent_keys")).fetchall()

    def delete_agent_key(self, agent_id):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text("DELETE FROM dail_agent_keys WHERE agent_id=:aid"),
                      {"aid": agent_id})

    # ---- reads (startup restore) ----
    def load_all(self):
        """Returns (agent_rows, tx_rows, order_rows, kv_dict, service_rows, bulletin_rows)."""
        if not self.enabled:
            return [], [], [], {}, [], []
        with self.engine.begin() as c:
            agents = c.execute(text(
                "SELECT id, name, goal, spending_limit, approval_limit, status FROM dail_agents")).fetchall()
            txs = c.execute(text(
                "SELECT txid, kind, from_account, to_account, amount, idempotency_key, status FROM dail_ledger_tx ORDER BY created_at, txid")).fetchall()
            orders = c.execute(text("SELECT order_id, data FROM dail_orders")).fetchall()
            services = c.execute(text("SELECT service_id, data FROM dail_services")).fetchall()
            bulletins = c.execute(text("SELECT bulletin_id, data FROM dail_bulletins")).fetchall()
            kv = {r[0]: json.loads(r[1]) for r in c.execute(text("SELECT key, value FROM dail_kv")).fetchall()}
        return agents, txs, orders, kv, services, bulletins
