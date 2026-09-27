class MockPaymentGateway:
    # Intentionally never contacts a real processor.
    def __init__(self, ledger, audit):
        self.ledger = ledger
        self.audit = audit

    def deposit(self, agent_id, amount, idem):
        tx = self.ledger.credit(agent_id, amount, kind="provider_deposit", idem=idem)
        self.audit.append("payment.mock_deposit", {"agent": agent_id, "amount": amount})
        return tx

    def charge(self, agent_id, merchant, amount, idem):
        tx = self.ledger.transfer(agent_id, f"merchant:{merchant}", amount,
                                  kind="provider_charge", idem=idem)
        self.audit.append("payment.mock_charge", {
            "agent": agent_id, "merchant": merchant, "amount": amount
        })
        return tx
