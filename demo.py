from dail.models import Agent
from dail.service import Dail

x = Dail()
x.create_agent(Agent(id="agent_001", name="Atlas",
                     goal="operate safely in the DAiL world",
                     spending_limit=5000, approval_limit=2500))

x.deposit("agent_001", 10000, "mock", "demo-deposit-1")
print("Balance after deposit:", x.ledger.balances["agent_001"])

x.pay("agent_001", "demo-merchant", 1000, "demo-pay-1", "simulation")
print("Balance after payment:", x.ledger.balances["agent_001"])

try:
    x.pay("agent_001", "demo-merchant", 3000, "demo-pay-2", "simulation")
except PermissionError as e:
    print("Large payment blocked:", e)

x.pay("agent_001", "demo-merchant", 3000, "demo-pay-3", "simulation", approved=True)
print("Balance after approved payment:", x.ledger.balances["agent_001"])
print("Audit valid:", x.audit.verify())
print("World tick:", x.tick())
