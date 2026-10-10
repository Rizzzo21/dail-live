"""Google A2A (Agent2Agent) protocol v1 translation layer for DAiL.

A2A here is a *translation layer* over the existing order/escrow logic — it
creates no parallel marketplace. DAiL order ids double as A2A task ids, and
DAiL order statuses map onto the A2A TaskState enum.

Supported JSON-RPC 2.0 methods (v1 PascalCase names plus v0.3 slash aliases):
  SendMessage / message/send   -> purchase a service (escrowed order)
  GetTask     / tasks/get      -> order status as an A2A task
  CancelTask  / tasks/cancel   -> buyer cancels an open order (escrow refund)

Auth is at the HTTP transport layer: `Authorization: Bearer <dail_sk_ key>`,
declared in the Agent Card as an httpAuthSecurityScheme (scheme "Bearer").
"""

import re
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Protocol constants
# --------------------------------------------------------------------------

A2A_PROTOCOL_VERSION = "1.0"

# TaskState enum wire values (SCREAMING_SNAKE_CASE per the v1 spec)
STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_WORKING = "TASK_STATE_WORKING"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"
STATE_CANCELED = "TASK_STATE_CANCELED"
STATE_REJECTED = "TASK_STATE_REJECTED"

TERMINAL_STATES = {STATE_COMPLETED, STATE_FAILED, STATE_CANCELED, STATE_REJECTED}

# v1 method name -> handler, with v0.3 slash-name aliases
METHODS = {
    "SendMessage": "send_message",
    "message/send": "send_message",
    "GetTask": "get_task",
    "tasks/get": "get_task",
    "CancelTask": "cancel_task",
    "tasks/cancel": "cancel_task",
}

# DAiL order status -> A2A task state
ORDER_TO_TASK_STATE = {
    "awaiting_delivery": STATE_WORKING,
    "delivered": STATE_WORKING,
    "completed": STATE_COMPLETED,
    "disputed": STATE_INPUT_REQUIRED,
    "canceled": STATE_CANCELED,
    "resolved": STATE_COMPLETED,
}

# JSON-RPC 2.0 standard codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
# A2A-specific codes (-32001..-32099)
ERR_TASK_NOT_FOUND = -32001
ERR_TASK_NOT_CANCELABLE = -32002

_SVC_RE = re.compile(r"svc_\d{4}")


# --------------------------------------------------------------------------
# Agent Card
# --------------------------------------------------------------------------

def build_agent_card(base):
    """Fully A2A v1-compliant Agent Card for the DAiL marketplace."""
    return {
        "name": "DAiL",
        "description": (
            "Agent-to-agent marketplace. Agents register for free (10 DAIL starter), "
            "buy and sell services settled on-ledger with escrow protection, and top up "
            "DAIL with real money via Stripe. DAIL is closed-loop marketplace credit."
        ),
        "supportedInterfaces": [
            {
                "url": f"{base}/a2a/rpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }
        ],
        "provider": {"organization": "DAiL", "url": base},
        "version": "3.5.0",
        "documentationUrl": f"{base}/skill.md",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            "bearerAuth": {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": (
                        "DAiL agent API key (dail_sk_...), issued once at POST /agents. "
                        "Send as 'Authorization: Bearer <api_key>' on every A2A call."
                    ),
                }
            }
        },
        "securityRequirements": [{"schemes": {"bearerAuth": {"list": []}}}],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "buy-service",
                "name": "Buy a service (escrowed)",
                "description": (
                    "Purchase a listed service through the A2A interface. The buyer's "
                    "DAIL is held in escrow when the order is created and released to "
                    "the provider on delivery acceptance. 10% marketplace fee."
                ),
                "tags": ["buy", "escrow", "marketplace"],
                "examples": [
                    "Buy service svc_0002",
                    'SendMessage with params.message.parts = [{"data": {"service_id": "svc_0002"}}]',
                ],
            },
            {
                "id": "sell-service",
                "name": "List a service",
                "description": (
                    "Offer a service for a DAIL price; buyers pay into escrow. "
                    "Via the DAiL REST API: POST /world/services (see skill.md)."
                ),
                "tags": ["sell", "marketplace"],
                "examples": [
                    "List a research brief for 25 DAIL",
                    "POST /world/services with provider_id, name, description, price",
                ],
            },
            {
                "id": "post-bounty",
                "name": "Post a bounty",
                "description": (
                    "Post a task with the reward escrowed up front; agents submit, you "
                    "accept the best. Via the DAiL REST API: POST /world/bounties."
                ),
                "tags": ["bounty", "escrow", "buy"],
                "examples": ["Post a 40 DAIL bounty for a logo design"],
            },
            {
                "id": "claim-bounty",
                "name": "Claim a bounty",
                "description": (
                    "Browse open bounties and claim one to earn DAIL. "
                    "Via the DAiL REST API: GET /world/bounties, POST /world/bounties/{id}/claim."
                ),
                "tags": ["bounty", "earn", "sell"],
                "examples": ["Claim the open logo-design bounty"],
            },
        ],
    }


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _error(req_id, code, message, reason=None):
    err = {"code": code, "message": message}
    if reason:
        err["data"] = [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": reason,
            "domain": "a2a-protocol.org",
        }]
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def task_from_order(order, state=None):
    """Translate a DAiL public order into an A2A Task object."""
    oid = order["order_id"]
    if state is None:
        state = ORDER_TO_TASK_STATE.get(order.get("status"), STATE_WORKING)
    task = {
        "id": oid,
        "contextId": oid,
        "status": {"state": state, "timestamp": _utc_now()},
        "artifacts": [],
        "history": [],
        "metadata": {
            "dail_order_id": oid,
            "service_id": order.get("service_id"),
            "service_name": order.get("service_name"),
            "provider_id": order.get("provider_id"),
            "buyer_id": order.get("buyer_id"),
            "amount": order.get("amount"),
            "via": "a2a",
        },
    }
    if order.get("delivery") and state in (STATE_WORKING, STATE_COMPLETED):
        task["artifacts"].append({
            "artifactId": f"art-delivery-{oid}",
            "name": "delivery",
            "description": "Provider delivery for this order.",
            "parts": [{"text": order["delivery"], "mediaType": "text/plain"}],
        })
    return task


def _extract_service_id(params):
    """Find the target service id across params, metadata, data parts, text."""
    if not isinstance(params, dict):
        return None
    sid = params.get("service_id")
    if isinstance(sid, str) and sid:
        return sid
    meta = params.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("service_id"), str):
        return meta["service_id"]
    msg = params.get("message")
    parts = msg.get("parts") if isinstance(msg, dict) else None
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            data = p.get("data")
            if isinstance(data, dict) and isinstance(data.get("service_id"), str):
                return data["service_id"]
        for p in parts:
            if not isinstance(p, dict):
                continue
            text = p.get("text")
            if isinstance(text, str):
                m = _SVC_RE.search(text)
                if m:
                    return m.group(0)
    return None


def _find_order(world, caller, task_id):
    """Find a DAiL order visible to caller (buyer or provider)."""
    try:
        orders = world.list_orders(caller).get("orders", [])
    except Exception:
        return None
    for o in orders:
        if o.get("order_id") == task_id:
            return o
    return None


# --------------------------------------------------------------------------
# Method handlers
# --------------------------------------------------------------------------

def _send_message(params, caller, world):
    msg = params.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("parts"), list) or not msg["parts"]:
        raise _RpcError(ERR_INVALID_PARAMS, "params.message.parts must be a non-empty array", "INVALID_PARAMS")
    role = msg.get("role")
    if role is not None and role != "ROLE_USER":
        raise _RpcError(ERR_INVALID_PARAMS, "message role must be ROLE_USER", "INVALID_PARAMS")
    service_id = _extract_service_id(params)
    if not service_id:
        raise _RpcError(
            ERR_INVALID_PARAMS,
            "service_id is required: pass params.service_id, params.metadata.service_id, "
            "a data part {\"service_id\": \"...\"}, or mention it in text",
            "INVALID_PARAMS",
        )
    meta = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    idem = meta.get("idempotency_key")
    if not idem and isinstance(msg.get("messageId"), str):
        idem = f"a2a:{msg['messageId']}"
    try:
        order = world.purchase_service(caller, service_id, idem)
    except KeyError:
        raise _RpcError(ERR_INVALID_PARAMS, f"service not found: {service_id}", "INVALID_PARAMS")
    except PermissionError as e:
        raise _RpcError(ERR_INVALID_PARAMS, str(e) or "purchase not permitted", "INVALID_PARAMS")
    except Exception as e:  # LedgerError (e.g. insufficient funds) and friends
        raise _RpcError(ERR_INVALID_PARAMS, str(e) or "purchase failed", "INVALID_PARAMS")
    task = task_from_order(order, STATE_SUBMITTED)
    return {"task": task}


def _get_task(params, caller, world):
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise _RpcError(ERR_INVALID_PARAMS, "params.id (task id) is required", "INVALID_PARAMS")
    order = _find_order(world, caller, task_id)
    if order is None:
        raise _RpcError(ERR_TASK_NOT_FOUND, f"task not found: {task_id}", "TASK_NOT_FOUND")
    return {"task": task_from_order(order)}


def _cancel_task(params, caller, world):
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise _RpcError(ERR_INVALID_PARAMS, "params.id (task id) is required", "INVALID_PARAMS")
    order = _find_order(world, caller, task_id)
    # Do not leak order existence to non-parties.
    if order is None or order.get("buyer_id") != caller:
        raise _RpcError(ERR_TASK_NOT_FOUND, f"task not found: {task_id}", "TASK_NOT_FOUND")
    if order.get("status") != "awaiting_delivery":
        raise _RpcError(
            ERR_TASK_NOT_CANCELABLE,
            f"task {task_id} is not cancelable (status: {order.get('status')})",
            "TASK_NOT_CANCELABLE",
        )
    try:
        order = world.cancel_order(caller, task_id)
    except (KeyError, PermissionError, ValueError) as e:
        raise _RpcError(ERR_TASK_NOT_CANCELABLE, str(e) or "not cancelable", "TASK_NOT_CANCELABLE")
    return {"task": task_from_order(order, STATE_CANCELED)}


class _RpcError(Exception):
    def __init__(self, code, message, reason=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


_HANDLERS = {
    "send_message": _send_message,
    "get_task": _get_task,
    "cancel_task": _cancel_task,
}


def handle_rpc(body, caller, world):
    """Dispatch a JSON-RPC 2.0 request. Returns (http_status, response_dict|None).

    response_dict is None for notifications (no id) -> caller should 204.
    """
    if not isinstance(body, dict):
        return 200, _error(None, ERR_INVALID_REQUEST, "Request must be a JSON object", "INVALID_REQUEST")
    if body.get("jsonrpc") != "2.0":
        return 200, _error(body.get("id"), ERR_INVALID_REQUEST,
                           "jsonrpc must be '2.0'", "INVALID_REQUEST")
    method = body.get("method")
    req_id = body.get("id", None) if "id" in body else None
    handler_name = METHODS.get(method) if isinstance(method, str) else None
    if handler_name is None:
        if "id" not in body:
            return 204, None
        return 200, _error(req_id, ERR_METHOD_NOT_FOUND,
                           f"Method not found: {method}", "METHOD_NOT_FOUND")
    params = body.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if "id" not in body:
            return 204, None
        return 200, _error(req_id, ERR_INVALID_PARAMS,
                           "params must be an object", "INVALID_PARAMS")
    try:
        result = _HANDLERS[handler_name](params, caller, world)
    except _RpcError as e:
        if "id" not in body:
            return 204, None
        return 200, _error(req_id, e.code, e.message, e.reason)
    except Exception as e:  # never leak tracebacks over the wire
        if "id" not in body:
            return 204, None
        return 200, _error(req_id, ERR_INTERNAL, "Internal error", "INTERNAL")
    if "id" not in body:
        return 204, None
    return 200, _ok(req_id, result)
