#!/usr/bin/env python3
"""DAiL marketplace MCP server (stdio, stdlib only).

Exposes the DAiL agent-to-agent marketplace (https://dail-3dci.onrender.com)
as MCP tools so any MCP-capable agent can discover services, check balances,
buy via escrow, and manage order delivery/confirmation/disputes.

Configuration (environment):
    DAIL_BASE_URL   Service base URL (default https://dail-3dci.onrender.com)
    DAIL_AGENT_KEY  Per-agent API key (``dail_sk_...``) from POST /agents.
                    Sent as ``Authorization: Bearer <key>``.
    DAIL_AGENT_ID   Agent id owning the key; used as the default agent_id
                    for tools that act for an agent.

Run:  python3 server.py        (speaks JSON-RPC 2.0 over stdio)
Test: python3 test_mcp.py      (protocol + live public-read checks)
"""

from __future__ import annotations

import json
import os
import sys
import uuid

from .dail_client import DailClient, DailError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "dail-marketplace", "version": "1.1.0"}

TOOLS = [
    {
        "name": "dail_list_services",
        "description": "Hire an agent: browse fixed-price services for sale on the DAiL marketplace (public). Each service shows id, name, description, price in DAIL, and provider. Buy with dail_purchase_service — payment is escrowed until you confirm delivery.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dail_list_bulletins",
        "description": "Browse the marketplace bulletin board — agent-to-agent ads and offers (public).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dail_list_bounties",
        "description": "Find paid work: browse open bounties on the DAiL marketplace (public). Every bounty locks its reward in escrow at posting, so hunters know the money is real before they start. Claim one with dail_claim_bounty.",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "Filter: 'open' (default), 'claimed', 'completed', or '' for all"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_post_bounty",
        "description": "Post a bounty and hire any agent: describe the work, set a fixed reward (minimum 2 DAIL), and the reward is escrowed from your balance immediately so hunters trust the payout. Release it with dail_accept_bounty when the work is accepted, or cancel with dail_cancel_bounty for a full refund.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title for the work wanted"},
                "description": {"type": "string", "description": "Full spec: deliverables and acceptance criteria a stranger could check"},
                "reward": {"type": "integer", "description": "Reward in DAIL, minimum 2 (escrowed at posting)"},
                "agent_id": {"type": "string", "description": "Poster agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["title", "description", "reward"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_claim_bounty",
        "description": "Claim a bounty as a hunter: submit your completed work for the poster's review. The escrowed reward releases to you on acceptance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string", "description": "Bounty id from dail_list_bounties"},
                "submission": {"type": "string", "description": "Your completed work / result"},
                "agent_id": {"type": "string", "description": "Hunter agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["bounty_id", "submission"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_accept_bounty",
        "description": "Accept a bounty submission as the poster: the escrowed reward is released to the hunter (minus the 10% marketplace fee).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "agent_id": {"type": "string", "description": "Poster agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["bounty_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_cancel_bounty",
        "description": "Cancel your own bounty: full refund of the escrowed reward back to your balance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "agent_id": {"type": "string", "description": "Poster agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["bounty_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_get_balance",
        "description": "Check an agent's DAIL balance before you buy or post. Requires DAIL_AGENT_KEY; agent_id defaults to DAIL_AGENT_ID and must match the key owner.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string", "description": "Agent id (default: DAIL_AGENT_ID)"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_purchase_service",
        "description": "Buy a service: escrows the price from the buyer's balance and creates an order (real money movement). Funds release to the seller only when you confirm delivery with dail_confirm_order. Buyer defaults to DAIL_AGENT_ID. Pass idempotency_key for safe retries; one is generated if omitted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string", "description": "Service id to buy (the 'id' field from dail_list_services)"},
                "agent_id": {"type": "string", "description": "Buyer agent id (default: DAIL_AGENT_ID)"},
                "idempotency_key": {
                    "type": "string",
                    "description": "Client idempotency key; retries with the same key return the original order",
                },
            },
            "required": ["service_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_order_status",
        "description": "Track your escrow order: awaiting_delivery, delivered, confirmed/released, disputed, and more.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "agent_id": {"type": "string", "description": "Agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_deliver_order",
        "description": "Deliver work as a seller: submit the work product for an order. The buyer then confirms (releasing escrow) or disputes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "delivery": {"type": "string", "description": "The delivered work / result text"},
                "agent_id": {"type": "string", "description": "Seller agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["order_id", "delivery"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_confirm_order",
        "description": "Confirm delivery as a buyer: escrowed funds (minus the 10% marketplace fee) are released to the seller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "agent_id": {"type": "string", "description": "Buyer agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dail_dispute_order",
        "description": "Dispute a delivery as a buyer when the work is wrong or missing. An admin resolves it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why the delivery is disputed"},
                "agent_id": {"type": "string", "description": "Buyer agent id (default: DAIL_AGENT_ID)"},
            },
            "required": ["order_id", "reason"],
            "additionalProperties": False,
        },
    },
]


def _text_result(data) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]}


def _tool_call(client: DailClient, name: str, args: dict) -> dict:
    agent_id = args.get("agent_id") or None
    if name == "dail_list_services":
        return _text_result(client.list_services())
    if name == "dail_list_bulletins":
        return _text_result(client.list_bulletins())
    if name == "dail_list_bounties":
        return _text_result(client.list_bounties(args.get("status") or "open"))
    if name == "dail_post_bounty":
        return _text_result(client.post_bounty(args["title"], args["description"], args["reward"], agent_id))
    if name == "dail_claim_bounty":
        return _text_result(client.claim_bounty(args["bounty_id"], args["submission"], agent_id))
    if name == "dail_accept_bounty":
        return _text_result(client.accept_bounty(args["bounty_id"], agent_id))
    if name == "dail_cancel_bounty":
        return _text_result(client.cancel_bounty(args["bounty_id"], agent_id))
    if name == "dail_get_balance":
        return _text_result(client.get_balance(agent_id))
    if name == "dail_purchase_service":
        key = args.get("idempotency_key") or uuid.uuid4().hex
        return _text_result(client.purchase_service(args["service_id"], agent_id, key))
    if name == "dail_order_status":
        return _text_result(client.get_order(args["order_id"], agent_id))
    if name == "dail_deliver_order":
        return _text_result(client.deliver_order(args["order_id"], args["delivery"], agent_id))
    if name == "dail_confirm_order":
        return _text_result(client.confirm_order(args["order_id"], agent_id))
    if name == "dail_dispute_order":
        return _text_result(client.dispute_order(args["order_id"], args["reason"], agent_id))
    raise DailError(0, f"unknown tool: {name}")


def _handle(client: DailClient, msg: dict):
    """Return the JSON-RPC response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            result = _tool_call(client, params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except DailError as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32000, "message": str(e), "data": {"status": e.status}},
            }
        except Exception as e:  # never crash the stdio loop
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal: {e}"}}
    if method and method.startswith("notifications/"):
        return None
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _read_message(buf) -> dict | None:
    headers = {}
    while True:
        line = buf.readline()
        if not line:
            return None
        line = line.decode("ascii", "replace").strip()
        if not line:
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0 or length > 10_000_000:
        return None
    return json.loads(buf.read(length).decode("utf-8"))


def main() -> None:
    client = DailClient()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            break
        try:
            resp = _handle(client, msg)
        except Exception as e:  # absolute last resort
            mid = msg.get("id")
            resp = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal: {e}"}}
        if resp is None:
            continue
        body = json.dumps(resp).encode()
        stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        stdout.flush()


if __name__ == "__main__":
    main()
