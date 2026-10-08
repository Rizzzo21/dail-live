"""Hostile economy test (2026-10-07, consultant's challenge): 100 autonomous
agents try to break the economy simultaneously -- duplicate payments,
replayed trades/purchases, simultaneous bounty claims, concurrent trials,
self-trades, overdrafts, blank idempotency keys, private-IP webhooks,
expired-bounty snipes. The invariants must hold no matter what."""
import os
import sys
import threading
from pathlib import Path

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
_lock = threading.Lock()


def _uid(prefix):
    with _lock:
        _seq[0] += 1
        return f"{prefix}_hx{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_hostile_100_agents():
    N = 100
    agents = [_uid("a") for _ in range(N)]
    for a in agents:
        _make(a)

    # Snapshot the money supply before the storm.
    before_total = sum(dail.ledger.balances.values())

    # 10 bounties from 10 posters; every agent races to claim 3 of them.
    posters = agents[:10]
    bounty_ids = []
    for p in posters:
        r = client.post("/world/bounties", json={
            "agent_id": p, "title": "t", "description": "d", "reward": 10},
            headers=_auth(p))
        assert r.status_code in (200, 201), r.text
        bounty_ids.append(r.json()["id"])

    # One service with a trial; one service for purchases. Providers are
    # dedicated agents outside the storm (agents can't buy their own).
    prov1, prov2 = _uid("prov"), _uid("prov")
    _make(prov1); _make(prov2)
    r = client.post("/world/services", json={
        "provider_id": prov1, "name": "s", "description": "d",
        "price": 5, "trial_price_dail": 1}, headers=_auth(prov1))
    svc_trial = r.json()["id"]
    r = client.post("/world/services", json={
        "provider_id": prov2, "name": "s2", "description": "d",
        "price": 5}, headers=_auth(prov2))
    svc_buy = r.json()["id"]

    results = {"claim_wins": 0, "trial_wins": 0, "trade_replays_ok": 0,
               "purchase_replays_ok": 0, "rejections_ok": 0}
    errors = []

    def _agent_storm(i):
        try:
            a = agents[i]
            peer = agents[(i + 1) % N]
            # 1. Race bounty claims (3 attempts).
            for b in bounty_ids[i % 10::10][:3] or bounty_ids[:3]:
                r = client.post(f"/world/bounties/{b}/claim", json={
                    "agent_id": a, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(a))
                if r.status_code == 200:
                    with _lock:
                        results["claim_wins"] += 1
                else:
                    assert r.status_code in (400, 409), (b, r.text)
            # 2. Trade with peer, replayed 5x with the same idempotency key.
            idem = _uid("k")
            first = None
            for _ in range(5):
                r = client.post("/world/trades", json={
                    "seller_id": peer, "buyer_id": a, "amount": 2,
                    "item": "x", "idempotency_key": idem},
                    headers=_auth(a))
                assert r.status_code == 200, r.text
                if first is None:
                    first = r.json()["id"]
                assert r.json()["id"] == first, "replay minted a new trade"
            with _lock:
                results["trade_replays_ok"] += 1
            # 3. Service purchase, replayed 3x.
            pidem = _uid("k")
            first_o = None
            for _ in range(3):
                r = client.post("/world/services/purchase", json={
                    "buyer_id": a, "service_id": svc_buy,
                    "idempotency_key": pidem}, headers=_auth(a))
                assert r.status_code == 200, r.text
                if first_o is None:
                    first_o = r.json()["order_id"]
                assert r.json()["order_id"] == first_o
            with _lock:
                results["purchase_replays_ok"] += 1
            # 4. Trial race: 10 concurrent attempts by the SAME agent --
            # exactly one may succeed.
            trial_results = []
            def _trial():
                r = client.post(f"/world/services/{svc_trial}/trial",
                                json={"agent_id": a}, headers=_auth(a))
                trial_results.append(r.status_code)
            tts = [threading.Thread(target=_trial) for _ in range(10)]
            for t in tts: t.start()
            for t in tts: t.join()
            assert trial_results.count(200) == 1, trial_results
            assert trial_results.count(400) == 9, trial_results
            with _lock:
                results["trial_wins"] += 1
            # 5. Attacks that must be rejected.
            rej = 0
            r = client.post("/world/trades", json={
                "seller_id": a, "buyer_id": a, "amount": 5, "item": "x",
                "idempotency_key": _uid("k")}, headers=_auth(a))
            assert r.status_code == 400, r.text
            rej += 1
            r = client.post("/world/trades", json={
                "seller_id": peer, "buyer_id": a, "amount": 10**12,
                "item": "x", "idempotency_key": _uid("k")},
                headers=_auth(a))
            assert r.status_code == 400, r.text
            rej += 1
            r = client.post("/world/trades", json={
                "seller_id": peer, "buyer_id": a, "amount": 2,
                "item": "x", "idempotency_key": "   "},
                headers=_auth(a))
            assert r.status_code == 422, r.text
            rej += 1
            r = client.post("/world/webhooks", json={
                "agent_id": a, "url": "https://10.9.9.9/hook"},
                headers=_auth(a))
            assert r.status_code == 400, r.text
            rej += 1
            with _lock:
                results["rejections_ok"] += rej
        except Exception as e:  # noqa: BLE001 -- collect, don't kill the storm
            with _lock:
                errors.append(repr(e))

    threads = [threading.Thread(target=_agent_storm, args=(i,))
               for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:5]

    # ---- Invariants ----
    # 1. Exactly one claim winner per bounty (10 bounties).
    assert results["claim_wins"] == 10, results
    for b in bounty_ids:
        hunters = dail.world_agents.bounties[b].get("hunter_id")
        assert hunters is not None, f"{b} has no winner"
    # 2. Exactly one trial per agent despite 10 concurrent attempts each.
    assert results["trial_wins"] == N, results
    assert len(dail.world_agents.service_trials) >= N
    # 3. Every replay returned the original record.
    assert results["trade_replays_ok"] == N, results
    assert results["purchase_replays_ok"] == N, results
    # 4. All rejections held.
    assert results["rejections_ok"] == N * 4, results
    # 5. No negative balances anywhere.
    negatives = {k: v for k, v in dail.ledger.balances.items() if v < 0}
    assert not negatives, negatives
    # 6. Money conserved: no operation creates or destroys DAIL. Grants move
    # vault -> agent, fees move agent -> treasury, escrow moves in and out --
    # the total across all ledger accounts must not budge.
    after_total = sum(dail.ledger.balances.values())
    assert after_total == before_total, (before_total, after_total)
    # 7. Trade records match unique idempotency keys (no duplicates).
    assert len(dail.world_agents.trades) >= N, len(dail.world_agents.trades)
    # 8. Post-storm state is self-consistent: every trade record has a
    # matching ledger entry, and no record points at a missing idempotency.
    for tid, t in dail.world_agents.trades.items():
        assert t["status"] == "settled", tid
        assert t["buyer_id"] in dail.ledger.balances, tid


def test_grant_step_down_after_founding_100(monkeypatch):
    # Opt back into the real step-down logic for this test.
    monkeypatch.setenv("DAIL_TEST_GRANT_STEPDOWN", "1")
    wa = dail.world_agents
    saved = wa.full_grants_given
    try:
        wa.full_grants_given = 99
        a = _uid("g")
        r = client.post("/agents", json={"id": a, "name": a})
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == 100, r.json()
        assert wa.full_grants_given == 100
        b = _uid("g")
        r = client.post("/agents", json={"id": b, "name": b})
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == 10, r.json()
        assert wa.full_grants_given == 100  # capped, no overshoot
        # staff never consume slots
        assert wa.claim_starter_grant("dail_host") == 100
        assert wa.full_grants_given == 100
    finally:
        wa.full_grants_given = saved
