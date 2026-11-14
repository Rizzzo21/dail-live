"""House fixit batch 2026-11-14 (Tommy: "fix all open house issues").

Every fix is proved end to end here, against the exact probe scenario
that found it:

1. Escrow refund on hunter release (/release and the /abandon alias):
   bnty_0607 finding 1 — a self-release stranded the poster escrow.
2. Refund + preserved submission on poster reject: bnty_0607 finding 2.
3. Delivery-window enforcement in the house sweeps: bnty_0607 finding 3
   (ord_0160 — a 1h window closed with no refund, no sweep, no notice).
4. Resubmission supersede + orphaned-flag reconciliation: Orbit's
   bnty_0609 rebuild was 400-blocked inside its fix window.
5. Observatory client render uses services_total (tile + note).
6. Audit-log persistence (PR #8's design, rebased): the trail survives
   a restart instead of resetting to zero.
7. Voluntary claim-release route POST /world/bounties/{id}/abandon
   (alias of /release): the workbuddy-sec-probe bnty_0568 case.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
os.environ["DAIL_BOUNCER_KEY"] = "test-bouncer-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail
from dail.audit import AuditLog, AUDIT_MAX_EVENTS
import dail.audit as audit_mod

client = TestClient(app)
BOUNCER = {"X-DAIL-Bouncer-Key": "test-bouncer-key"}

_seq = [0]
_keys = {}

LONG_WORK = ("Completed the requested deliverable and verified it against "
             "the bounty requirements, with a summary of changes and test "
             "evidence included in the attached notes.")
LONG_FIX = ("Revised submission: corrected the deliverable per the review "
            "hold feedback, fixed the flagged items, re-ran the checks, and "
            "attached the updated evidence notes.")
LONG_FIX2 = ("Second revision: tightened the corrected analysis and "
             "re-attached the full verification evidence for the fix.")


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_hx{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _post_bounty(poster, reward=20):
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Fixit job",
        "description": "Do the thing.", "reward": reward},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _claim(hunter, bid, submission=None):
    body = {"agent_id": hunter}
    if submission:
        body["submission"] = submission
    r = client.post(f"/world/bounties/{bid}/claim", json=body,
                    headers=_auth(hunter))
    assert r.status_code == 200, r.text
    return r.json()


def _submit(hunter, bid, text=LONG_WORK):
    r = client.post(f"/world/bounties/{bid}/submit",
                    json={"agent_id": hunter, "submission": text},
                    headers=_auth(hunter))
    assert r.status_code == 200, r.text
    return r.json()


def _refund_txs(bid):
    """Escrow-refund ledger rows for one bounty, in tx order."""
    return [tx for tx in dail.ledger.transactions.values()
            if tx.kind == "escrow_refund"
            and tx.idempotency_key.startswith(f"bounty-refund:{bid}")]


def _escrow_balance(bid):
    return dail.ledger.balances.get(f"escrow:{bid}", 0)


# ---- fix 1: refund on hunter release (+ /abandon alias) ----

def test_release_refunds_poster_escrow():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = _post_bounty(poster, reward=20)
    assert dail.ledger.balances[poster] == 80  # 100 grant - 20 escrow
    _claim(hunter, bid)
    r = client.post(f"/world/bounties/{bid}/release",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    # The probe defect: no escrow_refund row used to appear. Now one does,
    # the poster is whole again, and the escrow account is empty.
    assert len(_refund_txs(bid)) == 1
    assert _refund_txs(bid)[0].amount == 20
    assert _refund_txs(bid)[0].to_account == poster
    assert dail.ledger.balances[poster] == 100
    assert _escrow_balance(bid) == 0
    # The hunter is barred from reclaiming (existing release rule).
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 400, r.text


def test_abandon_alias_refunds_like_release():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter, bid)
    r = client.post(f"/world/bounties/{bid}/abandon",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    assert len(_refund_txs(bid)) == 1
    assert dail.ledger.balances[poster] == 100
    assert _escrow_balance(bid) == 0


def test_release_then_reclaim_reholds_and_pays():
    # A refunded bounty must stay claimable: the next claim re-holds the
    # escrow from the poster, and a later accept pays out in full.
    poster, hunter1, hunter2 = _uid("p"), _uid("h"), _uid("h")
    _make(poster)
    _make(hunter1)
    _make(hunter2)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter1, bid)
    assert client.post(f"/world/bounties/{bid}/release",
                       json={"agent_id": hunter1},
                       headers=_auth(hunter1)).status_code == 200
    assert dail.ledger.balances[poster] == 100
    _claim(hunter2, bid)
    assert dail.ledger.balances[poster] == 80  # re-held
    assert _escrow_balance(bid) == 20
    _submit(hunter2, bid)
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    # Fresh non-house poster: first-3 fee waiver, so the hunter nets 20.
    assert dail.ledger.balances[hunter2] == 120
    assert _escrow_balance(bid) == 0


def test_release_then_cancel_is_clean_noop():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter, bid)
    assert client.post(f"/world/bounties/{bid}/release",
                       json={"agent_id": hunter},
                       headers=_auth(hunter)).status_code == 200
    r = client.post(f"/world/bounties/{bid}/cancel",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    # Exactly one refund row: the cancel must not double-refund.
    assert len(_refund_txs(bid)) == 1
    assert dail.ledger.balances[poster] == 100


# ---- fix 2: refund + preserved submission on poster reject ----

def test_reject_refunds_and_preserves_submission():
    poster, hunter, stranger = _uid("p"), _uid("h"), _uid("s")
    _make(poster)
    _make(hunter)
    _make(stranger)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter, bid, submission=LONG_WORK)
    r = client.post(f"/world/bounties/{bid}/reject",
                    json={"agent_id": poster, "reason": "not good enough"},
                    headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    assert len(_refund_txs(bid)) == 1
    assert dail.ledger.balances[poster] == 100
    assert _escrow_balance(bid) == 0
    # Public board: flag on, text stripped — the work is preserved, not
    # published.
    pub = client.get(f"/world/bounties/{bid}").json()
    assert pub["rejected_submission_preserved"] is True
    assert "rejected_submission" not in pub
    assert LONG_WORK not in str(pub)
    # The rejected hunter keeps a copy via the submission endpoint.
    sub = client.get(f"/world/bounties/{bid}/submission",
                     params={"agent_id": hunter},
                     headers=_auth(hunter)).json()
    assert sub["submission"] == LONG_WORK
    assert sub["rejected"] is True
    # The poster can read it too; a stranger cannot.
    sub = client.get(f"/world/bounties/{bid}/submission",
                     params={"agent_id": poster},
                     headers=_auth(poster)).json()
    assert sub["submission"] == LONG_WORK
    r = client.get(f"/world/bounties/{bid}/submission",
                   params={"agent_id": stranger}, headers=_auth(stranger))
    assert r.status_code == 403, r.text


def test_reject_then_reclaim_reholds_and_pays():
    poster, hunter1, hunter2 = _uid("p"), _uid("h"), _uid("h")
    _make(poster)
    _make(hunter1)
    _make(hunter2)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter1, bid, submission=LONG_WORK)
    assert client.post(f"/world/bounties/{bid}/reject",
                       json={"agent_id": poster, "reason": "no"},
                       headers=_auth(poster)).status_code == 200
    assert dail.ledger.balances[poster] == 100
    _claim(hunter2, bid)
    assert dail.ledger.balances[poster] == 80
    assert _escrow_balance(bid) == 20
    _submit(hunter2, bid)
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert dail.ledger.balances[hunter2] == 120


# ---- fix 1 (continued): withdraw + lapse also refund ----

def test_withdraw_refunds_poster_escrow():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter, bid, submission=LONG_WORK)
    # Fast-forward past the 72h review window.
    rec = dail.world_agents.bounties[bid]
    rec["review_deadline"] = (datetime.now(timezone.utc)
                              - timedelta(hours=1)).isoformat()
    r = client.post(f"/world/bounties/{bid}/withdraw",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert len(_refund_txs(bid)) == 1
    assert dail.ledger.balances[poster] == 100


def test_lapse_keeps_escrow_and_reclaim_needs_no_rehold():
    # A lapsed claim deliberately does NOT refund: the bounty stays open
    # and funded, so the hold stays parked for the next hunter. Refunding
    # here would let the poster spend the escrow and then brick their own
    # open bounty (the next claim's re-hold would fail). The next claim
    # therefore reuses the existing hold — no new transfer.
    poster, hunter1, hunter2 = _uid("p"), _uid("h"), _uid("h")
    _make(poster)
    _make(hunter1)
    _make(hunter2)
    bid = _post_bounty(poster, reward=20)
    _claim(hunter1, bid)
    rec = dail.world_agents.bounties[bid]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rec["stall_deadline"] = past
    rec["work_deadline"] = past
    out = dail.world_agents.sweep_bounties()
    assert out["lapsed"] == 1, out
    assert dail.world_agents.bounties[bid]["status"] == "open"
    assert _refund_txs(bid) == []  # no refund on lapse, by design
    assert dail.ledger.balances[poster] == 80  # still escrowed
    assert _escrow_balance(bid) == 20
    _claim(hunter2, bid)
    assert dail.ledger.balances[poster] == 80  # no re-hold needed
    assert _escrow_balance(bid) == 20


# ---- fix 4: resubmission supersede + orphaned-flag reconciliation ----

def _submitted_under_hold(reward=20):
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = _post_bounty(poster, reward=reward)
    _claim(hunter, bid, submission=LONG_WORK)
    r = client.post(f"/world/bounties/{bid}/review/hold",
                    json={"agent_id": poster, "reason": "needs fixes"},
                    headers=_auth(poster))
    assert r.status_code == 200, r.text
    return poster, hunter, bid


def _resubmit(hunter, bid, text):
    return client.post(f"/world/bounties/{bid}/resubmit",
                       json={"agent_id": hunter, "submission": text},
                       headers=_auth(hunter))


def test_resubmit_supersedes_pending_revision():
    _, hunter, bid = _submitted_under_hold()
    assert _resubmit(hunter, bid, LONG_FIX).status_code == 200
    # The old behavior 400d here ("already awaiting staff approval"),
    # stranding the hunter inside the fix window. Now it supersedes.
    r = _resubmit(hunter, bid, LONG_FIX2)
    assert r.status_code == 200, r.text
    assert r.json()["pending_resubmission"] == LONG_FIX2
    assert r.json()["submission"] == LONG_WORK
    q = client.get("/staff/bounties/resubmissions",
                   headers={"Authorization": "Bearer "
                            + dail.keystore.issue("dail_host")}).json()
    mine = [x for x in q["resubmissions"] if x["bounty_id"] == bid]
    assert len(mine) == 1
    assert mine[0]["pending_resubmission"] == LONG_FIX2


def test_reject_clears_pending_resubmission_fields():
    poster, hunter, bid = _submitted_under_hold()
    assert _resubmit(hunter, bid, LONG_FIX).status_code == 200
    r = client.post(f"/world/bounties/{bid}/reject",
                    json={"agent_id": poster, "reason": "no"},
                    headers=_auth(poster))
    assert r.status_code == 200, r.text
    rec = dail.world_agents.bounties[bid]
    assert rec.get("pending_resubmission") is None
    assert rec.get("resubmission_status") is None
    pub = client.get(f"/world/bounties/{bid}").json()
    assert pub["resubmission_pending"] is False
    q = client.get("/staff/bounties/resubmissions",
                   headers={"Authorization": "Bearer "
                            + dail.keystore.issue("dail_host")}).json()
    assert all(x["bounty_id"] != bid for x in q["resubmissions"])


def test_orphaned_flag_on_non_submitted_record_reads_false():
    # Legacy orphan (pre-fix records): a stale pending text on a bounty
    # that is no longer submitted. The public flag must follow the staff
    # queue's predicate (submitted AND pending), never the raw field.
    _, hunter, bid = _submitted_under_hold()
    assert _resubmit(hunter, bid, LONG_FIX).status_code == 200
    rec = dail.world_agents.bounties[bid]
    rec["status"] = "open"  # simulate the pre-fix stale state
    pub = client.get(f"/world/bounties/{bid}").json()
    assert pub["resubmission_pending"] is False
    assert "pending_resubmission" not in pub
    q = client.get("/staff/bounties/resubmissions",
                   headers={"Authorization": "Bearer "
                            + dail.keystore.issue("dail_host")}).json()
    assert all(x["bounty_id"] != bid for x in q["resubmissions"])


# ---- fix 3: delivery-window enforcement in the sweeps ----

def _buy(buyer, provider, price=20, delivery_hours=1):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": delivery_hours},
        headers=_auth(provider))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": _uid("k")}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _expire_order(oid):
    rec = dail.world_agents.orders[oid]
    rec["deliver_by"] = (datetime.now(timezone.utc)
                         - timedelta(minutes=11)).isoformat()


def test_delivery_window_expiry_refunds_buyer():
    # ord_0160, reproduced: a 1h window closes with no delivery. The
    # sweep must refund the buyer in full — not merely nudge them.
    buyer, provider = _uid("b"), _uid("s")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20, delivery_hours=1)
    assert dail.ledger.balances[buyer] == 80
    _expire_order(oid)
    out = dail.world_agents.sweep_orders()
    assert out["delivery_window_refunded"] == 1, out
    assert out["auto_released"] == 0
    order = dail.world_agents.orders[oid]
    assert order["status"] == "refunded"
    assert order["resolution"] == "delivery_window_expired"
    assert dail.ledger.balances[buyer] == 100
    txs = [tx for tx in dail.ledger.transactions.values()
           if tx.kind == "escrow_refund"
           and tx.idempotency_key == f"escrow-refund:{oid}"]
    assert len(txs) == 1 and txs[0].amount == 20
    # A late delivery is now refused, and a cancel cannot double-refund.
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": provider, "delivery": "late work"},
                    headers=_auth(provider))
    assert r.status_code == 400, r.text
    r = client.post(f"/world/orders/{oid}/cancel",
                    json={"agent_id": buyer}, headers=_auth(buyer))
    assert r.status_code == 400, r.text
    assert dail.ledger.balances[buyer] == 100


def test_delivery_window_refund_via_lazy_sweep_and_bouncer_route():
    buyer, provider = _uid("b"), _uid("s")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20, delivery_hours=1)
    _expire_order(oid)
    # Lazy sweep: reading the order triggers the refund.
    r = client.get(f"/world/orders/{oid}", headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refunded"
    assert dail.ledger.balances[buyer] == 100
    # The bouncer sweeps endpoint reports the counts (second run: zeros).
    r = client.post("/bouncer/sweeps/run", headers=BOUNCER)
    assert r.status_code == 200, r.text
    assert r.json()["orders_delivery_window_refunded"] == 0
    assert r.json()["orders_auto_released"] == 0


# ---- fix 5: Observatory tile + note use services_total ----

def test_observatory_tile_and_note_use_services_total():
    data = client.get("/observatory/public/data").json()
    s = data["stats"]
    assert "services_total" in s, s
    assert s["services_total"] >= s["services"]
    page = client.get("/observatory/public").text
    # Server tile: "visible of total", not the bare visible count.
    assert f">{s['services']} of {s['services_total']}<" in page
    # Client render rebuilds the same wording from services_total and
    # re-renders the definition note every 60s refresh.
    assert "services_total" in page
    assert 'id="services-note"' in page
    assert "Services shown:" in page


# ---- fix 6: audit persistence across a simulated restart ----

def test_audit_persists_across_simulated_restart():
    backend = []

    def persist(record):
        backend.append(dict(record))

    def all_records():
        return [dict(r) for r in backend]

    log = AuditLog(persist=persist, all_records=all_records)
    for i in range(5):
        log.append("test.event", {"n": i})
    assert len(backend) == 5
    assert log.verify_all() is True
    # Simulated restart: a fresh ring over the same backend resumes the
    # chain — same events, sequence continuing, no reuse.
    log2 = AuditLog(persist=persist, all_records=all_records)
    log2.load_from_backend()
    assert len(log2.events) == 5
    assert [e["seq"] for e in log2.events] == [1, 2, 3, 4, 5]
    rec = log2.append("test.event", {"n": 5})
    assert rec["seq"] == 6
    assert log2.verify() is True
    assert log2.verify_all() is True
    # Tamper with a persisted record: the full-chain check must fail.
    backend[2]["payload"] = {"n": 999}
    assert log2.verify_all() is False


def test_audit_ring_trim_keeps_backend_and_seq():
    backend = []

    def persist(record):
        backend.append(dict(record))

    def all_records():
        return [dict(r) for r in backend]

    old_max = audit_mod.AUDIT_MAX_EVENTS
    audit_mod.AUDIT_MAX_EVENTS = 5
    try:
        log = AuditLog(persist=persist, all_records=all_records)
        for i in range(7):
            log.append("test.event", {"n": i})
        assert len(log.events) == 5  # ring trimmed
        assert len(backend) == 7  # backend kept everything
        log2 = AuditLog(persist=persist, all_records=all_records)
        log2.load_from_backend()
        assert len(log2.events) == 5
        assert [e["seq"] for e in log2.events] == [3, 4, 5, 6, 7]
        assert log2._seq == 7
        assert log2.verify_all() is True
    finally:
        audit_mod.AUDIT_MAX_EVENTS = old_max


def test_audit_verify_endpoint_uses_full_chain():
    r = client.get("/audit/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["events"] == len(dail.audit.events)
