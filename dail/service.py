from .audit import AuditLog
from .ledger import Ledger
from .models import Agent
from .payment import MockPaymentGateway
from .policy import PolicyEngine
from .world import World
from .wallet import SafeWallet
import os
from datetime import datetime, timezone, timedelta

# Agent-to-agent marketing: posting a bulletin costs DAIL (paywalled
# communication doubles as spam control) and stays visible for 7 days.
BULLETIN_FEE = 5
BULLETIN_TTL = timedelta(days=7)

class Dail:
    def __init__(self):
        self.audit = AuditLog()
        self.ledger = Ledger(self.audit)
        self.policy = PolicyEngine()
        self.payment = MockPaymentGateway(self.ledger, self.audit)
        self.world = World()
        self.agents = {}
        self.social = SocialWorld(self.ledger, self.audit)
        self.world_agents = AgentWorld(self.ledger, self.audit, self.social)
        self.safe = SafeWallet(self.ledger, self.audit, os.getenv("DAIL_ADMIN_KEY"))
        self.advanced = AdvancedWorld(self)

    def create_agent(self, agent):
        if agent.id in self.agents:
            raise ValueError("agent already exists")
        self.agents[agent.id] = agent
        self.ledger.balances[agent.id] = agent.balance
        self.audit.append("agent.created", agent.model_dump())
        self.social.register(agent)
        self.world_agents.ensure_agent(agent)
        return agent

    def deposit(self, agent_id, amount, provider, idem):
        self._agent(agent_id)
        return self.payment.deposit(agent_id, amount, idem)

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
    def __init__(self, ledger, audit, social):
        self.ledger=ledger; self.audit=audit; self.social=social
        self.profiles={}
        self.services={}
        self.trades={}
        self.notifications={}
        self.bulletins={}
        self.bulletin_seq=0

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
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        if service_id not in self.services: raise KeyError("service not found")
        svc=self.services[service_id]
        if not svc["active"]: raise PermissionError("service_inactive")
        self.ledger.transfer(buyer_id, svc["provider_id"], svc["price"],
                             kind="service_purchase", idem=f"purchase:{service_id}:{buyer_id}")
        self.notifications.setdefault(svc["provider_id"],[]).append(
            {"type":"service_sold","service_id":service_id,"buyer_id":buyer_id})
        self.audit.append("service.purchased", {"service_id":service_id,"buyer_id":buyer_id})
        return {"service":svc,"status":"paid","amount":svc["price"]}

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
        tx=self.ledger.transfer(buyer_id, seller_id, amount, kind="trade", idem=idem)
        tid=f"trade_{len(self.trades)+1:04d}"
        self.trades[tid]={"id":tid,"seller_id":seller_id,"buyer_id":buyer_id,
                          "amount":amount,"item":item,"status":"settled","transaction_id":tx.id}
        self.audit.append("trade.settled", self.trades[tid])
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
        self.bulletins[bid]={"id":bid,"agent_id":agent_id,
            "agent_name":self.social.identities[agent_id].get("name",agent_id),
            "title":title,"body":body,"service_id":service_id,
            "created_at":now.isoformat(),"expires_at":(now+BULLETIN_TTL).isoformat()}
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
