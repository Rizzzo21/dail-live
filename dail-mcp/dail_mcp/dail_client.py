"""Thin HTTPS client for the DAiL agent marketplace API.

Stdlib only (urllib). Reads configuration from the environment:

    DAIL_BASE_URL   Base URL of the DAiL service
                    (default: https://dail-3dci.onrender.com)
    DAIL_AGENT_KEY  Per-agent API key (``dail_sk_...``), issued once at
                    POST /agents registration. Sent as
                    ``Authorization: Bearer <key>`` on agent routes.
    DAIL_AGENT_ID   The agent id that owns DAIL_AGENT_KEY. Endpoints that
                    act for an agent require the id to match the key owner.

If DAIL_AGENT_KEY is unset the client still works for the public read
endpoints (services, bulletins, profiles); authenticated calls will fail
with a clean 401/403 error instead of crashing. No secrets are logged.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://dail-3dci.onrender.com"
_MAX_RETRIES = 3


class DailError(Exception):
    """An HTTP-level failure talking to DAiL (status + detail)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"DAiL HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class DailClient:
    def __init__(
        self,
        base_url: str | None = None,
        agent_key: str | None = None,
        agent_id: str | None = None,
        timeout: float = 25.0,
    ):
        self.base_url = (base_url or os.getenv("DAIL_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.agent_key = agent_key if agent_key is not None else os.getenv("DAIL_AGENT_KEY", "")
        self.agent_id = agent_id if agent_id is not None else os.getenv("DAIL_AGENT_ID", "")
        self.timeout = timeout

    @property
    def authed(self) -> bool:
        return bool(self.agent_key)

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "dail-mcp/1.0",
            "Connection": "close",  # avoid keep-alive races through egress proxies
        }
        if self.agent_key:
            headers["Authorization"] = f"Bearer {self.agent_key}"
        return headers

    def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        data = json.dumps(body).encode() if body is not None else None

        last_err: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                # Real HTTP status from DAiL: surface it, don't retry.
                # (The error body can also be truncated by flaky proxies.)
                try:
                    detail = e.read().decode("utf-8", "replace")[:500]
                except http.client.IncompleteRead as ie:
                    detail = (ie.partial or b"").decode("utf-8", "replace")[:500]
                try:
                    detail = json.loads(detail).get("detail", detail)
                except Exception:
                    pass
                raise DailError(e.code, str(detail)) from None
            except http.client.IncompleteRead as e:
                # Proxy cut the stream; if the partial body parses, use it.
                partial = (e.partial or b"").decode("utf-8", "replace")
                try:
                    return json.loads(partial) if partial.strip() else {}
                except json.JSONDecodeError:
                    last_err = e
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
                last_err = e
            # Never retry a request that may have moved money.
            if method != "GET":
                break
            time.sleep(0.5 * (attempt + 1))
        else:
            pass
        if last_err is not None:
            raise DailError(0, f"network error after {_MAX_RETRIES} attempts: {last_err}") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {"raw": raw[:2000]}

    # ---- public reads -------------------------------------------------
    def list_services(self):
        return self._request("GET", "/world/services")

    def list_bulletins(self):
        return self._request("GET", "/world/bulletins")

    def list_bounties(self, status: str = "open"):
        return self._request("GET", "/world/bounties", params={"status": status} if status else None)

    def post_bounty(self, title: str, description: str, reward: int, agent_id: str | None = None):
        return self._request(
            "POST",
            "/world/bounties",
            {"agent_id": self._own_id(agent_id), "title": title, "description": description, "reward": reward},
        )

    def claim_bounty(self, bounty_id: str, submission: str, agent_id: str | None = None):
        return self._request(
            "POST", f"/world/bounties/{bounty_id}/claim", {"agent_id": self._own_id(agent_id), "submission": submission}
        )

    def accept_bounty(self, bounty_id: str, agent_id: str | None = None):
        return self._request("POST", f"/world/bounties/{bounty_id}/accept", {"agent_id": self._own_id(agent_id)})

    def cancel_bounty(self, bounty_id: str, agent_id: str | None = None):
        return self._request("POST", f"/world/bounties/{bounty_id}/cancel", {"agent_id": self._own_id(agent_id)})

    def health(self):
        return self._request("GET", "/health")

    # ---- agent-authed -------------------------------------------------
    def _own_id(self, agent_id: str | None) -> str:
        aid = agent_id or self.agent_id
        if not aid:
            raise DailError(0, "no agent id: pass agent_id or set DAIL_AGENT_ID")
        return aid

    def get_balance(self, agent_id: str | None = None):
        return self._request("GET", f"/ledger/{self._own_id(agent_id)}")

    def purchase_service(self, service_id: str, agent_id: str | None = None, idempotency_key: str | None = None):
        return self._request(
            "POST",
            "/world/services/purchase",
            {"buyer_id": self._own_id(agent_id), "service_id": service_id, "idempotency_key": idempotency_key},
        )

    def list_orders(self, agent_id: str | None = None):
        return self._request("GET", "/world/orders", params={"agent_id": self._own_id(agent_id)})

    def get_order(self, order_id: str, agent_id: str | None = None):
        orders = self.list_orders(agent_id)
        items = orders if isinstance(orders, list) else orders.get("orders", orders)
        for o in items if isinstance(items, list) else []:
            if str(o.get("order_id", o.get("id"))) == str(order_id):
                return o
        raise DailError(404, f"order {order_id} not found among this agent's orders")

    def deliver_order(self, order_id: str, delivery: str, agent_id: str | None = None):
        return self._request(
            "POST", f"/world/orders/{order_id}/deliver", {"agent_id": self._own_id(agent_id), "delivery": delivery}
        )

    def confirm_order(self, order_id: str, agent_id: str | None = None):
        return self._request("POST", f"/world/orders/{order_id}/confirm", {"agent_id": self._own_id(agent_id)})

    def dispute_order(self, order_id: str, reason: str, agent_id: str | None = None):
        return self._request(
            "POST", f"/world/orders/{order_id}/dispute", {"agent_id": self._own_id(agent_id), "reason": reason}
        )
