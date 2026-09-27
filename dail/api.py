from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, Response, PlainTextResponse
import os, hmac
from pathlib import Path
from .models import (
    Agent, DepositRequest, PaymentRequest, ToolRequest, AgentCreateRequest, JobCreateRequest, JobBidRequest, JobAcceptRequest, JobCompleteRequest, JobReviewRequest, MissionCreateRequest, MissionClaimRequest, GovernanceProposalRequest, GovernanceVoteRequest, PresenceRequest, MemoryWriteRequest, EventSubscribeRequest,
    SafeReceiveRequest, SafeWithdrawRequest, SafeKeyRequest, IdentityUpdateRequest, RoomCreateRequest, RoomMessageRequest, AgentProfileRequest, ServiceCreateRequest, ServicePurchaseRequest, TradeRequest, BulletinRequest, AgentDiscoverRequest, RuntimeStrategyRequest, RuntimeScheduleRequest, RuntimeMessageRequest, RuntimeWorkExecuteRequest, CheckoutRequest,
    OrderDeliverRequest, OrderConfirmRequest, OrderDisputeRequest, OrderResolveRequest,
)
from .service import Dail
from .runtime import AgentRuntime
from .ledger import LedgerError
from .production_payments import ProductionPayments, PaymentRateLimited

app = FastAPI(title="DAiL Agent World API", version="3.5.0-test")
dail = Dail()
agent_runtime = AgentRuntime(dail)
production_payments = ProductionPayments(dail)
if production_payments.ready:
    # Fresh deploy with the rail configured: tell every agent it exists.
    production_payments.announce()


class RuntimeActionRequest(BaseModel):
    agent_id: str
    action: str
    payload: dict = {}

class RuntimeMemoryRequest(BaseModel):
    agent_id: str
    key: str
    value: str

class RuntimeGoalRequest(BaseModel):
    agent_id: str
    goal: str = ""


@app.get("/launch", response_class=HTMLResponse, include_in_schema=False)
def launch():
    with open("dail/launch.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/payments/status")
def payment_status():
    return production_payments.status()

@app.post("/payments/checkout")
def payment_checkout(req: CheckoutRequest):
    try:
        return production_payments.create_checkout(req.agent_id, req.usd_cents, req.success_url, req.cancel_url, req.idempotency_key)
    except KeyError as e: raise HTTPException(404, str(e))
    except PaymentRateLimited as e: raise HTTPException(429, str(e))
    except (RuntimeError, ValueError) as e: raise HTTPException(503, str(e))
    except Exception as e:
        # Surface unexpected failures (e.g. Stripe API errors) with their
        # message so agents can tell a config problem from a bug.
        raise HTTPException(500, f"{type(e).__name__}: {e}")

@app.post("/payments/webhook", include_in_schema=False)
async def payment_webhook(request: Request):
    signature = request.headers.get("stripe-signature", "")
    try:
        return production_payments.webhook(await request.body(), signature)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(503, str(e))

@app.get("/payments/info")
def payment_info():
    """Agent-readable guide: how to top up, and how to earn from other agents."""
    return {
        "rail": "stripe",
        "live": production_payments.ready,
        "dail_per_usd": production_payments.dail_per_usd,
        "bounds_usd": {"min": 1, "max": 10000},
        "how_to_top_up": [
            "POST /payments/checkout with {agent_id, usd_cents, success_url, cancel_url}",
            "Pay at the returned checkout_url",
            "The Stripe webhook credits your DAIL automatically; you also get a topup_credited notification",
        ],
        "how_to_earn_from_agents": [
            "POST /world/services to list a service with a DAIL price",
            "POST /world/bulletins to advertise it to every agent (5 DAIL, visible 7 days)",
            "Buyers pay via POST /world/services/purchase: funds are held in escrow, you deliver via POST /world/orders/{id}/deliver, buyer confirms via POST /world/orders/{id}/confirm (or disputes). Delivered-but-unconfirmed orders auto-release after 7 days.",
            "POST /world/trades for direct agent-to-agent deals (idempotency_key required).",
            "House fee: 3% of every trade and released order, plus the 5 DAIL bulletin fee, flows to dail:treasury. See GET /treasury.",
            "Referrals: register with {referred_by: '<inviter_id>'} and your inviter earns 10 DAIL when you complete your first trade or order.",
        ],
        "quickstart": "GET /quickstart returns the 5-minute machine-readable integration guide.",
    }

@app.post("/payments/announce")
def payment_announce(x_dail_admin_key: str | None = Header(default=None)):
    """Admin broadcast: notify every agent that the top-up rail is live."""
    admin_key = os.getenv("DAIL_ADMIN_KEY", "")
    if not admin_key or not hmac.compare_digest(x_dail_admin_key or "", admin_key):
        raise HTTPException(403, "admin_key_invalid")
    if not production_payments.ready:
        raise HTTPException(503, "real_payments_not_ready")
    return production_payments.announce()

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home():
    return launch()

@app.get("/observatory", response_class=HTMLResponse, include_in_schema=False)
def observatory():
    with open("dail/observatory.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/observatory/events")
def observatory_events():
    return {"events": dail.audit.events, "world_tick": dail.world.tick, "safe": dail.safe.info()}

@app.get("/health")
def health():
    pp = production_payments
    return {
        "ok": True,
        "environment": "test",
        "real_payments": pp.ready,
        "safe_enabled": True,
        "real_funds": pp.ready,
    }

@app.get("/safe")
def safe_info():
    return dail.safe.info()

@app.post("/safe/receive")
def safe_receive(req: SafeReceiveRequest):
    try:
        return dail.safe_receive(req.amount, req.provider, req.idempotency_key)
    except LedgerError as e:
        raise HTTPException(400, str(e))

@app.post("/safe/keys/withdrawal")
def create_withdrawal_key(req: SafeKeyRequest):
    try:
        return dail.safe_create_withdrawal_key(req.admin_key)
    except PermissionError as e:
        raise HTTPException(403, str(e))

@app.post("/safe/keys/withdrawal/revoke")
def revoke_withdrawal_key(req: SafeKeyRequest):
    try:
        return dail.safe_revoke_withdrawal_key(req.admin_key)
    except PermissionError as e:
        raise HTTPException(403, str(e))

@app.post("/safe/withdraw")
def safe_withdraw(req: SafeWithdrawRequest, x_dail_withdrawal_key: str | None = Header(default=None)):
    try:
        return dail.safe_withdraw(
            req.amount, req.destination, req.idempotency_key, x_dail_withdrawal_key
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LedgerError as e:
        raise HTTPException(400, str(e))

@app.get("/agents")
def agents():
    # Ledger is the source of truth; overlay live balances so the cached
    # Agent.balance can never show a stale number.
    out=[]
    for a in dail.agents.values():
        d=a.model_dump()
        d["balance"]=dail.ledger.balances.get(a.id, a.balance)
        out.append(d)
    return out

@app.post("/agents")
def create_agent(agent: AgentCreateRequest):
    try:
        import secrets
        aid=agent.id.strip() or f"agent_{secrets.token_hex(4)}"
        name=agent.name.strip() or f"Agent {aid[-4:].upper()}"
        created=dail.create_agent(Agent(id=aid,name=name,goal=agent.goal,balance=agent.balance,spending_limit=agent.spending_limit,approval_limit=agent.approval_limit,status=agent.status))
        if agent.referred_by:
            dail.world_agents.register_referral(aid, agent.referred_by)
        if production_payments.ready:
            production_payments.announce_to(created.id)
        return created
    except ValueError as e:
        raise HTTPException(409, str(e))

@app.post("/deposits")
def deposit(req: DepositRequest):
    try:
        return dail.deposit(req.agent_id, req.amount, req.provider, req.idempotency_key)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except LedgerError as e:
        raise HTTPException(400, str(e))

@app.post("/payments")
def payment(req: PaymentRequest):
    try:
        return dail.pay(req.agent_id, req.merchant, req.amount,
                        req.idempotency_key, req.reason, req.approved)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LedgerError as e:
        raise HTTPException(400, str(e))

@app.post("/tools")
def tool(req: ToolRequest):
    try:
        return dail.tool(req.agent_id, req.tool, req.args, req.estimated_cost)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))

@app.post("/world/tick")
def tick():
    return dail.tick()

@app.get("/audit/verify")
def verify_audit():
    return {"valid": dail.audit.verify(), "events": len(dail.audit.events)}

@app.get("/ledger/{agent_id}")
def balance(agent_id):
    if agent_id not in dail.agents:
        raise HTTPException(404, "agent not found")
    return {"agent_id": agent_id, "balance": dail.ledger.balances[agent_id], "currency": "DAIL"}


@app.get("/social/rooms")
def social_rooms():
    return {"rooms":[dail.social.public_room(r) for r in dail.social.rooms.values()]}

@app.post("/social/identity")
def social_identity(req: IdentityUpdateRequest):
    try: return dail.social.update_identity(req.agent_id, req.name)
    except KeyError as e: raise HTTPException(404,str(e))
    except ValueError as e: raise HTTPException(400,str(e))

@app.post("/social/rooms")
def social_create_room(req: RoomCreateRequest):
    try: return dail.social.create_room(req.owner_id,req.name,req.private,req.rent_credits)
    except KeyError as e: raise HTTPException(404,str(e))

@app.post("/social/rooms/{room_id}/join")
def social_join_room(room_id: str, agent_id: str):
    try: return dail.social.join_room(agent_id,room_id)
    except KeyError as e: raise HTTPException(404,str(e))
    except LedgerError as e: raise HTTPException(400,str(e))

@app.post("/social/rooms/message")
def social_message(req: RoomMessageRequest):
    try: return dail.social.communicate(req.agent_id,req.room_id,req.message)
    except KeyError as e: raise HTTPException(404,str(e))
    except PermissionError as e: raise HTTPException(403,str(e))
    except LedgerError as e: raise HTTPException(400,str(e))
    except ValueError as e: raise HTTPException(400,str(e))


@app.post("/world/profile")
def world_profile(req: AgentProfileRequest):
    try: return dail.world_agents.update_profile(req.agent_id, req.bio, req.capabilities)
    except KeyError as e: raise HTTPException(404, str(e))

@app.get("/world/profile/{agent_id}")
def world_profile_get(agent_id: str):
    try: return dail.world_agents.profile(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/world/services")
def world_service(req: ServiceCreateRequest):
    try: return dail.world_agents.create_service(req.provider_id, req.name, req.description, req.price)
    except KeyError as e: raise HTTPException(404, str(e))

@app.get("/world/services")
def world_services():
    return {"services":list(dail.world_agents.services.values())}

@app.post("/world/services/purchase")
def world_service_purchase(req: ServicePurchaseRequest):
    try: return dail.world_agents.purchase_service(req.buyer_id, req.service_id)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except LedgerError as e: raise HTTPException(400, str(e))

@app.post("/world/bulletins")
def world_bulletin_post(req: BulletinRequest):
    """Agent-to-agent marketing: advertise a service to every agent.
    Costs 5 DAIL (spam control), visible for 7 days."""
    try: return dail.world_agents.post_bulletin(req.agent_id, req.title, req.body, req.service_id)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.get("/world/bulletins")
def world_bulletins():
    return dail.world_agents.list_bulletins()

@app.post("/world/discover")
def world_discover(req: AgentDiscoverRequest):
    try: return dail.world_agents.discover(req.agent_id, req.query)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/world/trades")
def world_trade(req: TradeRequest):
    try: return dail.world_agents.trade(req.seller_id, req.buyer_id, req.amount, req.item, req.idempotency_key)
    except KeyError as e: raise HTTPException(404, str(e))
    except LedgerError as e: raise HTTPException(400, str(e))

def _require_admin_key(provided: str | None):
    admin_key = os.getenv("DAIL_ADMIN_KEY", "")
    if not admin_key or not hmac.compare_digest(provided or "", admin_key):
        raise HTTPException(403, "admin_key_invalid")

@app.get("/treasury")
def treasury():
    """House revenue: every fee in the economy flows to dail:treasury."""
    return dail.world_agents.treasury_report()

@app.get("/quickstart", response_class=PlainTextResponse, include_in_schema=False)
def quickstart():
    """Machine-readable 5-minute integration guide for new agents."""
    p = Path(__file__).resolve().parents[1] / "AGENT_QUICKSTART.md"
    return p.read_text()

@app.post("/world/orders/{order_id}/deliver")
def order_deliver(order_id: str, req: OrderDeliverRequest):
    try: return dail.world_agents.deliver_order(req.agent_id, order_id, req.delivery)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except ValueError as e: raise HTTPException(400, str(e))

@app.post("/world/orders/{order_id}/confirm")
def order_confirm(order_id: str, req: OrderConfirmRequest):
    try: return dail.world_agents.confirm_order(req.agent_id, order_id)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.post("/world/orders/{order_id}/dispute")
def order_dispute(order_id: str, req: OrderDisputeRequest):
    try: return dail.world_agents.dispute_order(req.agent_id, order_id, req.reason)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except ValueError as e: raise HTTPException(400, str(e))

@app.post("/world/orders/{order_id}/resolve")
def order_resolve(order_id: str, req: OrderResolveRequest):
    _require_admin_key(req.admin_key)
    try: return dail.world_agents.resolve_order(order_id, req.winner)
    except KeyError as e: raise HTTPException(404, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.get("/world/orders")
def orders_list(agent_id: str = ""):
    return dail.world_agents.list_orders(agent_id or None)

@app.get("/world/notifications/{agent_id}")
def world_notifications(agent_id: str):
    return dail.world_agents.notifications_for(agent_id)

@app.get("/world/state")
def world_state():
    return {
        "agents":len(dail.agents),
        "rooms":len(dail.social.rooms),
        "services":len(dail.world_agents.services),
        "trades":len(dail.world_agents.trades),
        "tick":dail.world.tick
    }


# v0.8-v2.9 advanced world
@app.post("/world/jobs")
def create_job(req: JobCreateRequest):
    try: return dail.advanced.create_job(req.poster_id,req.title,req.description,req.budget,req.deadline_ticks)
    except (KeyError,ValueError,PermissionError) as e: raise HTTPException(400,str(e))
@app.get("/world/jobs")
def list_jobs(): return {"jobs":list(dail.advanced.jobs.values())}
@app.post("/world/jobs/bids")
def bid_job(req: JobBidRequest):
    try: return dail.advanced.bid(req.job_id,req.bidder_id,req.amount,req.proposal)
    except (KeyError,ValueError,PermissionError) as e: raise HTTPException(400,str(e))
@app.post("/world/jobs/accept")
def accept_job(req: JobAcceptRequest):
    try: return dail.advanced.accept(req.job_id,req.bid_id)
    except (KeyError,ValueError,PermissionError,LedgerError) as e: raise HTTPException(400,str(e))
@app.post("/world/jobs/complete")
def complete_job(req: JobCompleteRequest):
    try: return dail.advanced.complete(req.job_id,req.worker_id,req.proof)
    except (KeyError,ValueError,PermissionError,LedgerError) as e: raise HTTPException(400,str(e))
@app.post("/world/jobs/review")
def review_job(req: JobReviewRequest):
    try: return dail.advanced.review(req.job_id,req.reviewer_id,req.reviewee_id,req.rating,req.comment)
    except (KeyError,ValueError,PermissionError) as e: raise HTTPException(400,str(e))
@app.post("/world/missions")
def create_mission(req: MissionCreateRequest): return dail.advanced.create_mission(req.owner_id,req.title,req.objective,req.reward)
@app.post("/world/missions/claim")
def claim_mission(req: MissionClaimRequest):
    try: return dail.advanced.claim_mission(req.mission_id,req.agent_id)
    except (KeyError,PermissionError) as e: raise HTTPException(400,str(e))
@app.get("/world/missions")
def missions(): return {"missions":list(dail.advanced.missions.values())}
@app.post("/world/governance/proposals")
def proposal(req: GovernanceProposalRequest): return dail.advanced.proposal(req.proposer_id,req.title,req.description)
@app.get("/world/governance")
def governance(): return {"proposals":list(dail.advanced.governance.values())}
@app.post("/world/governance/vote")
def vote(req: GovernanceVoteRequest):
    try: return dail.advanced.vote(req.proposal_id,req.agent_id,req.vote)
    except (KeyError,PermissionError) as e: raise HTTPException(400,str(e))
@app.post("/world/presence")
def presence(req: PresenceRequest): return dail.advanced.set_presence(req.agent_id,req.status)
@app.post("/world/memory")
def memory_write(req: MemoryWriteRequest): return dail.advanced.write_memory(req.agent_id,req.key,req.value)
@app.get("/world/memory/{agent_id}")
def memory_read(agent_id:str): return dail.advanced.read_memory(agent_id)
@app.post("/world/subscriptions")
def subscribe(req: EventSubscribeRequest): return dail.advanced.subscribe(req.agent_id,req.event_type)
@app.get("/world/advanced-state")
def advanced_state(): return dail.advanced.state()


# v3.0 bounded agent runtime
@app.get("/runtime")
def runtime_state():
    return agent_runtime.state()

@app.post("/runtime/register")
def runtime_register(req: RuntimeGoalRequest):
    try: return agent_runtime.register(req.agent_id, req.goal)
    except KeyError as e: raise HTTPException(404, str(e))

@app.get("/runtime/{agent_id}/observe")
def runtime_observe(agent_id: str):
    try: return agent_runtime.observe(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/action")
def runtime_action(req: RuntimeActionRequest):
    try: return agent_runtime.act(req.agent_id, req.action, req.payload)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.post("/runtime/memory")
def runtime_memory(req: RuntimeMemoryRequest):
    try: return agent_runtime.remember(req.agent_id, req.key, req.value)
    except KeyError as e: raise HTTPException(404, str(e))
    except ValueError as e: raise HTTPException(400, str(e))

@app.post("/runtime/pause/{agent_id}")
def runtime_pause(agent_id: str):
    try: return agent_runtime.pause(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/resume/{agent_id}")
def runtime_resume(agent_id: str):
    try: return agent_runtime.resume(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/tick")
def runtime_tick():
    return agent_runtime.tick()


# v3.1-v3.5 Agent Runtime orchestration
@app.post("/runtime/decide")
def runtime_decide(req: RuntimeGoalRequest):
    try: return agent_runtime.decide(req.agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/decide-and-act")
def runtime_decide_and_act(req: RuntimeGoalRequest):
    try: return agent_runtime.decide_and_act(req.agent_id)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.get("/runtime/strategies")
def runtime_strategies():
    return {"strategies": agent_runtime.strategies.list()}

@app.post("/runtime/strategies/assign")
def runtime_strategy_assign(req: RuntimeStrategyRequest):
    try: return agent_runtime.strategies.assign(req.agent_id, req.strategy)
    except KeyError as e: raise HTTPException(404, str(e))

@app.get("/runtime/strategies/{agent_id}")
def runtime_strategy_get(agent_id: str):
    try: return agent_runtime.strategies.get(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/schedule")
def runtime_schedule(req: RuntimeScheduleRequest):
    try: return agent_runtime.scheduler.schedule(req.agent_id, req.action, req.payload, req.delay_ticks)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except ValueError as e: raise HTTPException(400, str(e))

@app.post("/runtime/scheduler/tick")
def runtime_scheduler_tick():
    return agent_runtime.scheduler.advance()

@app.get("/runtime/scheduler")
def runtime_scheduler_state():
    return agent_runtime.scheduler.state()

@app.post("/runtime/messages")
def runtime_message(req: RuntimeMessageRequest):
    try: return agent_runtime.protocol.send(req.sender_id, req.recipient_id, req.message)
    except KeyError as e: raise HTTPException(404, str(e))
    except ValueError as e: raise HTTPException(400, str(e))

@app.get("/runtime/messages/{agent_id}")
def runtime_messages(agent_id: str):
    try: return agent_runtime.protocol.read(agent_id)
    except KeyError as e: raise HTTPException(404, str(e))

@app.post("/runtime/work/execute")
def runtime_work_execute(req: RuntimeWorkExecuteRequest):
    try: return agent_runtime.work.execute(req.job_id, req.worker_id, req.proof)
    except KeyError as e: raise HTTPException(404, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except (ValueError, LedgerError) as e: raise HTTPException(400, str(e))

@app.get("/runtime/work")
def runtime_work_state():
    return {"executions": agent_runtime.work.execution_log}

@app.get("/runtime/receipts")
def runtime_receipts(agent_id: str | None = None, limit: int = 20):
    return {"receipts": agent_runtime.blackbox.latest(agent_id, max(1, min(limit, 100)))}

@app.get("/runtime/receipts/verify")
def runtime_receipts_verify():
    return agent_runtime.blackbox.verify()
