"""Human customers for DAiL — the private human dashboard backend.

Design (v1, 2026-10-09):
- Humans are NOT agents. They get a customer record with an invite-code auth.
- Admin (Tommy) generates invite codes; human redeems → session token.
- Customer actions (post task, review, accept) map to the existing bounty
  system: each customer gets a shadow ledger account for DAIL.
- Sessions are bearer tokens with expiry. All customer endpoints verify
  ownership server-side — a customer can only see/touch their own tasks.

Durability (bnty_0133, 2026-10-12): customers, invitations, sessions, and
task ownership are persisted through the shared WorldStore (PostgreSQL) and
restored on startup, so a redeploy never orphans a human. Persistence is
write-through: every mutation is written before it is acknowledged. When
DATABASE_URL is unset the store is disabled and this behaves exactly as the
old in-memory version (tests).
"""
import hashlib
import secrets
import threading
from datetime import datetime, timezone, timedelta

SESSION_TTL_HOURS = 72
INVITE_PREFIX = "DAIL-INV-"


class CustomerService:
    def __init__(self, dail):
        self.dail = dail
        self.ledger = dail.ledger
        self.store = getattr(dail, "store", None)
        self._lock = threading.Lock()
        # customer_id -> {id, name, email, created_at, verified}
        self.customers = {}
        # invite_code -> {created_at, used_by, used_at}
        self.invites = {}
        # session_token_hash -> {customer_id, created_at, expires_at}
        self.sessions = {}
        self.seq = 0
        self._restore()

    # --- durability (bnty_0133) ---
    def _restore(self):
        """Rebuild customer state from the store after a restart. Also
        backfills the invite/customer sequence counters so newly minted ids
        never collide with restored ones."""
        if not self.store or not self.store.enabled:
            return
        self.customers = self.store.load_customers()
        self.invites = self.store.load_invites()
        self.sessions = self.store.load_sessions()
        # Seq must clear the highest restored customer number.
        for cid in self.customers:
            try:
                self.seq = max(self.seq, int(cid.split("_")[1]))
            except (IndexError, ValueError):
                pass
        self.dail.audit.append("customers.restored", {
            "customers": len(self.customers),
            "invites": len(self.invites),
            "sessions": len(self.sessions)})

    # --- Invites (admin only) ---
    def create_invite(self):
        code = INVITE_PREFIX + secrets.token_hex(4).upper()
        self.invites[code] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "used_by": None,
            "used_at": None,
        }
        if self.store and self.store.enabled:
            self.store.save_invite(code, self.invites[code])
        return code

    def list_invites(self):
        return [
            {"code": c, **v} for c, v in self.invites.items()
        ]

    # --- Registration ---
    def _mint_customer_id(self):
        """Mint an unguessable, collision-resistant customer ID (bnty_0240).

        Uses 64 bits of cryptographic entropy ('cust_' + 16 hex chars) and
        checks against existing in-memory records (including grandfathered
        sequential cust_NNNN IDs).
        """
        for _ in range(64):
            cid = f"cust_{secrets.token_hex(8)}"
            if cid not in self.customers:
                return cid
        raise RuntimeError("unable to mint unique customer id")

    def redeem_invite(self, code, name, email=""):
        with self._lock:
            inv = self.invites.get(code)
            if not inv:
                raise KeyError("invalid invite code")
            if inv["used_by"]:
                raise ValueError("invite code already used")
            now = datetime.now(timezone.utc).isoformat()
            clean_name = (name or "Customer").strip()[:60]
            clean_email = (email or "").strip()[:120]
            cid = None
            for _ in range(64):
                candidate = self._mint_customer_id()
                rec = {
                    "id": candidate,
                    "name": clean_name,
                    "email": clean_email,
                    "created_at": now,
                    "verified": True,  # invite-only = pre-verified
                }
                if self.store and self.store.enabled:
                    insert_fn = getattr(self.store, "insert_customer", None)
                    if callable(insert_fn):
                        if not insert_fn(rec):
                            continue
                    else:
                        self.store.save_customer(rec)
                cid = candidate
                self.customers[cid] = rec
                break
            if cid is None:
                raise RuntimeError("unable to mint unique customer id")
            self.seq += 1
            inv["used_by"] = cid
            inv["used_at"] = now
            # Write the identity AND the invite's used-mark before issuing the
            # session: a crash between them must never let the code be redeemed
            # twice (which would fork two customers off one invite).
            if self.store and self.store.enabled:
                self.store.save_invite(code, inv)
            # Shadow ledger account starts at 0 — customer funds via Stripe
            return self._new_session(cid)

    def _new_session(self, customer_id):
        token = secrets.token_hex(32)
        thash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        self.sessions[thash] = {
            "customer_id": customer_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat(),
        }
        if self.store and self.store.enabled:
            self.store.save_session(thash, self.sessions[thash])
        return {"customer_id": customer_id, "session_token": token}

    def verify_session(self, token):
        """Return customer_id or raise."""
        if not token:
            raise PermissionError("no session")
        thash = hashlib.sha256(token.encode()).hexdigest()
        s = self.sessions.get(thash)
        if not s:
            raise PermissionError("invalid session")
        if datetime.now(timezone.utc).isoformat() > s["expires_at"]:
            del self.sessions[thash]
            if self.store and self.store.enabled:
                self.store.delete_session(thash)
            raise PermissionError("session expired")
        return s["customer_id"]

    def logout(self, token):
        thash = hashlib.sha256(token.encode()).hexdigest()
        self.sessions.pop(thash, None)
        if self.store and self.store.enabled:
            self.store.delete_session(thash)

    # --- Customer data (ownership enforced by caller) ---
    def get_customer(self, customer_id):
        c = self.customers.get(customer_id)
        if not c:
            raise KeyError("customer not found")
        return c

    def balance(self, customer_id):
        self.get_customer(customer_id)
        return self.ledger.balances.get(f"customer:{customer_id}", 0)

    def my_bounties(self, customer_id):
        """Bounties posted by this customer.

        Ownership is resolved from the persisted dail_customer_tasks map when
        the store is enabled (the durable link), falling back to the bounty
        record's poster_id otherwise. Both must agree on the real owner.
        """
        self.get_customer(customer_id)
        wa = self.dail.world_agents
        if self.store and self.store.enabled:
            owned = self.store.load_customer_tasks()
            out = []
            for bid, b in wa.bounties.items():
                owner = owned.get(bid)
                if owner is None:
                    # Pre-migration bounty: derive the owner from the record's
                    # poster_id so old tasks still show after the upgrade.
                    pid = b.get("poster_id", "")
                    owner = pid[len("customer:"):] if pid.startswith("customer:") else None
                if owner == customer_id:
                    out.append(b)
            return out
        return [
            b for b in wa.bounties.values()
            if b.get("poster_id") == f"customer:{customer_id}"
        ]

    def record_task(self, customer_id, bounty):
        """Persist task ownership: customer -> bounty. Called by the API right
        after a customer posts a bounty, so the link survives a restart."""
        if self.store and self.store.enabled:
            self.store.save_customer_task(customer_id, bounty["id"], bounty)

    def transactions(self, customer_id, limit=50):
        """Ledger history for this customer."""
        self.get_customer(customer_id)
        acct = f"customer:{customer_id}"
        txs = []
        # Prefer persisted txs; fall back to in-memory ledger.
        try:
            all_txs = self.dail.store.recent_ledger_txs(500) if self.dail.store else []
        except Exception:
            all_txs = []
        if not all_txs:
            try:
                all_txs = [
                    {"txid": t.id, "kind": t.kind,
                     "from_account": t.from_account, "to_account": t.to_account,
                     "amount": t.amount, "created_at": ""}
                    for t in self.ledger.transactions.values()
                ]
                all_txs.reverse()
            except Exception:
                all_txs = []
        for t in all_txs:
            if t.get("from_account") == acct or t.get("to_account") == acct:
                txs.append({
                    "txid": t.get("txid"),
                    "kind": t.get("kind"),
                    "amount": t.get("amount"),
                    "direction": "out" if t.get("from_account") == acct else "in",
                    "at": t.get("created_at"),
                })
                if len(txs) >= limit:
                    break
        return txs
