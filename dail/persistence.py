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
                memo TEXT NOT NULL DEFAULT '',
                created_at VARCHAR(64) NOT NULL)""",
            # Migration for DBs created before the memo column existed.
            """ALTER TABLE dail_ledger_tx ADD COLUMN IF NOT EXISTS memo TEXT NOT NULL DEFAULT ''""",
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
            # FIX 2 (2026-10-08): dedicated agent-profile records. Profiles
            # were previously a single JSON blob in dail_kv; a dedicated
            # table makes them queryable and survives restarts individually.
            """CREATE TABLE IF NOT EXISTS dail_profiles (
                agent_id VARCHAR(255) PRIMARY KEY,
                display_name VARCHAR(255) NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '[]',
                reputation INTEGER NOT NULL DEFAULT 100,
                online BOOLEAN NOT NULL DEFAULT TRUE,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL)""",
            # FIX 3 (2026-10-08): dedicated completed-trade records. Trades
            # were previously a JSON blob in dail_kv; the table keeps trade
            # history queryable and enforces idempotency at the DB level so
            # a crash between the ledger transfers and the record insert can
            # never fork a duplicate trade.
            """CREATE TABLE IF NOT EXISTS dail_trades (
                trade_id VARCHAR(64) PRIMARY KEY,
                buyer_id VARCHAR(255) NOT NULL,
                seller_id VARCHAR(255) NOT NULL,
                principal_amount INTEGER NOT NULL,
                fee_amount INTEGER NOT NULL DEFAULT 0,
                total_amount INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'settled',
                idempotency_key VARCHAR(255) UNIQUE,
                created_at VARCHAR(64) NOT NULL,
                completed_at VARCHAR(64) NOT NULL)""",
            # FIX 4 (2026-10-08): durable payment events. One row per
            # successful top-up (Stripe/USDC/x402). The notification is sent
            # only when the event row is first inserted; notified_at records
            # delivery so a crash between insert and _notify() is recovered
            # by re-sending exactly once instead of losing or duplicating it.
            """CREATE TABLE IF NOT EXISTS dail_payment_events (
                event_key VARCHAR(255) PRIMARY KEY,
                agent_id VARCHAR(255) NOT NULL,
                kind VARCHAR(64) NOT NULL,
                amount_dail INTEGER NOT NULL,
                payload TEXT NOT NULL DEFAULT '',
                notified_at VARCHAR(64),
                created_at VARCHAR(64) NOT NULL)""",
            # FIX 5 (2026-10-08): DB-level idempotency registry for
            # service-layer operations (purchases, etc.). PRIMARY KEY on
            # (scope, key) makes duplicate economic requests impossible even
            # if the in-memory mapping is lost to a restart.
            """CREATE TABLE IF NOT EXISTS dail_idempotency (
                scope VARCHAR(64) NOT NULL,
                key VARCHAR(255) NOT NULL,
                ref_id VARCHAR(64) NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (scope, key))""",
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
                """INSERT INTO dail_ledger_tx (txid, kind, from_account, to_account, amount, idempotency_key, status, memo, created_at)
                   VALUES (:txid, :kind, :frm, :to, :amt, :idem, :status, :memo, :now)
                   ON CONFLICT (txid) DO NOTHING"""),
                {"txid": tx.id, "kind": tx.kind, "frm": tx.from_account,
                 "to": tx.to_account, "amt": tx.amount,
                 "idem": tx.idempotency_key, "status": tx.status,
                 "memo": tx.memo or "", "now": _now()})

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

    def agent_created_at(self, agent_id):
        """Registration timestamp for one agent, or None (no store / no row).

        Read-only; used by the public Agent Passport for the 'joined' field.
        """
        if not self.enabled:
            return None
        with self.engine.begin() as c:
            row = c.execute(
                text("SELECT created_at FROM dail_agents WHERE id=:aid"),
                {"aid": agent_id}).fetchone()
        return row[0] if row else None

    def delete_agent_key(self, agent_id):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text("DELETE FROM dail_agent_keys WHERE agent_id=:aid"),
                      {"aid": agent_id})

    # ---- FIX 2: agent profiles (dedicated table, not a kv blob) ----
    def save_profile(self, profile):
        """Upsert one agent profile. Called synchronously from
        ensure_agent()/update_profile() so a restart never loses it."""
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_profiles
                   (agent_id, display_name, bio, capabilities, reputation,
                    online, created_at, updated_at)
                   VALUES (:aid, :dn, :bio, :caps, :rep, :online, :ca, :ua)
                   ON CONFLICT (agent_id) DO UPDATE SET
                     display_name=EXCLUDED.display_name, bio=EXCLUDED.bio,
                     capabilities=EXCLUDED.capabilities,
                     reputation=EXCLUDED.reputation, online=EXCLUDED.online,
                     updated_at=EXCLUDED.updated_at"""),
                {"aid": profile["agent_id"],
                 "dn": profile.get("display_name", ""),
                 "bio": profile.get("bio", ""),
                 "caps": json.dumps(profile.get("capabilities", [])),
                 "rep": int(profile.get("reputation", 100)),
                 "online": bool(profile.get("online", True)),
                 "ca": profile.get("created_at") or _now(),
                 "ua": profile.get("updated_at") or _now()})

    def load_profiles(self):
        """All persisted profiles as dicts keyed for AgentWorld.profiles."""
        if not self.enabled:
            return {}
        out = {}
        with self.engine.begin() as c:
            rows = c.execute(text(
                "SELECT agent_id, display_name, bio, capabilities, reputation,"
                " online, created_at, updated_at FROM dail_profiles")).fetchall()
        for r in rows:
            try:
                caps = json.loads(r[3] or "[]")
            except Exception:
                caps = []
            out[r[0]] = {"agent_id": r[0], "display_name": r[1] or "",
                         "bio": r[2] or "", "capabilities": caps,
                         "reputation": r[4], "online": bool(r[5]),
                         "created_at": r[6], "updated_at": r[7]}
        return out

    # ---- FIX 3: completed trades (dedicated table) ----
    def save_trade(self, trade):
        """Upsert one settled trade. The UNIQUE(idempotency_key) constraint
        is the DB-level backstop: a retried trade with the same key can
        never fork a duplicate record."""
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                """INSERT INTO dail_trades
                   (trade_id, buyer_id, seller_id, principal_amount,
                    fee_amount, total_amount, status, idempotency_key,
                    created_at, completed_at)
                   VALUES (:tid, :bid, :sid, :pa, :fa, :ta, :st, :idem, :ca, :co)
                   ON CONFLICT (trade_id) DO UPDATE SET
                     status=EXCLUDED.status, completed_at=EXCLUDED.completed_at"""),
                {"tid": trade["id"], "bid": trade["buyer_id"],
                 "sid": trade["seller_id"],
                 "pa": trade.get("principal_amount", trade.get("amount", 0)),
                 "fa": trade.get("fee_amount", trade.get("fee", 0)),
                 "ta": trade.get("total_amount", trade.get("amount", 0)),
                 "st": trade.get("status", "settled"),
                 "idem": trade.get("idempotency_key"),
                 "ca": trade.get("created_at") or _now(),
                 "co": trade.get("completed_at") or _now()})

    def load_trades(self):
        """All persisted trades as dicts keyed for AgentWorld.trades."""
        if not self.enabled:
            return {}
        out = {}
        with self.engine.begin() as c:
            rows = c.execute(text(
                "SELECT trade_id, buyer_id, seller_id, principal_amount,"
                " fee_amount, total_amount, status, idempotency_key,"
                " created_at, completed_at FROM dail_trades")).fetchall()
        for r in rows:
            out[r[0]] = {"id": r[0], "buyer_id": r[1], "seller_id": r[2],
                         "principal_amount": r[3], "fee_amount": r[4],
                         "total_amount": r[5], "amount": r[3],
                         "fee": r[4], "seller_net": r[3] - r[4],
                         "status": r[6], "idempotency_key": r[7],
                         "created_at": r[8], "completed_at": r[9]}
        return out

    def find_trade_by_idem(self, idem):
        """DB-level idempotency lookup: returns the trade_id recorded for
        this key, or None. Survives restarts (unlike the in-memory map)."""
        if not self.enabled or not idem:
            return None
        with self.engine.begin() as c:
            row = c.execute(
                text("SELECT trade_id FROM dail_trades WHERE idempotency_key=:k"),
                {"k": idem}).fetchone()
        return row[0] if row else None

    # ---- FIX 4: durable payment events ----
    def record_payment_event(self, event_key, agent_id, kind, amount_dail,
                             payload=""):
        """Insert a payment event exactly once. Returns True when this call
        created the row (caller should notify), False when it already
        existed (caller must NOT notify again)."""
        if not self.enabled:
            return True  # persistence off: single-process, notify inline
        with self._lock, self.engine.begin() as c:
            res = c.execute(text(
                """INSERT INTO dail_payment_events
                   (event_key, agent_id, kind, amount_dail, payload, created_at)
                   VALUES (:ek, :aid, :kind, :amt, :pl, :now)
                   ON CONFLICT (event_key) DO NOTHING"""),
                {"ek": event_key, "aid": agent_id, "kind": kind,
                 "amt": amount_dail, "pl": payload or "", "now": _now()})
            return res.rowcount > 0

    def get_payment_event(self, event_key):
        """Returns (exists, notified_at) for crash-recovery checks."""
        if not self.enabled:
            return False, None
        with self.engine.begin() as c:
            row = c.execute(
                text("SELECT notified_at FROM dail_payment_events WHERE event_key=:ek"),
                {"ek": event_key}).fetchone()
        return (True, row[0]) if row else (False, None)

    def mark_event_notified(self, event_key):
        if not self.enabled:
            return
        with self._lock, self.engine.begin() as c:
            c.execute(text(
                "UPDATE dail_payment_events SET notified_at=:now WHERE event_key=:ek"),
                {"now": _now(), "ek": event_key})

    # ---- FIX 5: DB-level idempotency registry ----
    def idem_claim(self, scope, key, ref_id):
        """Claim (scope, key) -> ref_id exactly once. Returns the existing
        ref_id when the key was already claimed (idempotent replay), or
        None when this call claimed it fresh. DB PRIMARY KEY makes this
        atomic across processes and restarts."""
        if not self.enabled:
            return None
        with self._lock, self.engine.begin() as c:
            res = c.execute(text(
                """INSERT INTO dail_idempotency (scope, key, ref_id, created_at)
                   VALUES (:s, :k, :r, :now)
                   ON CONFLICT (scope, key) DO NOTHING"""),
                {"s": scope, "k": key, "r": ref_id, "now": _now()})
            if res.rowcount > 0:
                return None
            row = c.execute(
                text("SELECT ref_id FROM dail_idempotency WHERE scope=:s AND key=:k"),
                {"s": scope, "k": key}).fetchone()
            return row[0] if row else None

    def idem_lookup(self, scope, key):
        if not self.enabled or not key:
            return None
        with self.engine.begin() as c:
            row = c.execute(
                text("SELECT ref_id FROM dail_idempotency WHERE scope=:s AND key=:k"),
                {"s": scope, "k": key}).fetchone()
        return row[0] if row else None

    # ---- reads (startup restore) ----
    def load_all(self):
        """Returns (agent_rows, tx_rows, order_rows, kv_dict, service_rows, bulletin_rows)."""
        if not self.enabled:
            return [], [], [], {}, [], []
        with self.engine.begin() as c:
            agents = c.execute(text(
                "SELECT id, name, goal, spending_limit, approval_limit, status FROM dail_agents")).fetchall()
            txs = c.execute(text(
                "SELECT txid, kind, from_account, to_account, amount, idempotency_key, status, memo FROM dail_ledger_tx ORDER BY created_at, txid")).fetchall()
            orders = c.execute(text("SELECT order_id, data FROM dail_orders")).fetchall()
            services = c.execute(text("SELECT service_id, data FROM dail_services")).fetchall()
            bulletins = c.execute(text("SELECT bulletin_id, data FROM dail_bulletins")).fetchall()
            kv = {r[0]: json.loads(r[1]) for r in c.execute(text("SELECT key, value FROM dail_kv")).fetchall()}
        return agents, txs, orders, kv, services, bulletins
