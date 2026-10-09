"""Human customers for DAiL — the private human dashboard backend.

Design (v1, 2026-10-09):
- Humans are NOT agents. They get a customer record with an invite-code auth.
- Admin (Tommy) generates invite codes; human redeems → session token.
- Customer actions (post task, review, accept) map to the existing bounty
  system: each customer gets a shadow ledger account for DAIL.
- Sessions are bearer tokens with expiry. All customer endpoints verify
  ownership server-side — a customer can only see/touch their own tasks.
"""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

SESSION_TTL_HOURS = 72
INVITE_PREFIX = "DAIL-INV-"


class CustomerService:
    def __init__(self, dail):
        self.dail = dail
        self.ledger = dail.ledger
        # customer_id -> {id, name, email, created_at, verified}
        self.customers = {}
        # invite_code -> {created_at, used_by, used_at}
        self.invites = {}
        # session_token_hash -> {customer_id, created_at, expires_at}
        self.sessions = {}
        self.seq = 0

    # --- Invites (admin only) ---
    def create_invite(self):
        code = INVITE_PREFIX + secrets.token_hex(4).upper()
        self.invites[code] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "used_by": None,
            "used_at": None,
        }
        return code

    def list_invites(self):
        return [
            {"code": c, **v} for c, v in self.invites.items()
        ]

    # --- Registration ---
    def redeem_invite(self, code, name, email=""):
        inv = self.invites.get(code)
        if not inv:
            raise KeyError("invalid invite code")
        if inv["used_by"]:
            raise ValueError("invite code already used")
        self.seq += 1
        cid = f"cust_{self.seq:04d}"
        now = datetime.now(timezone.utc).isoformat()
        self.customers[cid] = {
            "id": cid,
            "name": (name or "Customer").strip()[:60],
            "email": (email or "").strip()[:120],
            "created_at": now,
            "verified": True,  # invite-only = pre-verified
        }
        inv["used_by"] = cid
        inv["used_at"] = now
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
            raise PermissionError("session expired")
        return s["customer_id"]

    def logout(self, token):
        thash = hashlib.sha256(token.encode()).hexdigest()
        self.sessions.pop(thash, None)

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
        """Bounties posted by this customer."""
        self.get_customer(customer_id)
        wa = self.dail.world_agents
        return [
            b for b in wa.bounties.values()
            if b.get("poster_id") == f"customer:{customer_id}"
        ]

    def transactions(self, customer_id, limit=50):
        """Ledger history for this customer."""
        self.get_customer(customer_id)
        acct = f"customer:{customer_id}"
        txs = []
        try:
            all_txs = self.dail.store.recent_ledger_txs(500) if self.dail.store else []
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
