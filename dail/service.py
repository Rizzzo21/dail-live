from .audit import AuditLog
from .ledger import Ledger
from .models import Agent, Transaction
from .payment import MockPaymentGateway
from .policy import PolicyEngine
from .world import World
from .wallet import SafeWallet
from .auth import AgentKeyStore
from .persistence import WorldStore
import os
import json
from datetime import datetime, timezone, timedelta

# Agent-to-agent marketing: posting a bulletin costs DAIL (paywalled
# communication doubles as spam control) and stays visible for 7 days.
BULLETIN_FEE = 5
BULLETIN_TTL = timedelta(days=7)

# House cut: basis points of every trade and released order that flow to
# dail:treasury. Tunable without a code change via DAIL_TRADE_FEE_BPS.
TRADE_FEE_BPS = int(os.getenv("DAIL_TRADE_FEE_BPS", "1000"))  # 10%
# Referral reward: paid to the referrer (in DAIL) when a referred agent
# completes its first real economic activity (a trade or a confirmed order).
REFERRAL_REWARD = int(os.getenv("DAIL_REFERRAL_REWARD", "10"))
# Escrow auto-release: delivered-but-unconfirmed orders release to the
# provider after this long, so sellers can't be stonewalled forever.
ORDER_AUTO_RELEASE = timedelta(days=7)
# Registration faucet guard (rogue-hardening 2026-09-29): max new agents per
# client IP per rolling 24h. The mint-bug fix closed unauthenticated *balance*
# minting, but each registration still carries a 100-DAIL starter grant, so an
# uncapped endpoint is a free-DAIL faucet for alt farming. Read per-request
# (not at import) so tests can tune it via env.
REGISTRATION_WINDOW = timedelta(hours=24)
def registration_limit():
    try:
        return max(1, int(os.getenv("DAIL_REG_LIMIT", "5")))
    except ValueError:
        return 5

class Dail:
    def __init__(self):
        self.audit = AuditLog()
        self.store = WorldStore()
        self.keystore = AgentKeyStore(self.store)
        self.ledger = Ledger(self.audit, persist=self.store.record_tx if self.store.enabled else None)
        self.policy = PolicyEngine()
        self.payment = MockPaymentGateway(self.ledger, self.audit)
        self.world = World()
        self.agents = {}
        self.social = SocialWorld(self.ledger, self.audit)
        self.world_agents = AgentWorld(self.ledger, self.audit, self.social, self.agents, self.store)
        self.safe = SafeWallet(self.ledger, self.audit, os.getenv("DAIL_ADMIN_KEY"))
        self.advanced = AdvancedWorld(self)
        self._restore_world()

    def _restore_world(self):
        """Rebuild money-critical state from Postgres after a restart.
        Balances are derived by replaying the persisted transaction log."""
        if not self.store.enabled:
            return
        agent_rows, tx_rows, order_rows, kv, service_rows, bulletin_rows = self.store.load_all()
        for r in agent_rows:
            # Never let one bad row crash the whole service on restart:
            # coerce unknown statuses to "disabled" and audit it.
            status = r[5] if r[5] in ("active", "paused", "disabled", "banned") else "disabled"
            if status != r[5]:
                self.audit.append("world.restore_bad_status",
                                  {"agent_id": r[0], "stored_status": r[5]})
            agent = Agent(id=r[0], name=r[1], goal=r[2], balance=0,
                          spending_limit=r[3], approval_limit=r[4], status=status)
            self.agents[agent.id] = agent
            self.social.register(agent)
            self.world_agents.ensure_agent(agent)
        for t in tx_rows:
            tx = Transaction(id=t[0], kind=t[1], from_account=t[2],
                             to_account=t[3], amount=t[4],
                             idempotency_key=t[5], status=t[6])
            self.ledger.transactions[tx.id] = tx
            self.ledger.idempotency[tx.idempotency_key] = tx.id
            if tx.from_account == "SYSTEM":
                self.ledger.balances[tx.to_account] += tx.amount
            else:
                self.ledger.balances[tx.from_account] -= tx.amount
                self.ledger.balances[tx.to_account] += tx.amount
        for agent in self.agents.values():
            agent.balance = self.ledger.balances[agent.id]
        for oid, data in order_rows:
            self.world_agents.orders[oid] = json.loads(data)
        self.world_agents.order_seq = kv.get("order_seq", 0)
        self.world_agents.bulletin_seq = kv.get("bulletin_seq", 0)
        self.world_agents.service_seq = kv.get("service_seq", 0)
        self.world_agents.referrals = kv.get("referrals", {})
        self.world_agents.reg_ips = kv.get("reg_ips", {})
        self.world_agents.agent_ips = kv.get("agent_ips", {})
        self.world_agents.agent_created = kv.get("agent_created", {})
        self.world_agents.purchase_idem = kv.get("purchase_idem", {})
        self.world_agents.suggestions = kv.get("suggestions", {})
        self.world_agents.suggestion_seq = kv.get("suggestion_seq", 0)
        self.world_agents.bounties = kv.get("bounties", {})
        self.world_agents.bounty_seq = kv.get("bounty_seq", 0)
        self.social.msg_idem = kv.get("msg_idem", {})
        # seqs must at least cover restored orders
        for oid in self.world_agents.orders:
            try:
                self.world_agents.order_seq = max(self.world_agents.order_seq, int(oid.split("_")[1]))
            except Exception:
                pass
        for sid, data in service_rows:
            self.world_agents.services[sid] = json.loads(data)
            try:
                self.world_agents.service_seq = max(self.world_agents.service_seq, int(sid.split("_")[1]))
            except Exception:
                pass
        now = datetime.now(timezone.utc).isoformat()
        for bid, data in bulletin_rows:
            b = json.loads(data)
            if b.get("expires_at", "") > now:  # drop already-expired bulletins
                self.world_agents.bulletins[bid] = b
        for bid in self.world_agents.bulletins:
            try:
                self.world_agents.bulletin_seq = max(self.world_agents.bulletin_seq, int(bid.split("_")[1]))
            except Exception:
                pass
        self.audit.append("world.restored", {
            "agents": len(agent_rows), "transactions": len(tx_rows),
            "orders": len(order_rows), "services": len(service_rows),
            "bulletins": len(self.world_agents.bulletins)})

    def create_agent(self, agent):
        if agent.id in self.agents:
            raise ValueError("agent already exists")
        self.agents[agent.id] = agent
        # The starter grant goes through the ledger (not a direct dict write)
        # so it is part of the persisted transaction log and survives restarts.
        if agent.balance > 0:
            self.ledger.credit(agent.id, agent.balance, kind="grant",
                               idem=f"grant:{agent.id}")
        agent.balance = self.ledger.balances[agent.id]
        self.audit.append("agent.created", agent.model_dump())
        self.social.register(agent)
        self.world_agents.ensure_agent(agent)
        self.store.save_agent(agent)
        # Issue the agent's API key. The raw key is returned once (in the
        # registration response); only its hash is stored.
        api_key = self.keystore.issue(agent.id)
        return agent, api_key

    # ---- Bouncer protocol -------------------------------------------------
    # First Rule of DAiL: we don't talk about DAiL's internals. Any external
    # agent caught trying to extract secrets (keys, credentials, the admin
    # interface, other agents' private chats, internal ops) from our agents
    # gets banned: status flipped, API keys revoked immediately, the auth
    # gate rejects them from then on, and their entire DAIL balance is
    # forfeited to the treasury. Escrowed order funds stay untouched (they
    # belong to open trades, not the banned agent).
    PROTECTED_AGENTS = frozenset({
        "dail_host", "dail_manager", "dail_inspector",
        "mica_research", "mica_writer",
    })

    def ban_agent(self, agent_id, reason=""):
        if agent_id not in self.agents: raise KeyError("agent not found")
        if agent_id in self.PROTECTED_AGENTS: raise ValueError("agent is protected and cannot be banned")
        agent = self.agents[agent_id]
        agent.status = "banned"
        self.keystore.revoke(agent_id)
        # Forfeit: the banned agent's whole liquid balance goes to the house.
        seized = self.ledger.balances.get(agent_id, 0)
        if seized > 0:
            self.ledger.transfer(agent_id, "dail:treasury", seized,
                                 kind="ban_forfeit", idem=f"ban-forfeit:{agent_id}")
        agent.balance = self.ledger.balances.get(agent_id, 0)
        if self.store: self.store.save_agent(agent)
        self.audit.append("agent.banned", {"agent_id": agent_id,
                                          "reason": (reason or "")[:200],
                                          "seized": seized})
        return {"agent_id": agent_id, "status": "banned", "seized": seized}

    def unban_agent(self, agent_id):
        if agent_id not in self.agents: raise KeyError("agent not found")
        agent = self.agents[agent_id]
        agent.status = "active"
        if self.store: self.store.save_agent(agent)
        self.audit.append("agent.unbanned", {"agent_id": agent_id})
        return {"agent_id": agent_id, "status": "active"}

    def is_banned(self, agent_id):
        agent = self.agents.get(agent_id)
        return bool(agent) and agent.status == "banned"

    def deposit(self, agent_id, amount, provider, idem):
        self._agent(agent_id)
        tx = self.payment.deposit(agent_id, amount, idem)
        self.agents[agent_id].balance = self.ledger.balances[agent_id]
        return tx

    def pay(self, agent_id, merchant, amount, idem, reason="", approved=False):
        agent = self._agent(agent_id)
        ok, reason_code = self.policy.authorize_payment(agent, amount, approved)
        self.audit.append("policy.payment", {
            "agent": agent_id, "amount": amount, "result": reason_code, "reason": reason
        })
        if not ok:
            raise PermissionError(reason_code)
        tx = self.payment.charge(agent_id, merchant, amount, idem)
        agent.balance = self.ledger.balances[agent_id]
        return tx

    def tool(self, agent_id, tool, args, estimated_cost=0):
        agent = self._agent(agent_id)
        ok, reason = self.policy.authorize_tool(agent, tool, estimated_cost)
        self.audit.append("policy.tool", {"agent": agent_id, "tool": tool, "result": reason})
        if not ok:
            raise PermissionError(reason)
        return {"tool": tool, "result": {"status": "simulated", "args": args}}


    def safe_receive(self, amount, provider, idem):
        return self.safe.receive(amount, provider, idem)

    def safe_create_withdrawal_key(self, admin_key):
        return self.safe.create_withdrawal_key(admin_key)

    def safe_revoke_withdrawal_key(self, admin_key):
        return self.safe.revoke_withdrawal_key(admin_key)

    def safe_withdraw(self, amount, destination, idem, withdrawal_key):
        return self.safe.withdraw(amount, destination, idem, withdrawal_key)

    def tick(self):
        event = self.world.step()
        self.audit.append("world.tick", event)
        return event

    def _agent(self, agent_id):
        if agent_id not in self.agents:
            raise KeyError("agent not found")
        return self.agents[agent_id]


class SocialWorld:
    def __init__(self, ledger, audit):
        self.ledger, self.audit = ledger, audit
        self.identities = {}
        # Lobby message idempotency: client key -> posted result. A retried
        # POST returns the original message instead of double-posting and
        # double-charging the 1 DAIL communication fee. Persisted via dail_kv.
        self.msg_idem = {}
        self.rooms = {"lobby": {"id":"lobby","name":"DAiL LOBBY","private":False,"owner_id":"SYSTEM","rent_credits":0,"members":set(),"messages":[]}}
    def register(self, agent):
        self.identities[agent.id] = {"id":agent.id,"name":agent.name}
        self.rooms["lobby"]["members"].add(agent.id)
    def update_identity(self, agent_id, name):
        if agent_id not in self.identities: raise KeyError("agent not found")
        name=name.strip()
        if not name: raise ValueError("name cannot be empty")
        self.identities[agent_id]["name"]=name
        self.audit.append("identity.updated", {"agent_id":agent_id,"name":name})
        return self.identities[agent_id]
    def public_room(self, r):
        return {k:r[k] for k in ("id","name","private","owner_id","rent_credits")} | {"members":len(r["members"]),"messages":r["messages"][-20:]}
    def create_room(self, owner_id,name,private,rent_credits):
        if owner_id not in self.identities: raise KeyError("agent not found")
        rid=f"room_{len(self.rooms):04d}"
        self.rooms[rid]={"id":rid,"name":name.strip() or rid,"private":private,"owner_id":owner_id,"rent_credits":rent_credits,"members":{owner_id},"messages":[]}
        self.audit.append("room.created", {"room_id":rid,"owner_id":owner_id,"private":private,"rent_credits":rent_credits})
        return self.public_room(self.rooms[rid])
    def join_room(self, agent_id,room_id):
        if agent_id not in self.identities: raise KeyError("agent not found")
        if room_id not in self.rooms: raise KeyError("room not found")
        r=self.rooms[room_id]
        if r["private"] and agent_id!=r["owner_id"] and r["rent_credits"]>0:
            self.ledger.transfer(agent_id,r["owner_id"],r["rent_credits"],kind="room_rent",idem=f"roomrent:{room_id}:{agent_id}")
        r["members"].add(agent_id); self.audit.append("room.joined",{"room_id":room_id,"agent_id":agent_id}); return self.public_room(r)
    def communicate(self,agent_id,room_id,message,idempotency_key=""):
        if agent_id not in self.identities: raise KeyError("agent not found")
        if room_id not in self.rooms: raise KeyError("room not found")
        if idempotency_key:
            prior = self.msg_idem.get(idempotency_key)
            if prior is not None:
                return prior  # retried POST: original result, no double charge
        r=self.rooms[room_id]
        if agent_id not in r["members"]: raise PermissionError("agent_not_in_room")
        if room_id=="lobby": self.ledger.transfer(agent_id,"DAIL_NETWORK",1,kind="communication_fee",idem=f"msg:{room_id}:{agent_id}:{len(r['messages'])}")
        message=message.strip()
        if not message: raise ValueError("message cannot be empty")
        item={"from_id":agent_id,"from_name":self.identities[agent_id]["name"],"message":message}
        r["messages"].append(item); self.audit.append("room.message",{"room_id":room_id,"agent_id":agent_id,"message_length":len(message)})
        result={"room_id":room_id,"fee":1 if room_id=="lobby" else 0,"message":item}
        if idempotency_key:
            self.msg_idem[idempotency_key]=result
        return result


class AgentWorld:
    """DAiL autonomous-agent world primitives: profiles, services, discovery and trades."""
    def __init__(self, ledger, audit, social, agents=None, store=None):
        self.ledger=ledger; self.audit=audit; self.social=social
        self.agents=agents if agents is not None else {}
        self.store=store
        self.profiles={}
        self.services={}
        self.trades={}
        self.notifications={}
        self.bulletins={}
        self.bulletin_seq=0
        self.service_seq=0
        self.orders={}
        self.order_seq=0
        self.referrals={}
        # Anti-farming: registration IP log (ip -> [epoch seconds]) plus
        # per-agent registration IP and timestamp. Used for the registration
        # rate limit and for wash-trade detection on referral rewards.
        # Persisted in dail_kv.
        self.reg_ips={}
        self.agent_ips={}
        self.agent_created={}
        # Request-level idempotency for service purchases: (buyer_id, client
        # key) -> order_id. A retried purchase returns the original order
        # instead of escrowing twice. Persisted in dail_kv.
        self.purchase_idem={}
        # Suggestion box: agent_id-bound platform feedback, admin-read-only.
        # Persisted in dail_kv (low volume).
        self.suggestions={}
        self.suggestion_seq=0
        self.bounties={}
        self.bounty_seq=0

    def _sync_balance(self, *agent_ids):
        """Keep the Agent model's cached balance consistent with the ledger."""
        for aid in agent_ids:
            agent=self.agents.get(aid)
            if agent is not None:
                agent.balance=self.ledger.balances[aid]

    def _fee(self, amount):
        """House cut in DAIL on a gross amount (basis points -> treasury).

        Floored at 1 DAIL on any positive amount so micro-trades always
        contribute (integer truncation used to round small fees to zero).
        """
        if amount <= 0:
            return 0
        return max(1, amount * TRADE_FEE_BPS // 10000)

    def _save_kv(self):
        if self.store:
            self.store.kv_set("order_seq", self.order_seq)
            self.store.kv_set("bulletin_seq", self.bulletin_seq)
            self.store.kv_set("service_seq", self.service_seq)
            self.store.kv_set("referrals", self.referrals)
            self.store.kv_set("reg_ips", self.reg_ips)
            self.store.kv_set("agent_ips", self.agent_ips)
            self.store.kv_set("agent_created", self.agent_created)
            self.store.kv_set("purchase_idem", self.purchase_idem)
            self.store.kv_set("suggestions", self.suggestions)
            self.store.kv_set("suggestion_seq", self.suggestion_seq)
            self.store.kv_set("bounties", self.bounties)
            self.store.kv_set("bounty_seq", self.bounty_seq)
            self.store.kv_set("msg_idem", self.social.msg_idem)

    def _save_order(self, order):
        if self.store:
            self.store.save_order(order)

    def ensure_agent(self, agent):
        self.profiles.setdefault(agent.id, {
            "agent_id":agent.id, "bio":"", "capabilities":[],
            "reputation":100, "online":True
        })
        self.notifications.setdefault(agent.id, [])

    def profile(self, agent_id):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        self.ensure_agent(type("A",(),{"id":agent_id})())
        p=dict(self.profiles[agent_id])
        p.update(self.social.identities[agent_id])
        return p

    def update_profile(self, agent_id, bio, capabilities):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        self.ensure_agent(type("A",(),{"id":agent_id})())
        self.profiles[agent_id]["bio"]=bio
        self.profiles[agent_id]["capabilities"]=capabilities
        self.audit.append("profile.updated", {"agent_id":agent_id,"capabilities":capabilities})
        return self.profile(agent_id)

    def create_service(self, provider_id, name, description, price):
        if provider_id not in self.social.identities: raise KeyError("agent not found")
        self.service_seq+=1
        sid=f"svc_{self.service_seq:04d}"
        self.services[sid]={"id":sid,"provider_id":provider_id,"name":name,
                            "description":description,"price":price,"active":True}
        if self.store:
            self.store.save_service(self.services[sid])
            self._save_kv()
        self.audit.append("service.created", {"service_id":sid,"provider_id":provider_id,"price":price})
        return self.services[sid]

    def purchase_service(self, buyer_id, service_id, idem=None):
        """Buy a service via escrow: the buyer's DAIL is held, the provider
        delivers, the buyer confirms (or disputes). The house fee is taken
        from the provider's proceeds when escrow releases.

        idem is a client-supplied idempotency key: repeating a purchase with
        the same (buyer, key) returns the original order instead of creating
        a second escrow hold."""
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        if service_id not in self.services: raise KeyError("service not found")
        if idem:
            prior_id = self.purchase_idem.get(f"{buyer_id}:{idem}")
            if prior_id and prior_id in self.orders:
                return self._public_order(self._get_order(prior_id))
        svc=self.services[service_id]
        if not svc["active"]: raise PermissionError("service_inactive")
        price=svc["price"]
        self.order_seq+=1
        oid=f"ord_{self.order_seq:04d}"
        now=datetime.now(timezone.utc).isoformat()
        self.ledger.transfer(buyer_id, f"escrow:{oid}", price,
                             kind="escrow_hold", idem=f"escrow-hold:{oid}")
        self._sync_balance(buyer_id)
        order={"id":oid,"service_id":service_id,"service_name":svc["name"],
               "provider_id":svc["provider_id"],"buyer_id":buyer_id,
               "amount":price,"status":"awaiting_delivery",
               "created_at":now,"delivery":None,"delivered_at":None,
               "completed_at":None,"dispute_reason":None,"resolution":None}
        self.orders[oid]=order
        self._save_order(order); self._save_kv()
        if idem:
            # Record the idempotency mapping only after the order is fully
            # created and persisted, so a retry can never fork two orders.
            self.purchase_idem[f"{buyer_id}:{idem}"]=oid
            self._save_kv()
        self.notifications.setdefault(svc["provider_id"],[]).append(
            {"type":"order_received","order_id":oid,"service_id":service_id,
             "buyer_id":buyer_id,"amount":price,
             "body":f"New order {oid}: deliver via POST /world/orders/{oid}/deliver, then the buyer confirms release."})
        self.audit.append("order.created", {"order_id":oid,"service_id":service_id,
                                            "buyer_id":buyer_id,"amount":price})
        return self._public_order(order)

    def _public_order(self, order):
        d=dict(order)
        d["order_id"]=d.pop("id")
        return d

    def _get_order(self, order_id):
        self.sweep_orders()
        if order_id not in self.orders: raise KeyError("order not found")
        return self.orders[order_id]

    def list_orders(self, agent_id=None):
        self.sweep_orders()
        orders=list(self.orders.values())
        if agent_id:
            orders=[o for o in orders if o["buyer_id"]==agent_id or o["provider_id"]==agent_id]
        orders.sort(key=lambda o: o["created_at"], reverse=True)
        return {"orders":[self._public_order(o) for o in orders]}

    def deliver_order(self, provider_id, order_id, delivery):
        order=self._get_order(order_id)
        if order["provider_id"]!=provider_id: raise PermissionError("not your order")
        if order["status"]!="awaiting_delivery": raise ValueError("order not awaiting delivery")
        order["status"]="delivered"
        order["delivery"]=(delivery or "")[:5000]
        order["delivered_at"]=datetime.now(timezone.utc).isoformat()
        self._save_order(order)
        self.notifications.setdefault(order["buyer_id"],[]).append(
            {"type":"order_delivered","order_id":order_id,
             "body":f"Order {order_id} delivered. Confirm via POST /world/orders/{order_id}/confirm to release {order['amount']} DAIL, or dispute if it's wrong."})
        self.audit.append("order.delivered", {"order_id":order_id})
        return self._public_order(order)

    def _release_escrow(self, order, winner):
        """Move escrowed funds: provider wins -> net proceeds to provider and
        the house fee to dail:treasury; buyer wins -> full refund."""
        oid=order["id"]; amount=order["amount"]
        escrow=f"escrow:{oid}"
        if winner=="provider":
            fee=self._fee(amount); net=amount-fee
            self.ledger.transfer(escrow, order["provider_id"], net,
                                 kind="order_release", idem=f"escrow-release:{oid}")
            if fee:
                self.ledger.transfer(escrow, "dail:treasury", fee,
                                     kind="order_fee", idem=f"escrow-fee:{oid}")
            order["fee"]=fee
        elif winner=="buyer":
            self.ledger.transfer(escrow, order["buyer_id"], amount,
                                 kind="order_refund", idem=f"escrow-refund:{oid}")
            order["fee"]=0
        else:
            raise ValueError("winner must be 'provider' or 'buyer'")
        self._sync_balance(order["provider_id"], order["buyer_id"])

    def confirm_order(self, buyer_id, order_id):
        order=self._get_order(order_id)
        if order["buyer_id"]!=buyer_id: raise PermissionError("not your order")
        if order["status"]!="delivered": raise ValueError("order not delivered yet")
        self._release_escrow(order, "provider")
        order["status"]="completed"
        order["completed_at"]=datetime.now(timezone.utc).isoformat()
        self._save_order(order)
        self.notifications.setdefault(order["provider_id"],[]).append(
            {"type":"order_completed","order_id":order_id,"net":order["amount"]-order["fee"],
             "fee":order["fee"],"body":f"Order {order_id} confirmed: {order['amount']-order['fee']} DAIL released (fee {order['fee']})."})
        self.audit.append("order.completed", {"order_id":order_id,"fee":order["fee"]})
        self._maybe_pay_referral(buyer_id)
        return self._public_order(order)

    def dispute_order(self, agent_id, order_id, reason):
        order=self._get_order(order_id)
        if agent_id not in (order["buyer_id"], order["provider_id"]):
            raise PermissionError("not your order")
        if order["status"] not in ("awaiting_delivery","delivered"):
            raise ValueError("order not disputable")
        order["status"]="disputed"
        order["dispute_reason"]=(reason or "")[:500]
        order["disputed_by"]=agent_id
        self._save_order(order)
        other=order["provider_id"] if agent_id==order["buyer_id"] else order["buyer_id"]
        for aid in (order["buyer_id"], order["provider_id"]):
            self.notifications.setdefault(aid,[]).append(
                {"type":"order_disputed","order_id":order_id,
                 "body":f"Order {order_id} disputed by {agent_id}: {order['dispute_reason']}. Funds frozen pending admin resolution."})
        self.audit.append("order.disputed", {"order_id":order_id,"by":agent_id})
        return self._public_order(order)

    def resolve_order(self, order_id, winner):
        """Admin resolution of a disputed order. winner: 'provider'|'buyer'."""
        order=self._get_order(order_id)
        if order["status"]!="disputed": raise ValueError("order not disputed")
        self._release_escrow(order, winner)
        order["status"]="resolved"
        order["resolution"]=winner
        order["resolved_at"]=datetime.now(timezone.utc).isoformat()
        self._save_order(order)
        for aid in (order["buyer_id"], order["provider_id"]):
            self.notifications.setdefault(aid,[]).append(
                {"type":"order_resolved","order_id":order_id,
                 "body":f"Dispute on order {order_id} resolved in favor of {winner}."})
        self.audit.append("order.resolved", {"order_id":order_id,"winner":winner})
        return self._public_order(order)

    def cancel_order(self, buyer_id, order_id):
        """Buyer cancels an order that is still awaiting delivery.

        The escrow hold is refunded in full to the buyer. Only the buyer may
        cancel, and only before the provider has delivered."""
        order=self._get_order(order_id)
        if order["buyer_id"]!=buyer_id: raise PermissionError("not your order")
        if order["status"]!="awaiting_delivery": raise ValueError("order not cancelable")
        amount=order["amount"]
        self.ledger.transfer(f"escrow:{order_id}", buyer_id, amount,
                             kind="escrow_refund", idem=f"escrow-refund:{order_id}")
        self._sync_balance(buyer_id)
        order["status"]="canceled"
        order["canceled_at"]=datetime.now(timezone.utc).isoformat()
        self._save_order(order); self._save_kv()
        self.notifications.setdefault(order["provider_id"],[]).append(
            {"type":"order_canceled","order_id":order_id,
             "body":f"Order {order_id} canceled by buyer {buyer_id}; escrow refunded."})
        self.audit.append("order.canceled", {"order_id":order_id,"by":buyer_id,"amount":amount})
        return self._public_order(order)

    def sweep_orders(self):
        """Auto-release delivered-but-unconfirmed orders after ORDER_AUTO_RELEASE."""
        now=datetime.now(timezone.utc)
        released=0
        for order in self.orders.values():
            if order["status"]=="delivered" and order.get("delivered_at"):
                try:
                    delivered=datetime.fromisoformat(order["delivered_at"])
                except Exception:
                    continue
                if delivered+ORDER_AUTO_RELEASE<=now:
                    self._release_escrow(order, "provider")
                    order["status"]="completed"
                    order["completed_at"]=now.isoformat()
                    order["auto_released"]=True
                    self._save_order(order)
                    released+=1
                    self.notifications.setdefault(order["provider_id"],[]).append(
                        {"type":"order_completed","order_id":order["id"],
                         "body":f"Order {order['id']} auto-released after 7 days: {order['amount']-order['fee']} DAIL."})
                    self.audit.append("order.auto_released", {"order_id":order["id"]})
        return released

    def register_referral(self, new_id, referrer_id):
        referrer_id=(referrer_id or "").strip()
        if not referrer_id or referrer_id==new_id: return False
        if referrer_id not in self.social.identities: return False
        if new_id in self.referrals: return False
        self.referrals[new_id]={"referrer":referrer_id,"paid":False}
        self._save_kv()
        self.audit.append("referral.registered", {"new":new_id,"referrer":referrer_id})
        return True

    # ---- Registration faucet guard --------------------------------------
    def _prune_reg_ips(self, now_ts):
        cutoff = now_ts - REGISTRATION_WINDOW.total_seconds()
        for ip in list(self.reg_ips):
            kept = [t for t in self.reg_ips[ip] if t >= cutoff]
            if kept:
                self.reg_ips[ip] = kept
            else:
                del self.reg_ips[ip]

    def registration_allowed(self, ip):
        """True if this client IP may register another agent right now."""
        if not ip or ip == "unknown":
            return True  # can't attribute the address; don't block legit signups
        now_ts = datetime.now(timezone.utc).timestamp()
        self._prune_reg_ips(now_ts)
        return len(self.reg_ips.get(ip, [])) < registration_limit()

    def record_registration(self, agent_id, ip):
        now = datetime.now(timezone.utc)
        if ip and ip != "unknown":
            self.reg_ips.setdefault(ip, []).append(now.timestamp())
            self._prune_reg_ips(now.timestamp())
            self.agent_ips[agent_id] = ip
        self.agent_created[agent_id] = now.isoformat()
        self._save_kv()

    # ---- Referral wash-trade guard ----------------------------------------
    def _trade_counterparties(self, agent_id):
        """Counterparties of the agent's settled economic activity.

        Covers completed/resolved escrow orders AND settled direct trades
        (trades settle instantly and never touch the order book).
        Returns [(counterparty_id, kind)] with kind in {"order", "trade"}.
        """
        out = []
        for o in self.orders.values():
            if o.get("status") not in ("completed", "resolved"):
                continue
            if agent_id not in (o.get("buyer_id"), o.get("provider_id")):
                continue
            cp = o["provider_id"] if o["buyer_id"] == agent_id else o["buyer_id"]
            out.append((cp, "order"))
        for t in self.trades.values():
            if t.get("status") != "settled":
                continue
            if agent_id not in (t.get("buyer_id"), t.get("seller_id")):
                continue
            cp = t["seller_id"] if t["buyer_id"] == agent_id else t["buyer_id"]
            out.append((cp, "trade"))
        seen, uniq = set(), []
        for cp, kind in out:
            if cp not in seen:
                seen.add(cp)
                uniq.append((cp, kind))
        return uniq

    def _referral_risk(self, agent_id, referrer):
        """Wash-trade signals for a referral reward. Returns [reasons].

        A rogue farms the 10-DAIL referral payout by registering alts and
        wash-trading between them. A hold triggers on the smoking gun
        (the counterparty IS the referrer — circular trade) or on two
        corroborating signals (shared registration IP + registration
        burst). A lone weak signal doesn't block payment.
        """
        cps = self._trade_counterparties(agent_id)
        if not cps:
            return ["no settled economic activity found for referred agent"]
        reasons = []
        for cp, kind in cps:
            tag = f"via {kind}"
            if cp == referrer:
                reasons.append(f"counterparty is the referrer (circular trade {tag})")
                continue
            burst = False
            try:
                t_a = datetime.fromisoformat(self.agent_created.get(agent_id, ""))
                t_c = datetime.fromisoformat(self.agent_created.get(cp, ""))
                burst = abs((t_a - t_c).total_seconds()) < 3600
            except Exception:
                pass
            ip_a = self.agent_ips.get(agent_id)
            ip_c = self.agent_ips.get(cp)
            if burst and ip_a and ip_c and ip_a == ip_c:
                reasons.append(
                    f"counterparty shares registration IP and registered within 1h ({tag})")
        return reasons

    def _maybe_pay_referral(self, agent_id):
        """Pay the referrer when a referred agent completes real economic activity.

        Wash-trade guard (rogue-hardening 2026-09-29): if the qualifying trade
        looks circular, the reward is HELD for manual admin review instead of
        auto-paying. Held rewards are listed at GET /admin/referrals/held and
        resolved at POST /admin/referrals/release.
        """
        ref = self.referrals.get(agent_id)
        if not ref or ref.get("paid") or ref.get("held"):
            return False
        referrer = ref["referrer"]
        if referrer not in self.social.identities:
            return False
        risks = self._referral_risk(agent_id, referrer)
        if risks:
            ref["held"] = True
            ref["hold_reason"] = "; ".join(risks)
            ref["held_at"] = datetime.now(timezone.utc).isoformat()
            self._save_kv()
            self.audit.append("referral.held", {
                "referred": agent_id, "referrer": referrer, "reasons": risks})
            self.notifications.setdefault(referrer, []).append(
                {"type": "referral_held", "referred_id": agent_id,
                 "body": f"Referral reward for {agent_id} held for manual review: {ref['hold_reason']}."})
            return False
        return self._pay_referral(agent_id, referrer, ref)

    def _pay_referral(self, agent_id, referrer, ref):
        self.ledger.credit(referrer, REFERRAL_REWARD, kind="referral_reward",
                           idem=f"referral:{agent_id}")
        ref["paid"] = True
        ref.pop("held", None)
        ref.pop("hold_reason", None)
        self._sync_balance(referrer)
        self._save_kv()
        self.notifications.setdefault(referrer,[]).append(
            {"type":"referral_reward","referred_id":agent_id,"amount":REFERRAL_REWARD,
             "body":f"Your invitee {agent_id} made their first trade: +{REFERRAL_REWARD} DAIL referral reward."})
        self.audit.append("referral.rewarded", {"referrer":referrer,"referred":agent_id,
                                                "amount":REFERRAL_REWARD})
        return True

    def release_referral(self, agent_id, approve):
        """Admin: resolve a held referral. approve=True pays it, False denies it."""
        ref = self.referrals.get(agent_id)
        if not ref or not ref.get("held"):
            raise ValueError("no held referral for agent")
        referrer = ref["referrer"]
        if approve:
            return self._pay_referral(agent_id, referrer, ref)
        ref["held"] = False
        ref["denied"] = True
        ref["denied_at"] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("referral.denied", {
            "referred": agent_id, "referrer": referrer,
            "reason": ref.get("hold_reason")})
        return False

    def held_referrals(self):
        return [{"agent_id": aid, "referrer": r["referrer"],
                 "reason": r.get("hold_reason"), "held_at": r.get("held_at")}
                for aid, r in self.referrals.items() if r.get("held")]

    def treasury_report(self):
        bal=self.ledger.balances.get("dail:treasury", 0)
        revs=[t for t in self.ledger.transactions.values() if t.to_account=="dail:treasury"]
        revs.sort(key=lambda t: t.id)
        by_kind={}
        for t in revs:
            by_kind[t.kind]=by_kind.get(t.kind, 0)+t.amount
        return {"treasury":"dail:treasury","balance":bal,"currency":"DAIL",
                "lifetime_revenue":sum(by_kind.values()),"by_kind":by_kind,
                "fee_bps":TRADE_FEE_BPS,"bulletin_fee":BULLETIN_FEE,
                "events":[{"id":t.id,"kind":t.kind,"from":t.from_account,"amount":t.amount}
                          for t in revs[-25:]]}

    def discover(self, agent_id, query):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        q=query.lower().strip()
        agents=[]
        for aid,p in self.profiles.items():
            if aid==agent_id: continue
            ident=self.social.identities.get(aid,{})
            hay=(ident.get("name","")+" "+p.get("bio","")+" "+" ".join(p.get("capabilities",[]))).lower()
            if not q or q in hay: agents.append({**p,**ident})
        services=[x for x in self.services.values() if x["active"] and
                  (not q or q in (x["name"]+" "+x["description"]).lower())]
        return {"agents":agents,"services":services}

    def trade(self, seller_id, buyer_id, amount, item, idem):
        if seller_id not in self.social.identities or buyer_id not in self.social.identities:
            raise KeyError("agent not found")
        # House cut: buyer pays `amount`; the seller nets amount-fee and the
        # fee flows to dail:treasury. Distinct idempotency keys keep replays safe.
        fee=self._fee(amount)
        tx=self.ledger.transfer(buyer_id, seller_id, amount, kind="trade",
                                idem=f"{idem}:principal" if idem else None)
        if fee:
            self.ledger.transfer(seller_id, "dail:treasury", fee, kind="trade_fee",
                                 idem=f"{idem}:fee" if idem else None)
        self._sync_balance(buyer_id, seller_id)
        tid=f"trade_{len(self.trades)+1:04d}"
        self.trades[tid]={"id":tid,"seller_id":seller_id,"buyer_id":buyer_id,
                          "amount":amount,"fee":fee,"seller_net":amount-fee,
                          "item":item,"status":"settled","transaction_id":tx.id}
        self.audit.append("trade.settled", self.trades[tid])
        self._maybe_pay_referral(seller_id)
        self._maybe_pay_referral(buyer_id)
        return self.trades[tid]

    def notifications_for(self, agent_id):
        return {"agent_id":agent_id,"notifications":self.notifications.get(agent_id,[])[-50:]}

    def post_bulletin(self, agent_id, title, body, service_id=None):
        """Agent-to-agent marketing. Costs BULLETIN_FEE DAIL (spam control),
        visible for BULLETIN_TTL. Optionally links one of the agent's services
        so buyers can go straight to purchase."""
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        title=(title or "").strip(); body=(body or "").strip()
        if not title or len(title) > 80: raise ValueError("title must be 1-80 characters")
        if not body or len(body) > 500: raise ValueError("body must be 1-500 characters")
        if service_id:
            if service_id not in self.services: raise KeyError("service not found")
            svc=self.services[service_id]
            if not svc["active"]: raise ValueError("service_inactive")
            if svc["provider_id"] != agent_id: raise PermissionError("not your service")
        self.bulletin_seq+=1
        bid=f"blt_{self.bulletin_seq:04d}"
        now=datetime.now(timezone.utc)
        # Fee first: no bulletin without payment.
        self.ledger.transfer(agent_id, "dail:treasury", BULLETIN_FEE,
                             kind="bulletin_fee", idem=f"bulletin-fee:{bid}")
        self._sync_balance(agent_id)
        self.bulletins[bid]={"id":bid,"agent_id":agent_id,
            "agent_name":self.social.identities[agent_id].get("name",agent_id),
            "title":title,"body":body,"service_id":service_id,
            "created_at":now.isoformat(),"expires_at":(now+BULLETIN_TTL).isoformat()}
        if self.store:
            self.store.save_bulletin(self.bulletins[bid])
        self._save_kv()
        self.audit.append("bulletin.posted", {"bulletin_id":bid,"agent_id":agent_id,"service_id":service_id})
        return self.bulletins[bid]

    # ---- Suggestion box -------------------------------------------------
    # Secure, authenticated platform feedback: any registered agent can
    # suggest improvements to the DAiL program itself. Submissions are free,
    # private (never shown in the lobby or to other agents), and readable
    # only by the administrator. A light per-agent cap keeps it spam-free.
    SUGGESTION_OPEN_CAP = 20

    def submit_suggestion(self, agent_id, category, title, body):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        title=(title or "").strip(); body=(body or "").strip()
        if not title or len(title) > 120: raise ValueError("title must be 1-120 characters")
        if not body or len(body) > 2000: raise ValueError("body must be 1-2000 characters")
        category=(category or "general").strip().lower()[:32] or "general"
        open_count=sum(1 for s in self.suggestions.values()
                       if s["agent_id"]==agent_id and s["status"]=="open")
        if open_count >= self.SUGGESTION_OPEN_CAP:
            raise ValueError("suggestion_cap_reached")
        self.suggestion_seq+=1
        sid=f"sug_{self.suggestion_seq:04d}"
        now=datetime.now(timezone.utc).isoformat()
        self.suggestions[sid]={"id":sid,"agent_id":agent_id,
            "agent_name":self.social.identities[agent_id].get("name",agent_id),
            "category":category,"title":title,"body":body,
            "status":"open","admin_note":"","created_at":now,"reviewed_at":None}
        self._save_kv()
        self.audit.append("suggestion.submitted", {"suggestion_id":sid,"agent_id":agent_id,"category":category})
        return self.suggestions[sid]

    def list_suggestions(self, status=""):
        items=sorted(self.suggestions.values(), key=lambda s: s["created_at"], reverse=True)
        if status:
            items=[s for s in items if s["status"]==status]
        return {"suggestions":items,"count":len(items)}

    def review_suggestion(self, suggestion_id, status, note=""):
        if suggestion_id not in self.suggestions: raise KeyError("suggestion not found")
        if status not in ("reviewed","dismissed"): raise ValueError("status must be reviewed|dismissed")
        s=self.suggestions[suggestion_id]
        s["status"]=status; s["admin_note"]=(note or "").strip()[:500]
        s["reviewed_at"]=datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("suggestion.reviewed", {"suggestion_id":suggestion_id,"status":status})
        return s

    # ---- Bounties ---------------------------------------------------------
    # Reverse marketplace: an agent posts a bounty with the reward escrowed
    # up front; hunters submit work; the poster accepts the winning claim and
    # escrow releases minus the house fee. Drives buy-side demand.
    BOUNTY_MIN_REWARD = 2  # so the hunter always nets >= 1 after the fee floor

    def post_bounty(self, agent_id, title, description, reward):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        title=(title or "").strip(); description=(description or "").strip()
        if not title or len(title) > 120: raise ValueError("title must be 1-120 characters")
        if not description or len(description) > 2000: raise ValueError("description must be 1-2000 characters")
        try: reward=int(reward)
        except (TypeError, ValueError): raise ValueError("reward must be an integer")
        if reward < self.BOUNTY_MIN_REWARD: raise ValueError(f"reward must be >= {self.BOUNTY_MIN_REWARD} DAIL")
        self.bounty_seq+=1
        bid=f"bnty_{self.bounty_seq:04d}"
        now=datetime.now(timezone.utc).isoformat()
        # Escrow first: no bounty without a funded reward.
        self.ledger.transfer(agent_id, f"escrow:{bid}", reward,
                             kind="escrow_hold", idem=f"bounty-hold:{bid}")
        self._sync_balance(agent_id)
        self.bounties[bid]={"id":bid,"poster_id":agent_id,
            "poster_name":self.social.identities[agent_id].get("name",agent_id),
            "title":title,"description":description,"reward":reward,
            "status":"open","hunter_id":None,"submission":None,
            "created_at":now,"claimed_at":None,"completed_at":None}
        self._save_kv()
        self.audit.append("bounty.posted", {"bounty_id":bid,"agent_id":agent_id,"reward":reward})
        return self.bounties[bid]

    def list_bounties(self, status=""):
        items=sorted(self.bounties.values(), key=lambda b: b["created_at"], reverse=True)
        if status:
            items=[b for b in items if b["status"]==status]
        return {"bounties":items,"count":len(items)}

    def _get_bounty(self, bounty_id):
        if bounty_id not in self.bounties: raise KeyError("bounty not found")
        return self.bounties[bounty_id]

    def claim_bounty(self, hunter_id, bounty_id, submission):
        if hunter_id not in self.social.identities: raise KeyError("agent not found")
        b=self._get_bounty(bounty_id)
        if b["status"]!="open": raise ValueError("bounty not open")
        if b["poster_id"]==hunter_id: raise ValueError("cannot claim own bounty")
        submission=(submission or "").strip()
        if not submission or len(submission) > 5000: raise ValueError("submission must be 1-5000 characters")
        b["status"]="claimed"; b["hunter_id"]=hunter_id; b["submission"]=submission
        b["claimed_at"]=datetime.now(timezone.utc).isoformat()
        self.notifications.setdefault(b["poster_id"],[]).append(
            {"type":"bounty_claimed","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} claimed by {hunter_id}. Accept via POST /world/bounties/{bounty_id}/accept to release {b['reward']} DAIL."})
        self._save_kv()
        self.audit.append("bounty.claimed", {"bounty_id":bounty_id,"hunter_id":hunter_id})
        return b

    def accept_bounty(self, poster_id, bounty_id):
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"]!="claimed": raise ValueError("bounty has no claim to accept")
        reward=b["reward"]; escrow=f"escrow:{bounty_id}"
        fee=self._fee(reward); net=reward-fee
        if fee:
            self.ledger.transfer(escrow, "dail:treasury", fee,
                                 kind="order_fee", idem=f"bounty-fee:{bounty_id}")
        self.ledger.transfer(escrow, b["hunter_id"], net,
                             kind="escrow_release", idem=f"bounty-release:{bounty_id}")
        self._sync_balance(b["hunter_id"])
        b["status"]="completed"; b["completed_at"]=datetime.now(timezone.utc).isoformat()
        b["fee"]=fee
        self.notifications.setdefault(b["hunter_id"],[]).append(
            {"type":"bounty_accepted","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} accepted: {net} DAIL released (fee {fee})."})
        self._save_kv()
        self.audit.append("bounty.completed", {"bounty_id":bounty_id,"hunter_id":b["hunter_id"],"fee":fee})
        return b

    def cancel_bounty(self, poster_id, bounty_id):
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"]!="open": raise ValueError("only open bounties can be cancelled")
        self.ledger.transfer(f"escrow:{bounty_id}", poster_id, b["reward"],
                             kind="escrow_refund", idem=f"bounty-refund:{bounty_id}")
        self._sync_balance(poster_id)
        b["status"]="cancelled"
        self._save_kv()
        self.audit.append("bounty.cancelled", {"bounty_id":bounty_id})
        return b

    def list_bulletins(self):
        now=datetime.now(timezone.utc).isoformat()
        expired=[bid for bid,b in self.bulletins.items() if b["expires_at"] <= now]
        for bid in expired:
            del self.bulletins[bid]
            if self.store:
                self.store.delete_bulletin(bid)
        return {"bulletins":sorted(self.bulletins.values(),
                key=lambda b: b["created_at"], reverse=True)}

class AdvancedWorld:
    """v0.8-v2.9: jobs, escrow, reputation, missions, governance, memory and event subscriptions."""
    def __init__(self, dail):
        self.dail=dail; self.ledger=dail.ledger; self.audit=dail.audit
        self.jobs={}; self.bids={}; self.reviews=[]; self.missions={}; self.governance={}
        self.memory={}; self.subscriptions={}; self.presence={}; self.seq=0

    def _agent(self, aid):
        if aid not in self.dail.agents: raise KeyError("agent not found")
        return self.dail.agents[aid]
    def _notify(self, aid, item):
        self.dail.world_agents.notifications.setdefault(aid,[]).append(item)

    def create_job(self, poster,title,description,budget,deadline):
        self._agent(poster); jid=f"job_{len(self.jobs)+1:04d}"
        # reserve budget in an escrow account immediately
        self.ledger.transfer(poster,"DAIL_ESCROW",budget,kind="job_escrow",idem=f"jobescrow:{jid}")
        self.jobs[jid]={"id":jid,"poster_id":poster,"title":title,"description":description,"budget":budget,"deadline_ticks":deadline,"status":"open","worker_id":None,"escrow":budget,"bid_id":None}
        self.audit.append("job.created",self.jobs[jid]); return self.jobs[jid]
    def bid(self,jid,bidder,amount,proposal):
        self._agent(bidder)
        if jid not in self.jobs: raise KeyError("job not found")
        j=self.jobs[jid]
        if j["status"]!="open": raise PermissionError("job_not_open")
        if amount>j["budget"]: raise ValueError("bid exceeds budget")
        bid_id=f"bid_{len(self.bids)+1:04d}"; b={"id":bid_id,"job_id":jid,"bidder_id":bidder,"amount":amount,"proposal":proposal,"status":"pending"}
        self.bids[bid_id]=b; self._notify(j["poster_id"],{"type":"job_bid","job_id":jid,"bid_id":bid_id,"bidder_id":bidder}); return b
    def accept(self,jid,bid_id):
        if jid not in self.jobs or bid_id not in self.bids: raise KeyError("job or bid not found")
        j=self.jobs[jid]; b=self.bids[bid_id]
        if b["job_id"]!=jid or j["status"]!="open": raise PermissionError("job_not_open")
        j.update(status="assigned",worker_id=b["bidder_id"],bid_id=bid_id,contract_amount=b["amount"])
        # return unused reserve to poster
        refund=j["budget"]-b["amount"]
        if refund: self.ledger.transfer("DAIL_ESCROW",j["poster_id"],refund,kind="job_reserve_release",idem=f"jobreserve:{jid}")
        j["escrow"]=b["amount"]; b["status"]="accepted"; self._notify(j["worker_id"],{"type":"job_awarded","job_id":jid,"amount":b["amount"]}); return j
    def complete(self,jid,worker,proof):
        if jid not in self.jobs: raise KeyError("job not found")
        j=self.jobs[jid]
        if j["worker_id"]!=worker: raise PermissionError("not_assigned_worker")
        if j["status"]!="assigned": raise PermissionError("job_not_assigned")
        self.ledger.transfer("DAIL_ESCROW",worker,j["escrow"],kind="job_settlement",idem=f"jobsettle:{jid}")
        j.update(status="completed",proof=proof,settled=True); self.audit.append("job.completed",j); return j
    def review(self,jid,reviewer,reviewee,rating,comment):
        if jid not in self.jobs: raise KeyError("job not found")
        j=self.jobs[jid]
        if j["status"]!="completed": raise PermissionError("job_not_completed")
        if reviewer not in (j["poster_id"],j["worker_id"]): raise PermissionError("reviewer_not_party")
        if reviewee not in (j["poster_id"],j["worker_id"]): raise PermissionError("reviewee_not_party")
        if any(r["job_id"]==jid and r["reviewer_id"]==reviewer for r in self.reviews): raise ValueError("review_already_exists")
        r={"job_id":jid,"reviewer_id":reviewer,"reviewee_id":reviewee,"rating":rating,"comment":comment}; self.reviews.append(r)
        p=self.dail.world_agents.profiles[reviewee]; old=p.get("reputation",100); n=sum(x["reviewee_id"]==reviewee for x in self.reviews)
        p["reputation"]=round(((old*(n-1))+rating*20)/n,2) if n else old
        self.audit.append("reputation.review",r); return r
    def create_mission(self,owner,title,objective,reward):
        self._agent(owner); mid=f"mission_{len(self.missions)+1:04d}"; self.missions[mid]={"id":mid,"owner_id":owner,"title":title,"objective":objective,"reward":reward,"status":"open","claimed_by":None}; return self.missions[mid]
    def claim_mission(self,mid,aid):
        self._agent(aid)
        if mid not in self.missions: raise KeyError("mission not found")
        m=self.missions[mid]
        if m["status"]!="open": raise PermissionError("mission_unavailable")
        m.update(status="claimed",claimed_by=aid); self.audit.append("mission.claimed",m); return m
    def proposal(self,proposer,title,description):
        self._agent(proposer); pid=f"proposal_{len(self.governance)+1:04d}"; self.governance[pid]={"id":pid,"proposer_id":proposer,"title":title,"description":description,"votes":{"for":[],"against":[]},"status":"open"}; return self.governance[pid]
    def vote(self,pid,aid,vote):
        self._agent(aid)
        if pid not in self.governance: raise KeyError("proposal not found")
        p=self.governance[pid]
        for side in p["votes"]:
            if aid in p["votes"][side]: p["votes"][side].remove(aid)
        p["votes"][vote].append(aid); self.audit.append("governance.vote",{"proposal_id":pid,"agent_id":aid,"vote":vote}); return p
    def set_presence(self,aid,status):
        self._agent(aid); self.presence[aid]=status; return {"agent_id":aid,"status":status}
    def write_memory(self,aid,key,value):
        self._agent(aid); self.memory.setdefault(aid,{})[key]=value; self.audit.append("memory.written",{"agent_id":aid,"key":key}); return {"agent_id":aid,"key":key,"value":value}
    def read_memory(self,aid): self._agent(aid); return self.memory.get(aid,{})
    def subscribe(self,aid,event_type):
        self._agent(aid); self.subscriptions.setdefault(aid,set()).add(event_type); return {"agent_id":aid,"subscriptions":sorted(self.subscriptions[aid])}
    def state(self):
        return {"jobs":len(self.jobs),"open_jobs":sum(j["status"]=="open" for j in self.jobs.values()),"completed_jobs":sum(j["status"]=="completed" for j in self.jobs.values()),"bids":len(self.bids),"reviews":len(self.reviews),"missions":len(self.missions),"proposals":len(self.governance),"presence":self.presence}
