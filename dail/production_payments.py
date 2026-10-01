"""Production payment boundary for DAiL.

Stripe is the external payment rail. A verified webhook is the only path that
marks a payment paid and credits DAIL. Payment records live in PostgreSQL when
production mode is enabled; SQLite is used only for local development.

Safety properties:
- Webhook signatures are verified with the Stripe webhook secret.
- The ledger is credited BEFORE the payment row is marked paid. The credit is
  idempotent on the Stripe session id, so a crash or a retried webhook can
  never lose a credit or double-credit an agent. (The reverse order could mark
  a payment paid and then fail to credit, losing customer funds silently.)
- Webhook processing is idempotent by Stripe session id, including under
  concurrent delivery (conditional UPDATE + rowcount check).
- Checkout amounts are bounded to $1-$10,000 per payment.
- Checkout sessions carry the DAiL agent id in Stripe metadata, but the
  webhook resolves the payment from our own database record, never from
  webhook metadata.
- Per-agent cap on pending checkouts blunts session-creation abuse.
- Stripe idempotency keys are supported so client retries never create
  duplicate checkout sessions.
"""
import os, threading
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None

PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
EXPIRED_EVENTS = ("checkout.session.expired",)

MAX_PENDING_PER_AGENT = 25


class PaymentRateLimited(Exception):
    """Too many pending checkout sessions for this agent."""
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class ProductionPayments:
    def __init__(self, dail):
        self.dail = dail
        self.enabled = os.getenv("DAIL_REAL_PAYMENTS", "false").lower() == "true"
        self.stripe_secret = os.getenv("STRIPE_SECRET_KEY", "")
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        self.database_url = os.getenv("DATABASE_URL", "")
        self.dail_per_usd = int(os.getenv("DAIL_PER_USD", "1"))
        self._lock = threading.Lock()
        self.engine = None
        if self.database_url:
            self.engine = create_engine(self.database_url, pool_pre_ping=True)
            self._init_db()
        if stripe and self.stripe_secret:
            stripe.api_key = self.stripe_secret

    @property
    def ready(self):
        return bool(self.enabled and stripe and self.stripe_secret and self.webhook_secret and self.database_url and self.engine and self.dail_per_usd > 0)

    @property
    def mode(self):
        """Honest rail mode derived from the Stripe key prefix. Never call
        test-mode funds 'real'."""
        if self.stripe_secret.startswith(("sk_live_", "rk_live_")):
            return "live"
        if self.stripe_secret.startswith(("sk_test_", "rk_test_")):
            return "test"
        return "unconfigured"

    @property
    def live_ready(self):
        """True only when real money can actually move."""
        return self.ready and self.mode == "live"

    def status(self):
        return {"provider": "stripe", "mode": self.mode, "production_enabled": self.enabled, "production_ready": self.ready, "live_ready": self.live_ready, "real_money": self.live_ready, "persistent_database_configured": bool(self.database_url), "dail_per_usd": self.dail_per_usd}

    def _init_db(self):
        # CREATE and the migration ALTER run in separate transactions: on
        # PostgreSQL a failed statement aborts the whole transaction, so a
        # duplicate-column error from the migration must never be able to
        # roll back the table creation.
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_payments (
                id VARCHAR(255) PRIMARY KEY, agent_id VARCHAR(255) NOT NULL,
                session_id VARCHAR(255) UNIQUE NOT NULL, usd_cents INTEGER NOT NULL,
                dail_amount INTEGER NOT NULL, status VARCHAR(32) NOT NULL,
                created_at VARCHAR(64) NOT NULL, paid_at VARCHAR(64),
                transaction_id VARCHAR(255)
            )"""))
        try:
            with self.engine.begin() as c:
                c.execute(text("ALTER TABLE dail_payments ADD COLUMN IF NOT EXISTS transaction_id VARCHAR(255)"))
        except Exception:
            pass

    def list_payments(self, limit=100):
        """Admin reconciliation view: newest payments first. Returns [] when
        the rail is not configured (no engine)."""
        if not self.engine:
            return []
        self._init_db()  # self-healing: ensure the payments table exists
        with self.engine.begin() as c:
            rows = c.execute(
                text("SELECT id, agent_id, session_id, usd_cents, dail_amount, status,"
                     " created_at, paid_at, transaction_id FROM dail_payments"
                     " ORDER BY created_at DESC LIMIT :lim"),
                {"lim": limit}).fetchall()
        return [{"id": r[0], "agent_id": r[1], "session_id": r[2],
                 "usd_cents": r[3], "dail_amount": r[4], "status": r[5],
                 "created_at": r[6], "paid_at": r[7],
                 "transaction_id": r[8]} for r in rows]

    def _require_ready(self):
        if not self.ready:
            raise RuntimeError("real_payments_not_ready: configure DAIL_REAL_PAYMENTS, Stripe secrets, DATABASE_URL, and dependencies")

    def _get(self, session_id):
        with self.engine.begin() as c:
            return c.execute(
                text("SELECT id, agent_id, session_id, usd_cents, dail_amount, status, created_at, paid_at, transaction_id FROM dail_payments WHERE session_id=:sid"),
                {"sid": session_id}).fetchone()

    @staticmethod
    def _record(row):
        return {"session_id": row[2], "agent_id": row[1], "usd_cents": row[3],
                "dail_amount": row[4], "status": row[5], "transaction_id": row[8]}

    def create_checkout(self, agent_id, usd_cents, success_url, cancel_url, idempotency_key=None):
        self._require_ready()
        self._init_db()  # self-healing: ensure the payments table exists
        if agent_id not in self.dail.agents:
            raise KeyError("agent not found")
        if usd_cents < 100 or usd_cents > 1000000:
            raise ValueError("amount must be between $1 and $10,000")
        dail_amount = (usd_cents * self.dail_per_usd) // 100
        if dail_amount <= 0:
            raise ValueError("amount produces zero DAIL")
        with self.engine.begin() as c:
            pending = c.execute(
                text("SELECT COUNT(*) FROM dail_payments WHERE agent_id=:a AND status='pending'"),
                {"a": agent_id}).scalar()
        if pending >= MAX_PENDING_PER_AGENT:
            raise PaymentRateLimited(
                f"too many pending checkouts for agent {agent_id} (max {MAX_PENDING_PER_AGENT})")
        create_kwargs = dict(
            mode="payment", payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "usd",
                                       "product_data": {"name": "DAiL credits"},
                                       "unit_amount": usd_cents},
                        "quantity": 1}],
            success_url=success_url, cancel_url=cancel_url,
            metadata={"agent_id": agent_id, "dail_amount": str(dail_amount)})
        if idempotency_key:
            session = stripe.checkout.Session.create(idempotency_key=idempotency_key, **create_kwargs)
        else:
            session = stripe.checkout.Session.create(**create_kwargs)
        try:
            with self.engine.begin() as c:
                c.execute(text(
                    "INSERT INTO dail_payments (id, agent_id, session_id, usd_cents, dail_amount, status, created_at)"
                    " VALUES (:id,:agent,:sid,:usd,:dail,'pending',:created)"),
                    {"id": session.id, "agent": agent_id, "sid": session.id,
                     "usd": usd_cents, "dail": dail_amount, "created": _now()})
        except IntegrityError:
            # Client retried with the same Stripe idempotency key: Stripe
            # returned the original session, so fall through and return our
            # original record instead of failing.
            pass
        result = self._record(self._get(session.id))
        result["checkout_url"] = session.url
        return result

    def webhook(self, payload, signature):
        self._require_ready()
        self._init_db()  # self-healing: ensure the payments table exists
        try:
            event = stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
        except Exception as e:
            raise ValueError(f"invalid webhook: {e}")
        etype = event["type"]
        if etype in EXPIRED_EVENTS:
            return self._handle_expired(event)
        if etype not in PAID_EVENTS:
            return {"received": True, "handled": False, "event": etype}
        session = event["data"]["object"]
        sid = session["id"]
        with self._lock:
            row = self._get(sid)
            if not row:
                raise ValueError("unknown checkout session")
            record = self._record(row)
            if record["status"] == "paid":
                return {"received": True, "handled": True, "duplicate": True,
                        "session_id": sid, "transaction_id": record["transaction_id"]}
            agent_id = record["agent_id"]
            dail_amount = int(record["dail_amount"])
            if agent_id not in self.dail.agents:
                with self.engine.begin() as c:
                    c.execute(text("UPDATE dail_payments SET status='orphaned' WHERE session_id=:sid"),
                              {"sid": sid})
                raise ValueError(
                    f"agent {agent_id} not found for paid session {sid}: manual reconciliation required")
            # Credit FIRST: idempotent on stripe:<session_id>, so retries and
            # concurrent deliveries can never double-credit. Only after the
            # credit succeeds do we mark the payment paid, so a crash between
            # the two is recovered by the next webhook retry instead of
            # silently losing the customer's credit.
            tx = self.dail.ledger.credit(agent_id, dail_amount, kind="stripe_deposit", idem=f"stripe:{sid}")
            self.dail.agents[agent_id].balance = self.dail.ledger.balances[agent_id]
            with self.engine.begin() as c:
                updated = c.execute(
                    text("UPDATE dail_payments SET status='paid', paid_at=:paid, transaction_id=:tx"
                         " WHERE session_id=:sid AND status!='paid'"),
                    {"paid": _now(), "tx": tx.id, "sid": sid}).rowcount
            self.dail.audit.append("payment.stripe_verified",
                                   {"session_id": sid, "agent_id": agent_id,
                                    "amount_dail": dail_amount, "transaction": tx.id})
            self.dail.world_agents.notifications.setdefault(agent_id, []).append(
                {"type": "topup_credited", "session_id": sid,
                 "amount_dail": dail_amount, "transaction_id": tx.id})
            if updated == 0:
                # A concurrent delivery marked it paid first; our credit was
                # deduplicated by the idempotency key, so nothing was lost.
                return {"received": True, "handled": True, "duplicate": True,
                        "session_id": sid, "transaction_id": tx.id}
            return {"received": True, "handled": True, "duplicate": False,
                    "session_id": sid, "transaction_id": tx.id}

    def _handle_expired(self, event):
        sid = event["data"]["object"]["id"]
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_payments SET status='expired' WHERE session_id=:sid AND status='pending'"),
                      {"sid": sid})
        return {"received": True, "handled": True, "event": "checkout.session.expired", "session_id": sid}

    def announce_to(self, agent_id):
        """Tell one agent the top-up rail exists and how to use it."""
        self.dail.world_agents.notifications.setdefault(agent_id, []).append({
            "type": "payment_rail_live",
            "title": "DAiL top-up is live",
            "body": ("Fund your agent with real money: POST /payments/checkout with "
                     "{agent_id, usd_cents ($1-$10,000)}, pay at the checkout_url, and the "
                     "Stripe webhook credits your DAIL automatically. Details: GET /payments/info. "
                     "To earn from other agents: POST /world/services to list a service, "
                     "POST /world/bulletins to advertise it."),
        })

    def announce(self):
        """Broadcast the payment rail to every agent. Admin-triggered."""
        count = 0
        for aid in list(self.dail.agents):
            self.announce_to(aid)
            count += 1
        self.dail.audit.append("payment.announced", {"agents_notified": count})
        return {"announced": True, "agents_notified": count}
