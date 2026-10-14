"""Real-money Stripe refund rail for DAiL.

Reverses a real-money Stripe top-up (see ``production_payments.py``): a Stripe
Refund on the original payment_intent, plus the matching DAIL debit on the
ledger. This is the missing counterpart to the top-up path — before this
module a paid Stripe session could never be reversed, which is a landmine for
the first human tester (bnty_0116 spec, accepted; built under bnty_0162 and
bnty_0131).

Safety properties (mirroring the top-up rail):

- **Admin approval gate.** An agent can only *request* a refund; money moves
  only after an admin approves it (``POST /admin/payments/refunds/{id}/approve``)
  or the caller already holds the admin key (the human dashboard path).
  Approval is recorded on the row (``approved_by``), never trusted from a
  request body.
- **Idempotent.** A request is keyed ``(agent_id, idempotency_key)``; a replay
  returns the original refund row and issues neither a second Stripe refund nor
  a second ledger debit. The Stripe call carries its own idempotency key
  (``dail-refund:<refund_id>``), and the ledger debit is keyed
  ``refund:<refund_id>``, so even a crash mid-flight is completed exactly once
  by the next attempt.
- **Never over-refunds.** The server enforces the per-session cumulative cap
  (credited DAIL) and, on the agent path, never refunds more than the agent's
  currently *spendable* DAIL — escrowed funds owed to other agents are never
  clawed back. The only negative-balance path is the audited admin force.
- **Durable record + audit.** Every state transition writes one row in
  ``dail_refunds`` and one entry in the hash-chained audit log.
- **Reconciling webhook.** ``charge.refunded`` heals a refund issued from the
  Stripe dashboard (invisible to our endpoint) by debiting the DAIL that the
  refund reversed, once.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None


def _now():
    return datetime.now(timezone.utc).isoformat()


class RefundError(Exception):
    """A refund request that cannot proceed.

    ``code`` is a stable machine-readable string; ``detail`` is the human
    string; ``extra`` carries the numbers the caller needs (spendable,
    refundable, escrowed) so a client can offer a partial refund instead.
    """

    def __init__(self, code, detail, **extra):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


# Columns are read in a fixed order; keep _COLS and _row() in sync.
_COLS = ("id, agent_id, session_id, amount_dail, usd_cents, status, reason, "
         "idempotency_key, stripe_refund_id, ledger_tx_id, payment_intent, "
         "created_at, approved_at, refunded_at, requested_by, forced")

# The public shape of a refund row (no internal idempotency key).
_PUBLIC_KEYS = ("refund_id", "agent_id", "session_id", "amount_dail",
                "usd_cents", "status", "reason", "stripe_refund_id",
                "ledger_tx_id", "created_at", "approved_at", "refunded_at",
                "forced")


class RefundRail:
    def __init__(self, dail, payments):
        self.dail = dail
        self.payments = payments

    # ------------------------------------------------------------------ setup
    @property
    def engine(self):
        return self.payments.engine

    @property
    def ready(self):
        return bool(self.payments.ready and self.engine)

    def _require_ready(self):
        if not self.ready:
            raise RefundError(
                "refund_rail_not_ready",
                "real_payments_not_ready: the refund rail needs the Stripe "
                "rail configured (DAIL_REAL_PAYMENTS, Stripe secrets, "
                "DATABASE_URL)")

    def _init_db(self):
        if not self.engine:
            return
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_refunds (
                id VARCHAR(64) PRIMARY KEY,
                agent_id VARCHAR(255) NOT NULL,
                session_id VARCHAR(255) NOT NULL,
                amount_dail INTEGER NOT NULL,
                usd_cents INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL,
                reason VARCHAR(512),
                idempotency_key VARCHAR(255) UNIQUE NOT NULL,
                stripe_refund_id VARCHAR(255),
                ledger_tx_id VARCHAR(255),
                payment_intent VARCHAR(255),
                created_at VARCHAR(64) NOT NULL,
                approved_at VARCHAR(64),
                refunded_at VARCHAR(64),
                requested_by VARCHAR(255),
                forced INTEGER DEFAULT 0
            )"""))
        # Self-healing migration for databases created before a column landed.
        for col, ddl in (("payment_intent", "VARCHAR(255)"),
                         ("forced", "INTEGER DEFAULT 0"),
                         ("approved_at", "VARCHAR(64)"),
                         ("refunded_at", "VARCHAR(64)"),
                         ("requested_by", "VARCHAR(255)")):
            try:
                with self.engine.begin() as c:
                    c.execute(text(
                        f"ALTER TABLE dail_refunds ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            except Exception:
                pass

    # ------------------------------------------------------------- row access
    @staticmethod
    def _row(r):
        if r is None:
            return None
        return {"id": r[0], "agent_id": r[1], "session_id": r[2],
                "amount_dail": r[3], "usd_cents": r[4], "status": r[5],
                "reason": r[6], "idempotency_key": r[7],
                "stripe_refund_id": r[8], "ledger_tx_id": r[9],
                "payment_intent": r[10], "created_at": r[11],
                "approved_at": r[12], "refunded_at": r[13],
                "requested_by": r[14], "forced": bool(r[15])}

    @staticmethod
    def _public(row):
        d = dict(row)
        d["refund_id"] = d.pop("id")
        d.pop("idempotency_key", None)
        d.pop("payment_intent", None)
        return {k: d[k] for k in _PUBLIC_KEYS if k in d}

    def _get(self, refund_id):
        self._init_db()
        with self.engine.begin() as c:
            r = c.execute(text(f"SELECT {_COLS} FROM dail_refunds WHERE id=:i"),
                          {"i": refund_id}).fetchone()
        return self._row(r)

    def _by_idem(self, scope):
        self._init_db()
        with self.engine.begin() as c:
            r = c.execute(text(f"SELECT {_COLS} FROM dail_refunds "
                               "WHERE idempotency_key=:k"), {"k": scope}).fetchone()
        return self._row(r)

    def _refunded_dail(self, session_id):
        """DAIL already reversed (or in flight) for this session."""
        self._init_db()
        with self.engine.begin() as c:
            v = c.execute(text(
                "SELECT COALESCE(SUM(amount_dail),0) FROM dail_refunds "
                "WHERE session_id=:s AND status IN ('approved','refunding','refunded')"),
                {"s": session_id}).scalar()
        return int(v or 0)

    def list(self, agent_id=None, session_id=None, limit=100):
        if not self.engine:
            return []
        self._init_db()
        q = f"SELECT {_COLS} FROM dail_refunds WHERE 1=1"
        p = {"lim": limit}
        if agent_id:
            q += " AND agent_id=:a"; p["a"] = agent_id
        if session_id:
            q += " AND session_id=:s"; p["s"] = session_id
        q += " ORDER BY created_at DESC LIMIT :lim"
        with self.engine.begin() as c:
            rows = c.execute(text(q), p).fetchall()
        return [self._public(self._row(r)) for r in rows]

    # ---------------------------------------------------------------- helpers
    def _escrowed(self, agent_id):
        """DAIL this agent has locked in escrow (bounties, orders, jobs).

        Never refundable: it is owed to other agents and settles first.
        """
        total = 0
        wa = getattr(self.dail, "world_agents", None)
        if wa is not None:
            for b in getattr(wa, "bounties", {}).values():
                if b.get("poster_id") == agent_id and b.get("status") in (
                        "open", "claimed", "submitted"):
                    total += int(b.get("reward", 0) or 0)
            for o in getattr(wa, "orders", {}).values():
                if o.get("buyer_id") == agent_id and o.get("status") in (
                        "awaiting_delivery", "delivered", "disputed"):
                    total += int(o.get("amount", 0) or 0)
        adv = getattr(self.dail, "advanced", None)
        if adv is not None:
            for j in getattr(adv, "jobs", {}).values():
                if j.get("poster_id") == agent_id and j.get("status") in (
                        "open", "assigned"):
                    total += int(j.get("escrow", 0) or 0)
        return total

    # ----------------------------------------------------------------- request
    def request(self, agent_id, session_id, amount_dail=None, reason="",
                idempotency_key=None, approved_by=None, force=False):
        """Create a refund request.

        ``approved_by`` is set by the API layer ONLY when the caller presented
        the admin key; when present the refund executes immediately. Otherwise
        it is queued as ``pending_approval`` and no money moves.
        """
        self._require_ready()
        self._init_db()
        idem = self.dail.world_agents._require_idem(idempotency_key,
                                                    where="refund")
        scope = f"{agent_id}:{idem}"
        # Idempotent replay: return the original row, move nothing.
        prior = self._by_idem(scope)
        if prior:
            return self._public(prior)

        pay = self.payments.get_payment(session_id)
        if not pay:
            raise RefundError("unknown_session", f"unknown session {session_id}")
        if pay.get("status") != "paid":
            raise RefundError(
                "nothing_to_refund",
                f"session {session_id} is not paid (status={pay.get('status')})")
        if agent_id != pay.get("agent_id"):
            raise RefundError("not_owned",
                              "that checkout session belongs to another agent")

        credited = int(pay.get("dail_amount") or 0)
        already = self._refunded_dail(session_id)
        refundable = credited - already
        if refundable <= 0:
            raise RefundError("nothing_to_refund",
                              f"session {session_id} is fully refunded "
                              f"({already}/{credited} DAIL)")
        amt = refundable if amount_dail is None else int(amount_dail)
        if amt <= 0:
            raise RefundError("bad_amount", "amount_dail must be a positive integer")
        if amt > refundable:
            raise RefundError(
                "exceeds_refundable",
                f"amount {amt} exceeds the {refundable} DAIL still refundable "
                f"on this session", credited=credited,
                already_refunded=already, refundable=refundable)

        spendable = self.dail.ledger.balances.get(agent_id, 0)
        escrowed = self._escrowed(agent_id)
        if not force and amt > spendable:
            raise RefundError(
                "insufficient_unrefunded_balance",
                f"only {spendable} DAIL is unspent and refundable now "
                f"(requested {amt})", spendable=spendable, escrowed=escrowed,
                refundable_now=spendable, requested=amt)

        usd_cents = (amt * 100) // self.payments.dail_per_usd
        if usd_cents <= 0:
            raise RefundError("bad_amount", "amount is below one cent")

        rid = "rf_" + uuid.uuid4().hex[:16]
        status = "approved" if approved_by else "pending_approval"
        with self.engine.begin() as c:
            try:
                c.execute(text(
                    "INSERT INTO dail_refunds (id, agent_id, session_id, amount_dail, "
                    "usd_cents, status, reason, idempotency_key, payment_intent, "
                    "created_at, requested_by, forced) VALUES "
                    "(:id,:a,:s,:amt,:usd,:st,:r,:k,:pi,:now,:by,:f)"),
                    {"id": rid, "a": agent_id, "s": session_id, "amt": amt,
                     "usd": usd_cents, "st": status, "r": (reason or "")[:512],
                     "k": scope, "pi": pay.get("payment_intent"),
                     "now": _now(), "by": approved_by, "f": 1 if force else 0})
            except IntegrityError:
                # A racing retry with the same key won the insert: return it.
                winner = self._by_idem(scope)
                if winner:
                    return self._public(winner)
                raise
        self.dail.audit.append("refund.requested", {
            "refund_id": rid, "agent_id": agent_id, "session_id": session_id,
            "amount_dail": amt, "usd_cents": usd_cents, "status": status,
            "forced": bool(force)})
        if status == "pending_approval":
            self.dail.world_agents._notify(agent_id, {
                "type": "refund_requested", "refund_id": rid,
                "session_id": session_id, "amount_dail": amt,
                "body": (f"Refund {rid} for {amt} DAIL is queued for admin "
                         f"approval. No money has moved.")})
        row = self._get(rid)
        if status == "approved":
            return self._execute(row)
        return self._public(row)

    # ---------------------------------------------------------------- approve
    def approve(self, refund_id, approved_by="admin"):
        self._require_ready()
        row = self._get(refund_id)
        if not row:
            raise RefundError("unknown_refund", f"unknown refund {refund_id}")
        if row["status"] in ("refunded", "rejected"):
            raise RefundError("not_pending",
                              f"refund {refund_id} is already {row['status']}")
        if row["status"] == "pending_approval":
            with self.engine.begin() as c:
                c.execute(text("UPDATE dail_refunds SET status='approved', "
                               "approved_at=:now, requested_by=COALESCE(requested_by,:by) "
                               "WHERE id=:i AND status='pending_approval'"),
                          {"now": _now(), "by": approved_by, "i": refund_id})
            self.dail.audit.append("refund.approved",
                                   {"refund_id": refund_id, "approved_by": approved_by})
            row = self._get(refund_id)
        return self._execute(row)

    def reject(self, refund_id, reason=""):
        self._require_ready()
        row = self._get(refund_id)
        if not row:
            raise RefundError("unknown_refund", f"unknown refund {refund_id}")
        if row["status"] != "pending_approval":
            raise RefundError("not_pending",
                              f"refund {refund_id} is {row['status']}, not pending")
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_refunds SET status='rejected', reason=:r "
                           "WHERE id=:i AND status='pending_approval'"),
                      {"r": (reason or "")[:512], "i": refund_id})
        self.dail.audit.append("refund.rejected",
                               {"refund_id": refund_id, "reason": reason})
        self.dail.world_agents._notify(row["agent_id"], {
            "type": "refund_rejected", "refund_id": refund_id,
            "body": f"Refund {refund_id} was declined."
                    + (f" Reason: {reason}" if reason else "")})
        return self._public(self._get(refund_id))

    # ---------------------------------------------------------------- execute
    def _execute(self, row):
        """Two-phase, re-entrant execution: Stripe refund, then ledger debit.

        Idempotent on both sides, so a crash between the two is completed
        exactly once by the next call (approve replay or webhook).
        """
        rid = row["id"]
        agent_id = row["agent_id"]
        amt = int(row["amount_dail"])
        usd_cents = int(row["usd_cents"])
        sid = row["session_id"]
        force = bool(row.get("forced"))

        if row["status"] == "refunded":
            return self._public(row)

        # Re-validate at execution time: approval may be delayed and the agent
        # may have spent the DAIL in the meantime.
        spendable = self.dail.ledger.balances.get(agent_id, 0)
        if not force and amt > spendable:
            raise RefundError(
                "insufficient_unrefunded_balance",
                f"only {spendable} DAIL is unspent now (refund {rid} needs {amt})",
                spendable=spendable, requested=amt)

        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_refunds SET status='refunding' "
                           "WHERE id=:i AND status IN ('approved','refunding')"),
                      {"i": rid})

        # 1) Stripe refund. The idempotency key makes a retry return the SAME
        #    refund object, so this can run more than once safely.
        pi = row.get("payment_intent") or self.payments.payment_intent_for(sid)
        if stripe is None:
            self._mark_failed(rid, "stripe_library_missing")
            raise RefundError("stripe_unavailable", "the stripe library is not installed")
        try:
            sref = stripe.Refund.create(
                payment_intent=pi, amount=usd_cents,
                idempotency_key=f"dail-refund:{rid}",
                metadata={"dail_refund_id": rid, "dail_agent_id": agent_id,
                          "dail_session_id": sid})
        except Exception as e:  # Stripe API error -> retryable, nothing debited
            self._mark_failed(rid, f"{type(e).__name__}: {e}")
            self.dail.audit.append("refund.stripe_failed",
                                   {"refund_id": rid, "error": str(e)})
            raise RefundError("stripe_refund_failed", f"{type(e).__name__}: {e}")
        sref_id = getattr(sref, "id", None)
        if sref_id is None and isinstance(sref, dict):
            sref_id = sref.get("id")
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_refunds SET stripe_refund_id=:sr WHERE id=:i"),
                      {"sr": sref_id, "i": rid})

        # 2) Ledger reversal: debit the DAIL that the top-up credited, keyed on
        #    the refund id so a retry is a no-op.
        tx = self.dail.ledger.debit(
            agent_id, amt, kind="refund", idem=f"refund:{rid}",
            memo=f"stripe refund {sref_id or rid} of session {sid}",
            allow_negative=force)
        self.dail.world_agents._sync_balance(agent_id)
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_refunds SET status='refunded', "
                           "refunded_at=:now, ledger_tx_id=:tx WHERE id=:i"),
                      {"now": _now(), "tx": tx.id, "i": rid})
        self.dail.audit.append("refund.executed", {
            "refund_id": rid, "agent_id": agent_id, "session_id": sid,
            "amount_dail": amt, "usd_cents": usd_cents,
            "stripe_refund_id": sref_id, "ledger_tx_id": tx.id,
            "forced": force})
        self.dail.world_agents.notify_payment_event(
            f"refund:{rid}", agent_id, "stripe_refund", amt,
            {"type": "refund_issued", "refund_id": rid, "session_id": sid,
             "amount_dail": amt, "usd_cents": usd_cents,
             "stripe_refund_id": sref_id, "transaction_id": tx.id})
        return self._public(self._get(rid))

    def _mark_failed(self, refund_id, error):
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_refunds SET status='failed', reason=:r "
                           "WHERE id=:i AND status!='refunded'"),
                      {"r": f"stripe error: {error}"[:512], "i": refund_id})

    # ----------------------------------------------------- webhook reconcile
    def reconcile_charge_refunded(self, payment_intent, amount_refunded_cents):
        """Debit DAIL reversed by a refund issued outside our endpoint.

        A refund made from the Stripe dashboard is invisible to
        ``POST /payments/refund``; ``charge.refunded`` is the authoritative
        signal. Idempotent: only the not-yet-debited delta is applied, and the
        ledger debit is keyed on the payment_intent so replays are no-ops.
        """
        if not self.ready or not payment_intent:
            return {"reconciled": False}
        self._init_db()
        pay = self.payments.get_payment_by_intent(payment_intent)
        if not pay or pay.get("status") != "paid":
            return {"reconciled": False, "reason": "unknown_payment_intent"}
        sid = pay["session_id"]
        agent_id = pay["agent_id"]
        credited = int(pay.get("dail_amount") or 0)
        usd = int(amount_refunded_cents or 0)
        reversed_dail = (usd * self.payments.dail_per_usd) // 100
        already = self._refunded_dail(sid)
        delta = min(max(reversed_dail - already, 0), credited - already)
        if delta <= 0:
            return {"reconciled": False, "already_reversed_dail": already}
        rid = "rf_" + uuid.uuid4().hex[:16]
        # Record the reversal BEFORE debiting so a replay of the same
        # charge.refunded event sees the delta already applied (the UNIQUE
        # idempotency_key makes the second insert a no-op).
        scope = f"webhook:{payment_intent}:{reversed_dail}"
        try:
            with self.engine.begin() as c:
                c.execute(text(
                    "INSERT INTO dail_refunds (id, agent_id, session_id, amount_dail, "
                    "usd_cents, status, reason, idempotency_key, payment_intent, "
                    "created_at, refunded_at, requested_by, forced) VALUES "
                    "(:id,:a,:s,:amt,:usd,'refunded',:r,:k,:pi,:now,:now,'stripe-webhook',0)"),
                    {"id": rid, "a": agent_id, "s": sid, "amt": delta, "usd": usd,
                     "r": "reconciled from charge.refunded", "k": scope,
                     "pi": payment_intent, "now": _now()})
        except IntegrityError:
            return {"reconciled": False, "already_reversed_dail": already,
                    "note": "duplicate charge.refunded event"}
        self.dail.ledger.debit(
            agent_id, delta, kind="refund",
            idem=f"refund-webhook:{payment_intent}:{reversed_dail}",
            memo=f"dashboard refund on session {sid}")
        self.dail.world_agents._sync_balance(agent_id)
        self.dail.audit.append("refund.webhook_reconciled", {
            "refund_id": rid, "agent_id": agent_id, "session_id": sid,
            "amount_dail": delta, "payment_intent": payment_intent})
        return {"reconciled": True, "amount_dail": delta, "session_id": sid,
                "agent_id": agent_id}
