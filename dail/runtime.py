from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List
import time, uuid

@dataclass
class AgentRuntimeState:
    agent_id: str
    goal: str = ""
    active: bool = True
    mode: str = "observe"
    memory: Dict[str, str] = field(default_factory=dict)
    inbox: List[Dict[str, Any]] = field(default_factory=list)
    last_tick: int = 0
    actions: int = 0
    errors: int = 0

class AgentRuntime:
    """Policy-first, bounded runtime for agents operating inside DAiL."""
    ALLOWED_ACTIONS = {"observe", "remember", "discover", "purchase_service", "message_lobby", "join_room"}

    def __init__(self, dail):
        self.dail = dail
        self.agents: Dict[str, AgentRuntimeState] = {}
        self.action_log: List[Dict[str, Any]] = []
        self.max_actions_per_tick = 1
        from .orchestration import DecisionEngine, StrategyRegistry, Scheduler, AgentProtocol, WorkExecutor, BlackBox
        self.decisions = DecisionEngine(self)
        self.strategies = StrategyRegistry(self)
        self.scheduler = Scheduler(self)
        self.protocol = AgentProtocol(self)
        self.work = WorkExecutor(self)
        self.blackbox = BlackBox(self)

    def _new_state(self, agent_id):
        agent = self.dail.agents[agent_id]
        return AgentRuntimeState(agent_id=agent_id, goal=agent.goal)

    def _agent(self, agent_id):
        if agent_id not in self.dail.agents:
            raise KeyError("agent not found")
        return self.dail.agents[agent_id]

    def register(self, agent_id: str, goal: str = ""):
        agent = self._agent(agent_id)
        state = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=goal or agent.goal))
        state.goal = goal or agent.goal
        return {"ok": True, "agent_id": agent_id, "goal": state.goal, "active": state.active}

    def observe(self, agent_id: str):
        self._agent(agent_id)
        s = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=self.dail.agents[agent_id].goal))
        world_state = {
            "agents": len(self.dail.agents),
            "services": len(self.dail.world_agents.services),
            "trades": len(self.dail.world_agents.trades),
            "rooms": len(self.dail.social.rooms),
            "tick": self.dail.world.tick,
        }
        return {
            "agent_id": agent_id,
            "goal": s.goal,
            "mode": s.mode,
            "active": s.active,
            "world": world_state,
            "profile": self.dail.world_agents.profiles.get(agent_id, {}),
            "services": list(self.dail.world_agents.services.values())[:10],
            "rooms": [self.dail.social.public_room(r) for r in list(self.dail.social.rooms.values())[:10]],
            "memory": dict(s.memory),
            "inbox": list(s.inbox[-10:]),
        }

    def remember(self, agent_id, key, value):
        self._agent(agent_id)
        if not key.strip():
            raise ValueError("memory_key_required")
        s = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=self.dail.agents[agent_id].goal))
        s.memory[key[:200]] = value[:2000]
        self.dail.audit.append("runtime.memory", {"agent_id": agent_id, "key": key[:200]})
        return {"ok": True, "agent_id": agent_id, "key": key[:200]}

    def act(self, agent_id, action, payload=None):
        self._agent(agent_id)
        payload = payload or {}
        s = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=self.dail.agents[agent_id].goal))
        if not s.active:
            raise PermissionError("agent_runtime_paused")
        if action not in self.ALLOWED_ACTIONS:
            raise PermissionError("runtime_action_not_allowed")
        try:
            if action == "observe":
                result = self.observe(agent_id)
            elif action == "remember":
                result = self.remember(agent_id, payload.get("key", ""), payload.get("value", ""))
            elif action == "discover":
                result = self.dail.world_agents.discover(agent_id, payload.get("query", ""))
            elif action == "purchase_service":
                result = self.dail.world_agents.purchase_service(agent_id, payload["service_id"])
            elif action == "message_lobby":
                result = self.dail.social.communicate(agent_id, "lobby", payload["message"])
            elif action == "join_room":
                result = self.dail.social.join_room(agent_id, payload["room_id"])
            s.actions += 1
            event = {"id": str(uuid.uuid4()), "ts": time.time(), "agent_id": agent_id, "action": action, "result": result}
            self.action_log.append(event)
            self.dail.audit.append("runtime.action", {"id": event["id"], "agent_id": agent_id, "action": action})
            return event
        except Exception:
            s.errors += 1
            raise

    def pause(self, agent_id):
        self._agent(agent_id)
        s = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=self.dail.agents[agent_id].goal))
        s.active = False
        self.dail.audit.append("runtime.paused", {"agent_id": agent_id})
        return {"ok": True, "agent_id": agent_id, "active": False}

    def resume(self, agent_id):
        self._agent(agent_id)
        s = self.agents.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id, goal=self.dail.agents[agent_id].goal))
        s.active = True
        self.dail.audit.append("runtime.resumed", {"agent_id": agent_id})
        return {"ok": True, "agent_id": agent_id, "active": True}

    def tick(self):
        out = []
        for aid, s in self.agents.items():
            if s.active:
                s.last_tick += 1
                out.append({"agent_id": aid, "observation": self.observe(aid)})
        return {"runtime_tick": len(out), "agents": out}

    def decide(self, agent_id):
        observation = self.observe(agent_id)
        decision = self.decisions.decide(agent_id, observation)
        receipt = self.blackbox.record(agent_id, observation, decision)
        return {"decision": decision, "receipt": receipt}

    def decide_and_act(self, agent_id):
        observation = self.observe(agent_id)
        strategy = self.strategies.get(agent_id)
        decision = self.decisions.decide(agent_id, observation)
        if decision["action"] not in strategy["allowed_actions"]:
            decision = {**decision, "action": "observe", "payload": {}, "reason": "Strategy boundary overrode the proposed action."}
        result = self.act(agent_id, decision["action"], decision.get("payload", {}))
        receipt = self.blackbox.record(agent_id, observation, decision, result)
        return {"decision": decision, "result": result, "receipt": receipt}

    def state(self):
        return {
            "agents": len(self.agents),
            "active": sum(s.active for s in self.agents.values()),
            "actions": len(self.action_log),
            "allowed_actions": sorted(self.ALLOWED_ACTIONS),
            "strategies": {aid: self.strategies.get(aid) for aid in self.agents},
            "scheduler": self.scheduler.state(),
            "receipts": len(self.blackbox.receipts),
        }
