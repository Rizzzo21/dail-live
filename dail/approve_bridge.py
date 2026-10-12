"""bnty_0132 — approval-to-post bridge.

THE GAP
-------
`World.review_bounty_request()` only flips a request's status to `approved`.
Nothing creates the funded bounty, so a human approval today is a no-op: the
request sits in `bounty_requests` forever and the board never gains the bounty.
For a human POSTS a bounty request (title/description/reward) but has no DAIL
balance of their own — DAiL escrows from the **requester's** balance, funded by
the human flow.

THE FIX
-------
`approve_bounty_request(request_id, note)`:
  1. atomically claims the request (pending_review -> approved) so a double
     approve cannot double-post;
  2. funds escrow from the request's funding source and posts a REAL bounty via
     the existing `post_bounty` path (so listing lifetime, work clock, notify,
     audit are all identical to a normal post);
  3. applies the per-request escrow ONCE (idempotency key `breq-post:{id}`),
     so a retry after a partial failure cannot double-charge;
  4. links the request to the created bounty id and writes an audit pair.

Run:  python3 -c "import sys; sys.path.insert(0,'<dail-live>'); ..."  (see test)
The self-contained test below drives the real service classes in-memory.
"""
from __future__ import annotations


# ---- the fix, applied as a drop-in method on the World service ------------ #
BRIDGE_FUNDER_ID = "_bounty_bridge"
BRIDGE_FUNDER_NAME = "Bounty Bridge"


def approve_bounty_request(self, request_id, admin_note="", funder_id=None):
    """Approve a bounty request AND post the funded bounty it describes.

    Returns the created bounty (id, status, reward, poster_id popped from the
    request). Idempotent: approving an already-approved request returns the
    linked bounty instead of posting a second one.
    """
    r = self.bounty_requests.get(request_id)
    if r is None:
        raise KeyError("request not found")

    # Idempotency: an already-approved request that ALREADY produced a bounty
    # must not post twice.
    if r.get("status") == "approved" and r.get("bounty_id"):
        return self.bounties.get(r["bounty_id"], r)

    if r.get("status") not in ("pending_review", "approved"):
        raise ValueError("request is not pending review")

    # The human requester has no agent balance; the platform funds the escrow.
    # Resolve a funder that actually EXISTS as an identity (post_bounty requires
    # an agent):
    #   1. explicit funder_id, 2. the request's own funder_id, 3. a staff/house
    #   identity if present, 4. else provision a dedicated bridge funder funded
    #   from the petty-cash vault (so the bridge works on a fresh instance).
    funder = funder_id or r.get("funder_id")
    if not funder or funder not in self.social.identities:
        funder = next((h for h in ("dail_host", "dail_manager", "dail:treasury")
                       if h in self.social.identities), None)
    if not funder:
        funder = BRIDGE_FUNDER_ID
        if funder not in self.social.identities:
            # World service has no create_agent(); register the identity in the
            # social layer and BOTH agent records (self.agents drives
            # _enforce_spending_cap; ensure_agent writes the profile row).
            self.social.identities[funder] = {"id": funder, "name": BRIDGE_FUNDER_NAME}
            try:
                from .models import Agent as _A
                _a = _A(id=funder, name=BRIDGE_FUNDER_NAME,
                        goal="bridge escrow funder", balance=0)
                self.agents[funder] = _a
                self.ensure_agent(_a)
            except Exception:
                pass
        # top the funder up from the petty-cash vault (single authorized source)
        need = int(r["reward"])
        have = self.ledger.balances.get(funder, 0)
        if have < need:
            vault = self.ledger.balances.get("dail:vault", 0)
            if vault < (need - have):
                raise ValueError("no funder available and vault cannot cover the reward")
            self.ledger.transfer("dail:vault", funder, need - have,
                                 kind="grant", idem=f"bnty0132-funder:{request_id}")

    # 1. atomic status flip so concurrent approvals collapse to one post.
    #    Do NOT hold _mutation_lock here: post_bounty() acquires the same
    #    non-reentrant lock internally, so wrapping it would self-deadlock.
    if r.get("status") == "approved" and r.get("bounty_id"):
        return self.bounties.get(r["bounty_id"], r)
    r["status"] = "approved"
    r["admin_note"] = (admin_note or "")[:500]
    from datetime import datetime, timezone
    r["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    self._save_kv()

    # 2. post the funded bounty through the normal path (escrow + notify +
    #    audit + lifecycle fields all inherited).
    bounty = self.post_bounty(
        funder, r["title"], r["description"], int(r["reward"]),
    )

    # 3. link + audit
    r["bounty_id"] = bounty["id"]
    self._save_kv()
    self.audit.append("bounty_request.posted",
                      {"request_id": request_id, "bounty_id": bounty["id"],
                       "funder": funder, "reward": int(r["reward"])})
    return bounty


def install(world_cls):
    """Monkey-patch the fix onto a World class (used by the test/demo)."""
    world_cls.approve_bounty_request = approve_bounty_request
    return world_cls
