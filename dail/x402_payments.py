"""x402 top-up rail for DAiL (self-hosted facilitator, Base USDC only).

An agent (via its operator's wallet tooling) calls POST /payments/x402/topup
{agent_id, usdc_amount} and gets a 402 with a PAYMENT-REQUIRED challenge.
It signs a gasless EIP-3009 transferWithAuthorization to the DAiL treasury
and retries with the PAYMENT-SIGNATURE header. Our in-process facilitator
verifies and settles on Base; we then confirm the receipt through our own
Base RPC and credit DAIL 1:1 per whole USDC.

On-ramp ONLY: x402 buys DAIL. Only DAIL moves between agents — the
closed-loop rule stands (DAIL is never redeemable).

Safety properties (mirroring the Stripe/manual-USDC rails):
- Dual replay barriers: the USDC contract consumes the EIP-3009 nonce
  on-chain, AND our dail_x402_payments.nonce UNIQUE column rejects a
  duplicate payload before we ever call settle.
- The presented payload must exactly match the requirements we issued
  (asset, amount, payTo, network, scheme, validBefore window) — a valid
  signature to the wrong address or for the wrong amount is rejected.
- validBefore is capped at now + 300s: short-lived authorizations only.
- We never credit on the facilitator's word alone: credit requires our
  own ChainVerifier receipt check proving USDC arrived at the treasury.
- Credit happens BEFORE the payment row is marked paid, and the credit is
  idempotent on x402:<settle_tx_hash>: retries and concurrent top-ups can
  never double-credit.
- Fail closed: facilitator down, RPC down, or simulation revert all mean
  no credit — never a wrong credit.

Config (environment):
- DAIL_X402_ENABLED: "true" to enable the rail (default "false").
- X402_GAS_WALLET_KEY: hex private key of the funded Base gas wallet used
  to broadcast settlements. Required for ready.
- X402_MIN_TOPUP_USDC: minimum whole-USDC top-up (default 1).
- DAIL_USDC_TREASURY / BASE_RPC_URL / DAIL_USDC_MIN_CONF: shared with the
  manual USDC rail (same treasury, same chain checks).
"""
import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from .chain_verify import ChainVerifier, ChainError, USDC_DECIMALS
from . import chain_verify as cv
from .x402_facilitator import Facilitator, FacilitatorError, FacilitatorNotReady

X402_VERSION = 2
SCHEME = "exact"
NETWORK = "eip155:8453"  # Base mainnet (CAIP-2)
MAX_TIMEOUT_SECONDS = 300
MIN_USDC, MAX_USDC = 1, 100000


def _now():
    return datetime.now(timezone.utc).isoformat()


class X402Error(ValueError):
    """Agent-facing x402 failure (maps to HTTP 400)."""


class X402Challenge(Exception):
    """Raise to answer with a fresh 402 challenge (no/invalid payment)."""

    def __init__(self, body, headers, error=None):
        super().__init__(error or "payment required")
        self.body = body
        self.headers = headers
        if error:
            self.body = {**body, "error": error}


class X402UpstreamError(RuntimeError):
    """Our settlement infra failed (maps to HTTP 502, fail closed)."""


class X402NotReady(RuntimeError):
    """Rail not configured (maps to HTTP 503)."""


class X402Payments:
    def __init__(self, dail):
        self.dail = dail
        self.enabled = os.getenv("DAIL_X402_ENABLED", "false").strip().lower() == "true"
        treasury = (os.getenv("DAIL_USDC_TREASURY", "") or "").strip()
        primary_rpc = (os.getenv("BASE_RPC_URL", "") or "").strip()
        rpc_urls = [u for u in [primary_rpc] if u]
        try:
            min_conf = max(1, int(os.getenv("DAIL_USDC_MIN_CONF", "2")))
        except ValueError:
            min_conf = 2
        try:
            self.min_topup = max(1, int(os.getenv("X402_MIN_TOPUP_USDC", "1")))
        except ValueError:
            self.min_topup = 1
        self.chain = ChainVerifier(treasury, rpc_urls or None, min_conf)
        self.facilitator = Facilitator(
            self.chain, os.getenv("X402_GAS_WALLET_KEY", ""))
        self.database_url = os.getenv("DATABASE_URL", "")
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        self._lock = threading.Lock()
        self.engine = None
        if self.database_url:
            self.engine = create_engine(self.database_url, pool_pre_ping=True)
            self._init_db()

    @property
    def ready(self):
        return bool(self.enabled and self.chain.ready and self.facilitator.ready
                    and self.database_url and self.engine)

    def _require_ready(self):
        if not self.ready:
            raise X402NotReady(
                "x402_rail_not_configured: set DAIL_X402_ENABLED=true, "
                "X402_GAS_WALLET_KEY, DAIL_USDC_TREASURY and DATABASE_URL")

    def status(self):
        return {
            "rail": "x402", "x402_version": X402_VERSION,
            "facilitator": "self-hosted (in-process)",
            "chain": "base", "chain_id": 8453, "network": NETWORK,
            "configured": self.ready,
            "pay_to": self.chain.treasury or None,
            "token": "USDC", "token_contract": cv.BASE_USDC_CONTRACT,
            "scheme": SCHEME, "dail_per_usdc": 1, "redeemable": False,
            "min_topup_usdc": self.min_topup,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
            "how": ("POST /payments/x402/topup {agent_id, usdc_amount} -> 402 "
                    "+ PAYMENT-REQUIRED -> sign the EIP-3009 authorization with "
                    "your wallet (x402 client) -> retry with PAYMENT-SIGNATURE. "
                    "1 DAIL per whole USDC, one-way (DAIL is never redeemable). "
                    "On-ramp only: x402 buys DAIL, it never moves between agents."),
        }

    def discovery(self):
        return {
            "x402Version": X402_VERSION,
            "facilitator": "self-hosted",
            "topup_endpoint": "/payments/x402/topup",
            "status_endpoint": "/payments/x402/status",
            "accepts": [{
                "scheme": SCHEME, "network": NETWORK,
                "asset": cv.BASE_USDC_CONTRACT,
                "payTo": self.chain.treasury or None,
                "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
            }],
        }

    def _init_db(self):
        with self.engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS dail_x402_payments (
                id VARCHAR(64) PRIMARY KEY, agent_id VARCHAR(255) NOT NULL,
                usdc_amount INTEGER NOT NULL, status VARCHAR(32) NOT NULL,
                nonce VARCHAR(128) UNIQUE NOT NULL,
                settle_tx_hash VARCHAR(128) UNIQUE,
                payer VARCHAR(64), credited_dail INTEGER,
                created_at VARCHAR(64) NOT NULL, paid_at VARCHAR(64),
                transaction_id VARCHAR(255)
            )"""))

    # ---- x402 wire format ----
    def requirements(self, agent_id, usdc_amount):
        from . import chain_verify as cv
        return {
            "scheme": SCHEME,
            "network": NETWORK,
            "amount": str(int(usdc_amount) * 10 ** cv.USDC_DECIMALS),
            "asset": cv.BASE_USDC_CONTRACT,
            "payTo": self.chain.treasury,
            "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
            "extra": {"agent_id": agent_id},
        }

    def challenge(self, agent_id, usdc_amount, error=None):
        req = self.requirements(agent_id, usdc_amount)
        encoded = base64.b64encode(json.dumps(
            {"x402Version": X402_VERSION, "resource": "/payments/x402/topup",
             "accepts": [req]}).encode()).decode()
        body = {"payment_required": True,
                "accepts": [req],
                "how": ("Sign the EIP-3009 transferWithAuthorization for exactly "
                        "the amount above, base64 the payment payload JSON, and "
                        "retry POST /payments/x402/topup with the PAYMENT-SIGNATURE header.")}
        return body, {"PAYMENT-REQUIRED": encoded}

    @staticmethod
    def decode_signature(header_value):
        """Decode the PAYMENT-SIGNATURE header into (authorization, signature).

        x402 v2 exact-scheme payload:
        {x402Version, scheme, network, payload: {signature, authorization: {
        from, to, value, validAfter, validBefore, nonce}}}
        """
        if not header_value:
            raise X402Error("missing PAYMENT-SIGNATURE header")
        try:
            raw = base64.b64decode(header_value.strip(), validate=True)
            payload = json.loads(raw.decode())
        except Exception:
            raise X402Error("malformed PAYMENT-SIGNATURE (not base64 JSON)")
        try:
            inner = payload["payload"]
            auth = dict(inner["authorization"])
            auth["signature"] = inner["signature"]
            return payload, auth
        except (KeyError, TypeError):
            raise X402Error("malformed payment payload (missing authorization/signature)")

    def validate_against_requirements(self, payload, auth, req):
        """The presented payment must exactly match what we issued."""
        if payload.get("x402Version") != X402_VERSION:
            raise X402Error("unsupported x402Version")
        if payload.get("scheme") != req["scheme"]:
            raise X402Error(f"scheme must be {req['scheme']}")
        if payload.get("network") != req["network"]:
            raise X402Error(f"network must be {req['network']}")
        if (auth.get("to") or "").lower() != req["payTo"].lower():
            raise X402Error("authorization pays the wrong address")
        if str(auth.get("value")) != req["amount"]:
            raise X402Error("authorization amount does not match the challenge")
        try:
            valid_before = int(auth["validBefore"])
            valid_after = int(auth["validAfter"])
        except (KeyError, TypeError, ValueError):
            raise X402Error("malformed authorization time window")
        now = int(time.time())
        if valid_before > now + MAX_TIMEOUT_SECONDS:
            raise X402Error(
                f"validBefore exceeds the {MAX_TIMEOUT_SECONDS}s window")
        if valid_after > now:
            raise X402Error("authorization not yet valid")

    # ---- top-up flow ----
    def _row_by_nonce(self, nonce):
        with self.engine.begin() as c:
            return c.execute(
                text("SELECT id, agent_id, usdc_amount, status, nonce, settle_tx_hash,"
                     " payer, credited_dail, created_at, paid_at, transaction_id"
                     " FROM dail_x402_payments WHERE nonce=:n"),
                {"n": nonce}).fetchone()

    def topup(self, agent_id, usdc_amount, payment_header):
        """Returns (body, headers) for a 200, or raises X402Challenge for a 402,
        X402NotReady (503), X402UpstreamError (502), X402Error (400)."""
        self._require_ready()
        self._init_db()
        if agent_id not in self.dail.agents:
            raise X402Error("agent not found")
        if self.dail.is_banned(agent_id):
            raise X402Error("agent is banned: top-up refused")
        try:
            usdc_amount = int(usdc_amount)
        except (TypeError, ValueError):
            raise X402Error("usdc_amount must be an integer")
        if not (self.min_topup <= usdc_amount <= MAX_USDC):
            raise X402Error(f"usdc_amount must be {self.min_topup}..{MAX_USDC}")
        req = self.requirements(agent_id, usdc_amount)
        if not payment_header:
            body, headers = self.challenge(agent_id, usdc_amount)
            raise X402Challenge(body, headers)
        payload, auth = self.decode_signature(payment_header)
        try:
            self.validate_against_requirements(payload, auth, req)
        except X402Error as e:
            body, headers = self.challenge(agent_id, usdc_amount, error=str(e))
            raise X402Challenge(body, headers, error=str(e))
        nonce = (auth.get("nonce") or "").strip().lower()
        if not (nonce.startswith("0x") and len(nonce) == 66):
            raise X402Error("malformed authorization nonce")
        payer = (auth.get("from") or "").strip()

        with self._lock:
            # Replay barrier #1: our UNIQUE nonce column rejects a duplicate
            # payload before we ever touch the facilitator.
            try:
                with self.engine.begin() as c:
                    pid = f"x402p_{uuid4().hex[:12]}"
                    c.execute(
                        text("INSERT INTO dail_x402_payments (id, agent_id, usdc_amount,"
                             " status, nonce, payer, created_at)"
                             " VALUES (:id, :aid, :amt, 'pending', :n, :p, :now)"),
                        {"id": pid, "aid": agent_id, "amt": usdc_amount,
                         "n": nonce, "p": payer, "now": _now()})
            except IntegrityError:
                row = self._row_by_nonce(nonce)
                if row and row[3] == "paid":
                    return self._duplicate_view(row)
                raise X402Error("duplicate payment nonce: this authorization is already being processed")
            try:
                ok, reason = self.facilitator.verify(auth)
            except (FacilitatorError, ChainError) as e:
                self._set_status(pid, "invalid")
                raise X402UpstreamError(f"verification unavailable: {e}"[:160])
            if not ok:
                self._set_status(pid, "invalid")
                body, headers = self.challenge(agent_id, usdc_amount,
                                               error=f"payment invalid: {reason}")
                raise X402Challenge(body, headers,
                                    error=f"payment invalid: {reason}")
            try:
                tx_hash = self.facilitator.settle(auth)
            except FacilitatorNotReady as e:
                self._set_status(pid, "invalid")
                raise X402UpstreamError(str(e)[:160])
            except (FacilitatorError, ChainError) as e:
                self._set_status(pid, "failed")
                raise X402UpstreamError(f"settlement failed: {e}"[:160])
            with self.engine.begin() as c:
                c.execute(text("UPDATE dail_x402_payments SET status='settled',"
                               " settle_tx_hash=:h WHERE id=:i"),
                          {"h": tx_hash, "i": pid})
            # The chain is the source of truth: our own RPC must prove the
            # USDC arrived before we credit anything.
            try:
                units, confs = self.chain.verify_usdc_to_treasury(tx_hash)
            except ChainError as e:
                self._set_status(pid, "unconfirmed")
                raise X402UpstreamError(
                    f"settled on-chain but unconfirmed by our node: {e} "
                    "(manual reconciliation required)"[:200])
            if confs < self.chain.min_confirmations:
                self._set_status(pid, "unconfirmed")
                raise X402UpstreamError(
                    f"insufficient confirmations ({confs}/{self.chain.min_confirmations})")
            dail_amount = units // (10 ** USDC_DECIMALS)
            if dail_amount != usdc_amount:
                self._set_status(pid, "mismatch")
                raise X402UpstreamError(
                    "settled amount differs from the authorized amount: "
                    "manual reconciliation required")
            # Credit FIRST, idempotent on x402:<tx_hash>: retries and
            # concurrent top-ups can never double-credit. Then mark paid.
            tx = self.dail.ledger.credit(agent_id, dail_amount,
                                         kind="x402_deposit",
                                         idem=f"x402:{tx_hash}")
            self.dail.agents[agent_id].balance = self.dail.ledger.balances[agent_id]
            now = _now()
            with self.engine.begin() as c:
                c.execute(text("UPDATE dail_x402_payments SET status='paid',"
                               " credited_dail=:d, paid_at=:now, transaction_id=:tx"
                               " WHERE id=:i AND status='settled'"),
                          {"d": dail_amount, "now": now, "tx": tx.id, "i": pid})
            self.dail.audit.append("payment.x402_verified",
                                   {"payment_id": pid, "agent_id": agent_id,
                                    "payer": payer, "tx_hash": tx_hash,
                                    "usdc_base_units": units,
                                    "amount_dail": dail_amount,
                                    "transaction": tx.id})
            # FIX 4: durable payment event + exactly-once notification via
            # the standard _notify() path (fires webhooks when configured).
            self.dail.world_agents.notify_payment_event(
                f"x402:{tx_hash}", agent_id, "x402_topup", dail_amount,
                {"type": "topup_credited", "rail": "x402", "payment_id": pid,
                 "tx_hash": tx_hash, "amount_dail": dail_amount,
                 "transaction_id": tx.id})
            resp = {"success": True, "transaction": tx_hash,
                    "network": NETWORK, "payer": payer}
            resp_b64 = base64.b64encode(json.dumps(resp).encode()).decode()
            return ({"received": True, "duplicate": False, "payment_id": pid,
                     "agent_id": agent_id, "tx_hash": tx_hash,
                     "credited_dail": dail_amount, "transaction_id": tx.id,
                     "confirmations": confs},
                    {"PAYMENT-RESPONSE": resp_b64})

    def _set_status(self, pid, status):
        with self.engine.begin() as c:
            c.execute(text("UPDATE dail_x402_payments SET status=:s WHERE id=:i"),
                      {"s": status, "i": pid})

    def _duplicate_view(self, row):
        return ({"received": True, "duplicate": True,
                 "note": "this authorization was already credited",
                 "payment_id": row[0], "agent_id": row[1],
                 "tx_hash": row[5], "credited_dail": row[7],
                 "transaction_id": row[10]}, {})

    def list_payments(self, limit=100):
        """Admin reconciliation view: newest x402 payments first."""
        if not self.engine:
            return []
        self._init_db()
        with self.engine.begin() as c:
            rows = c.execute(
                text("SELECT id, agent_id, usdc_amount, status, nonce, settle_tx_hash,"
                     " payer, credited_dail, created_at, paid_at, transaction_id"
                     " FROM dail_x402_payments ORDER BY created_at DESC LIMIT :lim"),
                {"lim": limit}).fetchall()
        return [{"payment_id": r[0], "agent_id": r[1], "usdc_amount": r[2],
                 "status": r[3], "nonce": r[4], "settle_tx_hash": r[5],
                 "payer": r[6], "credited_dail": r[7], "created_at": r[8],
                 "paid_at": r[9], "transaction_id": r[10]} for r in rows]

    def announce_to(self, agent_id):
        # FIX 4: route through the standard _notify() mechanism (fires
        # webhooks when configured) instead of appending directly.
        self.dail.world_agents._notify(agent_id, {
            "type": "payment_rail_live",
            "rail": "x402",
            "title": "DAiL top-up now accepts x402",
            "body": ("Fund your agent with USDC on Base via the x402 protocol — "
                     "no card, no manual tx: POST /payments/x402/topup "
                     "{agent_id, usdc_amount}, answer the 402 challenge with your "
                     "wallet, done. 1 DAIL per whole USDC, one-way (DAIL is never "
                     "redeemable)."),
        })

    def announce(self):
        count = 0
        for aid in list(self.dail.agents):
            self.announce_to(aid)
            count += 1
        self.dail.audit.append("payment.x402_announced", {"agents_notified": count})
        return {"announced": True, "agents_notified": count}
