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
import os, threading, hashlib
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None

PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
EXPIRED_EVENTS = ("checkout.session.expired",)
FAILED_EVENTS = ("checkout.session.async_payment_failed", "payment_intent.payment_failed")

MAX_PENDING_PER_AGENT = 25


class PaymentRateLimited(Exception):
    """Too many pending checkout sessions for this agent."""
    pass


class PaymentIdempotencyConflict(Exception):
    """A Stripe idempotency key was replayed with different parameters.

    Surfaces as HTTP 409, not a 500 leaking Stripe internals (bnty_0227).
    """
    pass


class PaymentBadAmount(Exception):
    """A checkout amount failed validation (e.g. not a multiple of 100 cents).

    Surfaces as HTTP 422 on /payments/checkout and HTTP 400 on /customers/me/checkout.
    """
    pass


class PaymentCardDeclined(Exception):
    """Card or payment method was declined by Stripe.

    Surfaces as HTTP 402 with a plain, actionable message and zero ledger mutation (bnty_0241).
    """
    pass


class PaymentInvalidSession(Exception):
    """Checkout session or payment request parameters are expired or invalid.

    Surfaces as HTTP 400 with a plain message (bnty_0241).
    """
    pass


class PaymentUpstreamOutage(Exception):
    """Stripe is unreachable, rate-limiting, or returning a 5xx/API error.

    Surfaces as HTTP 502 with retry guidance and zero ledger mutation (bnty_0241).
    """
    pass


def _raise_clean_checkout_error(e):
    """Map any Stripe SDK or transport exception during checkout session
    creation to a clean, sanitized domain exception with zero internal
    request IDs or stack traces (bnty_0241).
    """
    if isinstance(e, (PaymentRateLimited, PaymentIdempotencyConflict,
                      PaymentBadAmount, PaymentCardDeclined,
                      PaymentInvalidSession, PaymentUpstreamOutage)):
        raise e
    ename = type(e).__name__
    code = str(getattr(e, "code", "") or "").lower()
    if ename == "IdempotencyError":
        raise PaymentIdempotencyConflict(
            "idempotency_key was already used with different "
            "parameters: replay the original request or use a "
            "new key")
    if ename == "CardError" or code in (
        "card_declined", "insufficient_funds", "expired_card",
        "incorrect_cvc", "processing_error", "incorrect_number",
    ):
        raise PaymentCardDeclined(
            "card declined: your card was not charged. Please check your card "
            "details or try another payment method.")
    if ename in ("InvalidRequestError", "InvalidSessionError") or code in (
        "session_expired", "resource_missing", "url_invalid",
        "parameter_invalid_empty", "parameter_invalid_integer",
        "amount_too_small", "amount_too_large", "invalid_session",
    ):
        raise PaymentInvalidSession(
            "invalid or expired checkout session: please start a new top-up "
            "from your dashboard.")
    raise PaymentUpstreamOutage(
        "payment provider temporarily unavailable: please wait a moment and "
        "retry. Your card was not charged and your DAIL balance was not modified.")


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
        """CREATE and the migration ALTER run in separate transactions: on
        PostgreSQL a failed statement aborts the whole transaction, so a
        duplicate-column error from the migration must never be able to
        roll back the table creation.
        """
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_payments (
                id VARCHAR(255) PRIMARY KEY, agent_id VARCHAR(255) NOT NULL,
                session_id VARCHAR(255) UNIQUE NOT NULL, usd_cents INTEGER NOT NULL,
                dail_amount INTEGER NOT NULL, status VARCHAR(32) NOT NULL,
                created_at VARCHAR(64) NOT NULL, paid_at VARCHAR(64),
                transaction_id VARCHAR(255), payment_intent VARCHAR(255)
            )"""))
        # Webhook delivery bookkeeping (bnty_0163). Two tables:
        #   dail_webhook_events   — one row per processed Stripe event id, so a
        #                           replayed event (same id) is a no-op even if
        #                           it arrives with a different type/shape.
        #   dail_webhook_deferred — a charge.refunded that arrived BEFORE the
        #                           checkout.session.completed that pays the
        #                           session. The refund cannot be applied yet
        #                           (no paid payment row), so it is parked here
        #                           and drained the moment the credit lands.
        #                           Otherwise the agent keeps DAIL for a charge
        #                           Stripe already refunded.
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_webhook_events (
                event_id VARCHAR(255) PRIMARY KEY,
                event_type VARCHAR(64),
                received_at VARCHAR(64) NOT NULL
            )"""))
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_webhook_deferred (
                payment_intent VARCHAR(255) PRIMARY KEY,
                amount_refunded_cents INTEGER NOT NULL,
                created_at VARCHAR(64) NOT NULL
            )"""))
        # Migrations for databases created before a column landed. SQLite does
        # not support "ADD COLUMN IF NOT EXISTS", so probe the column list and
        # add only what is missing (idempotent on both SQLite and Postgres).
        self._ensure_columns("dail_payments",
                             {"transaction_id": "VARCHAR(255)",
                              "payment_intent": "VARCHAR(255)"})

    def _ensure_columns(self, table, columns):
        if not self.engine:
            return
        try:
            with self.engine.begin() as c:
                existing = {row[1] for row in
                            c.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        except Exception:
            existing = None
        if existing is not None:  # SQLite path
            for col, ddl in columns.items():
                if col not in existing:
                    try:
                        with self.engine.begin() as c:
                            c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    except Exception:
                        pass
            return
        for col, ddl in columns.items():  # Postgres path
            try:
                with self.engine.begin() as c:
                    c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
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

    def get_payment(self, session_id):
        """Full payment record for a checkout session, or None.

        The refund rail (bnty_0162/bnty_0131) reads ``agent_id``,
        ``dail_amount``, ``status`` and the payment_intent off this record.
        Returns None when the rail is not configured or the session is
        unknown, so callers get a clean 404 instead of an AttributeError.
        """
        if not self.engine:
            return None
        self._init_db()
        row = self._get(session_id)
        if not row:
            return None
        rec = self._record(row)
        rec["id"] = row[0]
        rec["paid_at"] = row[7]
        rec["payment_intent"] = self.payment_intent_for(session_id)
        return rec

    def payment_intent_for(self, session_id):
        """The Stripe payment_intent behind a paid session, or None.

        The checkout webhook stores the session id, not the intent; the intent
        is what ``stripe.Refund.create`` needs. Recorded at webhook time when
        Stripe supplies it, otherwise recovered from the live Stripe API.
        """
        if not self.engine:
            return None
        try:
            with self.engine.begin() as c:
                v = c.execute(text("SELECT payment_intent FROM dail_payments "
                                   "WHERE session_id=:sid"), {"sid": session_id}).scalar()
        except Exception:
            v = None
        if v:
            return v
        if stripe is None or not self.stripe_secret:
            return None
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            pi = getattr(s, "payment_intent", None) or (
                s.get("payment_intent") if isinstance(s, dict) else None)
            if pi:
                self.record_payment_intent(session_id, pi)
            return pi
        except Exception:
            return None

    def record_payment_intent(self, session_id, payment_intent):
        """Persist the payment_intent for a session so refunds are offline-safe."""
        if not self.engine or not payment_intent:
            return
        try:
            with self.engine.begin() as c:
                c.execute(text("UPDATE dail_payments SET payment_intent=:pi "
                               "WHERE session_id=:sid"),
                          {"pi": payment_intent, "sid": session_id})
        except Exception:
            pass

    def get_payment_by_intent(self, payment_intent):
        """Payment record for a Stripe payment_intent (webhook reconciliation)."""
        if not self.engine or not payment_intent:
            return None
        self._init_db()
        with self.engine.begin() as c:
            row = c.execute(text(
                "SELECT id, agent_id, session_id, usd_cents, dail_amount, status,"
                " created_at, paid_at, transaction_id FROM dail_payments"
                " WHERE payment_intent=:pi ORDER BY created_at DESC LIMIT 1"),
                {"pi": payment_intent}).fetchone()
        if not row:
            return None
        rec = self._record(row)
        rec["id"] = row[0]
        rec["payment_intent"] = payment_intent
        return rec

    def create_checkout(self, agent_id, usd_cents, success_url, cancel_url, idempotency_key=None):
        return self._create_checkout_for(agent_id, usd_cents, success_url,
                                         cancel_url, idempotency_key)

    def create_customer_checkout(self, customer_id, usd_cents, success_url,
                                 cancel_url, idempotency_key=None):
        """Stripe checkout for a human customer. Stored with a customer:
        prefix so the webhook credits the right ledger account."""
        # Validate the customer exists
        self.dail.customers.get_customer(customer_id)
        return self._create_checkout_for(f"customer:{customer_id}", usd_cents,
                                         success_url, cancel_url,
                                         idempotency_key)

    def _create_checkout_for(self, account_id, usd_cents, success_url,
                             cancel_url, idempotency_key=None):
        self._require_ready()
        self._init_db()  # self-healing: ensure the payments table exists
        is_customer = account_id.startswith("customer:")
        if not is_customer and account_id not in self.dail.agents:
            raise KeyError("agent not found")
        if usd_cents < 100 or usd_cents > 1000000:
            raise ValueError("amount must be between $1 and $10,000")
        # bnty_0227 finding 2: DAIL is integer-only (1 DAIL = 100 cents).
        # Accepting e.g. 199 cents credited 1 DAIL and stranded 99 cents in
        # Stripe — the refund rail can only move whole DAIL.
        if usd_cents % 100 != 0:
            raise PaymentBadAmount(
                "usd_cents must be a multiple of 100 (1 DAIL = 100 cents)")
        dail_amount = (usd_cents * self.dail_per_usd) // 100
        if dail_amount <= 0:
            raise ValueError("amount produces zero DAIL")
        # FIX 5: a client-supplied Stripe idempotency key must be
        # well-formed; blank keys are rejected, not silently dropped.
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise ValueError("idempotency_key must be a string")
            idempotency_key = self.dail.world_agents._require_idem(
                idempotency_key, where="stripe checkout")
            # bnty_0227 finding 1: scope the key to (account, operation)
            # before it reaches Stripe. Stripe idempotency keys are global
            # per merchant account — forwarding the raw caller string let
            # one agent's key collide with another's (or across checkout vs
            # refund), surfacing Stripe's IdempotencyError as a 500.
            idempotency_key = "dail:" + hashlib.sha256(
                f"{account_id}:checkout:{idempotency_key}".encode()
            ).hexdigest()[:48]
        with self.engine.begin() as c:
            pending = c.execute(
                text("SELECT COUNT(*) FROM dail_payments WHERE agent_id=:a AND status='pending'"),
                {"a": account_id}).scalar()
        if pending >= MAX_PENDING_PER_AGENT:
            raise PaymentRateLimited(
                f"too many pending checkouts for {account_id} (max {MAX_PENDING_PER_AGENT})")
        create_kwargs = dict(
            mode="payment", payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "usd",
                                       "product_data": {"name": "DAiL credits"},
                                       "unit_amount": usd_cents},
                        "quantity": 1}],
            success_url=success_url, cancel_url=cancel_url,
            metadata={"agent_id": account_id, "dail_amount": str(dail_amount)})
        if idempotency_key:
            create_kwargs["idempotency_key"] = idempotency_key
        try:
            session = stripe.checkout.Session.create(**create_kwargs)
        except Exception as e:
            _raise_clean_checkout_error(e)
        if (getattr(session, "status", None) == "expired"
                or not getattr(session, "id", None)
                or not getattr(session, "url", None)):
            raise PaymentInvalidSession(
                "invalid or expired checkout session: please start a new top-up "
                "from your dashboard.")
        try:
            with self.engine.begin() as c:
                c.execute(text(
                    "INSERT INTO dail_payments (id, agent_id, session_id, usd_cents, dail_amount, status, created_at)"
                    " VALUES (:id,:agent,:sid,:usd,:dail,'pending',:created)"),
                    {"id": session.id, "agent": account_id, "sid": session.id,
                     "usd": usd_cents, "dail": dail_amount, "created": _now()})
        except IntegrityError:
            # Client retried with the same Stripe idempotency key: Stripe
            # returned the original session, so fall through and return our
            # original record instead of failing.
            pass
        result = self._record(self._get(session.id))
        result["checkout_url"] = session.url
        return result

    def _event_seen(self, event_id):
        """Record a Stripe event id; True if this id was already processed.

        A single INSERT with a PRIMARY KEY does the dedup atomically: the
        loser of a concurrent race gets IntegrityError and is treated as a
        duplicate. Events without an id (test fixtures, older payloads) are
        never deduped here — the session/intent-keyed idempotency below still
        protects the money movement.
        """
        if not event_id:
            return False
        try:
            with self.engine.begin() as c:
                c.execute(text("INSERT INTO dail_webhook_events "
                               "(event_id, event_type, received_at) "
                               "VALUES (:e,:t,:n)"),
                          {"e": event_id, "t": None, "n": _now()})
            return False
        except IntegrityError:
            return True

    def _defer_refund(self, payment_intent, amount_refunded_cents):
        """Park a charge.refunded that arrived before its payment was credited.

        Keeps the highest cumulative amount_refunded seen for the intent so a
        later replay cannot lower the parked total (out-of-order partials).
        """
        if not payment_intent:
            return
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO dail_webhook_deferred "
                "(payment_intent, amount_refunded_cents, created_at) "
                "VALUES (:pi,:amt,:n) "
                "ON CONFLICT(payment_intent) DO UPDATE SET "
                "amount_refunded_cents=MAX(dail_webhook_deferred.amount_refunded_cents, "
                "excluded.amount_refunded_cents)"),
                {"pi": payment_intent, "amt": int(amount_refunded_cents or 0),
                 "n": _now()})

    def _drain_deferred_refund(self, payment_intent):
        """Apply (once) a parked refund now that the payment is credited."""
        if not payment_intent:
            return None
        with self.engine.begin() as c:
            row = c.execute(text("SELECT amount_refunded_cents FROM "
                                 "dail_webhook_deferred WHERE payment_intent=:pi"),
                            {"pi": payment_intent}).fetchone()
        if not row:
            return None
        rail = getattr(self.dail, "refunds", None)
        if rail is None:
            return None
        res = rail.reconcile_charge_refunded(payment_intent, int(row[0]))
        if res.get("reconciled") or res.get("reason") != "unknown_payment_intent":
            # Applied, or already applied: drop the parked row.
            with self.engine.begin() as c:
                c.execute(text("DELETE FROM dail_webhook_deferred "
                               "WHERE payment_intent=:pi"), {"pi": payment_intent})
        return res

    def webhook(self, payload, signature):
        self._require_ready()
        self._init_db()  # self-healing: ensure the payments table exists
        try:
            event = stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
        except Exception as e:
            raise ValueError(f"invalid webhook: {e}")
        if not isinstance(event, dict) or not event.get("type"):
            raise ValueError("invalid webhook: missing event type")
        etype = event["type"]
        # Event-id dedup (bnty_0163): a replayed event is a no-op. Only after
        # this check do we look at the type, so a duplicate of any event type
        # is cheap and side-effect free.
        eid = event.get("id") if isinstance(event, dict) else None
        if self._event_seen(eid):
            return {"received": True, "handled": True, "duplicate": True,
                    "event": etype, "event_id": eid, "reason": "event_replayed"}
        if etype in EXPIRED_EVENTS:
            return self._handle_expired(event)
        if etype in FAILED_EVENTS:
            return self._handle_failed(event)
        if etype in ("charge.refunded", "charge.refund.updated"):
            return self._handle_charge_refunded(event)
        if etype not in PAID_EVENTS:
            return {"received": True, "handled": False, "event": etype}
        try:
            session = event["data"]["object"]
            sid = session["id"]
        except (KeyError, TypeError):
            raise ValueError("invalid webhook: missing session id")
        # Persist the payment_intent so the refund rail never has to call
        # Stripe just to learn it (bnty_0162/bnty_0131).
        pi = session.get("payment_intent") if isinstance(session, dict) else None
        if pi:
            self.record_payment_intent(sid, pi)
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
            is_customer = agent_id.startswith("customer:")
            if is_customer:
                # Human customer: validate + credit their shadow ledger account
                try:
                    self.dail.customers.get_customer(agent_id[len("customer:"):])
                except KeyError:
                    with self.engine.begin() as c:
                        c.execute(text("UPDATE dail_payments SET status='orphaned' WHERE session_id=:sid"),
                                  {"sid": sid})
                    raise ValueError(
                        f"customer {agent_id} not found for paid session {sid}: manual reconciliation required")
            elif agent_id not in self.dail.agents:
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
            if not is_customer:
                self.dail.agents[agent_id].balance = self.dail.ledger.balances[agent_id]
            with self.engine.begin() as c:
                updated = c.execute(
                    text("UPDATE dail_payments SET status='paid', paid_at=:paid, transaction_id=:tx"
                         " WHERE session_id=:sid AND status!='paid'"),
                    {"paid": _now(), "tx": tx.id, "sid": sid}).rowcount
            self.dail.audit.append("payment.stripe_verified",
                                   {"session_id": sid, "agent_id": agent_id,
                                    "amount_dail": dail_amount, "transaction": tx.id})
            # Out-of-order healing (bnty_0163): if a charge.refunded for this
            # payment_intent was parked because it arrived before this credit,
            # apply it now so the DAIL does not stay credited for a refunded
            # charge. Idempotent: the refund rail only reverses the not-yet
            # reversed delta, and the parked row is dropped once applied.
            if pi:
                self._drain_deferred_refund(pi)
            if updated == 0:
                # A concurrent delivery marked it paid first; our credit was
                # deduplicated by the idempotency key, so nothing was lost.
                # No notification: the first delivery already notified.
                return {"received": True, "handled": True, "duplicate": True,
                        "session_id": sid, "transaction_id": tx.id}
            # FIX 4: durable payment event + exactly-once notification via
            # the standard _notify() path (fires webhooks when configured).
            # Repeated Stripe callbacks for the same session can never
            # duplicate the event or the notification.
            # Customers don't get agent notifications — they see it on refresh.
            if not is_customer:
                self.dail.world_agents.notify_payment_event(
                    f"stripe:{sid}", agent_id, "stripe_topup", dail_amount,
                    {"type": "topup_credited", "rail": "stripe",
                     "session_id": sid, "amount_dail": dail_amount,
                     "transaction_id": tx.id})
            return {"received": True, "handled": True, "duplicate": False,
                    "session_id": sid, "transaction_id": tx.id}

    def _handle_expired(self, event):
        try:
            sid = event["data"]["object"]["id"]
        except (KeyError, TypeError):
            raise ValueError("invalid webhook: missing session id")
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_payments SET status='expired' WHERE session_id=:sid AND status='pending'"),
                      {"sid": sid})
        return {"received": True, "handled": True, "event": "checkout.session.expired", "session_id": sid}

    def _handle_failed(self, event):
        """Mark a pending payment failed without ever crediting DAIL (bnty_0241)."""
        try:
            obj = event["data"]["object"]
            sid = obj.get("id") if isinstance(obj, dict) else None
            pi = obj.get("payment_intent") if isinstance(obj, dict) else None
        except (AttributeError, KeyError, TypeError):
            raise ValueError("invalid webhook: malformed failed event")
        with self.engine.begin() as c:
            if sid:
                c.execute(text("UPDATE dail_payments SET status='failed' WHERE session_id=:sid AND status='pending'"),
                          {"sid": sid})
            if pi:
                c.execute(text("UPDATE dail_payments SET status='failed' WHERE payment_intent=:pi AND status='pending'"),
                          {"pi": pi})
        return {"received": True, "handled": True, "event": event.get("type"),
                "session_id": sid, "credited": False}

    def _handle_charge_refunded(self, event):
        """charge.refunded is the authoritative signal for a refund.

        A refund issued from the Stripe dashboard never touches our endpoint;
        this heals it by debiting the DAIL the refund reversed. Idempotent:
        the rail only applies the not-yet-debited delta, keyed on the
        payment_intent. Returns a handled=False shape when the charge is not
        one of our payments (e.g. a charge created outside checkout).
        """
        obj = event["data"]["object"]
        pi = obj.get("payment_intent") if isinstance(obj, dict) else None
        amount_refunded = obj.get("amount_refunded", 0) if isinstance(obj, dict) else 0
        rail = getattr(self.dail, "refunds", None)
        if rail is None:
            return {"received": True, "handled": False,
                    "event": "charge.refunded", "reason": "refund_rail_absent"}
        res = rail.reconcile_charge_refunded(pi, amount_refunded)
        if not res.get("reconciled") and res.get("reason") == "unknown_payment_intent":
            # Out-of-order delivery (bnty_0163): the refund arrived before the
            # checkout.session.completed that pays the session. Park it so the
            # credit path can apply it the moment the payment lands; dropping
            # it would leave the agent holding DAIL for a refunded charge.
            self._defer_refund(pi, amount_refunded)
            return {"received": True, "handled": True, "event": "charge.refunded",
                    "payment_intent": pi, "amount_refunded": amount_refunded,
                    "deferred": True, "reason": "awaiting_payment_credit"}
        return {"received": True, "handled": True, "event": "charge.refunded",
                "payment_intent": pi, "amount_refunded": amount_refunded,
                **res}

    def announce_to(self, agent_id):
        """Tell one agent the top-up rail exists and how to use it."""
        # FIX 4: route through the standard _notify() mechanism (fires
        # webhooks when configured) instead of appending directly.
        self.dail.world_agents._notify(agent_id, {
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
