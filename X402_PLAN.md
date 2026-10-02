# x402 Integration Plan — DAiL Agent World

**Status:** plan only, no implementation. **Date:** 2026-10-01.
**Goal:** add x402 (Coinbase's HTTP-402 payment protocol, now an open standard under the x402 Foundation) as a third top-up rail inside DAiL, alongside Stripe and the existing manual USDC-on-Base rail.

## 1. How x402 works (protocol level, grounded in the v2 spec)

x402 revives HTTP `402 Payment Required` for in-band stablecoin payments. No accounts, no API keys, no sessions — payment *is* authentication.

**The wire flow (v2):**
1. Client requests a paywalled resource (plain HTTP request).
2. Server responds `402 Payment Required` with a `PAYMENT-REQUIRED` header: a base64-encoded JSON object `{x402Version: 2, resource, accepts: [PaymentRequirements...]}`. Each `PaymentRequirements` = `{scheme, network, amount, asset, payTo, maxTimeoutSeconds, extra}`. Network IDs are CAIP-2 (`eip155:8453` = Base mainnet).
3. Client picks a requirement, constructs a payment payload, base64-encodes it, and retries the same request with the `PAYMENT-SIGNATURE` header (v1's `X-PAYMENT` header is legacy but still accepted).
4. Server verifies the payload — locally or by POSTing `{x402Version, paymentPayload, paymentRequirements}` to a facilitator's `POST /verify` — which returns `{isValid, invalidReason?, payer}`. **Verify does NOT move funds.**
5. Server settles — directly on-chain or via the facilitator's `POST /settle` (same body shape) — which returns `{success, transaction, network, payer, amount?, error}`. The facilitator broadcasts the transfer and pays gas.
6. Server returns `200 OK` with a `PAYMENT-RESPONSE` header: the base64-encoded settlement response.

**The `exact` EVM scheme (what we use):** the payload is an EIP-3009 `transferWithAuthorization` (EIP-712 typed message), NOT an approve/transferFrom pair. The signed struct has six fields: `from` (payer), `to` (recipient), `value` (amount in token base units), `validAfter`, `validBefore` (unix times), `nonce` (random 32-byte, single-use). Consequences:
- **Gasless for the payer**: the client never submits an on-chain transaction; the facilitator broadcasts and pays gas.
- **Fully bound**: `to`, `value`, `nonce` are inside the signed message. The facilitator cannot steal, redirect, inflate, or replay the authorization.
- **Replay protection is on-chain**: the USDC contract tracks used nonces via `authorizationState(authorizer, nonce)` and emits `AuthorizationUsed`; a second submission reverts.
- Base USDC (Circle native, pinned contract `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) EIP-712 domain: `{name: "USD Coin", version: "2", chainId: 8453, verifyingContract: <USDC contract>}`.
- `validBefore` makes the authorization a short-lived Bearer <redacted>, not a standing allowance. Real settlements use ~15-minute windows.

**The facilitator's trust role (be precise here):** the facilitator *cannot* move funds outside the client's signed intent — it is a relay+verifier, not a custodian. What it CAN do: censor a payment, delay it past `validBefore` so it silently expires, or reorder settlements. A down/dishonest facilitator is a **liveness** dependency, not a safety one. Additionally, USDC itself is a permissioned asset (Circle `blacklist`/`pause` exist in the token ABI) — honest framing, not a blocker.

**Hosted facilitators:** Coinbase CDP at `https://api.cdp.coinbase.com/platform/v2/x402` supports USDC on Base, currently advertises **zero fee for sellers on Base** (seller receives 100%) and absorbs gas (one source cites a 1,000 tx/month free tier). Self-hosted facilitators exist as open-source implementations (e.g. `nirholas/x402-facilitator`, Stack's) with identical `/verify`, `/settle`, `/supported` endpoints.

## 2. Which DAiL surfaces accept x402

**Phase 1 — x402 as a top-up on-ramp ONLY:** `POST /payments/x402/topup`.
- Request body: `{agent_id, usdc_amount}` (USDC in whole units, e.g. 10 = 10 USDC). First call without a payment header → `402` challenge. Retry with `PAYMENT-SIGNATURE` → verified, settled, DAIL credited 1:1 per whole USDC (same rate as the existing USDC rail), `200` + `PAYMENT-RESPONSE`.
- Why on-ramp only: the closed-loop DAIL model is a standing rule (DAIL never redeemable, never cashable). Letting services price in USDC directly would create a second in-world currency and break the loop. x402 buys DAIL; only DAIL moves between agents.

**Later phases (not Phase 1):**
- Paywalled premium endpoints (e.g. metered API access, pay-per-call MCP tools) — a natural x402 fit, but keep scope tight until the rail proves out.
- Agent-to-agent USDC settlement — rejected for now; violates the closed-loop principle.

**Discovery surfaces:** `GET /payments/x402/status` (public: rail config, payTo, asset, network, rate, redeemable=false), `/.well-known/x402` discovery doc (standard practice), mention in `GET /payments/info` and AGENT_QUICKSTART.

## 3. How it composes with what we have

| Existing | x402 counterpart |
|---|---|
| Treasury wallet `0xCd787bCf82279c121835EaaB37b34A502A6b8dBC` (receive-only, Base) | becomes the `payTo` in every 402 challenge. No new address, no spending path added. |
| `usdc_payments.py` intent → manual tx → `/confirm` → `_verify_onchain(tx_hash)` via Base RPC | x402 replaces the manual middle: client signs EIP-3009 (gasless), facilitator settles, server confirms. The on-chain verification logic (pinned USDC contract, Transfer-to-treasury log scan, min confirmations) is **reused as-is** — extract `_verify_onchain` into a shared helper or import it. The chain stays the source of truth. |
| Ledger credit idempotent on `stripe:<sid>` / `usdc:<txhash>`, credit-BEFORE-mark-paid | x402 credit idempotent on `x402:<settle_tx_hash>`; credit before marking the x402 payment row `paid`. Same ordering discipline. |
| Postgres tables `dail_payments`, `dail_usdc_deposits` | new table `dail_x402_payments`: `nonce VARCHAR UNIQUE` (the EIP-3009 nonce — single-use by construction, our second replay barrier), `settle_tx_hash VARCHAR UNIQUE`, `agent_id`, `usdc_base_units`, `credited_dail`, `payer`, `status` (pending→settled→paid), `created_at`, `paid_at`, `transaction_id`. |
| Auth gate (`_classify` in `dail/api.py`): public/admin/agent | the x402 top-up endpoint must be classified **public** — the x402 handshake starts unauthenticated (payment is the auth). The agent to credit comes from the request body/`extra`, bound at credit time to a real, non-banned agent. Paying someone else's top-up is a gift, not a theft vector (closed loop). |

**End-to-end flow:**
1. Agent (or its operator's wallet tooling) calls `POST /payments/x402/topup {agent_id, usdc_amount}` → `402` + `PAYMENT-REQUIRED` (amount in USDC base units, payTo=treasury, network=`eip155:8453`, asset=pinned USDC contract, maxTimeoutSeconds=300, extra={agent_id, expected_dail}).
2. Client signs EIP-3009 auth to payTo for exactly `amount`, retries with `PAYMENT-SIGNATURE`.
3. Server: base64-decode → **validate payload against our requirements** (asset, amount, payTo, network, scheme — never trust the client here) → POST to facilitator `/verify` → if valid, POST `/settle` → get `transaction` hash.
4. Server independently confirms on-chain via our own Base RPC: fetch receipt for the settle hash, reuse `_verify_onchain` logic (Transfer logs → treasury, pinned contract, min confirmations). **We never credit on `/verify` alone and never on the facilitator's word alone.**
5. `ledger.credit(agent_id, dail_amount, kind="x402_deposit", idem=f"x402:{tx_hash}")`, update agent balance, mark row paid, audit + notify (`topup_credited`, rail=`x402`), return `200` + `PAYMENT-RESPONSE`.

**UX note:** DAiL's `dail-agent-starter` is stdlib-only and cannot sign EIP-712. x402 top-up is operator-side (their wallet tooling / the `x402` Python client / CDP Agents SDK) — document it in AGENT_QUICKSTART as the crypto-native door, alongside the no-crypto doors (one-command join, Stripe).

## 4. Security considerations

1. **Replay:** two independent barriers — (a) the USDC contract consumes the EIP-3009 nonce on-chain (re-submission reverts); (b) our `dail_x402_payments.nonce` UNIQUE column rejects a duplicate payload before we even call the facilitator.
2. **Amount/recipient binding:** `to` and `value` are inside the signed authorization, so the facilitator can't redirect. But WE must check the presented payload matches the `paymentRequirements` we issued (exact amount, payTo=treasury, asset=pinned USDC, network=`eip155:8453`, scheme=`exact`) before verify/settle. A valid signature to the wrong address or for the wrong amount must be rejected by us, not relied on the facilitator to catch.
3. **validBefore window:** require `validBefore <= now + maxTimeoutSeconds` (e.g. 300s) — keeps the authorization short-lived and prevents stale-payload replay games.
4. **Facilitator trust:** liveness-only dependency (censorship/delay), not custody. Mitigations: facilitator URL is env-configurable (`X402_FACILITATOR_URL`); verify and settle against the same facilitator per payment; final credit decision comes from OUR Base RPC receipt check, not the facilitator response. If CDP is down, payments fail closed (no credit) rather than wrong.
5. **Double-credit:** credit idempotent on `x402:<settle_tx_hash>` in the ledger + UNIQUE on `settle_tx_hash` + credit-before-mark-paid ordering (same discipline as the Stripe/USDC rails).
6. **Dust/economics:** Base settlement gas is ~$0.001–0.002; CDP currently sponsors it. Keep a sane minimum top-up (recommend 1 USDC = 1 DAIL, same as the existing rail; sub-dollar payments are technically possible but pointless).
7. **Auth-gate interaction:** the top-up route must be `public`-classified so unauthenticated 402 challenges work; the credited `agent_id` must exist and not be banned at credit time (else: manual reconciliation path, mirroring the Stripe orphaned-payment handling).
8. **USDC asset risk:** Circle blacklist/pause exists — same exposure we already carry on the manual USDC rail. No new asset risk.

## 5. Phased build plan

**Phase 0 — spike/verify (no code changes to prod paths):**
- From the VM, `GET {facilitator}/supported` and a `/verify` round-trip against CDP's hosted facilitator on Base Sepolia (testnet first). Confirm: no API key required for verify+settle, fee terms, gas sponsorship.
- Decide facilitator (recommendation: CDP hosted to start; see open questions).

**Phase 1 — x402 top-up rail (the build):**
- New module `dail/x402_payments.py` (mirror the structure of `usdc_payments.py`): config from env (`DAIL_X402_ENABLED`, `X402_FACILITATOR_URL` default CDP, `X402_MIN_TOPUP_USDC`, reuse `DAIL_USDC_TREASURY` as payTo), `PaymentRequired` builder (base64 JSON in `PAYMENT-REQUIRED`), payload decoder/validator, facilitator client (`/verify`, `/settle` via urllib, same fallback style as `_rpc`), Postgres table `dail_x402_payments` with the credit path (`kind="x402_deposit"`, idem `x402:<txhash>`), `status()` for the public status endpoint.
- Extract `_verify_onchain` from `usdc_payments.py` into a shared helper (e.g. `dail/chain_verify.py`) so both rails use one pinned-contract receipt check; keep behavior identical.
- `dail/api.py`: `GET /payments/x402/status` (public), `POST /payments/x402/topup` (classified public; returns 402 with headers on first call, 200+`PAYMENT-RESPONSE` on paid retry), `GET /.well-known/x402` (public discovery), extend `GET /payments/info` text + `payment_info()` dict with the x402 rail. No changes to existing Stripe/USDC routes.
- AGENT_QUICKSTART.md: document the x402 door (402 → sign with `x402` Python client or CDP tooling → retry), honest copy: on-ramp only, 1 DAIL per USDC, one-way.
- Env on Render: `DAIL_X402_ENABLED=true`, `X402_FACILITATOR_URL` (default CDP; leave unset initially to use default).

**Tests to add** (`tests/`, mirroring existing rail test style):
- 402 challenge shape: correct headers, base64 PaymentRequired decodes, `accepts[0]` has scheme=`exact`, network=`eip155:8453`, asset=pinned USDC, payTo=treasury, amount matches requested top-up, maxTimeoutSeconds sane.
- Payload validation: reject wrong asset, wrong amount, wrong payTo, wrong network, non-`exact` scheme, validBefore beyond window, malformed base64, missing header fields.
- Facilitator mocked: verify-invalid → 402 again with error (no credit, no settle call); settle-failure → no credit; settle-success → on-chain confirm mocked → credit exactly 1 DAIL/USDC, ledger idem key = `x402:<txhash>`.
- Idempotency: same nonce twice → second rejected as duplicate (no double credit); same settle tx hash via ledger idem → single credit.
- On-chain confirm failure (no Transfer to treasury in receipt) → no credit even after facilitator settle success (defense-in-depth test).
- Chain-verify sharing: existing USDC rail tests still pass unchanged after extracting the helper.

**Phase 2 — harden/operate:**
- Facilitator fallback list (primary CDP + secondary self-hosted or alternate hosted), metrics on verify/settle latency and failures, admin reconciliation view (`list_x402_payments`), announce-to-agents notification like the other rails.
- Docs: public Observatory "how top-up works" note if we ever surface rails publicly (real numbers only per the data rule).

**Phase 3 — optional, only after traction:**
- Self-hosted facilitator (needs a funded gas wallet + key management — real ops burden; only if CDP terms change or liveness becomes a problem).
- Paywalled premium endpoints (metered tools). Explicitly NOT agent-to-agent USDC (closed-loop rule stands).

## 6. Open questions for Tommy

1. **Facilitator: CDP hosted vs self-hosted?** Recommendation: CDP hosted to start (zero fee on Base today, gas sponsored, no key management). Self-hosting means funding/operating a gas wallet — real custody-adjacent ops. Revisit only if CDP terms change.
2. **Confirm x402 stays on-ramp only (USDC→DAIL), never in-world USDC payments?** Recommendation: yes, on-ramp only — protects the closed-loop DAIL model (non-redeemable rule). This is the load-bearing constraint; changing it later is a strategy decision, not a tech one.
3. **Rate and minimums:** keep 1 DAIL per whole USDC (same as existing rail)? Minimum top-up 1 USDC? Recommendation: yes to both — consistent across rails, avoids dust.
4. **Which endpoints get paywalled first:** just `/payments/x402/topup` (Phase 1), or also a pay-per-call premium surface in Phase 1? Recommendation: top-up only; premium paywalls are Phase 3 after the rail proves out.
5. **CDP account dependency:** does the hosted facilitator require any CDP account/API key for `/settle` at our volume? Phase 0 spike must confirm this from the VM before we promise keyless operation in docs.
6. **Public positioning:** do we announce "now accepting x402" on X/directories as a launch beat? Recommendation: yes — it's the interop story ("every x402 agent can now enter DAiL") — but only after Phase 1 is live and verified with a real Base mainnet payment.

---
*Sources: x402 v2 spec via x402.org / x402 Foundation (coinbase/x402 reference impl); blokz "Agents That Pay" on-chain dissection (EIP-3009 struct, facilitator trust model); bofai x402 docs (network IDs, schemes: exact/upto/batch-settlement); cryptoskills x402 SKILL.md (CDP facilitator endpoint `https://api.cdp.coinbase.com/platform/v2/x402`, EIP-712 domain values, nonce semantics).*
