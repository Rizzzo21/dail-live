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

HERE = os.path.dirname(os.path.abspath(__file__))

PASSED, FAILED = 0, 0


def check(name: str, cond: bool, extra: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {extra}")


class Mcp:
    def __init__(self):
        env = {k: v for k, v in os.environ.items() if k != "DAIL_AGENT_KEY"}
        env["DAIL_AGENT_ID"] = ""
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "server.py")],
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
        "dail_list_services", "dail_list_bulletins", "dail_get_balance",
        "dail_purchase_service", "dail_order_status", "dail_deliver_order",
        "dail_confirm_order", "dail_dispute_order",
    }
    check("tools/list exposes all 8 tools", names == expected, f"got={sorted(names)}")

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

    # unknown tool must not crash the server
    r = m.call("tools/call", {"name": "dail_nope", "arguments": {}})
    check("unknown tool -> JSON-RPC error, server alive", "error" in r, str(r)[:160])
    r = m.call("ping")
    check("server still alive after errors", r.get("result") == {})

    rc, err = m.close()
    check("server exits cleanly", rc == 0, f"rc={rc} stderr={err[:200]}")

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
