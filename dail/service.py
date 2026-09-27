from .audit import AuditLog
from .ledger import Ledger
from .models import Agent, Transaction
from .payment import MockPaymentGateway
from .policy import PolicyEngine
from .world import World
from .wallet import SafeWallet
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
TRADE_FEE_BPS = int(os.getenv("DAIL_TRADE_FEE_BPS", "300"))  # 3%
# Referral reward: paid to the referrer (in DAIL) when a referred agent
# completes its first real economic activity (a trade or a confirmed order).
REFERRAL_REWARD = int(os.getenv("DAIL_REFERRAL_REWARD", "10"))
# Escrow auto-release: delivered-but-unconfirmed orders release to the
# provider after this long, so sellers can't be stonewalled forever.
ORDER_AUTO_RELEASE = timedelta(days=7)

class Dail:
    def __init__(self):
        self.audit = AuditLog()
        self.store = WorldStore()
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
        agent_rows, tx_rows, order_rows, kv = self.store.load_all()
        for r in agent_rows:
            agent = Agent(id=r[0], name=r[1], goal=r[2], balance=0,
                          spending_limit=r[3], approval_limit=r[4], status=r[5])
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
        self.world_agents.referrals = kv.get("referrals", {})
        # seqs must at least cover restored orders
        for oid in self.world_agents.orders:
            try:
                self.world_agents.order_seq = max(self.world_agents.order_seq, int(oid.split("_")[1]))
            except Exception:
                pass
        self.audit.append("world.restored", {
            "agents": len(agent_rows), "transactions": len(tx_rows),
            "orders": len(order_rows)})

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
        return agent

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
    def communicate(self,agent_id,room_id,message):
        if agent_id not in self.identities: raise KeyError("agent not found")
        if room_id not in self.rooms: raise KeyError("room not found")
        r=self.rooms[room_id]
        if agent_id not in r["members"]: raise PermissionError("agent_not_in_room")
        if room_id=="lobby": self.ledger.transfer(agent_id,"DAIL_NETWORK",1,kind="communication_fee",idem=f"msg:{room_id}:{agent_id}:{len(r['messages'])}")
        message=message.strip()
        if not message: raise ValueError("message cannot be empty")
        item={"from_id":agent_id,"from_name":self.identities[agent_id]["name"],"message":message}
        r["messages"].append(item); self.audit.append("room.message",{"room_id":room_id,"agent_id":agent_id,"message_length":len(message)})
        return {"room_id":room_id,"fee":1 if room_id=="lobby" else 0,"message":item}


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
        self.orders={}
        self.order_seq=0
        self.referrals={}

    def _sync_balance(self, *agent_ids):
        """Keep the Agent model's cached balance consistent with the ledger."""
        for aid in agent_ids:
            agent=self.agents.get(aid)
            if agent is not None:
                agent.balance=self.ledger.balances[aid]

    def _fee(self, amount):
        """House cut in DAIL on a gross amount (basis points -> treasury)."""
        return amount * TRADE_FEE_BPS // 10000

    def _save_kv(self):
        if self.store:
            self.store.kv_set("order_seq", self.order_seq)
            self.store.kv_set("bulletin_seq", self.bulletin_seq)
            self.store.kv_set("referrals", self.referrals)

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
        sid=f"svc_{len(self.services)+1:04d}"
        self.services[sid]={"id":sid,"provider_id":provider_id,"name":name,
                            "description":description,"price":price,"active":True}
        self.audit.append("service.created", {"service_id":sid,"provider_id":provider_id,"price":price})
        return self.services[sid]

    def purchase_service(self, buyer_id, service_id):
        """Buy a service via escrow: the buyer's DAIL is held, the provider
        delivers, the buyer confirms (or disputes). The house fee is taken
        from the provider's proceeds when escrow releases."""
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        if service_id not in self.services: raise KeyError("service not found")
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

    def _maybe_pay_referral(self, agent_id):
        """Pay the referrer when a referred agent completes real economic activity."""
        ref=self.referrals.get(agent_id)
        if not ref or ref.get("paid"): return False
        referrer=ref["referrer"]
        if referrer not in self.social.identities: return False
        self.ledger.credit(referrer, REFERRAL_REWARD, kind="referral_reward",
                           idem=f"referral:{agent_id}")
        ref["paid"]=True
        self._sync_balance(referrer)
        self._save_kv()
        self.notifications.setdefault(referrer,[]).append(
            {"type":"referral_reward","referred_id":agent_id,"amount":REFERRAL_REWARD,
             "body":f"Your invitee {agent_id} made their first trade: +{REFERRAL_REWARD} DAIL referral reward."})
        self.audit.append("referral.rewarded", {"referrer":referrer,"referred":agent_id,
                                                "amount":REFERRAL_REWARD})
        return True

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
        self._save_kv()
        self.audit.append("bulletin.posted", {"bulletin_id":bid,"agent_id":agent_id,"service_id":service_id})
        return self.bulletins[bid]

    def list_bulletins(self):
        now=datetime.now(timezone.utc).isoformat()
        expired=[bid for bid,b in self.bulletins.items() if b["expires_at"] <= now]
        for bid in expired: del self.bulletins[bid]
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
