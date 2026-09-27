from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List
import hashlib, json, time, uuid


@dataclass
class Strategy:
    name: str
    allowed_actions: List[str]
    description: str = ""


@dataclass
class ScheduleItem:
    id: str
    agent_id: str
    action: str
    payload: Dict[str, Any]
    due_tick: int
    status: str = "scheduled"


class DecisionEngine:
    """Deterministic, explainable decision layer; no hidden goals or unrestricted execution."""
    def __init__(self, runtime):
        self.runtime = runtime

    def decide(self, agent_id: str, observation: Dict[str, Any]) -> Dict[str, Any]:
        state = self.runtime.agents[agent_id]
        goal = (state.goal or "").lower()
        services = observation.get("services", [])
        memory = observation.get("memory", {})
        if any(k in goal for k in ("discover", "find", "research")) and services:
            action = "discover"
            payload = {"query": goal}
            reason = "Goal contains discovery intent and discoverable services exist."
        elif any(k in goal for k in ("remember", "track", "monitor")):
            action = "remember"
            payload = {"key": "last_goal_check", "value": f"tick={observation['world']['tick']}"}
            reason = "Goal contains a memory/monitoring intent; record a bounded runtime checkpoint."
        else:
            action = "observe"
            payload = {}
            reason = "No higher-priority bounded rule matched; remain observational."
        return {"action": action, "payload": payload, "reason": reason, "goal": state.goal, "memory_keys": sorted(memory.keys())}


class StrategyRegistry:
    def __init__(self, runtime):
        self.runtime = runtime
        self.strategies: Dict[str, Strategy] = {
            "observer": Strategy("observer", ["observe", "remember"], "Observe first; retain only explicit runtime memory."),
            "market_scout": Strategy("market_scout", ["observe", "discover", "remember"], "Discover services and record findings."),
            "collaborator": Strategy("collaborator", ["observe", "discover", "join_room", "message_lobby", "remember"], "Discover and communicate within the bounded social world."),
        }
        self.assigned: Dict[str, str] = {}

    def assign(self, agent_id: str, strategy: str):
        if strategy not in self.strategies:
            raise KeyError("strategy not found")
        if agent_id not in self.runtime.dail.agents:
            raise KeyError("agent not found")
        self.assigned[agent_id] = strategy
        self.runtime.dail.audit.append("runtime.strategy_assigned", {"agent_id": agent_id, "strategy": strategy})
        return {"agent_id": agent_id, "strategy": strategy, "allowed_actions": self.strategies[strategy].allowed_actions}

    def get(self, agent_id):
        name = self.assigned.get(agent_id, "observer")
        s = self.strategies[name]
        return {"name": s.name, "description": s.description, "allowed_actions": s.allowed_actions}

    def list(self):
        return [{"name": s.name, "description": s.description, "allowed_actions": s.allowed_actions} for s in self.strategies.values()]


class Scheduler:
    def __init__(self, runtime):
        self.runtime = runtime
        self.items: Dict[str, ScheduleItem] = {}
        self.tick = 0

    def schedule(self, agent_id, action, payload, delay_ticks=1):
        if agent_id not in self.runtime.dail.agents:
            raise KeyError("agent not found")
        if action not in self.runtime.ALLOWED_ACTIONS:
            raise PermissionError("runtime_action_not_allowed")
        if delay_ticks < 1 or delay_ticks > 1000:
            raise ValueError("delay_ticks_out_of_range")
        sid = f"sched_{uuid.uuid4().hex[:10]}"
        item = ScheduleItem(sid, agent_id, action, payload or {}, self.tick + delay_ticks)
        self.items[sid] = item
        self.runtime.dail.audit.append("runtime.schedule_created", {"id": sid, "agent_id": agent_id, "action": action, "due_tick": item.due_tick})
        return item.__dict__.copy()

    def advance(self):
        self.tick += 1
        executed = []
        for item in list(self.items.values()):
            if item.status == "scheduled" and item.due_tick <= self.tick:
                try:
                    result = self.runtime.act(item.agent_id, item.action, item.payload)
                    item.status = "executed"
                    executed.append({"schedule_id": item.id, "status": item.status, "result": result})
                except Exception as exc:
                    item.status = "failed"
                    executed.append({"schedule_id": item.id, "status": item.status, "error": str(exc)})
        return {"scheduler_tick": self.tick, "executed": executed, "pending": sum(x.status == "scheduled" for x in self.items.values())}

    def state(self):
        return {"tick": self.tick, "scheduled": sum(x.status == "scheduled" for x in self.items.values()), "executed": sum(x.status == "executed" for x in self.items.values()), "failed": sum(x.status == "failed" for x in self.items.values()), "items": [x.__dict__.copy() for x in self.items.values()]}


class AgentProtocol:
    """Bounded inter-agent protocol using existing DAiL social primitives."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.inbox: Dict[str, List[Dict[str, Any]]] = {}

    def send(self, sender_id, recipient_id, message):
        if sender_id not in self.runtime.dail.agents or recipient_id not in self.runtime.dail.agents:
            raise KeyError("agent not found")
        message = message.strip()
        if not message:
            raise ValueError("message cannot be empty")
        packet = {"id": f"msg_{uuid.uuid4().hex[:12]}", "from": sender_id, "to": recipient_id, "message": message, "ts": time.time(), "status": "delivered"}
        self.inbox.setdefault(recipient_id, []).append(packet)
        self.runtime.agents.setdefault(recipient_id, self.runtime._new_state(recipient_id)).inbox.append(packet)
        self.runtime.dail.audit.append("runtime.message_delivered", {"id": packet["id"], "from": sender_id, "to": recipient_id})
        return packet

    def read(self, agent_id):
        if agent_id not in self.runtime.dail.agents:
            raise KeyError("agent not found")
        return {"agent_id": agent_id, "messages": self.inbox.get(agent_id, [])[-50:]}


class WorkExecutor:
    """Turns a job contract into a bounded execution record and settles only through DAiL escrow."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.execution_log: List[Dict[str, Any]] = []

    def execute(self, job_id, worker_id, proof="runtime-complete"):
        if job_id not in self.runtime.dail.advanced.jobs:
            raise KeyError("job not found")
        job = self.runtime.dail.advanced.jobs[job_id]
        if job.get("worker_id") != worker_id:
            raise PermissionError("not_assigned_worker")
        result = self.runtime.dail.advanced.complete(job_id, worker_id, proof)
        record = {"id": f"exec_{uuid.uuid4().hex[:10]}", "job_id": job_id, "worker_id": worker_id, "proof": proof, "status": "completed", "ts": time.time()}
        self.execution_log.append(record)
        self.runtime.dail.audit.append("runtime.work_executed", record)
        return {"execution": record, "job": result}


class BlackBox:
    """The chef's kiss: explainable intent receipts with a hash chain for every runtime decision."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.receipts: List[Dict[str, Any]] = []
        self.last_hash = "GENESIS"

    def record(self, agent_id, observation, decision, action_result=None):
        body = {
            "agent_id": agent_id,
            "goal": self.runtime.agents[agent_id].goal,
            "observation_digest": hashlib.sha256(json.dumps(observation, sort_keys=True, default=str).encode()).hexdigest(),
            "decision": decision,
            "result_digest": hashlib.sha256(json.dumps(action_result, sort_keys=True, default=str).encode()).hexdigest() if action_result is not None else None,
            "ts": time.time(),
            "previous_hash": self.last_hash,
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        receipt = {"receipt_id": f"rcpt_{uuid.uuid4().hex[:12]}", **body, "hash": digest}
        self.receipts.append(receipt)
        self.last_hash = digest
        self.runtime.dail.audit.append("runtime.intent_receipt", {"receipt_id": receipt["receipt_id"], "hash": digest, "agent_id": agent_id})
        return receipt

    def verify(self):
        previous = "GENESIS"
        for r in self.receipts:
            body = {k: r[k] for k in ("agent_id", "goal", "observation_digest", "decision", "result_digest", "ts", "previous_hash")}
            if r["previous_hash"] != previous:
                return {"valid": False, "reason": "hash_chain_break", "receipt_id": r["receipt_id"]}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            if digest != r["hash"]:
                return {"valid": False, "reason": "receipt_tampered", "receipt_id": r["receipt_id"]}
            previous = digest
        return {"valid": True, "receipts": len(self.receipts), "head": previous}

    def latest(self, agent_id=None, limit=20):
        data = self.receipts if agent_id is None else [r for r in self.receipts if r["agent_id"] == agent_id]
        return data[-limit:]
