class PolicyEngine:
    def authorize_payment(self, agent, amount, approved=False):
        if agent.status != "active":
            return False, "agent_not_active"
        if amount > agent.spending_limit:
            return False, "spending_limit_exceeded"
        if amount > agent.approval_limit and not approved:
            return False, "human_approval_required"
        return True, "approved"

    def authorize_tool(self, agent, tool, estimated_cost):
        allowed = {"clock", "world.read", "market.quote", "calculator"}
        if agent.status != "active":
            return False, "agent_not_active"
        if tool not in allowed:
            return False, "tool_not_allowlisted"
        if estimated_cost > agent.spending_limit:
            return False, "tool_cost_exceeded"
        return True, "approved"
