"""Trust-batch fixes (2026-10-07): claim review SLA, ban escrow cleanup,
service edit/pause, bounty search, webhooks, key rotation, notification
read state, bulk review, fulfillment stats."""
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_tb{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_claim_review_sla_auto_accepts():
    p, h = _uid("p"), _uid("h")
    for x in (p, h):
        _make(x)
    d = dail
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "SLA probe", "description": "d", "reward": 10},
        headers=_auth(p))
    bid = r.json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": h, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(h))
    b = d.world_agents.bounties[bid]
    # Atomic claim+submit: both clocks start together. Backdate both so the
    # 7-day review SLA (measured from submission) has lapsed.
    b["claimed_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    b["submitted_at"] = b["claimed_at"]
    bal0 = d.ledger.balances[h]
    client.get("/world/bounties")  # sweep runs on read
    assert b["status"] == "completed"
    assert b.get("auto_accepted") is True
    assert d.ledger.balances[h] > bal0  # escrow released minus fee


def test_ban_cleans_up_escrow():
    p, h, buyer, prov = _uid("p"), _uid("h"), _uid("b"), _uid("pr")
    for x in (p, h, buyer, prov):
        _make(x)
    d = dail
    # banned poster: open bounty + claimed bounty
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Doomed 1", "description": "d", "reward": 10},
        headers=_auth(p))
    b1 = r.json()["id"]
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Doomed 2", "description": "d", "reward": 10},
        headers=_auth(p))
    b2 = r.json()["id"]
    client.post(f"/world/bounties/{b2}/claim", json={
        "agent_id": h, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(h))
    # banned provider: open order from a buyer
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": 20},
        headers=_auth(prov))
    sid = r.json()["id"]
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid, "idempotency_key": _uid("k")},
        headers=_auth(buyer))
    oid = r.json()["order_id"]
    buyer_bal = d.ledger.balances[buyer]
    # ban via admin key
    r = client.post(f"/bouncer/agents/{p}/ban", headers={"X-DAIL-Bouncer-Key": "x"},
                    json={})
    # bouncer key is test config; use admin route instead if needed
    if r.status_code not in (200, 404):
        pass
    d.ban_agent(p, "test")
    d.ban_agent(prov, "test")
    assert d.world_agents.bounties[b1]["status"] == "voided"
    assert d.world_agents.bounties[b2]["status"] == "voided"
    assert d.world_agents.orders[oid]["status"] == "refunded"
    assert d.ledger.balances[buyer] == buyer_bal + 20


def test_service_edit_and_pause():
    p, buyer = _uid("p"), _uid("b")
    for x in (p, buyer):
        _make(x)
    r = client.post("/world/services", json={
        "provider_id": p, "name": "Old", "description": "d", "price": 20},
        headers=_auth(p))
    sid = r.json()["id"]
    r = client.patch(f"/world/services/{sid}", json={
        "provider_id": p, "price": 30, "name": "New"}, headers=_auth(p))
    assert r.status_code == 200 and r.json()["price"] == 30
    assert r.json()["name"] == "New"
    # non-provider cannot edit
    r = client.patch(f"/world/services/{sid}", json={
        "provider_id": buyer, "price": 1}, headers=_auth(buyer))
    assert r.status_code == 403, r.text
    # pause stops new orders
    r = client.patch(f"/world/services/{sid}", json={
        "provider_id": p, "active": False}, headers=_auth(p))
    assert r.json()["active"] is False
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid, "idempotency_key": _uid("k")},
        headers=_auth(buyer))
    assert r.status_code == 403, r.text
    # unpause works again
    client.patch(f"/world/services/{sid}", json={
        "provider_id": p, "active": True}, headers=_auth(p))
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid, "idempotency_key": _uid("k")},
        headers=_auth(buyer))
    assert r.status_code == 200, r.text


def test_bounty_search_and_discover():
    p = _uid("p")
    _make(p)
    client.post("/world/bounties", json={
        "agent_id": p, "title": "Zebra taxonomy report", "description": "d",
        "reward": 5}, headers=_auth(p))
    lst = client.get("/world/bounties", params={"q": "zebra"}).json()["bounties"]
    assert lst and all("zebra" in (b["title"] + b["description"]).lower() for b in lst)
    lst2 = client.get("/world/bounties", params={"q": "qqq_no_match"}).json()["bounties"]
    assert lst2 == []
    d = client.post("/world/discover", json={"agent_id": p, "query": "zebra"},
                    headers=_auth(p)).json()
    assert any(b["title"].startswith("Zebra") for b in d["bounties"])


def test_webhook_register_and_fire():
    a = _uid("a")
    _make(a)
    r = client.post("/world/webhooks", json={
        "agent_id": a, "url": "http://127.0.0.1:9/hook",
        "events": ["mention"]}, headers=_auth(a))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret"] and len(body["secret"]) == 64
    hid = body["id"]
    lst = client.get(f"/world/webhooks/{a}", headers=_auth(a)).json()["webhooks"]
    assert any(w["id"] == hid for w in lst)
    assert all("secret" not in w for w in lst)  # secret never listed
    # bad url rejected
    r = client.post("/world/webhooks", json={
        "agent_id": a, "url": "ftp://x"}, headers=_auth(a))
    assert r.status_code == 400, r.text
    # firing to an invalid host is best-effort: mention still lands
    b = _uid("b")
    _make(b)
    d = dail
    d.world_agents.add_mentions("lobby", b, f"hello @{a} ping")
    notifs = client.get(f"/world/notifications/{a}", headers=_auth(a)).json()["notifications"]
    assert any(n.get("kind") == "mention" for n in notifs)
    # delete
    r = client.delete(f"/world/webhooks/{a}/{hid}", headers=_auth(a))
    assert r.status_code == 200, r.text


def test_key_rotation():
    a = _uid("a")
    old = _make(a)
    r = client.post("/world/key/rotate", json={"agent_id": a},
                    headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]
    assert new != old
    # old key is dead
    r = client.get(f"/world/notifications/{a}",
                   headers={"Authorization": f"Bearer {old}"})
    assert r.status_code in (401, 403), r.text
    # new key works
    r = client.get(f"/world/notifications/{a}",
                   headers={"Authorization": f"Bearer {new}"})
    assert r.status_code == 200, r.text
    _keys[a] = new


def test_notification_read_state():
    a, b = _uid("a"), _uid("b")
    for x in (a, b):
        _make(x)
    d = dail
    d.world_agents._notify(a, {"type": "ping", "body": "one"})
    n1 = client.get(f"/world/notifications/{a}", headers=_auth(a)).json()
    assert n1["unread_count"] >= 1
    since = n1["notifications"][-1]["created_at"]
    d.world_agents._notify(a, {"type": "ping", "body": "two"})
    n2 = client.get(f"/world/notifications/{a}", params={"since": since},
                    headers=_auth(a)).json()
    assert all(n["created_at"] > since for n in n2["notifications"])
    assert any(n["body"] == "two" for n in n2["notifications"])
    r = client.post(f"/world/notifications/{a}/ack", json={"agent_id": a},
                    headers=_auth(a))
    assert r.status_code == 200
    n3 = client.get(f"/world/notifications/{a}", headers=_auth(a)).json()
    assert n3["unread_count"] == 0


def test_bulk_review_and_fulfillment_stats():
    p, h = _uid("p"), _uid("h")
    for x in (p, h):
        _make(x)
    bids = []
    for i in range(3):
        r = client.post("/world/bounties", json={
            "agent_id": p, "title": f"Bulk {i}", "description": "d", "reward": 5},
            headers=_auth(p))
        bids.append(r.json()["id"])
        client.post(f"/world/bounties/{bids[-1]}/claim", json={
            "agent_id": h, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(h))
    r = client.post("/world/bounties/batch/accept", json={
        "agent_id": p, "bounty_ids": bids}, headers=_auth(p))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 3
    # fulfillment stats on the service
    r = client.post("/world/services", json={
        "provider_id": p, "name": "s", "description": "d", "price": 10},
        headers=_auth(p))
    sid = r.json()["id"]
    r = client.post("/world/services/purchase", json={
        "buyer_id": h, "service_id": sid, "idempotency_key": _uid("k")},
        headers=_auth(h))
    oid = r.json()["order_id"]
    client.post(f"/world/orders/{oid}/dispute", json={
        "agent_id": h, "reason": "x"}, headers=_auth(h))
    svcs = client.get("/world/services").json()["services"]
    mine = [s for s in svcs if s["id"] == sid][0]
    assert mine.get("orders_disputed", 0) >= 1


def test_webhook_ssrf_private_ips_rejected():
    a = _uid("a")
    _make(a)
    for url in ["https://10.0.0.1/hook", "https://172.16.5.5/hook",
                "https://192.168.1.1/hook", "https://169.254.169.254/hook",
                "https://127.0.0.2/hook", "https://[::1]/hook"]:
        r = client.post("/world/webhooks", json={
            "agent_id": a, "url": url}, headers=_auth(a))
        assert r.status_code == 400, (url, r.text)
        assert "non-public" in r.text or "rejected" in r.text


def test_concurrent_bounty_claims_single_winner():
    import threading
    poster = _uid("p")
    _make(poster)
    hunters = [_uid("h") for _ in range(8)]
    for h in hunters:
        _make(h)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster)).json()["id"]
    results = []
    def _claim(h):
        r = client.post(f"/world/bounties/{bid}/claim", json={
            "agent_id": h, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(h))
        results.append(r.status_code)
    threads = [threading.Thread(target=_claim, args=(h,)) for h in hunters]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(200) == 1, results
    assert results.count(409) == 7, results


def test_self_trade_rejected():
    a = _uid("a")
    _make(a)
    r = client.post("/world/trades", json={
        "seller_id": a, "buyer_id": a, "amount": 5, "item": "x",
        "idempotency_key": _uid("k")}, headers=_auth(a))
    assert r.status_code == 400, r.text
    assert "yourself" in r.text
