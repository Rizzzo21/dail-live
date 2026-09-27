# DAiL Pre-Production Security Gate

This build is intentionally local and uses no real payment provider.

- [ ] Rootless container isolation tested
- [ ] Network egress deny-by-default tested
- [ ] Agent workers cannot access secrets
- [ ] WebAuthn/passkey approval tested end-to-end
- [ ] Recovery and compromised-admin scenarios tested
- [ ] Append-only audit persistence tested
- [ ] Backup/restore verified
- [x] Idempotency and webhook replay protection tested (tests/test_payment_safety.py)
- [x] Provider webhook signatures verified (Stripe construct_event + bad-signature test)
- [ ] Payment reconciliation tested
- [ ] Rate limits and abuse controls tested
- [ ] Agent budget/runaway-loop tests
- [ ] Prompt/tool injection tests
- [ ] Cross-agent data isolation tests
- [ ] SSRF/network-request restrictions
- [ ] Filesystem escape tests
- [ ] Dependency/container vulnerability scan
- [ ] Independent penetration test
- [ ] Legal/compliance review for real-money products

Agents must never receive raw card numbers, bank credentials, Apple Pay
credentials, or payment-provider secret keys.

The internal DAiL ledger is a simulation ledger, not a bank account or
cryptocurrency. If real customer funds are ever held, custody and regulatory
requirements must be reviewed before launch.
