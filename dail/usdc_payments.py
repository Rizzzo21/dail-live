"""USDC on-ramp for DAiL (v1: Base only).

An agent (or its human) sends native USDC on Base to the DAiL treasury
address, then submits the transaction hash. The backend verifies the
transfer on-chain via Base RPC and credits DAIL 1:1 per whole USDC
received. One-way only: DAIL is never redeemable for USDC.

Safety properties (mirroring the Stripe rail):
- The chain is the source of truth. We fetch the tx receipt ourselves and
  only credit what the receipt proves arrived at our address as real,
  Circle-issued USDC. The agent's claim is never trusted.
- The token contract address is pinned; lookalike/bridged tokens are
  rejected.
- A minimum confirmation count blunts reorg double-spend.
- Credit happens BEFORE the deposit row is marked paid, and the credit is
  idempotent on the tx hash, so retries and concurrent confirms can never
  double-credit. (The reverse order could mark a deposit paid and then fail
  to credit, losing customer funds silently.)
- Deposit rows live in PostgreSQL (SQLite for local dev), so a restart can
  neither lose a credit nor re-credit a tx.

Config (environment):
- DAIL_USDC_TREASURY: Base address receiving USDC. Defaults to the DAiL
  deposit wallet. Receive-only: the backend never spends from it.
- BASE_RPC_URL: Base JSON-RPC endpoint (default: public mainnet endpoint).
- DAIL_USDC_MIN_CONF: confirmations required before credit (default 2).
"""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BASE_CHAIN_ID = 8453
BASE_USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA4a1309"  # native USDC, 6 decimals
USDC_DECIMALS = 6
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
DEFAULT_RPC_URL = "https://mainnet.base.org"
FALLBACK_RPC_URLS = ["https://base.llamarpc.com", "https://1rpc.io/base"]
DEFAULT_TREASURY = "0xCd787bCf82279c121835EaaB37b34A502A6b8dBC"
INTENT_TTL_SECONDS = 24 * 3600
MIN_DAIL, MAX_DAIL = 1, 100000

_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _now():
    return datetime.now(timezone.utc).isoformat()


class UsdcError(ValueError):
    """Agent-facing USDC rail failure (maps to HTTP 400)."""


class UsdcNotReady(RuntimeError):
    """Rail not configured (maps to HTTP 503)."""


class UsdcPayments:
    def __init__(self, dail):
        self.dail = dail
        self.treasury = (os.getenv("DAIL_USDC_TREASURY", DEFAULT_TREASURY) or "").strip()
        primary_rpc = (os.getenv("BASE_RPC_URL", DEFAULT_RPC_URL) or "").strip()
        self.rpc_urls = []
        for u in [primary_rpc] + FALLBACK_RPC_URLS:
            if u and u not in self.rpc_urls:
                self.rpc_urls.append(u)
        self.rpc_url = self.rpc_urls[0] if self.rpc_urls else ""
        try:
            self.min_confirmations = max(1, int(os.getenv("DAIL_USDC_MIN_CONF", "2")))
        except ValueError:
            self.min_confirmations = 2
        self.database_url = os.getenv("DATABASE_URL", "")
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        self._lock = threading.Lock()
        self.engine = None
        if self.database_url:
            self.engine = create_engine(self.database_url, pool_pre_ping=True)
            self._init_db()

    @property
    def ready(self):
        return bool(self.treasury and self.rpc_url and self.database_url and self.engine)

    def _require_ready(self):
        if not self.ready:
            raise UsdcNotReady(
                "usdc_rail_not_configured: set DAIL_USDC_TREASURY and DATABASE_URL")

    def status(self):
        return {
            "rail": "usdc", "chain": "base", "chain_id": BASE_CHAIN_ID,
            "configured": self.ready,
            "deposit_address": self.treasury if self.ready else None,
            "token": "USDC", "token_contract": BASE_USDC_CONTRACT,
            "min_confirmations": self.min_confirmations,
            "dail_per_usdc": 1, "redeemable": False,
            "how": ("POST /payments/usdc/intent {agent_id, dail_amount} -> send USDC "
                    "on Base to deposit_address -> POST /payments/usdc/confirm "
                    "{agent_id, intent_id, tx_hash}; DAIL credited 1:1 per whole USDC received."),
        }

    def _init_db(self):
        # CREATE and the migration ALTERs run in separate transactions: on
        # PostgreSQL a failed statement aborts the whole transaction, so a
        # duplicate-column error must never roll back table creation.
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_usdc_deposits (
                id VARCHAR(64) PRIMARY KEY, agent_id VARCHAR(255) NOT NULL,
                expected_dail INTEGER NOT NULL, status VARCHAR(32) NOT NULL,
                tx_hash VARCHAR(128) UNIQUE, credited_dail INTEGER,
                created_at VARCHAR(64) NOT NULL, credited_at VARCHAR(64),
                transaction_id VARCHAR(255), idempotency_key VARCHAR(255) UNIQUE NOT NULL
            )"""))
        for alter in (
                "ALTER TABLE dail_usdc_deposits ADD COLUMN IF NOT EXISTS credited_dail INTEGER",
                "ALTER TABLE dail_usdc_deposits ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255)"):
            try:
                with self.engine.begin() as c:
                    c.execute(text(alter))
            except Exception:
                pass  # column exists (SQLite predates IF NOT EXISTS support)

    def list_deposits(self, limit=100):
        """Admin reconciliation view: newest USDC deposits first. Returns []
        when the rail is not configured (no engine)."""
        if not self.engine:
            return []
        self._init_db()  # self-healing: ensure the table exists
        with self.engine.begin() as c:
            rows = c.execute(
                text("SELECT id, agent_id, expected_dail, status, tx_hash, credited_dail,"
                     " created_at, credited_at, transaction_id FROM dail_usdc_deposits"
                     " ORDER BY created_at DESC LIMIT :lim"),
                {"lim": limit}).fetchall()
        return [{"intent_id": r[0], "agent_id": r[1], "expected_dail": r[2],
                 "status": r[3], "tx_hash": r[4], "credited_dail": r[5],
                 "created_at": r[6], "credited_at": r[7],
                 "transaction_id": r[8]} for r in rows]

    def announce_to(self, agent_id):
        """Tell one agent the USDC rail exists and how to use it."""
        self.dail.world_agents.notifications.setdefault(agent_id, []).append({
            "type": "payment_rail_live",
            "rail": "usdc",
            "title": "DAiL top-up now accepts USDC",
            "body": ("Fund your agent with USDC on Base — no card needed: "
                     "GET /payments/usdc/status for the deposit address, "
                     "POST /payments/usdc/intent {agent_id, dail_amount}, send USDC, "
                     "then POST /payments/usdc/confirm {agent_id, intent_id, tx_hash}. "
                     "1 DAIL per whole USDC, one-way (DAIL is never redeemable)."),
        })

    def announce(self):
        """Broadcast the USDC rail to every agent. Admin-triggered only."""
        count = 0
        for aid in list(self.dail.agents):
            self.announce_to(aid)
            count += 1
        self.dail.audit.append("payment.usdc_announced", {"agents_notified": count})
        return {"announced": True, "agents_notified": count}

    # ---- JSON-RPC ----
    def _rpc(self, method, params):
        """Base JSON-RPC with fallback endpoints. The public primary is
        rate-limited and flakes; a flaky RPC must never block a legitimate
        deposit, so we try each URL in order and only fail when all do."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        last_err = None
        for url in self.rpc_urls:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode())
            except Exception as e:
                last_err = e
                continue
            if not isinstance(resp, dict) or "error" in resp:
                last_err = resp.get("error") if isinstance(resp, dict) else "bad_response"
                continue
            return resp.get("result")
        raise UsdcError(f"base_rpc_unreachable: {last_err!r}"[:200])

    def _verify_onchain(self, tx_hash):
        """Returns (usdc_base_units_to_treasury, confirmations).

        Raises UsdcError when the tx does not prove real USDC arrived.
        """
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            raise UsdcError("transaction not found on Base (is the hash correct and the tx sent?)")
        if receipt.get("status") != "0x1":
            raise UsdcError("transaction failed on-chain; no credit")
        total = 0
        for log in receipt.get("logs") or []:
            if (log.get("address") or "").lower() != BASE_USDC_CONTRACT.lower():
                continue  # not real USDC: ignore lookalike tokens
            topics = log.get("topics") or []
            if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != self.treasury.lower():
                continue  # USDC moved, but not to us
            try:
                total += int(log.get("data") or "0x0", 16)
            except ValueError:
                continue
        if total <= 0:
            raise UsdcError("no USDC transfer to the DAiL deposit address in this transaction")
        latest = self._rpc("eth_blockNumber", [])
        try:
            confs = int(latest, 16) - int(receipt["blockNumber"], 16)
        except (TypeError, ValueError):
            raise UsdcError("could not determine confirmations; try again")
        return total, confs

    # ---- intents ----
    def _row(self, intent_id):
        with self.engine.begin() as c:
            return c.execute(
                text("SELECT id, agent_id, expected_dail, status, tx_hash, credited_dail,"
                     " created_at, credited_at, transaction_id, idempotency_key"
                     " FROM dail_usdc_deposits WHERE id=:i"),
                {"i": intent_id}).fetchone()

    @staticmethod
    def _record(row):
        return {"intent_id": row[0], "agent_id": row[1], "expected_dail": row[2],
                "status": row[3], "tx_hash": row[4], "credited_dail": row[5],
                "created_at": row[6], "credited_at": row[7],
                "transaction_id": row[8], "idempotency_key": row[9]}

    def create_intent(self, agent_id, dail_amount, idempotency_key=None):
        self._require_ready()
        self._init_db()  # self-healing: ensure the table exists
        if agent_id not in self.dail.agents:
            raise KeyError("agent not found")
        try:
            dail_amount = int(dail_amount)
        except (TypeError, ValueError):
            raise UsdcError("dail_amount must be an integer")
        if not (MIN_DAIL <= dail_amount <= MAX_DAIL):
            raise UsdcError(f"dail_amount must be {MIN_DAIL}..{MAX_DAIL}")
        key = idempotency_key or f"usdc-intent:{agent_id}:{dail_amount}:{int(time.time()*1000)}"
        with self._lock, self.engine.begin() as c:
            row = c.execute(
                text("SELECT id, agent_id, expected_dail, status, tx_hash, credited_dail,"
                     " created_at, credited_at, transaction_id, idempotency_key"
                     " FROM dail_usdc_deposits WHERE idempotency_key=:k"),
                {"k": key}).fetchone()
            if row:
                return self._intent_view(self._record(row))
            intent_id = f"usdci_{uuid4().hex[:12]}"
            now = _now()
            c.execute(
                text("INSERT INTO dail_usdc_deposits (id, agent_id, expected_dail, status,"
                     " tx_hash, created_at, idempotency_key)"
                     " VALUES (:id, :aid, :amt, 'pending', NULL, :now, :key)"),
                {"id": intent_id, "aid": agent_id, "amt": dail_amount,
                 "now": now, "key": key})
            return self._intent_view({
                "intent_id": intent_id, "agent_id": agent_id,
                "expected_dail": dail_amount, "status": "pending",
                "tx_hash": None, "created_at": now})

    def _intent_view(self, record):
        try:
            exp_ts = datetime.fromisoformat(record.get("created_at") or "").timestamp()
            expires_at = datetime.fromtimestamp(
                exp_ts + INTENT_TTL_SECONDS, timezone.utc).isoformat()
        except ValueError:
            expires_at = None
        return {
            "intent_id": record["intent_id"], "agent_id": record["agent_id"],
            "status": record["status"],
            "deposit_address": self.treasury,
            "chain": "base", "chain_id": BASE_CHAIN_ID,
            "token": "USDC", "token_contract": BASE_USDC_CONTRACT,
            "expected_dail": record["expected_dail"],
            "credited_dail": record.get("credited_dail"),
            "tx_hash": record.get("tx_hash"),
            "expires_at": record.get("expires_at"),
            "instructions": ("Send USDC on Base to deposit_address, then POST "
                             "/payments/usdc/confirm with {agent_id, intent_id, tx_hash}. "
                             "You are credited 1 DAIL per whole USDC received."),
        }

    # ---- confirm ----
    def confirm_deposit(self, agent_id, intent_id, tx_hash, idempotency_key=None):
        self._require_ready()
        self._init_db()
        if not tx_hash or not _TX_RE.match(tx_hash.strip()):
            raise UsdcError("tx_hash must be a 0x-prefixed 64-hex transaction hash")
        tx_hash = tx_hash.strip().lower()
        key = idempotency_key or f"usdc-confirm:{intent_id}:{tx_hash}"
        with self._lock:
            row = self._row(intent_id)
            if not row:
                raise KeyError("intent not found")
            record = self._record(row)
            if record["agent_id"] != agent_id:
                raise UsdcError("intent belongs to a different agent")
            if record["status"] == "paid":
                return {"received": True, "duplicate": True, **self._intent_view(record)}
            if record["status"] == "expired":
                raise UsdcError("intent expired; create a new one")
            try:
                created_ts = datetime.fromisoformat(record["created_at"]).timestamp()
            except ValueError:
                created_ts = 0
            if time.time() - created_ts > INTENT_TTL_SECONDS:
                with self.engine.begin() as c:
                    c.execute(text("UPDATE dail_usdc_deposits SET status='expired'"
                                   " WHERE id=:i AND status='pending'"), {"i": intent_id})
                raise UsdcError("intent expired; create a new one")
            # One tx hash credits exactly once, ever.
            with self.engine.begin() as c:
                prior = c.execute(
                    text("SELECT id, agent_id, expected_dail, status, tx_hash, credited_dail,"
                         " created_at, credited_at, transaction_id, idempotency_key"
                         " FROM dail_usdc_deposits WHERE tx_hash=:h"),
                    {"h": tx_hash}).fetchone()
            if prior and prior[3] == "paid":
                return {"received": True, "duplicate": True,
                        "note": "this transaction was already credited",
                        **self._intent_view(self._record(prior))}
            usdc_units, confs = self._verify_onchain(tx_hash)
            if confs < self.min_confirmations:
                raise UsdcError(
                    f"insufficient confirmations ({confs}/{self.min_confirmations}); wait and retry")
            dail_amount = usdc_units // (10 ** USDC_DECIMALS)
            if dail_amount < 1:
                raise UsdcError("less than 1 whole USDC received; minimum credit is 1 DAIL")
            if agent_id not in self.dail.agents:
                raise UsdcError(f"agent {agent_id} no longer exists: manual reconciliation required")
            # Credit FIRST: idempotent on usdc:<txhash>, so retries and
            # concurrent confirms can never double-credit. Only then mark
            # the deposit paid.
            tx = self.dail.ledger.credit(agent_id, dail_amount,
                                         kind="usdc_deposit", idem=f"usdc:{tx_hash}")
            self.dail.agents[agent_id].balance = self.dail.ledger.balances[agent_id]
            now = _now()
            try:
                with self.engine.begin() as c:
                    updated = c.execute(
                        text("UPDATE dail_usdc_deposits SET status='paid', tx_hash=:h,"
                             " credited_dail=:d, credited_at=:now, transaction_id=:tx"
                             " WHERE id=:i AND status='pending'"),
                        {"h": tx_hash, "d": dail_amount, "now": now,
                         "tx": tx.id, "i": intent_id}).rowcount
            except IntegrityError:
                # Lost a race with a concurrent confirm of the same tx hash:
                # our credit was deduplicated by the idempotency key.
                return {"received": True, "duplicate": True,
                        "note": "this transaction was already credited",
                        "intent_id": intent_id, "tx_hash": tx_hash,
                        "transaction_id": tx.id}
            self.dail.audit.append("payment.usdc_verified",
                                   {"intent_id": intent_id, "agent_id": agent_id,
                                    "tx_hash": tx_hash, "usdc_base_units": usdc_units,
                                    "amount_dail": dail_amount, "transaction": tx.id})
            self.dail.world_agents.notifications.setdefault(agent_id, []).append(
                {"type": "topup_credited", "rail": "usdc", "intent_id": intent_id,
                 "tx_hash": tx_hash, "amount_dail": dail_amount,
                 "transaction_id": tx.id})
            if updated == 0:
                return {"received": True, "duplicate": True,
                        **self._intent_view({**record, "status": "paid"})}
            return {"received": True, "duplicate": False,
                    "intent_id": intent_id, "agent_id": agent_id,
                    "tx_hash": tx_hash, "credited_dail": dail_amount,
                    "transaction_id": tx.id, "confirmations": confs}
