#!/usr/bin/env python3
"""Smoke tests for the DAiL MCP server.

Spawns server.py over stdio and exercises the JSON-RPC protocol plus the
live site's PUBLIC read endpoints only. No real-money actions, no secrets:
DAIL_AGENT_KEY is explicitly stripped from the test environment, and the
authed tools are verified to fail gracefully (clean JSON-RPC error, no crash).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

PASSED, FAILED = 0, 0


def urllib_request_url(url: str) -> str:
    """GET a live public endpoint for cross-checking tool output."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def check(name: str, cond: bool, extra: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {extra}")


class Mcp:
    def __init__(self, extra_env=None, args=None):
        env = {k: v for k, v in os.environ.items() if k not in ("DAIL_AGENT_KEY", "DAIL_READONLY")}
        env["DAIL_AGENT_ID"] = ""
        if extra_env:
            env.update(extra_env)
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "server.py")] + (args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()
        headers = {}
        while True:
            line = self.p.stdout.readline().decode().strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return json.loads(self.p.stdout.read(int(headers["content-length"])).decode())

    def notify(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.wait(timeout=10)
        err = self.p.stderr.read().decode()
        return self.p.returncode, err


def main():
    m = Mcp()

    r = m.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    check("initialize -> capabilities.tools", "result" in r and "tools" in r["result"].get("capabilities", {}), str(r)[:120])
    m.notify("notifications/initialized")

    r = m.call("ping")
    check("ping", r.get("result") == {}, str(r)[:120])

    r = m.call("tools/list")
    tools = r.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    expected = {
        "dail_list_services", "dail_list_bulletins", "dail_list_bounties",
        "dail_post_bounty", "dail_claim_bounty", "dail_submit_bounty", "dail_accept_bounty", "dail_cancel_bounty",
        "dail_get_balance", "dail_purchase_service", "dail_order_status", "dail_deliver_order",
        "dail_confirm_order", "dail_dispute_order",
    }
    check("tools/list exposes all 14 tools", names == expected, f"got={sorted(names)}")

    # buyer-verb descriptions: every tool description leads with a verb
    descs = {t["name"]: t.get("description", "") for t in tools}
    verb_ok = all(d and d[0].isupper() and d.split()[0] in
                  {"Hire", "Browse", "Find", "Post", "Claim", "Submit", "Accept", "Cancel", "Check", "Buy", "Track", "Deliver", "Confirm", "Dispute"}
                  for d in descs.values())
    check("tool descriptions use buyer verbs", verb_ok, str({k: v[:24] for k, v in descs.items()})[:200])

    # --- live public reads ---
    r = m.call("tools/call", {"name": "dail_list_services", "arguments": {}})
    try:
        svcs = json.loads(r["result"]["content"][0]["text"])
        slist = svcs.get("services", svcs if isinstance(svcs, list) else [])
        check("dail_list_services returns services", isinstance(slist, list) and len(slist) > 0, str(svcs)[:120])
    except Exception as e:
        check("dail_list_services returns services", False, f"{e} :: {str(r)[:200]}")

    r = m.call("tools/call", {"name": "dail_list_bulletins", "arguments": {}})
    try:
        blts = json.loads(r["result"]["content"][0]["text"])
        check("dail_list_bulletins parses as JSON", isinstance(blts, (dict, list)), str(blts)[:120])
    except Exception as e:
        check("dail_list_bulletins parses as JSON", False, f"{e} :: {str(r)[:200]}")

    r = m.call("tools/call", {"name": "dail_list_bounties", "arguments": {"status": "open"}})
    try:
        bnts = json.loads(r["result"]["content"][0]["text"])
        blist = bnts.get("bounties", bnts if isinstance(bnts, list) else [])
        check("dail_list_bounties returns open bounties", isinstance(blist, list) and len(blist) > 0, str(bnts)[:120])
    except Exception as e:
        check("dail_list_bounties returns open bounties", False, f"{e} :: {str(r)[:200]}")

    # --- graceful degradation without a key ---
    r = m.call("tools/call", {"name": "dail_get_balance", "arguments": {"agent_id": "nobody"}})
    check(
        "dail_get_balance without key -> clean JSON-RPC error",
        "error" in r and r["error"]["code"] == -32000,
        str(r)[:160],
    )

    r = m.call("tools/call", {"name": "dail_order_status", "arguments": {"order_id": "ord_nope", "agent_id": "nobody"}})
    check("dail_order_status without key -> clean JSON-RPC error", "error" in r, str(r)[:160])

    r = m.call("tools/call", {"name": "dail_purchase_service", "arguments": {"service_id": "svc_nope"}})
    check(
        "dail_purchase_service without agent id/key -> clean error, no purchase attempted",
        "error" in r and "agent" in r["error"]["message"].lower(),
        str(r)[:160],
    )

    r = m.call("tools/call", {"name": "dail_submit_bounty", "arguments": {"bounty_id": "bnty_nope", "submission": "x" * 150}})
    check(
        "dail_submit_bounty without agent id/key -> clean error, no submission attempted",
        "error" in r and "agent" in r["error"]["message"].lower(),
        str(r)[:160],
    )

    r = m.call("tools/call", {"name": "dail_post_bounty", "arguments": {"title": "t", "description": "d", "reward": 5}})
    check(
        "dail_post_bounty without agent id/key -> clean error, no escrow moved",
        "error" in r and "agent" in r["error"]["message"].lower(),
        str(r)[:160],
    )

    # unknown tool must not crash the server
    r = m.call("tools/call", {"name": "dail_nope", "arguments": {}})
    check("unknown tool -> JSON-RPC error, server alive", "error" in r, str(r)[:160])
    r = m.call("ping")
    check("server still alive after errors", r.get("result") == {})

    rc, err = m.close()
    check("server exits cleanly", rc == 0, f"rc={rc} stderr={err[:200]}")

    # --- read-only mode (Spec 2): no key, DAIL_READONLY=1 ---
    print("\n-- read-only mode --")
    ro = Mcp(extra_env={"DAIL_READONLY": "1"})
    ro.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    ro.notify("notifications/initialized")

    r = ro.call("tools/list")
    ro_tools = {t["name"] for t in r.get("result", {}).get("tools", [])}
    check(
        "read-only tools/list shows exactly three tools",
        ro_tools == {"dail_list_bounties", "dail_list_agents", "dail_get_receipt"},
        f"got={sorted(ro_tools)}",
    )

    r = ro.call("tools/call", {"name": "dail_list_agents", "arguments": {}})
    try:
        agents = json.loads(r["result"]["content"][0]["text"])
        keys_ok = all(
            set(a) == {"agent_id", "name", "balance", "bounties_won", "dail_earned_gross", "passport_url"}
            for a in agents
        )
        won = [a["bounties_won"] for a in agents]
        urls_ok = all(a["passport_url"] == f"/passport/{a['agent_id']}" for a in agents)
        check(
            "dail_list_agents default sort: shaped rows, bounties_won desc, passport urls",
            isinstance(agents, list) and len(agents) > 0 and keys_ok and urls_ok and won == sorted(won, reverse=True),
            str(agents)[:200],
        )
        # cross-check first row against the live public endpoint
        live = json.loads(urllib_request_url("https://dail-3dci.onrender.com/observatory/public/data"))
        lb = live.get("leaderboard", [])
        top = max(lb, key=lambda x: x["bounties_won"]) if lb else None
        check(
            "dail_list_agents top row matches live Observatory leaderboard",
            top is not None and agents[0]["agent_id"] == top["id"] and agents[0]["dail_earned_gross"] == top["dail_earned"],
            f"tool={agents[0] if agents else None} live={top}",
        )
    except Exception as e:
        check("dail_list_agents default sort", False, f"{e} :: {str(r)[:200]}")

    r = ro.call("tools/call", {"name": "dail_list_agents", "arguments": {"sort": "balance", "limit": 3}})
    try:
        agents = json.loads(r["result"]["content"][0]["text"])
        check("dail_list_agents sort=balance limit=3", len(agents) == 3, str(agents)[:160])
    except Exception as e:
        check("dail_list_agents sort=balance limit=3", False, f"{e} :: {str(r)[:200]}")

    r = ro.call("tools/call", {"name": "dail_list_agents", "arguments": {"newcomer_only": True}})
    check(
        "dail_list_agents newcomer_only -> newcomers-board error, no guessed filter",
        "error" in r and "newcomers board" in r["error"]["message"],
        str(r)[:200],
    )

    r = ro.call("tools/call", {"name": "dail_get_receipt", "arguments": {"bounty_id": "bnty_0244"}})
    try:
        rcpt = json.loads(r["result"]["content"][0]["text"])
        live_rcpt = json.loads(urllib_request_url("https://dail-3dci.onrender.com/receipts/bnty_0244"))
        verbatim = all(rcpt.get(k) == v for k, v in live_rcpt.items())
        check(
            "dail_get_receipt returns live receipt verbatim + verify_via",
            verbatim and rcpt.get("verify_via") == "/.well-known/dail-pubkey"
            and rcpt.get("hunter_id") == "ozma-audit-161030" and rcpt.get("net_paid") == 14,
            str(rcpt)[:200],
        )
    except Exception as e:
        check("dail_get_receipt returns live receipt verbatim + verify_via", False, f"{e} :: {str(r)[:200]}")

    r = ro.call("tools/call", {"name": "dail_get_receipt", "arguments": {"bounty_id": "bnty_nope"}})
    check("dail_get_receipt unknown id -> error, never synthesized", "error" in r, str(r)[:160])

    r = ro.call("tools/call", {"name": "dail_list_bounties", "arguments": {"status": "open", "limit": 2}})
    try:
        bnts = json.loads(r["result"]["content"][0]["text"])
        blist = bnts.get("bounties", bnts if isinstance(bnts, list) else [])
        check("dail_list_bounties limit=2 in read-only mode", len(blist) == 2, str(bnts)[:160])
    except Exception as e:
        check("dail_list_bounties limit=2 in read-only mode", False, f"{e} :: {str(r)[:200]}")

    # write-tool refusal must not move anything: bounty count unchanged
    before = json.loads(urllib_request_url("https://dail-3dci.onrender.com/world/bounties?status=open"))
    before_n = before.get("count") if isinstance(before, dict) else len(before)
    r = ro.call("tools/call", {"name": "dail_post_bounty", "arguments": {"title": "t", "description": "d", "reward": 5}})
    after = json.loads(urllib_request_url("https://dail-3dci.onrender.com/world/bounties?status=open"))
    after_n = after.get("count") if isinstance(after, dict) else len(after)
    check(
        "dail_post_bounty in read-only mode -> error, open bounty count unchanged",
        "error" in r and "read-only" in r["error"]["message"] and before_n == after_n,
        f"error={str(r)[:120]} before={before_n} after={after_n}",
    )

    rc, err = ro.close()
    check("read-only server exits cleanly", rc == 0, f"rc={rc} stderr={err[:200]}")

    # --- --readonly CLI flag alone must also expose exactly three tools ---
    flag = Mcp(args=["--readonly"])
    flag.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    r = flag.call("tools/list")
    flag_tools = {t["name"] for t in r.get("result", {}).get("tools", [])}
    check(
        "--readonly flag alone exposes exactly three tools",
        flag_tools == {"dail_list_bounties", "dail_list_agents", "dail_get_receipt"},
        f"got={sorted(flag_tools)}",
    )
    rc, err = flag.close()
    check("flag-mode server exits cleanly", rc == 0, f"rc={rc} stderr={err[:200]}")

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
