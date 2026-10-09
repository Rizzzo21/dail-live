"""Regression tests for Tommy's Top-5 Bug Fixes (2026-10-08).

FIX 1: Webhook SSRF — production HTTPS-only, private IP rejection, no redirects.
FIX 2: Agent profiles persist across restarts (dail_profiles table).
FIX 3: Completed trades persist across restarts (dail_trades table).
FIX 4: Payment notifications via durable event system (dail_payment_events).
FIX 5: Idempotency keys enforced on all money-moving endpoints.

Uses throwaway SQLite databases; each test constructs fresh Dail instances.
"""
import os
import sys
from pathlib import Path

os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import fund_vault


def _fresh_dail(tmp_path, name="t.db"):
    """Fresh Dail against a throwaway SQLite DB."""
    from dail.service import Dail
    db_url = f"sqlite:///{tmp_path}/{name}"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d = Dail()
        fund_vault(d)
        return d
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def _make_agent(d, aid):
    from dail.models import Agent
    d.create_agent(Agent(id=aid, name=aid, goal="test", balance=100))
    return aid


# ---------------------------------------------------------------- FIX 1 ----
class TestWebhookSsrf:
    def test_production_rejects_localhost_http(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DAIL_ENV", "production")
        monkeypatch.delenv("DAIL_REAL_PAYMENTS", raising=False)
        d = _fresh_dail(tmp_path, "ssrf1.db")
        a = _make_agent(d, "ssrf_a1")
        for url in ["http://localhost/hook", "http://127.0.0.1:8080/hook"]:
            try:
                d.world_agents.register_webhook(a, url)
                assert False, f"should have rejected {url} in production"
            except ValueError as e:
                assert "https" in str(e).lower()

    def test_dev_allows_localhost_http(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DAIL_ENV", raising=False)
        monkeypatch.delenv("DAIL_REAL_PAYMENTS", raising=False)
        d = _fresh_dail(tmp_path, "ssrf2.db")
        a = _make_agent(d, "ssrf_a2")
        r = d.world_agents.register_webhook(a, "http://localhost:9999/hook")
        assert r["url"] == "http://localhost:9999/hook"

    def test_private_ips_rejected(self, tmp_path):
        d = _fresh_dail(tmp_path, "ssrf3.db")
        a = _make_agent(d, "ssrf_a3")
        bad = [
            "https://10.0.0.1/hook",
            "https://10.255.255.1/hook",
            "https://172.16.0.1/hook",
            "https://172.31.255.255/hook",
            "https://192.168.1.1/hook",
            "https://127.0.0.2/hook",
            "https://169.254.169.254/hook",  # link-local (cloud metadata)
            "https://[::1]/hook",            # IPv6 loopback
            "https://[fe80::1]/hook",        # IPv6 link-local
            "https://[fc00::1]/hook",        # IPv6 unique local
        ]
        for url in bad:
            try:
                d.world_agents.register_webhook(a, url)
                assert False, f"should have rejected {url}"
            except ValueError as e:
                assert "rejected" in str(e).lower() or "non-public" in str(e).lower(), (url, e)

    def test_dns_rebinding_rejected(self, tmp_path, monkeypatch):
        """A hostname resolving to a private IP is rejected (rebinding)."""
        import socket
        d = _fresh_dail(tmp_path, "ssrf4.db")
        a = _make_agent(d, "ssrf_a4")
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "evil.example.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
            return real_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        try:
            d.world_agents.register_webhook(a, "https://evil.example.com/hook")
            assert False, "DNS-rebinding hostname should be rejected"
        except ValueError as e:
            assert "non-public" in str(e)

    def test_redirects_not_followed(self, tmp_path):
        """_dispatch_webhooks must not follow HTTP redirects."""
        import urllib.request
        d = _fresh_dail(tmp_path, "ssrf5.db")
        # Reach into the dispatch opener construction by inspecting the
        # handler class behavior directly.
        from dail.service import AgentWorld
        import inspect
        src = inspect.getsource(AgentWorld._dispatch_webhooks)
        assert "_NoRedirect" in src
        assert "redirect_request" in src

    def test_blocked_attempts_logged(self, tmp_path):
        d = _fresh_dail(tmp_path, "ssrf6.db")
        a = _make_agent(d, "ssrf_a6")
        try:
            d.world_agents.register_webhook(a, "https://192.168.99.99/hook")
        except ValueError:
            pass
        # The block must appear in the audit log.
        found = [e for e in d.audit.events
                 if isinstance(e, dict) and e.get("event") == "webhook.blocked"]
        assert found, "blocked webhook attempt not logged"


# ---------------------------------------------------------------- FIX 2 ----
class TestProfilePersistence:
    def test_profile_survives_restart(self, tmp_path):
        from dail.service import Dail
        db_url = f"sqlite:///{tmp_path}/prof.db"
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_url
        try:
            d1 = Dail()
            fund_vault(d1)
            _make_agent(d1, "prof_a")
            p1 = d1.world_agents.update_profile(
                "prof_a", "I build trading bots",
                ["python", "defi"])
            assert p1["bio"] == "I build trading bots"
            assert p1["capabilities"] == ["python", "defi"]
            created = p1.get("created_at")

            # Simulated restart: brand-new instance on the same DB.
            d2 = Dail()
            p2 = d2.world_agents.profile("prof_a")
            assert p2["bio"] == "I build trading bots"
            assert p2["capabilities"] == ["python", "defi"]
            assert p2["reputation"] == 100
            assert p2.get("created_at") == created
            assert p2["display_name"] == "prof_a"
        finally:
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old

    def test_ensure_agent_idempotent(self, tmp_path):
        """ensure_agent() must not overwrite an existing profile."""
        from dail.service import Dail
        from dail.models import Agent
        db_url = f"sqlite:///{tmp_path}/prof2.db"
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_url
        try:
            d1 = Dail()
            fund_vault(d1)
            d1.create_agent(Agent(id="prof_b", name="B", goal="g", balance=100))
            d1.world_agents.update_profile("prof_b", "original bio", ["x"])
            created = d1.world_agents.profiles["prof_b"]["created_at"]
            # Second ensure must not reset bio or created_at.
            d1.world_agents.ensure_agent(Agent(id="prof_b", name="B", goal="g", balance=100))
            assert d1.world_agents.profiles["prof_b"]["bio"] == "original bio"
            assert d1.world_agents.profiles["prof_b"]["created_at"] == created
        finally:
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old


# ---------------------------------------------------------------- FIX 3 ----
class TestTradePersistence:
    def test_trade_survives_restart(self, tmp_path):
        from dail.service import Dail
        db_url = f"sqlite:///{tmp_path}/trade.db"
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_url
        try:
            d1 = Dail()
            fund_vault(d1)
            _make_agent(d1, "tr_s")
            _make_agent(d1, "tr_b")
            t1 = d1.world_agents.trade("tr_s", "tr_b", 40, "widget",
                                       idem="trade-persist-0001")
            assert t1["status"] == "settled"
            assert t1["amount"] == 40

            d2 = Dail()
            assert t1["id"] in d2.world_agents.trades
            t2 = d2.world_agents.trades[t1["id"]]
            assert t2["buyer_id"] == "tr_b"
            assert t2["seller_id"] == "tr_s"
            assert t2["principal_amount"] == 40
            assert t2["status"] == "settled"
            # Balances derived from the replayed ledger still agree.
            assert d2.ledger.balances["tr_b"] == 100 - 40
        finally:
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old

    def test_trade_replay_10x_single_record(self, tmp_path):
        """Same economic request 10x -> exactly 1 trade record."""
        d = _fresh_dail(tmp_path, "trade10.db")
        _make_agent(d, "tr10_s")
        _make_agent(d, "tr10_b")
        key = "trade-replay-0001"
        first = None
        for _ in range(10):
            t = d.world_agents.trade("tr10_s", "tr10_b", 25, "widget", idem=key)
            if first is None:
                first = t["id"]
            assert t["id"] == first
        assert len(d.world_agents.trades) == 1
        # Exactly one principal debit on the buyer.
        assert d.ledger.balances["tr10_b"] == 100 - 25
        # DB has exactly one row for the key.
        assert d.store.find_trade_by_idem(key) == first


# ---------------------------------------------------------------- FIX 4 ----
class TestPaymentEvents:
    def test_notify_payment_event_exactly_once(self, tmp_path):
        d = _fresh_dail(tmp_path, "payevt.db")
        a = _make_agent(d, "pay_a")
        item = {"type": "topup_credited", "rail": "stripe",
                "amount_dail": 5, "transaction_id": "tx_000001"}
        # First call: notifies.
        assert d.world_agents.notify_payment_event(
            "evt:test:001", a, "stripe_topup", 5, item) is True
        notes = d.world_agents.notifications_for(a)["notifications"]
        assert sum(1 for n in notes if n.get("type") == "topup_credited") == 1
        # Replay: no duplicate notification.
        assert d.world_agents.notify_payment_event(
            "evt:test:001", a, "stripe_topup", 5, item) is False
        notes = d.world_agents.notifications_for(a)["notifications"]
        assert sum(1 for n in notes if n.get("type") == "topup_credited") == 1
        # Event row is durable.
        exists, notified_at = d.store.get_payment_event("evt:test:001")
        assert exists and notified_at

    def test_announce_to_uses_notify(self, tmp_path):
        """announce_to must route through _notify (webhook-capable)."""
        from dail.usdc_payments import UsdcPayments
        from dail.production_payments import ProductionPayments
        from dail.x402_payments import X402Payments
        d = _fresh_dail(tmp_path, "payann.db")
        a = _make_agent(d, "pay_b")
        notified = []
        orig = d.world_agents._notify
        d.world_agents._notify = lambda aid, item: notified.append((aid, item))
        try:
            UsdcPayments(d).announce_to(a)
            ProductionPayments(d).announce_to(a)
            X402Payments(d).announce_to(a)
        finally:
            d.world_agents._notify = orig
        assert len(notified) == 3
        assert all(n[0] == a for n in notified)
        assert all(n[1].get("type") == "payment_rail_live" for n in notified)


# ---------------------------------------------------------------- FIX 5 ----
class TestIdempotencyEnforcement:
    def test_trade_rejects_bad_keys(self, tmp_path):
        d = _fresh_dail(tmp_path, "idem1.db")
        _make_agent(d, "id_s")
        _make_agent(d, "id_b")
        for bad in ["", "   ", "ab", "x", "a" * 129, "has space",
                    "semi;colon", "quote'"]:
            try:
                d.world_agents.trade("id_s", "id_b", 10, "w", idem=bad)
                assert False, f"should have rejected key {bad!r}"
            except ValueError:
                pass
        # None is also rejected for trade (key required).
        try:
            d.world_agents.trade("id_s", "id_b", 10, "w", idem=None)
            assert False, "None key should be rejected for trade"
        except ValueError:
            pass

    def test_trade_accepts_good_keys(self, tmp_path):
        d = _fresh_dail(tmp_path, "idem2.db")
        _make_agent(d, "id2_s")
        _make_agent(d, "id2_b")
        t = d.world_agents.trade("id2_s", "id2_b", 10, "w",
                                 idem="valid-key:0001")
        assert t["status"] == "settled"

    def test_purchase_rejects_blank_key(self, tmp_path):
        d = _fresh_dail(tmp_path, "idem3.db")
        _make_agent(d, "id3_p")
        _make_agent(d, "id3_b")
        svc = d.world_agents.create_service("id3_p", "S", "desc", 20)
        for bad in ["", "   "]:
            try:
                d.world_agents.purchase_service("id3_b", svc["id"], idem=bad)
                assert False, f"should have rejected key {bad!r}"
            except ValueError:
                pass

    def test_purchase_replay_10x_single_order(self, tmp_path):
        d = _fresh_dail(tmp_path, "idem4.db")
        _make_agent(d, "id4_p")
        _make_agent(d, "id4_b")
        svc = d.world_agents.create_service("id4_p", "S", "desc", 20)
        key = "purchase-replay-0001"
        first = None
        for _ in range(10):
            o = d.world_agents.purchase_service("id4_b", svc["id"], idem=key)
            if first is None:
                first = o["order_id"]
            assert o["order_id"] == first
        # Buyer escrowed exactly once.
        assert d.ledger.balances["id4_b"] == 100 - 20

    def test_deposit_rejects_blank_key(self, tmp_path):
        d = _fresh_dail(tmp_path, "idem5.db")
        _make_agent(d, "id5_a")
        for bad in ["", "   ", None]:
            try:
                d.deposit("id5_a", 10, "mock", bad)
                assert False, f"should have rejected key {bad!r}"
            except ValueError:
                pass
