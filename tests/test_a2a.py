import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_a2a{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _rpc(aid, payload, raw=None):
    headers = dict(_auth(aid))
    headers["Content-Type"] = "application/json"
    if raw is not None:
        return client.post("/a2a/rpc", content=raw, headers=headers)
    return client.post("/a2a/rpc", json=payload, headers=headers)


def _send(buyer, service_id, req_id="r1", method="SendMessage", extra=None):
    params = {
        "message": {
            "messageId": f"msg-{req_id}",
            "role": "ROLE_USER",
            "parts": [{"data": {"service_id": service_id}, "mediaType": "application/json"}],
        }
    }
    if extra:
        params.update(extra)
    return _rpc(buyer, {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


def _setup_pair(price=25):
    seller, buyer = _uid("s"), _uid("b")
    _make(seller)
    _make(buyer)
    r = client.post("/world/services", json={
        "provider_id": seller, "name": "A2A test service",
        "description": "sold via A2A", "price": price}, headers=_auth(seller))
    assert r.status_code == 200, r.text
    return seller, buyer, r.json()["id"]


# ---------------------------------------------------------------- card ---

def test_agent_card_a2a_compliant():
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200, r.text
    card = r.json()
    # required v1 fields
    for f in ("name", "description", "supportedInterfaces", "version",
              "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
        assert f in card, f"missing card field: {f}"
    iface = card["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC"
    assert iface["protocolVersion"] == "1.0"
    assert iface["url"].endswith("/a2a/rpc")
    assert card["capabilities"]["streaming"] is False
    assert card["capabilities"]["pushNotifications"] is False
    # auth declared as Bearer httpAuth scheme
    schemes = card["securitySchemes"]
    assert "bearerAuth" in schemes
    assert schemes["bearerAuth"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert card["securityRequirements"][0]["schemes"]["bearerAuth"] == {"list": []}
    # commerce skills with id/name/description/tags/examples
    by_id = {s["id"]: s for s in card["skills"]}
    for sid in ("buy-service", "sell-service", "post-bounty", "claim-bounty"):
        assert sid in by_id, f"missing skill: {sid}"
        s = by_id[sid]
        for f in ("id", "name", "description", "tags", "examples"):
            assert f in s and s[f], f"skill {sid} missing {f}"
    # alias still works
    r2 = client.get("/.well-known/agent.json")
    assert r2.status_code == 200 and r2.json()["name"] == card["name"]


# ------------------------------------------------------- message/send ---

def test_send_message_creates_escrowed_order():
    seller, buyer, svc = _setup_pair(price=25)
    b0 = _bal(buyer)
    r = _send(buyer, svc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == "r1"
    task = body["result"]["task"]
    assert task["id"].startswith("ord_")
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert task["metadata"]["dail_order_id"] == task["id"]
    assert task["metadata"]["service_id"] == svc
    assert task["metadata"]["amount"] == 25
    # escrow: buyer charged in full
    assert _bal(buyer) == b0 - 25
    # GetTask shows working while awaiting delivery
    g = _rpc(buyer, {"jsonrpc": "2.0", "id": "r2", "method": "GetTask",
                     "params": {"id": task["id"]}})
    assert g.json()["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"


def test_send_message_v03_alias_and_idempotent_retry():
    seller, buyer, svc = _setup_pair(price=15)
    b0 = _bal(buyer)
    r = _send(buyer, svc, req_id="v3", method="message/send")
    assert r.status_code == 200, r.text
    tid = r.json()["result"]["task"]["id"]
    # same messageId retried -> same order, no double charge
    r2 = _send(buyer, svc, req_id="v3", method="message/send")
    assert r2.json()["result"]["task"]["id"] == tid
    assert _bal(buyer) == b0 - 15


def test_send_message_service_id_variants():
    seller, buyer, svc = _setup_pair(price=10)
    # service_id in params.metadata
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "m1", "method": "SendMessage", "params": {
        "message": {"messageId": "mm000001", "role": "ROLE_USER",
                    "parts": [{"text": "please buy this", "mediaType": "text/plain"}]},
        "metadata": {"service_id": svc}}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["task"]["metadata"]["service_id"] == svc
    # service_id mentioned in text part
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "m2", "method": "SendMessage", "params": {
        "message": {"messageId": "mm000002", "role": "ROLE_USER",
                    "parts": [{"text": f"buy {svc} now", "mediaType": "text/plain"}]}}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["task"]["metadata"]["service_id"] == svc


def test_send_message_missing_service_id():
    seller, buyer, svc = _setup_pair()
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "e1", "method": "SendMessage", "params": {
        "message": {"messageId": "em1", "role": "ROLE_USER",
                    "parts": [{"text": "buy something", "mediaType": "text/plain"}]}}})
    body = r.json()
    assert body["error"]["code"] == -32602


def test_send_message_unknown_service():
    seller, buyer, svc = _setup_pair()
    r = _send(buyer, "svc_9999", req_id="unk")
    assert r.json()["error"]["code"] == -32602


# ------------------------------------------------------------ tasks/get ---

def test_get_task_full_lifecycle():
    seller, buyer, svc = _setup_pair(price=20)
    tid = _send(buyer, svc, req_id="lc1").json()["result"]["task"]["id"]
    # seller delivers via REST
    r = client.post(f"/world/orders/{tid}/deliver",
                    json={"agent_id": seller, "delivery": "the finished report"},
                    headers=_auth(seller))
    assert r.status_code == 200, r.text
    g = _rpc(buyer, {"jsonrpc": "2.0", "id": "lc2", "method": "GetTask",
                     "params": {"id": tid}}).json()["result"]["task"]
    assert g["status"]["state"] == "TASK_STATE_WORKING"
    assert any(a["name"] == "delivery" for a in g["artifacts"])
    # buyer confirms -> completed
    r = client.post(f"/world/orders/{tid}/confirm", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 200, r.text
    g = _rpc(buyer, {"jsonrpc": "2.0", "id": "lc3", "method": "GetTask",
                     "params": {"id": tid}}).json()["result"]["task"]
    assert g["status"]["state"] == "TASK_STATE_COMPLETED"


def test_get_task_disputed_maps_to_input_required():
    seller, buyer, svc = _setup_pair(price=12)
    tid = _send(buyer, svc, req_id="dp1").json()["result"]["task"]["id"]
    r = client.post(f"/world/orders/{tid}/dispute",
                    json={"agent_id": buyer, "reason": "not what I asked"},
                    headers=_auth(buyer))
    assert r.status_code == 200, r.text
    g = _rpc(buyer, {"jsonrpc": "2.0", "id": "dp2", "method": "GetTask",
                     "params": {"id": tid}}).json()["result"]["task"]
    assert g["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


def test_get_task_not_found_and_not_yours():
    seller, buyer, svc = _setup_pair()
    other = _uid("o")
    _make(other)
    tid = _send(buyer, svc, req_id="nf1").json()["result"]["task"]["id"]
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "nf2", "method": "GetTask",
                     "params": {"id": "ord_9999"}})
    assert r.json()["error"]["code"] == -32001
    # unrelated agent cannot see the task either
    r = _rpc(other, {"jsonrpc": "2.0", "id": "nf3", "method": "GetTask",
                     "params": {"id": tid}})
    assert r.json()["error"]["code"] == -32001


# ------------------------------------------------------------ tasks/cancel ---

def test_cancel_task_refunds_escrow():
    seller, buyer, svc = _setup_pair(price=30)
    b0 = _bal(buyer)
    tid = _send(buyer, svc, req_id="cx1").json()["result"]["task"]["id"]
    assert _bal(buyer) == b0 - 30
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "cx2", "method": "CancelTask",
                     "params": {"id": tid}})
    body = r.json()
    assert body["result"]["task"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert _bal(buyer) == b0  # full escrow refund
    g = _rpc(buyer, {"jsonrpc": "2.0", "id": "cx3", "method": "GetTask",
                     "params": {"id": tid}}).json()["result"]["task"]
    assert g["status"]["state"] == "TASK_STATE_CANCELED"


def test_cancel_task_after_delivery_rejected():
    seller, buyer, svc = _setup_pair(price=18)
    tid = _send(buyer, svc, req_id="nc1").json()["result"]["task"]["id"]
    client.post(f"/world/orders/{tid}/deliver",
                json={"agent_id": seller, "delivery": "done"},
                headers=_auth(seller))
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "nc2", "method": "CancelTask",
                     "params": {"id": tid}})
    assert r.json()["error"]["code"] == -32002  # TaskNotCancelableError


def test_cancel_task_by_non_buyer_rejected():
    seller, buyer, svc = _setup_pair()
    other = _uid("o2")
    _make(other)
    tid = _send(buyer, svc, req_id="nb1").json()["result"]["task"]["id"]
    r = _rpc(other, {"jsonrpc": "2.0", "id": "nb2", "method": "CancelTask",
                     "params": {"id": tid}})
    assert r.json()["error"]["code"] == -32001


# ------------------------------------------------------------------ auth ---

def test_a2a_requires_bearer_key():
    r = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": "a1",
                                      "method": "GetTask", "params": {"id": "ord_0001"}})
    assert r.status_code == 401
    r = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": "a2", "method": "GetTask",
                                      "params": {"id": "ord_0001"}},
                    headers={"Authorization": "Bearer bogus-key"})
    assert r.status_code == 401


# --------------------------------------------------------------- malformed ---

def test_malformed_json_rpc():
    seller, buyer, svc = _setup_pair()
    h = dict(_auth(buyer))
    h["Content-Type"] = "application/json"
    # invalid JSON body
    r = client.post("/a2a/rpc", content="{not json", headers=h)
    assert r.json()["error"]["code"] == -32700
    # missing jsonrpc version
    r = client.post("/a2a/rpc",
                    json={"id": "x1", "method": "GetTask", "params": {"id": "ord_0001"}},
                    headers=_auth(buyer))
    assert r.json()["error"]["code"] == -32600
    # unknown method (both v1 and v0.3 namespaces)
    for m in ("tasks/resubscribe", "Frobnicate"):
        r = _rpc(buyer, {"jsonrpc": "2.0", "id": "x2", "method": m, "params": {}})
        assert r.json()["error"]["code"] == -32601, m
    # params not an object
    r = _rpc(buyer, {"jsonrpc": "2.0", "id": "x3", "method": "GetTask",
                     "params": ["ord_0001"]})
    assert r.json()["error"]["code"] == -32602
