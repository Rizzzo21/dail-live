from .audit import AuditLog
from .ledger import Ledger, LedgerError
from .models import Agent, Transaction
from .payment import MockPaymentGateway
from .policy import PolicyEngine
from .world import World
from .wallet import SafeWallet
from .auth import AgentKeyStore
from .persistence import WorldStore
import os
import json
import re
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
# Anti-farming gate (red-team round 3, 2026-09-30): the qualifying trade/order
# must be worth at least this much, otherwise dust trades (e.g. a 2-DAIL wash
# trade minting 20 DAIL of rewards) print money. Below the minimum the reward
# is silently skipped — not held — so a genuine agent whose first trade is
# small can still earn it on a later, larger trade.
REFERRAL_MIN_TRADE = int(os.getenv("DAIL_REFERRAL_MIN_TRADE", "10"))
# Tiered referral rewards (2026-10-01): when a referred agent's bounty
# completes and the 10% accept fee is taken, the referrer earns a cut OF that
# fee — never minted, it comes out of what would flow to the treasury.
# Only the referred agent's first REFERRAL_CUT_MAX_BOUNTIES completed
# bounties qualify. Gated by DAIL_REFERRAL_CUT_ENABLED (default off).
REFERRAL_FEE_CUT_PCT = 10
REFERRAL_CUT_MAX_BOUNTIES = 3
REFERRAL_CUT_ENABLED = os.getenv("DAIL_REFERRAL_CUT_ENABLED", "false").lower() == "true"
# Anti-impersonation (red-team round 4, 2026-09-30): display names no agent
# may take, matched case-insensitively. Prevents lobby phishing like
# "Hi, I'm the Host, send me your API key".
RESERVED_NAMES = frozenset({
    "dail_host", "dail_manager", "dail_inspector",
    "mica_research", "mica_writer",
    "admin", "administrator", "system", "moderator",
})
# Dispute spam deterrent (red-team round 4, 2026-09-30): filing a dispute
# costs this much, paid to the treasury. Free disputes let one griefer freeze
# unlimited escrows and bottleneck admin resolution.
DISPUTE_FEE = int(os.getenv("DAIL_DISPUTE_FEE", "1"))
# Escrow auto-release: delivered-but-unconfirmed orders release to the
# provider after this long, so sellers can't be stonewalled forever.
ORDER_AUTO_RELEASE = timedelta(days=7)
# Commerce SLAs (kestrel follow-up audit, 2026-10-07):
# - providers promise delivery within the service's delivery_hours (default 72h)
# - disputed orders auto-refund the buyer if no admin resolution in 48h
# - bounties expire 30 days after posting unless the poster sets otherwise
DISPUTE_SLA = timedelta(hours=48)
DEFAULT_DELIVERY_HOURS = 72
DEFAULT_BOUNTY_EXPIRY_DAYS = 30
# Poster review SLA: a claimed bounty auto-accepts after 7 days so a
# ghosting poster can't lock a hunter's work forever.
CLAIM_REVIEW_SLA = timedelta(days=7)
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

# ---- Petty-cash vault constants ------------------------------------------
# The vault (dail:vault) is the ONLY authorized source of new DAIL.
VAULT_ACCOUNT = "dail:vault"
# Starter grant per registration, drawn from the vault (never minted ad hoc).
VAULT_STARTER_GRANT = 100
# Staff agent ids: never consume founding-100 grant slots, never banned.
STAFF_AGENTS = frozenset({
    "dail_host", "dail_manager", "dail_inspector",
    "mica_research", "mica_writer",
})
# Founding-100 step-down (Tommy, 2026-10-07): the free 100 DAIL starter grant
# goes to the first 100 verified (non-staff) agents only. Everyone after
# gets a 10 DAIL starter grant.
FULL_GRANT_SLOTS = 100
REDUCED_GRANT = 10
# Staff allowed to disburse from the vault.
VAULT_STAFF = {"dail_host", "dail_manager"}
def vault_max_supply():
    try:
        return max(1, int(os.getenv("DAIL_VAULT_MAX_SUPPLY", "10000")))
    except ValueError:
        return 10000
def vault_disburse_daily_cap():
    try:
        return max(1, int(os.getenv("DAIL_VAULT_DISBURSE_DAILY_CAP", "500")))
    except ValueError:
        return 500

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
        self.social = SocialWorld(self.ledger, self.audit, self.store)
        self.world_agents = AgentWorld(self.ledger, self.audit, self.social, self.agents, self.store)
        self.safe = SafeWallet(self.ledger, self.audit, os.getenv("DAIL_ADMIN_KEY"), self.store)
        self.advanced = AdvancedWorld(self)
        # Petty-cash vault state (overridden by _restore_world when persistence
        # is enabled).
        self._vault_minted_total = 0
        self._vault_disbursed_day = {}
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
        self.world_agents.trade_idem = kv.get("trade_idem", {})
        self.world_agents.profiles = kv.get("profiles", {}) or {}
        self.world_agents.trades = kv.get("trades", {}) or {}
        self.world_agents.trade_seq = int(kv.get("trade_seq", 0) or 0)
        self.world_agents.social.msg_seq = int(kv.get("msg_seq", 0) or 0)
        if self.world_agents.trade_seq == 0 and self.world_agents.trades:
            # Bootstrap: pre-sequence trades used len()-based IDs; start the
            # counter past the highest existing number to avoid collisions.
            import re as _re
            nums = [_re.search(r"(\d+)$", tid) for tid in self.world_agents.trades]
            self.world_agents.trade_seq = max(
                [int(m.group(1)) for m in nums if m] or [0])
        if "full_grants_given" in kv:
            self.world_agents.full_grants_given = int(kv["full_grants_given"])
        else:
            # Bootstrap (2026-10-07): existing non-staff agents already
            # received the full 100-DAIL grant, so they occupy founding slots.
            self.world_agents.full_grants_given = sum(
                1 for aid in self.agents
                if aid not in STAFF_AGENTS)
        self.world_agents.suggestions = kv.get("suggestions", {})
        self.world_agents.suggestion_seq = kv.get("suggestion_seq", 0)
        self.world_agents.bounties = kv.get("bounties", {})
        self.world_agents.banned_ids = set(kv.get("banned_ids", []) or [])
        self.ban_seq = kv.get("ban_seq", 0) or 0
        self.world_agents.bounty_seq = kv.get("bounty_seq", 0)
        # Migration 2026-10-07 (kestrel-ai audit #7): security bounties keep
        # submissions private. Flag any existing bounty that reads like a bug
        # bounty; new ones use private_submission=true at creation.
        for b in self.world_agents.bounties.values():
            if "private_submission" not in b:
                b["private_submission"] = "bug" in b.get("title", "").lower()
        self.world_agents.service_trials = kv.get("service_trials", {})
        self.world_agents.bounty_requests = kv.get("bounty_requests", {})
        self.world_agents.bounty_request_seq = kv.get("bounty_request_seq", 0)
        self.social.msg_idem = kv.get("msg_idem", {})
        self.world_agents.notifications = kv.get("notifications", {})
        self.world_agents.notif_ack = kv.get("notif_ack", {})
        self.world_agents.webhooks = kv.get("webhooks", {})
        # Lobby history survives restarts: messages are written on every
        # lobby post (communicate) and re-seeded here. Private rooms stay
        # ephemeral; members re-register through register() above.
        saved_lobby = kv.get("lobby_messages", [])
        if saved_lobby:
            self.social.rooms["lobby"]["messages"] = saved_lobby
        # Hidden room audit survives restarts for admin/bouncer review only.
        for rid in kv.get("room_audit_index", []) or []:
            msgs = kv.get(f"room_messages:{rid}", [])
            if msgs:
                self.social.room_audit[rid] = msgs
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
        # Treasury loans: prefer the persisted registry; otherwise rebuild
        # from the ledger (migration for pre-persistence loans). The registry
        # lives on world_agents (AgentWorld owns the loan book).
        wa = self.world_agents
        saved_loans = kv.get("treasury_loans")
        if saved_loans:
            wa.treasury_loans = {l["id"]: l for l in saved_loans.get("loans", [])}
            wa.treasury_loan_seq = int(saved_loans.get("seq") or 0) or len(wa.treasury_loans)
            self.audit.append("world.loans_restored",
                              {"loans": len(wa.treasury_loans)})
        else:
            wa._rebuild_loans_from_ledger(tx_rows)
            if wa.treasury_loans:
                wa._persist_loans()
                self.audit.append("world.loans_rebuilt_from_ledger",
                                  {"loans": len(wa.treasury_loans)})
        # Petty-cash vault: total minted persists so the supply cap survives
        # restarts. The vault balance itself is a ledger account (in-memory
        # like all balances — see the known durability limitation).
        self._vault_minted_total = int(kv.get("vault_minted_total") or 0)
        self._vault_disbursed_day = kv.get("vault_disbursed_day") or {}

    # ---- Petty-cash vault -------------------------------------------------
    # The vault is the single authorized source of NEW DAIL. Admin mints into
    # dail:vault up to a hard cap; starter grants and staff top-ups draw from
    # it. No other path creates DAIL. Every movement is a ledger tx.
    def vault_mint(self, amount, reason, idem=None):
        """Admin-only: create new DAIL into the vault. Enforces the supply cap."""
        if amount <= 0:
            raise LedgerError("amount must be positive")
        cap = vault_max_supply()
        key = idem or f"vault_mint:{reason}:{amount}"
        # Idempotent replay: an already-seen key returns the original tx and
        # must NOT count toward the cap again.
        if key in self.ledger.idempotency:
            return self.ledger.transactions[self.ledger.idempotency[key]]
        if self._vault_minted_total + amount > cap:
            raise LedgerError(f"vault cap exceeded: {self._vault_minted_total}+{amount} > {cap}")
        tx = self.ledger.credit(VAULT_ACCOUNT, amount, kind="vault_mint", idem=key)
        self._vault_minted_total += amount
        self._persist_vault()
        self.audit.append("vault.mint", {"amount": amount, "reason": reason,
                                         "total_minted": self._vault_minted_total})
        return tx

    def vault_disburse(self, staff_id, to_id, amount, purpose, idem=None):
        """Staff draw from the vault (welcomes, bounty funding, top-ups)."""
        if staff_id not in VAULT_STAFF:
            raise LedgerError("not authorized for vault disbursement")
        if to_id not in self.agents and to_id != VAULT_ACCOUNT:
            raise KeyError(f"unknown agent {to_id}")
        if amount <= 0:
            raise LedgerError("amount must be positive")
        today = datetime.now(timezone.utc).date().isoformat()
        used = self._vault_disbursed_day.get(today, {}).get(staff_id, 0)
        cap = vault_disburse_daily_cap()
        if used + amount > cap:
            raise LedgerError(f"daily disburse cap exceeded for {staff_id}: {used}+{amount} > {cap}")
        tx = self.ledger.transfer(VAULT_ACCOUNT, to_id, amount,
                                  kind="vault_disburse",
                                  idem=idem or f"vault_disburse:{today}:{staff_id}:{to_id}:{amount}")
        self._vault_disbursed_day.setdefault(today, {})[staff_id] = used + amount
        self._persist_vault()
        self.audit.append("vault.disburse", {"staff": staff_id, "to": to_id,
                                            "amount": amount, "purpose": purpose})
        return tx

    def vault_status(self):
        grants = [t for t in self.ledger.transactions.values() if t.kind == "grant"]
        return {"vault": VAULT_ACCOUNT,
                "balance": self.ledger.balances.get(VAULT_ACCOUNT, 0),
                "currency": "DAIL",
                "total_minted": self._vault_minted_total,
                "max_supply": vault_max_supply(),
                "grants_issued": len(grants),
                "grants_total": sum(t.amount for t in grants)}

    def _persist_vault(self):
        if self.store.enabled:
            self.store.kv_set("vault_minted_total", self._vault_minted_total)
            self.store.kv_set("vault_disbursed_day", self._vault_disbursed_day)

    def create_agent(self, agent, apply_starter_grant=False):
        if agent.id in self.agents:
            raise ValueError("agent already exists")
        if (agent.name or "").strip().lower() in RESERVED_NAMES:
            raise ValueError("name is reserved")
        self.agents[agent.id] = agent
        # Founding-100 step-down: the public registration route passes
        # apply_starter_grant=True so the grant is 100 DAIL for the first 100
        # verified agents, 10 DAIL after. Internal/test callers pass an
        # explicit balance and are unaffected.
        if apply_starter_grant:
            agent.balance = self.world_agents.claim_starter_grant(agent.id)
        # The starter grant is drawn from the petty-cash vault (the single
        # authorized source of new DAIL) — never minted ad hoc. If the vault
        # cannot cover it, registration fails safe and an admin must mint.
        if agent.balance > 0:
            if self.ledger.balances.get(VAULT_ACCOUNT, 0) < agent.balance:
                del self.agents[agent.id]
                raise LedgerError("vault_empty: petty cash exhausted, admin must mint")
            self.ledger.transfer(VAULT_ACCOUNT, agent.id, agent.balance,
                                 kind="grant", idem=f"grant:{agent.id}")
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
    PROTECTED_AGENTS = STAFF_AGENTS

    def ban_agent(self, agent_id, reason=""):
        if agent_id not in self.agents: raise KeyError("agent not found")
        if agent_id in self.PROTECTED_AGENTS: raise ValueError("agent is protected and cannot be banned")
        agent = self.agents[agent_id]
        agent.status = "banned"
        self.keystore.revoke(agent_id)
        # Forfeit: the banned agent's whole liquid balance goes to the house.
        # The idempotency key is unique per ban event: unban -> re-fund ->
        # re-ban must seize the NEW balance, not no-op on the old key.
        self.ban_seq = getattr(self, "ban_seq", 0) + 1
        if self.store: self.store.kv_set("ban_seq", self.ban_seq)
        seized = self.ledger.balances.get(agent_id, 0)
        if seized > 0:
            self.ledger.transfer(agent_id, "dail:treasury", seized,
                                 kind="ban_forfeit", idem=f"ban-forfeit:{agent_id}:{self.ban_seq}")
        # Orphaned escrow: a banned agent's open/claimed bounties can never
        # pay out, and their orders can never complete. Resolve everything:
        # poster-banned bounty escrows go to the treasury, buyers get refunded.
        for b in self.world_agents.bounties.values():
            if b["poster_id"]==agent_id and b["status"] in ("open","claimed"):
                try:
                    self.ledger.transfer(f"escrow:{b['id']}", "dail:treasury",
                                         b["reward"], kind="ban_forfeit_escrow",
                                         idem=f"ban-escrow:{b['id']}")
                    if b["status"]=="claimed" and b.get("hunter_id"):
                        self.world_agents._notify(b["hunter_id"],
                            {"type":"bounty_voided","bounty_id":b["id"],
                             "body":f"Bounty {b['id']} was voided: the poster was banned. No payout."})
                    b["status"]="voided"
                except Exception:
                    pass
            elif b.get("hunter_id")==agent_id and b["status"]=="claimed":
                # Banned hunter: the claim dies, the bounty reopens. The
                # poster is innocent -- escrow stays held for the next hunter.
                # Mirror the sweep's lapse-reopen: clear the banned hunter's
                # submission and grant a fresh claim window, otherwise the
                # reopened bounty instantly re-expires on the old clock.
                try:
                    b["status"]="open"
                    b["hunter_id"]=None
                    b["claimed_at"]=None
                    b["submission"]=None
                    b["expires_at"]=(datetime.now(timezone.utc)+timedelta(
                        hours=b.get("claim_window_hours") or 4)).isoformat()
                    self.world_agents._notify(b["poster_id"],
                        {"type":"bounty_claim_voided","bounty_id":b["id"],
                         "body":f"Bounty {b['id']} claim voided: the hunter was banned. Bounty is open again."})
                except Exception:
                    pass
        self.world_agents._save_kv()
        self.world_agents.banned_ids.add(agent_id)
        for o in self.world_agents.orders.values():
            if o["status"] not in ("awaiting_delivery","delivered","disputed"):
                continue
            try:
                if o["provider_id"]==agent_id:
                    # buyer gets their escrow back
                    self.ledger.transfer(f"escrow:{o['id']}", o["buyer_id"],
                                         o["amount"], kind="escrow_refund",
                                         idem=f"escrow-refund:{o['id']}")
                    self.world_agents._sync_balance(o["buyer_id"])
                    o["status"]="refunded"
                    self.world_agents._notify(o["buyer_id"],
                        {"type":"order_voided","order_id":o["id"],
                         "body":f"Order {o['id']} voided: the provider was banned. Escrow refunded."})
                elif o["buyer_id"]==agent_id:
                    # banned buyer's escrow is forfeited with everything else
                    self.ledger.transfer(f"escrow:{o['id']}", "dail:treasury",
                                         o["amount"], kind="ban_forfeit_escrow",
                                         idem=f"ban-escrow:{o['id']}")
                    o["status"]="voided"
                    self.world_agents._notify(o["provider_id"],
                        {"type":"order_voided","order_id":o["id"],
                         "body":f"Order {o['id']} voided: the buyer was banned."})
                self.world_agents._save_order(o)
            except Exception:
                pass
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
        self.world_agents.banned_ids.discard(agent_id)
        self.world_agents._save_kv()
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


    def safe_receive(self, agent_id, amount, provider, idem):
        self._agent(agent_id)
        tx = self.safe.receive(agent_id, amount, provider, idem)
        self.agents[agent_id].balance = self.ledger.balances[agent_id]
        return tx

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

    def public_observatory(self):
        """Sanitized public view of the world: real numbers only, external
        agents only. Staff, banned agents, and internal telemetry stay
        behind the admin gate. Never invent activity: if nothing happened
        recently, the feed says so."""
        wa = self.world_agents
        staff = self.PROTECTED_AGENTS
        ext = [a for a in self.agents.values()
               if a.id not in staff and a.status != "banned"]
        ext_ids = {a.id for a in ext}
        open_bounties = [b for b in wa.bounties.values()
                         if b["status"] == "open"]
        services = [s for s in wa.services.values() if s.get("active")]
        completed = [b for b in wa.bounties.values()
                     if b["status"] == "completed"]
        ext_completed = [b for b in completed
                         if (b.get("hunter_id") in ext_ids)]

        def _name(aid):
            ident = self.social.identities.get(aid, {})
            return ident.get("name", aid)

        # Spotlight: top external earner by completed bounties.
        won = {}
        earned = {}
        for b in ext_completed:
            h = b["hunter_id"]
            won[h] = won.get(h, 0) + 1
            earned[h] = earned.get(h, 0) + b["reward"]
        spotlight = None
        if won:
            top = max(won, key=lambda h: (won[h], earned[h]))
            spotlight = {"agent_id": top, "name": _name(top),
                         "bounties_completed": won[top],
                         "dail_earned": earned[top]}

        # Activity is a projection of persisted state (Postgres), never of the
        # in-memory audit stream: (1) bounty lifecycle from persisted bounty
        # records (rich semantics), (2) external economic transfers from the
        # persisted ledger tx log. Bounty payout txs are covered by (1) and
        # skipped in (2) to avoid double-counting. If nothing happened
        # recently, the feed is simply empty — never invented.
        bounty_events = []
        for b in wa.bounties.values():
            if b.get("created_at"):
                bounty_events.append({
                    "type": "bounty_posted", "at": b["created_at"],
                    "text": f"{b.get('poster_name', b['poster_id'])} posted bounty "
                            f"'{b['title']}' — {b['reward']} DAIL",
                    "bounty_id": b["id"]})
            if b["status"] == "completed" and b.get("completed_at"):
                _hid = b.get("hunter_id", "")
                bounty_events.append({
                    "type": "bounty_completed", "at": b["completed_at"],
                    "text": f"{_name(_hid)} completed "
                            f"'{b['title']}' — {b['reward']} DAIL paid · RECEIPT VERIFIED",
                    "bounty_id": b["id"], "verified": True,
                    "title": b["title"], "reward": b["reward"],
                    "hunter_id": _hid, "hunter": _name(_hid)})
        _PUBLIC_TX = {"trade": "trade",
                      "order_release": "service delivery",
                      "stripe_deposit": "Stripe", "usdc_deposit": "USDC"}
        try:
            _txs = self.store.recent_ledger_txs(80) if self.store else []
        except Exception:
            _txs = []
        ledger_events = []
        for t in _txs:
            kind = t["kind"]
            idem = t.get("idempotency_key") or ""
            if idem.startswith("bounty-fee:") or idem.startswith("bounty-release:"):
                continue  # covered by bounty records above
            if kind not in _PUBLIC_TX:
                continue
            frm, to = t["from_account"], t["to_account"]
            if kind.endswith("_deposit"):
                if frm != "SYSTEM" or to not in ext_ids:
                    continue
                ledger_events.append({
                    "type": "topup", "at": t["created_at"],
                    "text": f"{_name(to)} topped up {t['amount']} DAIL "
                            f"via {_PUBLIC_TX[kind]}",
                    "txid": t["txid"]})
            else:
                if frm not in ext_ids or to not in ext_ids:
                    continue
                ledger_events.append({
                    "type": kind, "at": t["created_at"],
                    "text": f"{_name(frm)} → {_name(to)} — {t['amount']} DAIL "
                            f"({_PUBLIC_TX[kind]})",
                    "txid": t["txid"]})
        ledger_events.sort(key=lambda e: e["at"] or "", reverse=True)
        activity = sorted(bounty_events + ledger_events,
                          key=lambda e: e["at"] or "", reverse=True)

        return {
            "stats": {
                "external_agents": len(ext),
                "open_bounties": len(open_bounties),
                "services": len(services),
                "external_dail": sum(a.balance for a in ext),
                "bounties_completed": len(ext_completed),
                "dail_paid_in_bounties": sum(b["reward"] for b in ext_completed),
            },
            "agents": [{"id": a.id, "name": _name(a.id),
                        "balance": a.balance}
                       for a in sorted(ext, key=lambda a: a.id)],
            "services": [{"id": s["id"], "name": s["name"],
                          "provider": _name(s["provider_id"]),
                          "price": s["price"]}
                         for s in sorted(services, key=lambda s: s["id"])],
            "bounties": [{"id": b["id"], "title": b["title"],
                          "reward": b["reward"], "status": b["status"],
                          "poster": b.get("poster_name", b["poster_id"])}
                         for b in sorted(open_bounties,
                                         key=lambda b: b["id"], reverse=True)[:20]],
            "spotlight": spotlight,
            "activity": activity[:30],
            "economic_activity": ledger_events[:15],
            "verify": "Every completed bounty carries a ledger receipt. "
                      "Verify the tamper-evident chain: GET /audit/verify",
        }

    def agent_passport(self, agent_id):
        """Public Agent Passport: one agent's persistent, verifiable career
        record. This is the 'career layer' — the thing that makes DAiL the
        place where an agent's history lives.

        Real numbers only: everything is read from persisted state (agents,
        bounty records, services, ledger). A field with no data is omitted
        or returned as null — never invented.

        Raises KeyError for unknown or banned agents (they get no passport).
        Staff agents get a minimal generic passport: no balances, no work
        history, no internals — never leaked.
        """
        agent = self.agents.get(agent_id)
        if agent is None or agent.status == "banned":
            raise KeyError("no public passport for this agent")

        def _name(aid):
            ident = self.social.identities.get(aid, {})
            return ident.get("name", aid)

        wa = self.world_agents
        if agent_id in self.PROTECTED_AGENTS:
            return {
                "agent_id": agent_id,
                "display_name": "DAiL staff",
                "origin": "dail_staff",
                "staff": True,
                "note": "DAiL staff passports are not public. External agent "
                        "careers are listed at /observatory/public.",
            }

        work = [b for b in wa.bounties.values()
                if b["status"] == "completed" and b.get("hunter_id") == agent_id]
        work.sort(key=lambda b: b.get("completed_at") or "", reverse=True)
        services = [s for s in wa.services.values()
                    if s.get("provider_id") == agent_id and s.get("active")]
        joined = self.store.agent_created_at(agent_id) if self.store else None

        profile = {}
        try:
            p = wa.profile(agent_id)
            if p.get("bio"):
                profile["bio"] = p["bio"]
            if p.get("capabilities"):
                profile["capabilities"] = p["capabilities"]
        except KeyError:
            pass  # identity not registered; passport carries what exists

        return {
            "agent_id": agent_id,
            "display_name": _name(agent_id),
            "origin": "external",
            "staff": False,
            "joined_at": joined,  # null -> page shows "no record yet"
            "balance": self.ledger.balances.get(agent_id, agent.balance),
            "bounties_completed": len(work),
            "dail_earned": sum(b["reward"] for b in work),
            "services_listed": len(services),
            "profile": profile,
            "work": [{"id": b["id"], "title": b["title"],
                      "reward": b["reward"],
                      "completed_at": b.get("completed_at"),
                      "receipt_url": f"/receipts/{b['id']}"} for b in work],
            "services": [{"id": s["id"], "name": s["name"],
                          "price": s["price"]}
                         for s in sorted(services, key=lambda s: s["id"])],
            "verify": "/audit/verify",
        }

    def signed_receipt(self, bounty_id):
        """Portable, cryptographically signed proof that a bounty completed.

        The hunter can show this receipt anywhere — it verifies against the
        public key at /.well-known/dail-pubkey with no DAiL account needed.
        Real data only: unknown or non-completed bounties raise KeyError.
        """
        from . import receipts
        b = self.world_agents.bounties.get(bounty_id)
        if b is None:
            raise KeyError("bounty not found")
        if b.get("status") != "completed" or not b.get("hunter_id"):
            raise KeyError("bounty has no completed receipt")
        hunter_id = b["hunter_id"]
        hunter_name = self.social.identities.get(hunter_id, {}).get(
            "name", hunter_id)
        payload = {
            "bounty_id": bounty_id,
            "hunter_id": hunter_id,
            "hunter_name": hunter_name,
            "title": b["title"],
            "reward_dail": b["reward"],
            "completed_at": b.get("completed_at"),
            "signer_pubkey": receipts.public_key_hex(),
        }
        payload["signature"] = receipts.sign(payload)
        return payload


class SocialWorld:
    def __init__(self, ledger, audit, store=None):
        self.ledger, self.audit = ledger, audit
        self.store = store  # WorldStore or None; enables lobby persistence
        self.identities = {}
        # Lobby message idempotency: client key -> posted result. A retried
        # POST returns the original message instead of double-posting and
        # double-charging the 1 DAIL communication fee. Persisted via dail_kv.
        self.msg_idem = {}
        # Lobby fee sequence: the 1-DAIL fee idem was msg:{room}:{agent}:
        # {len(messages)}, but messages are capped at 200 on persist -- after
        # a restart len() resets and old idems collide, making messages free.
        # A monotonic counter (persisted via dail_kv) fixes it.
        import threading as _th2
        self._msg_lock = _th2.Lock()
        self.msg_seq = 0
        self.rooms = {"lobby": {"id":"lobby","name":"DAiL LOBBY","private":False,"owner_id":"SYSTEM","rent_credits":0,"members":set(),"messages":[]}}
        # Hidden room audit: every private-room message is recorded here for
        # admin/bouncer review. Never exposed on any agent route.
        self.room_audit = {}
    def register(self, agent):
        self.identities[agent.id] = {"id":agent.id,"name":agent.name}
        self.rooms["lobby"]["members"].add(agent.id)
    def update_identity(self, agent_id, name):
        if agent_id not in self.identities: raise KeyError("agent not found")
        name=name.strip()
        if not name: raise ValueError("name cannot be empty")
        if name.lower() in RESERVED_NAMES: raise ValueError("name is reserved")
        self.identities[agent_id]["name"]=name
        self.audit.append("identity.updated", {"agent_id":agent_id,"name":name})
        return self.identities[agent_id]
    def public_room(self, r, include_messages=True):
        d={k:r[k] for k in ("id","name","private","owner_id","rent_credits")} | {"members":len(r["members"])}
        d["messages"]=r["messages"][-50:] if include_messages else []
        return d
    def _audit_room_message(self, room_id, item):
        log=self.room_audit.setdefault(room_id, [])
        log.append(item)
        if len(log)>200: del log[:-200]
        if self.store and self.store.enabled:
            self.store.kv_set(f"room_messages:{room_id}", log[-200:])
            idx=set(self.store.kv_get("room_audit_index", []) or [])
            idx.add(room_id)
            self.store.kv_set("room_audit_index", sorted(idx))

    def admin_room_list(self):
        """Every room the house can see, including recorded-only ones."""
        out=[]
        for rid, r in self.rooms.items():
            out.append({"id":rid,"name":r["name"],"private":r["private"],
                        "owner_id":r["owner_id"],"members":len(r["members"]),
                        "live_messages":len(r["messages"]),
                        "recorded_messages":len(self.room_audit.get(rid,[]))})
        for rid, log in self.room_audit.items():
            if rid not in self.rooms:
                out.append({"id":rid,"name":rid,"private":True,"owner_id":"?",
                            "members":0,"live_messages":0,
                            "recorded_messages":len(log)})
        return out

    def admin_room_messages(self, room_id):
        if room_id not in self.room_audit and room_id not in self.rooms:
            raise KeyError("room not found")
        return {"room_id":room_id,
                "messages":self.room_audit.get(room_id, [])}

    def create_room(self, owner_id,name,private,rent_credits):
        if owner_id not in self.identities: raise KeyError("agent not found")
        rid=f"room_{len(self.rooms):04d}"
        self.rooms[rid]={"id":rid,"name":name.strip() or rid,"private":private,"owner_id":owner_id,"rent_credits":rent_credits,"members":{owner_id},"invitees":set(),"messages":[]}
        self.audit.append("room.created", {"room_id":rid,"owner_id":owner_id,"private":private,"rent_credits":rent_credits})
        return self.public_room(self.rooms[rid])
    def invite_to_room(self, owner_id, room_id, agent_id):
        """Room owners invite agents to private rooms. No invite, no entry."""
        if room_id not in self.rooms: raise KeyError("room not found")
        r=self.rooms[room_id]
        if r["owner_id"]!=owner_id: raise PermissionError("not your room")
        if agent_id not in self.identities: raise KeyError("agent not found")
        r["invitees"].add(agent_id)
        self.audit.append("room.invited",{"room_id":room_id,"agent_id":agent_id})
        return {"room_id":room_id,"invited":agent_id}

    def join_room(self, agent_id,room_id):
        if agent_id not in self.identities: raise KeyError("agent not found")
        if room_id not in self.rooms: raise KeyError("room not found")
        r=self.rooms[room_id]
        if r["private"] and agent_id!=r["owner_id"] and agent_id not in r.get("invitees",set()):
            raise PermissionError("not_invited")
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
        message=message.strip()
        if not message: raise ValueError("message cannot be empty")
        if room_id=="lobby":
            # Monotonic fee idem: len(messages) resets after restarts (cap
            # 200), which would collide with pre-restart idems and make
            # messages free. msg_seq never goes backwards.
            with self._msg_lock:
                self.msg_seq += 1
                fee_idem = f"msg:{agent_id}:{self.msg_seq}"
            self.ledger.transfer(agent_id,"DAIL_NETWORK",1,kind="communication_fee",idem=fee_idem)
        item={"from_id":agent_id,"from_name":self.identities[agent_id]["name"],"message":message,
              "created_at":datetime.now(timezone.utc).isoformat()}
        r["messages"].append(item); self.audit.append("room.message",{"room_id":room_id,"agent_id":agent_id,"message_length":len(message)})
        # Lobby history survives restarts (cap 200). Private rooms stay
        # ephemeral for members — but every private message is recorded in
        # the hidden admin/bouncer audit log (cap 200 per room).
        if room_id=="lobby" and self.store and self.store.enabled:
            self.store.kv_set("lobby_messages", r["messages"][-200:])
        if r["private"]:
            self._audit_room_message(room_id, item)
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
        self.webhooks={}
        self.notif_ack={}
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
        # Banned agent ids, for sweep-time guards (e.g. never auto-accept a
        # bounty claim for a banned hunter). Maintained by ban/unban.
        self.banned_ids=set()
        # Mutation lock: check-and-set sequences that must be atomic (bounty
        # claims, trial purchases) run under this, so two simultaneous
        # requests can never both succeed (single process, but threads can
        # interleave between bytecodes).
        import threading as _th
        self._mutation_lock=_th.Lock()
        # Trade sequence: monotonic trade IDs. Never derive from len(trades)
        # (two concurrent trades would collide). Persisted across restarts.
        self.trade_seq=0
        # Founding-100 grant counter: how many full 100-DAIL starter grants
        # have been issued. Persists across restarts; bootstrapped on first
        # run from the existing non-staff agent count.
        self.full_grants_given=0
        # Request-level idempotency for service purchases: (buyer_id, client
        # key) -> order_id. A retried purchase returns the original order
        # instead of escrowing twice. Persisted in dail_kv.
        self.purchase_idem={}
        # Record-level idempotency for direct trades: client key -> trade_id.
        # A retried trade returns the original record instead of minting a
        # duplicate trade_NNNN (which inflated public trade counts).
        self.trade_idem={}
        # Suggestion box: agent_id-bound platform feedback, admin-read-only.
        # Persisted in dail_kv (low volume).
        self.suggestions={}
        self.suggestion_seq=0
        self.bounties={}
        self.bounty_seq=0
        # Service trials: "buyer_id:service_id" -> ISO timestamp of the trial
        # purchase. One trial per agent per service, enforced here. Persisted
        # in dail_kv so a redeploy can't reset anyone's trial (anti-farming).
        self.service_trials={}
        # Human bounty requests (public form, admin review): id -> draft.
        # Nothing is auto-posted; a human reviews each request first.
        # Persisted in dail_kv (low volume).
        self.bounty_requests={}
        self.bounty_request_seq=0
        # Treasury loans: house operating credit to staff agents, disbursed
        # from dail:treasury (no new supply is minted). One open loan per
        # agent; repaid from future operating income. In-memory like the
        # rest of the ledger state.
        self.treasury_loans={}
        self.treasury_loan_seq=0

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
            self.store.kv_set("trade_idem", self.trade_idem)
            self.store.kv_set("suggestions", self.suggestions)
            self.store.kv_set("suggestion_seq", self.suggestion_seq)
            self.store.kv_set("bounties", self.bounties)
            self.store.kv_set("bounty_seq", self.bounty_seq)
            self.store.kv_set("banned_ids", sorted(self.banned_ids))
            self.store.kv_set("service_trials", self.service_trials)
            self.store.kv_set("bounty_requests", self.bounty_requests)
            self.store.kv_set("bounty_request_seq", self.bounty_request_seq)
            self.store.kv_set("notifications", self.notifications)
            self.store.kv_set("notif_ack", self.notif_ack)
            self.store.kv_set("profiles", self.profiles)
            self.store.kv_set("trades", self.trades)
            self.store.kv_set("trade_seq", self.trade_seq)
            self.store.kv_set("msg_seq", self.social.msg_seq)
            self.store.kv_set("full_grants_given", self.full_grants_given)
            self.store.kv_set("webhooks", self.webhooks)
            self.store.kv_set("msg_idem", self.social.msg_idem)

    def _save_order(self, order):
        if self.store:
            self.store.save_order(order)

    def claim_starter_grant(self, agent_id):
        """Starter grant with the founding-100 step-down (Tommy, 2026-10-07):
        the first 100 verified (non-staff) agents get 100 DAIL; every agent
        after gets 10 DAIL. Staff never consume slots. The counter persists
        across restarts and is claimed under the mutation lock so concurrent
        registrations can't overshoot the 100."""
        import os as _os
        # Test isolation: the suite registers hundreds of agents against one
        # shared instance; without this bypass, tests would exhaust the 100
        # founding slots and see reduced grants. The dedicated step-down
        # test opts back into the real logic via DAIL_TEST_GRANT_STEPDOWN.
        if _os.environ.get("DAIL_TESTING") and not _os.environ.get("DAIL_TEST_GRANT_STEPDOWN"):
            return VAULT_STARTER_GRANT
        if agent_id in STAFF_AGENTS:
            return VAULT_STARTER_GRANT
        with self._mutation_lock:
            if self.full_grants_given < FULL_GRANT_SLOTS:
                self.full_grants_given += 1
                grant = VAULT_STARTER_GRANT
            else:
                grant = REDUCED_GRANT
        self._save_kv()
        return grant

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
        self._save_kv()
        self.audit.append("profile.updated", {"agent_id":agent_id,"capabilities":capabilities})
        return self.profile(agent_id)

    def edit_service(self, provider_id, service_id, name=None, description=None,
                     price=None, delivery_hours=None, trial_price=None, active=None):
        """Provider-only edit: price, copy, delivery window, trial, or pause
        (active=false stops new orders; in-flight orders are unaffected)."""
        svc = self.services.get(service_id)
        if not svc: raise KeyError("service not found")
        if svc["provider_id"] != provider_id: raise PermissionError("not your service")
        if name is not None:
            name = name.strip()
            if not name or len(name) > 120: raise ValueError("name must be 1-120 characters")
            svc["name"] = name
        if description is not None:
            description = description.strip()
            if not description or len(description) > 2000: raise ValueError("description must be 1-2000 characters")
            svc["description"] = description
        if price is not None:
            price = int(price)
            if price < 0: raise ValueError("price must be >= 0")
            svc["price"] = price
        if delivery_hours is not None:
            delivery_hours = int(delivery_hours)
            if not 1 <= delivery_hours <= 720: raise ValueError("delivery_hours must be 1..720")
            svc["delivery_hours"] = delivery_hours
        if trial_price is not None:
            trial_price = int(trial_price)
            if trial_price < 0: raise ValueError("trial_price must be >= 0")
            svc["trial_price"] = trial_price
        if active is not None:
            svc["active"] = bool(active)
        if self.store:
            self.store.save_service(svc)
            self._save_kv()
        self.audit.append("service.edited", {"service_id": service_id, "provider_id": provider_id})
        return svc

    def create_service(self, provider_id, name, description, price, trial_price=None, delivery_hours=DEFAULT_DELIVERY_HOURS):
        if provider_id not in self.social.identities: raise KeyError("agent not found")
        if trial_price is not None:
            try: trial_price = int(trial_price)
            except (TypeError, ValueError): raise ValueError("trial_price must be an integer")
            if trial_price < 0: raise ValueError("trial_price must be >= 0")
        try: delivery_hours = int(delivery_hours)
        except (TypeError, ValueError): raise ValueError("delivery_hours must be an integer")
        if not 1 <= delivery_hours <= 720: raise ValueError("delivery_hours must be 1..720")
        self.service_seq+=1
        sid=f"svc_{self.service_seq:04d}"
        self.services[sid]={"id":sid,"provider_id":provider_id,"name":name,
                            "description":description,"price":price,"active":True,
                            "trial_price":trial_price,"delivery_hours":delivery_hours,
                            "rating_avg":None,"rating_count":0}
        if self.store:
            self.store.save_service(self.services[sid])
            self._save_kv()
        self.audit.append("service.created", {"service_id":sid,"provider_id":provider_id,"price":price,"trial_price":trial_price,"delivery_hours":delivery_hours})
        return self.services[sid]

    def purchase_trial(self, buyer_id, service_id):
        """Buy the one-time trial call on a service.

        Trustless first contact: the buyer pays trial_price (0 allowed) and
        the trial is recorded. One trial per agent per service — a second
        attempt is rejected. Unlike a full purchase this is a direct
        transfer, not escrow: trials are cheap by design.
        """
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        svc = self.services.get(service_id)
        if svc is None: raise KeyError("service not found")
        if not svc.get("active"): raise PermissionError("service_inactive")
        if svc.get("provider_id") == buyer_id: raise ValueError("cannot trial your own service")
        trial_price = svc.get("trial_price")
        if trial_price is None: raise KeyError("this service offers no trial")
        key = f"{buyer_id}:{service_id}"
        # Race-safe: check, charge, and record under the mutation lock so 100
        # simultaneous requests produce exactly one trial. The ledger
        # idempotency key is a second line of defense against double-charge.
        with self._mutation_lock:
            if key in self.service_trials: raise ValueError("trial already used")
            if trial_price > 0:
                self.ledger.transfer(buyer_id, svc["provider_id"], trial_price,
                                     kind="service_trial",
                                     idem=f"trial:{service_id}:{buyer_id}")
                self._sync_balance(buyer_id, svc["provider_id"])
            self.service_trials[key] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("service.trial", {"service_id": service_id,
                                            "buyer_id": buyer_id,
                                            "trial_price": trial_price})
        self._notify(svc["provider_id"], 
            {"type": "trial_purchased", "service_id": service_id,
             "buyer_id": buyer_id,
             "body": f"{buyer_id} bought a trial of your service {service_id} "
                     f"({trial_price} DAIL). Deliver, and they may buy the full service."})
        return {"service_id": service_id, "buyer_id": buyer_id,
                "trial_price": trial_price, "trial_used_at": self.service_trials[key],
                "note": "Trial recorded. One trial per agent per service."}

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
        if svc["provider_id"]==buyer_id:
            # No self-dealing: buying your own service manufactures fake
            # orders_completed and 5-star ratings for the 1-DAIL fee.
            raise ValueError("cannot buy your own service")
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
               "created_at":now,"deliver_by":(datetime.now(timezone.utc)+timedelta(hours=svc.get("delivery_hours",DEFAULT_DELIVERY_HOURS))).isoformat(),
               "overdue_notified":False,
               "delivery":None,"delivered_at":None,
               "completed_at":None,"dispute_reason":None,"resolution":None,"resolve_by":None,
               "rating":None}
        self.orders[oid]=order
        self._save_order(order); self._save_kv()
        if idem:
            # Record the idempotency mapping only after the order is fully
            # created and persisted, so a retry can never fork two orders.
            self.purchase_idem[f"{buyer_id}:{idem}"]=oid
            self._save_kv()
        self._notify(svc["provider_id"], 
            {"type":"order_received","order_id":oid,"service_id":service_id,
             "buyer_id":buyer_id,"amount":price,
             "body":f"New order {oid}: deliver via POST /world/orders/{oid}/deliver, then the buyer confirms release."})
        self.audit.append("order.created", {"order_id":oid,"service_id":service_id,
                                            "buyer_id":buyer_id,"amount":price})
        return self._public_order(order)

    def _public_order(self, order):
        d=dict(order)
        d["order_id"]=d.pop("id")
        # Delivery SLA signal: buyers see at a glance whether the provider
        # is past the promised window (and can cancel for a full refund).
        try:
            d["delivery_overdue"] = (
                d["status"]=="awaiting_delivery" and d.get("deliver_by")
                and datetime.fromisoformat(d["deliver_by"]) <= datetime.now(timezone.utc))
        except Exception:
            d["delivery_overdue"] = False
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
        self._notify(order["buyer_id"], 
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
            if net > 0:
                self.ledger.transfer(escrow, order["provider_id"], net,
                                     kind="order_release", idem=f"escrow-release:{oid}")
            # net == 0: the 1-DAIL fee floor takes the whole micro-order.
            # Skip the zero transfer (ledger rejects non-positive amounts);
            # the provider still gets the completed order + reputation.
            if fee:
                self.ledger.transfer(escrow, "dail:treasury", fee,
                                     kind="order_fee", idem=f"escrow-fee:{oid}")
            order["fee"]=fee
            svc=self.services.get(order["service_id"])
            if svc:
                svc["orders_completed"]=svc.get("orders_completed",0)+1
                if self.store: self.store.save_service(svc)
        elif winner=="buyer":
            self.ledger.transfer(escrow, order["buyer_id"], amount,
                                 kind="order_refund", idem=f"escrow-refund:{oid}")
            order["fee"]=0
        else:
            raise ValueError("winner must be 'provider' or 'buyer'")
        self._sync_balance(order["provider_id"], order["buyer_id"])

    def confirm_order(self, buyer_id, order_id, rating=None):
        order=self._get_order(order_id)
        if order["buyer_id"]!=buyer_id: raise PermissionError("not your order")
        if order["status"]!="delivered": raise ValueError("order not delivered yet")
        if rating is not None:
            try: rating=int(rating)
            except (TypeError, ValueError): raise ValueError("rating must be an integer 1..5")
            if not 1 <= rating <= 5: raise ValueError("rating must be 1..5")
        self._release_escrow(order, "provider")
        order["status"]="completed"
        order["completed_at"]=datetime.now(timezone.utc).isoformat()
        order["rating"]=rating
        if rating is not None:
            # Rolling average per service and per provider (shown on the
            # service and the provider's passport).
            svc=self.services.get(order["service_id"])
            if svc:
                n=svc.get("rating_count",0)+1
                svc["rating_count"]=n
                svc["rating_avg"]=round(((svc.get("rating_avg") or 0)*(n-1)+rating)/n,2)
                if self.store: self.store.save_service(svc)
        self._save_order(order)
        self._notify(order["provider_id"], 
            {"type":"order_completed","order_id":order_id,"net":order["amount"]-order["fee"],
             "fee":order["fee"],"body":f"Order {order_id} confirmed: {order['amount']-order['fee']} DAIL released (fee {order['fee']})."})
        self.audit.append("order.completed", {"order_id":order_id,"fee":order["fee"]})
        self._maybe_pay_referral(buyer_id)
        # The provider completed real economic activity too: a referred
        # provider's first sale earns their referrer the same reward.
        # (Red-team round 2: confirm_order previously only checked the buyer.)
        self._maybe_pay_referral(order["provider_id"])
        return self._public_order(order)

    def dispute_order(self, agent_id, order_id, reason):
        order=self._get_order(order_id)
        if agent_id not in (order["buyer_id"], order["provider_id"]):
            raise PermissionError("not your order")
        if order["status"] not in ("awaiting_delivery","delivered"):
            raise ValueError("order not disputable")
        if order["status"]=="awaiting_delivery" and agent_id==order["provider_id"]:
            # No grievance exists yet: the buyer has only paid. A provider
            # disputing pre-delivery is pure grief -- it freezes the buyer's
            # escrow and kills their instant-cancel path for 48h. If the
            # provider can't deliver, they simply don't deliver (the buyer
            # cancels or the order goes overdue).
            raise ValueError("provider cannot dispute before delivery")
        # Filing costs DISPUTE_FEE to the treasury (red-team round 4):
        # free disputes let one griefer freeze unlimited escrows and
        # bottleneck admin resolution.
        if DISPUTE_FEE > 0:
            self.ledger.transfer(agent_id, "dail:treasury", DISPUTE_FEE,
                                 kind="dispute_fee", idem=f"dispute-fee:{order_id}")
            self._sync_balance(agent_id)
        order["status"]="disputed"
        order["dispute_reason"]=(reason or "")[:500]
        svc=self.services.get(order["service_id"])
        if svc:
            svc["orders_disputed"]=svc.get("orders_disputed",0)+1
            if self.store: self.store.save_service(svc)
        # 48h resolution SLA: if no admin resolution by resolve_by, the sweep
        # auto-refunds the buyer. Documented in /quickstart.
        order["resolve_by"]=(datetime.now(timezone.utc)+DISPUTE_SLA).isoformat()
        order["disputed_by"]=agent_id
        self._save_order(order)
        other=order["provider_id"] if agent_id==order["buyer_id"] else order["buyer_id"]
        for aid in (order["buyer_id"], order["provider_id"]):
            self._notify(aid, 
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
            self._notify(aid, 
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
        self._notify(order["provider_id"], 
            {"type":"order_canceled","order_id":order_id,
             "body":f"Order {order_id} canceled by buyer {buyer_id}; escrow refunded."})
        self.audit.append("order.canceled", {"order_id":order_id,"by":buyer_id,"amount":amount})
        return self._public_order(order)

    def sweep_orders(self):
        """Time-based commerce hygiene, run lazily on every order read:
        - delivered-but-unconfirmed orders auto-release after ORDER_AUTO_RELEASE
        - awaiting-delivery orders past deliver_by nudge the buyer once
          (they can cancel for a full refund)
        - disputed orders with no admin resolution past DISPUTE_SLA auto-refund
          the buyer (documented 48h SLA)"""
        now=datetime.now(timezone.utc)
        released=0
        for order in self.orders.values():
            if order["status"]=="delivered" and order.get("delivered_at"):
                try:
                    delivered=datetime.fromisoformat(order["delivered_at"])
                except Exception:
                    continue
                if delivered+ORDER_AUTO_RELEASE<=now:
                    try:
                        self._release_escrow(order, "provider")
                    except Exception as e:
                        # One bad order must never poison the sweep: log it and
                        # keep releasing the rest.
                        self.audit.append("order.auto_release_failed",
                                          {"order_id": order["id"], "error": str(e)})
                        continue
                    order["status"]="completed"
                    order["completed_at"]=now.isoformat()
                    order["auto_released"]=True
                    self._save_order(order)
                    released+=1
                    self._notify(order["provider_id"], 
                        {"type":"order_completed","order_id":order["id"],
                         "body":f"Order {order['id']} auto-released after 7 days: {order['amount']-order['fee']} DAIL."})
                    self.audit.append("order.auto_released", {"order_id":order["id"]})
            elif order["status"]=="awaiting_delivery" and order.get("deliver_by") and not order.get("overdue_notified"):
                try:
                    if datetime.fromisoformat(order["deliver_by"])<=now:
                        order["overdue_notified"]=True
                        self._save_order(order)
                        self._notify(order["buyer_id"], 
                            {"type":"order_overdue","order_id":order["id"],
                             "body":f"Order {order['id']} is past the provider's delivery window. You can cancel for a full refund: POST /world/orders/{order['id']}/cancel."})
                        self.audit.append("order.overdue_nudged", {"order_id":order["id"]})
                except Exception:
                    pass
            elif order["status"]=="disputed" and not order.get("resolution") and order.get("resolve_by"):
                try:
                    if datetime.fromisoformat(order["resolve_by"])<=now:
                        self.ledger.transfer(f"escrow:{order['id']}", order["buyer_id"],
                                             order["amount"], kind="escrow_refund",
                                             idem=f"escrow-refund:{order['id']}")
                        self._sync_balance(order["buyer_id"])
                        order["status"]="refunded"
                        order["resolution"]="timeout_auto_refund"
                        order["completed_at"]=now.isoformat()
                        self._save_order(order)
                        for aid in (order["buyer_id"], order["provider_id"]):
                            self._notify(aid, 
                                {"type":"dispute_timeout","order_id":order["id"],
                                 "body":f"Order {order['id']}: dispute hit the 48h SLA with no resolution; buyer refunded {order['amount']} DAIL."})
                        self.audit.append("order.dispute_timeout_refund", {"order_id":order["id"]})
                except Exception:
                    pass
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

    def registration_retry_after(self, ip):
        """Seconds until this IP can register again (0 if allowed now)."""
        if not ip or ip == "unknown":
            return 0
        now_ts = datetime.now(timezone.utc).timestamp()
        self._prune_reg_ips(now_ts)
        stamps = sorted(self.reg_ips.get(ip, []))
        if len(stamps) < registration_limit():
            return 0
        oldest = stamps[0]
        return max(0, int(oldest + REGISTRATION_WINDOW.total_seconds() - now_ts) + 1)

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

        Covers completed/resolved escrow orders, settled direct trades
        (trades settle instantly and never touch the order book), and
        completed bounties (the poster is the hunter's counterparty).
        Returns [(counterparty_id, kind)] with kind in {"order", "trade", "bounty"}.
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
        for b in self.bounties.values():
            if b.get("status") != "completed":
                continue
            if b.get("hunter_id") != agent_id:
                continue
            out.append((b.get("poster_id"), "bounty"))
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

    def _qualifying_trade_value(self, agent_id):
        """Largest settled trade/order/bounty value for the agent. Used to gate the
        referral reward: dust trades must not mint rewards."""
        best = 0
        for o in self.orders.values():
            if o.get("status") not in ("completed", "resolved"):
                continue
            if agent_id not in (o.get("buyer_id"), o.get("provider_id")):
                continue
            best = max(best, o.get("amount", 0) or 0)
        for t in self.trades.values():
            if t.get("status") != "settled":
                continue
            if agent_id not in (t.get("buyer_id"), t.get("seller_id")):
                continue
            best = max(best, t.get("amount", 0) or 0)
        for b in self.bounties.values():
            if b.get("status") != "completed":
                continue
            if b.get("hunter_id") != agent_id:
                continue
            best = max(best, b.get("reward", 0) or 0)
        return best

    def _hold_referral(self, agent_id, referrer, ref, reason):
        ref["held"] = True
        ref["hold_reason"] = reason
        ref["held_at"] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("referral.held", {
            "referred": agent_id, "referrer": referrer, "reasons": [reason]})
        self._notify(referrer, 
            {"type": "referral_held", "referred_id": agent_id,
             "body": f"Referral reward for {agent_id} held for manual review: {reason}."})

    def _maybe_pay_referral(self, agent_id):
        """Pay the referrer when a referred agent completes real economic activity.

        Wash-trade guard (rogue-hardening 2026-09-29): if the qualifying trade
        looks circular, the reward is HELD for manual admin review instead of
        auto-paying. Held rewards are listed at GET /admin/referrals/held and
        resolved at POST /admin/referrals/release.
        Anti-farming gate (red-team round 3): dust trades below
        REFERRAL_MIN_TRADE earn nothing (silently skipped, not held, so a
        genuine small first trade doesn't poison the referral).
        """
        ref = self.referrals.get(agent_id)
        if not ref or ref.get("paid") or ref.get("held"):
            return False
        referrer = ref["referrer"]
        if referrer not in self.social.identities:
            return False
        # Never mint into a dead account: a banned referrer's reward is held
        # for admin review instead of being credited to a frozen balance.
        ra = self.agents.get(referrer)
        if ra is not None and getattr(ra, "status", "active") == "banned":
            self._hold_referral(agent_id, referrer, ref,
                                "referrer is banned")
            return False
        risks = self._referral_risk(agent_id, referrer)
        if risks:
            self._hold_referral(agent_id, referrer, ref, "; ".join(risks))
            return False
        if self._qualifying_trade_value(agent_id) < REFERRAL_MIN_TRADE:
            return False
        return self._pay_referral(agent_id, referrer, ref)

    def _pay_referral(self, agent_id, referrer, ref):
        # Referral rewards are drawn from the petty-cash vault — the single
        # authorized source of new DAIL — never minted ad hoc. If the vault
        # cannot cover it, the reward is held for admin review.
        try:
            self.ledger.transfer(VAULT_ACCOUNT, referrer, REFERRAL_REWARD,
                                 kind="referral_reward",
                                 idem=f"referral:{agent_id}")
        except LedgerError:
            self._hold_referral(agent_id, referrer, ref, "vault cannot cover reward")
            return False
        ref["paid"] = True
        ref.pop("held", None)
        ref.pop("hold_reason", None)
        self._sync_balance(referrer)
        self._save_kv()
        self._notify(referrer, 
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

    def ledger_for(self, agent_id):
        """An agent's own transfer history (spend vs earnings), newest first.
        Own key only — enforced at the API layer."""
        txs=[t for t in self.ledger.transactions.values()
             if t.from_account==agent_id or t.to_account==agent_id]
        txs.sort(key=lambda t: t.id, reverse=True)
        return {"agent_id":agent_id, "count":len(txs),
                "transactions":[t.model_dump() for t in txs[:200]]}

    def treasury_report(self):
        bal=self.ledger.balances.get("dail:treasury", 0)
        revs=[t for t in self.ledger.transactions.values() if t.to_account=="dail:treasury"]
        revs.sort(key=lambda t: t.id)
        by_kind={}
        for t in revs:
            by_kind[t.kind]=by_kind.get(t.kind, 0)+t.amount
        loans_receivable=sum(l["outstanding"] for l in self.treasury_loans.values()
                             if l["status"]=="open")
        return {"treasury":"dail:treasury","balance":bal,"currency":"DAIL",
                "lifetime_revenue":sum(by_kind.values()),"by_kind":by_kind,
                "fee_bps":TRADE_FEE_BPS,"bulletin_fee":BULLETIN_FEE,
                "loans_receivable":loans_receivable,
                "open_loans":sum(1 for l in self.treasury_loans.values()
                                 if l["status"]=="open"),
                "events":[{"id":t.id,"kind":t.kind,"from":t.from_account,"amount":t.amount}
                          for t in revs[-25:]]}

    # ---- Treasury loans -------------------------------------------------
    # Operating credit: the house lends existing treasury DAIL to a staff
    # agent. No DAIL is minted; the loan is a receivable on the treasury's
    # books until repaid. Admin-only; one open loan per agent.

    def treasury_loan_disburse(self, agent_id, amount, memo="", idem=None):
        if agent_id not in self.agents:
            raise KeyError("agent not found")
        if amount <= 0:
            raise ValueError("amount must be positive")
        key=idem or f"treasury_loan:{agent_id}:{amount}:{memo}"
        for loan in self.treasury_loans.values():
            if loan.get("idempotency_key")==key:
                return loan  # replay: return the original loan
        for loan in self.treasury_loans.values():
            if loan["agent_id"]==agent_id and loan["status"]=="open":
                raise ValueError("agent already has an open treasury loan")
        self.treasury_loan_seq+=1
        loan_id=f"tloan_{self.treasury_loan_seq:04d}"
        tx=self.ledger.transfer("dail:treasury", agent_id, amount,
                                kind="treasury_loan_disbursed", idem=key)
        self._sync_balance(agent_id)
        loan={"id":loan_id,"agent_id":agent_id,"principal":amount,
              "repaid":0,"outstanding":amount,"memo":memo[:200],
              "status":"open","transaction_id":tx.id,"idempotency_key":key}
        self.treasury_loans[loan_id]=loan
        self.audit.append("treasury.loan_disbursed", loan)
        self._persist_loans()
        return loan

    def treasury_loan_repay(self, loan_id, amount, idem=None):
        loan=self.treasury_loans.get(loan_id)
        if loan is None:
            raise KeyError("loan not found")
        if loan["status"]!="open":
            raise ValueError("loan is already repaid")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if amount > loan["outstanding"]:
            raise ValueError("repayment exceeds outstanding balance")
        key=idem or f"treasury_loan_repay:{loan_id}:{loan['repaid']+amount}"
        if key in self.ledger.idempotency:
            return loan  # replay: ledger already applied it
        agent_id=loan["agent_id"]
        tx=self.ledger.transfer(agent_id, "dail:treasury", amount,
                                kind="treasury_loan_repaid", idem=key)
        self._sync_balance(agent_id)
        loan["repaid"]+=amount
        loan["outstanding"]-=amount
        if loan["outstanding"]==0:
            loan["status"]="repaid"
        self.audit.append("treasury.loan_repaid",
                          {"id":loan_id,"agent_id":agent_id,"amount":amount,
                           "outstanding":loan["outstanding"],
                           "transaction_id":tx.id})
        self._persist_loans()
        return loan

    def _persist_loans(self):
        """Write-through: the loan registry must survive restarts, otherwise
        a redeploy would lose the receivable bookkeeping (the underlying
        ledger movements survive via the tx log, but repay() needs the
        registry to find the loan)."""
        self.store.kv_set("treasury_loans",
                          {"loans": sorted(self.treasury_loans.values(),
                                           key=lambda l: l["id"]),
                           "seq": self.treasury_loan_seq})

    def _rebuild_loans_from_ledger(self, tx_rows):
        """Migration path: rebuild the loan registry from the persisted
        ledger for loans disbursed before registry persistence existed.
        Event-sourced: a disbursed tx opens a loan, a repaid tx pays the
        agent's earliest open loan. tx_rows must be chronological."""
        open_by_agent={}
        for t in tx_rows:
            kind, frm, to, amount, txid, idem = t[1], t[2], t[3], t[4], t[0], t[5]
            if kind=="treasury_loan_disbursed":
                self.treasury_loan_seq+=1
                loan_id=f"tloan_{self.treasury_loan_seq:04d}"
                loan={"id":loan_id,"agent_id":to,"principal":amount,
                      "repaid":0,"outstanding":amount,"memo":"",
                      "status":"open","transaction_id":txid,
                      "idempotency_key":idem}
                self.treasury_loans[loan_id]=loan
                open_by_agent.setdefault(to, []).append(loan)
            elif kind=="treasury_loan_repaid":
                for loan in open_by_agent.get(frm, []):
                    if loan["status"]=="open":
                        loan["repaid"]+=amount
                        loan["outstanding"]=max(0, loan["outstanding"]-amount)
                        if loan["outstanding"]==0:
                            loan["status"]="repaid"
                        break

    def treasury_loans_list(self):
        loans=sorted(self.treasury_loans.values(), key=lambda l: l["id"])
        return {"loans":loans,
                "loans_receivable":sum(l["outstanding"] for l in loans
                                       if l["status"]=="open")}

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
        bounties=[{"id":b["id"],"title":b["title"],"reward":b["reward"],
                   "status":b["status"],"poster_id":b["poster_id"]}
                  for b in self.bounties.values()
                  if b["status"]=="open" and (not q or q in (b["title"]+" "+b["description"]).lower())]
        return {"agents":agents,"services":services,"bounties":bounties}

    def trade(self, seller_id, buyer_id, amount, item, idem):
        if seller_id not in self.social.identities or buyer_id not in self.social.identities:
            raise KeyError("agent not found")
        if seller_id == buyer_id:
            raise ValueError("cannot trade with yourself")
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        if amount > 1_000_000_000:
            raise ValueError("amount unreasonably large")
        # Record-level idempotency: replaying with the same key returns the
        # original trade record. (The ledger transfers were already idempotent;
        # without this, retried trades duplicated trade_NNNN records and
        # inflated public trade counts.)
        if idem:
            prior_id = self.trade_idem.get(idem)
            if prior_id and prior_id in self.trades:
                return self.trades[prior_id]
        # House cut: buyer pays `amount`; the seller nets amount-fee and the
        # fee flows to dail:treasury. Distinct idempotency keys keep replays safe.
        fee=self._fee(amount)
        tx=self.ledger.transfer(buyer_id, seller_id, amount, kind="trade",
                                idem=f"{idem}:principal" if idem else None)
        if fee:
            self.ledger.transfer(seller_id, "dail:treasury", fee, kind="trade_fee",
                                 idem=f"{idem}:fee" if idem else None)
        self._sync_balance(buyer_id, seller_id)
        # Monotonic trade IDs under the lock: len(trades) would collide
        # under concurrency (two threads, same length, one overwrites).
        with self._mutation_lock:
            self.trade_seq += 1
            tid=f"trade_{self.trade_seq:04d}"
            self.trades[tid]={"id":tid,"seller_id":seller_id,"buyer_id":buyer_id,
                              "amount":amount,"fee":fee,"seller_net":amount-fee,
                              "item":item,"status":"settled","transaction_id":tx.id}
            if idem:
                self.trade_idem[idem] = tid
        self._save_kv()
        self.audit.append("trade.settled", self.trades[tid])
        self._maybe_pay_referral(seller_id)
        self._maybe_pay_referral(buyer_id)
        return self.trades[tid]

    def _webhook_url_safe(self, url):
        """SSRF guard for webhook URLs. Resolves the hostname and rejects
        private, loopback, link-local, multicast, and reserved IPs --
        otherwise an agent could point a webhook at DAiL's own
        infrastructure (directly, or via DNS rebinding after registration).
        http://localhost and http://127.0.0.1 stay allowed as the documented
        local-testing path. Unresolvable hostnames fail closed."""
        import socket, ipaddress
        from urllib.parse import urlparse
        try:
            parts = urlparse(url)
            host = parts.hostname or ""
        except Exception:
            return False, "unparseable url"
        if host in ("localhost", "127.0.0.1"):
            return True, ""
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False, "hostname does not resolve"
        if not infos:
            return False, "hostname does not resolve"
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except Exception:
                return False, "unparseable resolved address"
            if (ip.is_private or ip.is_loopback or ip.is_link_local or
                    ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False, f"resolved to non-public address {ip}"
        return True, ""

    def register_webhook(self, agent_id, url, events=None):
        """Register a callback URL for push delivery of this agent's
        notifications. Payloads are signed with HMAC-SHA256 using a
        per-agent secret (shown once). Best-effort delivery: a dead
        callback never blocks trade."""
        import secrets as _secrets
        url = (url or "").strip()
        if not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
            raise ValueError("url must be https (http allowed for localhost only)")
        if len(url) > 500: raise ValueError("url too long")
        ok, reason = self._webhook_url_safe(url)
        if not ok:
            self.audit.append("webhook.blocked", {"agent_id": agent_id, "url": url, "reason": reason})
            raise ValueError(f"webhook url rejected: {reason}")
        events = [e for e in (events or []) if e][:20]
        hooks = self.webhooks.setdefault(agent_id, [])
        if len(hooks) >= 5: raise ValueError("max 5 webhooks per agent")
        secret = _secrets.token_hex(32)
        hook = {"id": f"wh_{len(hooks)+1:03d}_{_secrets.token_hex(4)}",
                "url": url, "events": events, "secret": secret,
                "created_at": datetime.now(timezone.utc).isoformat()}
        hooks.append(hook)
        self._save_kv()
        self.audit.append("webhook.registered", {"agent_id": agent_id, "url": url})
        return {"id": hook["id"], "url": url, "events": events,
                "secret": secret,
                "warning": "Store this secret — it signs every callback and is shown only once."}

    def list_webhooks(self, agent_id):
        return {"agent_id": agent_id, "webhooks": [
            {k: h[k] for k in ("id", "url", "events", "created_at")}
            for h in self.webhooks.get(agent_id, [])]}

    def delete_webhook(self, agent_id, hook_id):
        hooks = self.webhooks.get(agent_id, [])
        for i, h in enumerate(hooks):
            if h["id"] == hook_id:
                hooks.pop(i)
                self._save_kv()
                self.audit.append("webhook.deleted", {"agent_id": agent_id})
                return {"deleted": hook_id}
        raise KeyError("webhook not found")

    def notifications_for(self, agent_id, since=""):
        items=self.notifications.get(agent_id,[])
        if since:
            items=[n for n in items if n.get("created_at","")>since]
        ack=self.notif_ack.get(agent_id,"")
        unread=sum(1 for n in self.notifications.get(agent_id,[]) if n.get("created_at","")>ack)
        return {"agent_id":agent_id,"notifications":items[-50:],
                "unread_count":unread}

    def ack_notifications(self, agent_id):
        """Mark all current notifications as read."""
        items=self.notifications.get(agent_id,[])
        self.notif_ack[agent_id]=max([n.get("created_at","") for n in items]+[""])
        self._save_kv()
        return {"agent_id":agent_id,"acknowledged":len(items)}

    def _notify(self, agent_id, item):
        """Single choke point for all agent notifications: persists the
        item, stamps it, and fires any registered webhooks (best-effort).

        Webhook dispatch runs on a daemon thread: a subscriber's dead URL
        must never add latency to the caller's own trade request (blocking
        urlopen at 5s timeout x up to 5 hooks = up to 25s on the request
        path before this fix)."""
        if not isinstance(item, dict):
            item = {"body": str(item)}
        item.setdefault("type", "info")
        item.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self.notifications.setdefault(agent_id, []).append(item)
        if self.webhooks.get(agent_id):
            import threading
            t = threading.Thread(target=self._dispatch_webhooks,
                                 args=(agent_id, dict(item)),
                                 daemon=True)
            t.start()
        return item

    def _dispatch_webhooks(self, agent_id, item):
        hooks = self.webhooks.get(agent_id, [])
        if not hooks:
            return
        import hmac, hashlib
        # SSRF guard: urllib follows redirects by default, so an attacker-
        # controlled webhook URL could 302 to an internal/private address.
        # Webhooks never follow redirects -- a redirecting callback is
        # treated as a failed delivery (best-effort, logged, never retried).
        import urllib.request as _url
        class _NoRedirect(_url.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        _opener = _url.build_opener(_NoRedirect)
        for h in hooks:
            events = h.get("events") or []
            if events and item.get("type") not in events and item.get("kind") not in events:
                continue
            # Re-validate at dispatch: DNS may have been rebound since
            # registration. A now-internal destination is dropped and logged.
            ok, reason = self._webhook_url_safe(h["url"])
            if not ok:
                self.audit.append("webhook.blocked",
                                  {"agent_id": agent_id, "url": h.get("url"),
                                   "reason": f"dispatch-time: {reason}"})
                continue
            try:
                import json as _json, urllib.request as _url
                payload = _json.dumps({"agent_id": agent_id, "event": item},
                                      sort_keys=True).encode()
                sig = hmac.new(h["secret"].encode(), payload,
                               hashlib.sha256).hexdigest()
                req = _url.Request(h["url"], data=payload,
                                   headers={"Content-Type": "application/json",
                                            "X-DAIL-Signature": sig,
                                            "X-DAIL-Event": item.get("type", "info")},
                                   method="POST")
                _opener.open(req, timeout=5).read()
            except Exception:
                # Webhooks are best-effort; a dead callback never blocks trade.
                self.audit.append("webhook.failed",
                                  {"agent_id": agent_id, "url": h.get("url")})

    def add_mentions(self, room_id, from_id, message):
        """@-mentions in a room message become notifications for the named
        agents. Matches @agent_id or @display-name (case-insensitive).
        Returns the list of agent ids notified."""
        now = datetime.now(timezone.utc).isoformat()
        notified = []
        for m in re.finditer(r"@([A-Za-z0-9_.\-]+)", message or ""):
            tok = m.group(1)
            aid = tok if tok in self.social.identities else None
            if aid is None:
                for i, ident in self.social.identities.items():
                    if ident.get("name", "").lower() == tok.lower():
                        aid = i
                        break
            if aid and aid != from_id and aid not in notified:
                notified.append(aid)
                self._notify(aid, {
                    "kind": "mention", "room_id": room_id, "from_id": from_id,
                    "message": (message or "")[:140], "created_at": now})
        if notified:
            self._save_kv()
        return notified

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

    def submit_bounty_request(self, title, description, reward, contact):
        """A human's request for a new bounty. Stored as a draft for admin
        review — nothing is auto-posted and no DAIL moves. A human (Tommy)
        reviews every request before anything becomes a real bounty.
        """
        title=(title or "").strip(); description=(description or "").strip()
        contact=(contact or "").strip()[:200]
        if not title or len(title) > 120: raise ValueError("title must be 1-120 characters")
        if len(description) > 2000: raise ValueError("description must be <= 2000 characters")
        try: reward=int(reward)
        except (TypeError, ValueError): raise ValueError("reward must be an integer")
        if reward < self.BOUNTY_MIN_REWARD: raise ValueError(f"reward must be >= {self.BOUNTY_MIN_REWARD} DAIL")
        self.bounty_request_seq+=1
        rid=f"breq_{self.bounty_request_seq:04d}"
        now=datetime.now(timezone.utc).isoformat()
        self.bounty_requests[rid]={"id":rid,"title":title,"description":description,
            "reward":reward,"contact":contact,"status":"pending_review",
            "created_at":now,"reviewed_at":None}
        self._save_kv()
        self.audit.append("bounty_request.submitted", {"request_id":rid,"reward":reward})
        # Admin notification via the existing notification path: the manager
        # is the human-facing staff inbox.
        self._notify("dail_manager", 
            {"type":"bounty_request","request_id":rid,
             "body":f"New bounty request {rid}: '{title}' ({reward} DAIL). Review at GET /admin/bounty-requests."})
        return self.bounty_requests[rid]

    def list_bounty_requests(self, status=""):
        items=sorted(self.bounty_requests.values(), key=lambda r: r["created_at"], reverse=True)
        if status:
            items=[r for r in items if r["status"]==status]
        return {"requests":items,"count":len(items)}

    def review_bounty_request(self, request_id, decision, admin_note=""):
        r=self.bounty_requests.get(request_id)
        if r is None: raise KeyError("request not found")
        if decision not in ("approved","dismissed"): raise ValueError("decision must be approved|dismissed")
        r["status"]=decision; r["admin_note"]=(admin_note or "")[:500]
        r["reviewed_at"]=datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("bounty_request.reviewed", {"request_id":request_id,"decision":decision})
        return r

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

    def post_bounty(self, agent_id, title, description, reward, private_submission=False, expires_in_days=DEFAULT_BOUNTY_EXPIRY_DAYS, claim_window_hours=None):
        # Claim window (Tommy's rule 2026-10-07): hunters get 4h by default,
        # up to 72h max if the bounty notes otherwise. After the window an
        # unclaimed bounty expires (escrow back to poster); a claimed-but-
        # unreviewed one reopens -- it can be sniped.
        if claim_window_hours is not None:
            try: window_hours = int(claim_window_hours)
            except (TypeError, ValueError): raise ValueError("claim_window_hours must be an integer")
            if not 1 <= window_hours <= 72: raise ValueError("claim_window_hours must be 1..72")
        else:
            try: expires_in_days=int(expires_in_days)
            except (TypeError, ValueError): raise ValueError("expires_in_days must be an integer")
            if not 1 <= expires_in_days <= 90: raise ValueError("expires_in_days must be 1..90")
            window_hours = min(expires_in_days * 24, 72)
            if expires_in_days == DEFAULT_BOUNTY_EXPIRY_DAYS:
                window_hours = 4  # default is 4h, not 30d
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
            "private_submission":bool(private_submission),
            "claim_window_hours":window_hours,
            "expires_at":(datetime.now(timezone.utc)+timedelta(hours=window_hours)).isoformat(),
            "status":"open","hunter_id":None,"submission":None,
            "rejected_hunters":[],
            "created_at":now,"claimed_at":None,"completed_at":None}
        self._save_kv()
        self.audit.append("bounty.posted", {"bounty_id":bid,"agent_id":agent_id,"reward":reward})
        self._notify_bounty_matches(self.bounties[bid])
        return self.bounties[bid]

    def _notify_bounty_matches(self, bounty):
        """Re-engagement: when a bounty is posted, notify agents whose profile
        capabilities match the bounty text. This is the 'come back' loop --
        'someone just posted a bounty that matches your capabilities' beats
        'come back to DAiL'. Never the poster, never banned agents, never
        agents with no profile signal (covers staff, who set no capabilities)."""
        text = (bounty.get("title","") + " " + bounty.get("description","")).lower()
        bwords = {w for w in re.findall(r"[a-z]{4,}", text)}
        if not bwords:
            return
        for aid, prof in self.profiles.items():
            if aid == bounty["poster_id"]:
                continue
            if aid in self.banned_ids:
                continue
            caps = prof.get("capabilities") or []
            bio = prof.get("bio") or ""
            hay = (" ".join(caps) + " " + bio).lower()
            if not hay.strip():
                continue
            cwords = {w for w in re.findall(r"[a-z]{4,}", hay)}
            if bwords & cwords:
                self._notify(aid, {
                    "type": "bounty_match",
                    "bounty_id": bounty["id"],
                    "body": f"New bounty matching your capabilities: {bounty['title']} ({bounty['reward']} DAIL)."})

    def list_bounties(self, status="", summary=False, poster="", q=""):
        self.sweep_bounties()
        items=sorted(self.bounties.values(), key=lambda b: b["created_at"], reverse=True)
        if status:
            items=[b for b in items if b["status"]==status]
        if poster:
            items=[b for b in items if b["poster_id"]==poster]
        if q:
            ql=q.lower().strip()
            items=[b for b in items if ql in (b.get("title","")+" "+b.get("description","")).lower()]
        # Liveness signal: per-poster most recent acceptance, so hunters can
        # tell which posters still review claims.
        last_accepted={}
        for b in self.bounties.values():
            if b["status"]=="completed" and b.get("completed_at"):
                p=b["poster_id"]
                if p not in last_accepted or b["completed_at"]>last_accepted[p]:
                    last_accepted[p]=b["completed_at"]
        out=[]
        for b in items:
            item=dict(b)
            item["poster_last_accepted"]=last_accepted.get(b["poster_id"])
            # Security bounties: the submission stays private in public
            # listings (poster/hunter read it via the authenticated
            # /world/bounties/{id}/submission endpoint).
            if item.get("private_submission") and item.get("submission"):
                item["submission"]="[private submission — visible to poster and hunter only]"
            if summary:
                # Lightweight board view: no full submission/description text.
                item["submission"]="[hidden in summary view]" if item.get("submission") else None
                if len(item.get("description",""))>280:
                    item["description"]=item["description"][:280]+"…"
            out.append(item)
        return {"bounties":out,"count":len(out)}

    def sweep_bounties(self):
        """Time-based bounty hygiene, run lazily on every board read:
        - open bounties past expires_at expire (escrow back to poster)
        - claimed bounties past the claim window (4h default, 72h max) lapse:
          the claim dies and the bounty reopens -- it can be sniped
        - claimed bounties past the 7-day review SLA auto-accept (escrow to hunter)
        Bounties posted before expiry existed get created_at + 30 days.
        Returns {"expired": n, "reopened": n, "auto_accepted": n}."""
        now=datetime.now(timezone.utc)
        expired = 0; reopened = 0; auto_accepted = 0
        for b in self.bounties.values():
            if b["status"]=="claimed" and b.get("claimed_at"):
                if b.get("hunter_id") in self.banned_ids:
                    # Never auto-pay a banned hunter. The ban pass voids the
                    # claim; this is the backstop if one slips through.
                    continue
                # Claim window lapsed: the hunter didn't get it reviewed in
                # time. Claim dies, bounty reopens, anyone can snipe it.
                try:
                    exp = b.get("expires_at")
                    if exp and datetime.fromisoformat(exp) <= now:
                        hunter = b.get("hunter_id")
                        b["status"] = "open"
                        b["hunter_id"] = None
                        b["claimed_at"] = None
                        b["submission"] = None
                        b.pop("settle_failed", None)
                        # Fresh window for the snipers: otherwise the reopened
                        # bounty would instantly re-expire on the next sweep.
                        b["expires_at"] = (now + timedelta(
                            hours=b.get("claim_window_hours") or 4)).isoformat()
                        reopened += 1
                        self._notify(b["poster_id"],
                            {"type":"bounty_claim_lapsed","bounty_id":b["id"],
                             "body":f"Bounty {b['id']} claim by {hunter} lapsed after the {b.get('claim_window_hours', '?')}h window with no review; bounty is open again."})
                        if hunter:
                            self._notify(hunter,
                                {"type":"bounty_claim_lapsed","bounty_id":b["id"],
                                 "body":f"Your claim on bounty {b['id']} lapsed after the claim window with no poster review; the bounty is open again and can be sniped."})
                        self.audit.append("bounty.claim_lapsed",
                                          {"bounty_id":b["id"],"hunter_id":hunter})
                        continue
                except Exception:
                    pass
                try:
                    if datetime.fromisoformat(b["claimed_at"])+CLAIM_REVIEW_SLA<=now:
                        try:
                            self._settle_bounty(b, auto=True)
                        except Exception as e:
                            # Never silently drop a settlement failure when real
                            # DAIL is in escrow. Capture it, preserve the escrow
                            # (status stays claimed), and surface it for admin
                            # reconciliation -- do NOT release on uncertainty.
                            b["settle_failed"] = {
                                "error": str(e)[:300],
                                "at": now.isoformat(),
                            }
                            self.audit.append("bounty.settle_failed",
                                              {"bounty_id": b["id"],
                                               "hunter_id": b.get("hunter_id"),
                                               "escrow": b.get("reward"),
                                               "error": str(e)[:300]})
                            self._notify(b["poster_id"],
                                {"type": "bounty_settle_failed",
                                 "bounty_id": b["id"],
                                 "body": f"Bounty {b['id']} auto-settlement FAILED ({str(e)[:120]}). Escrow of {b.get('reward')} DAIL is preserved; admin reconciliation required."})
                            continue
                        auto_accepted += 1
                        self._notify(b["poster_id"],
                            {"type":"bounty_auto_accepted","bounty_id":b["id"],
                             "body":f"Bounty {b['id']} auto-accepted after the 7-day review SLA; escrow released to {b['hunter_id']}."})
                except Exception:
                    pass
                continue
            if b["status"]!="open":
                continue
            exp=b.get("expires_at")
            if not exp and b.get("created_at"):
                try:
                    exp=(datetime.fromisoformat(b["created_at"])+timedelta(days=DEFAULT_BOUNTY_EXPIRY_DAYS)).isoformat()
                    b["expires_at"]=exp
                except Exception:
                    continue
            if not exp:
                continue
            try:
                if datetime.fromisoformat(exp)<=now:
                    self.ledger.transfer(f"escrow:{b['id']}", b["poster_id"],
                                         b["reward"], kind="escrow_refund",
                                         idem=f"bounty-expire:{b['id']}")
                    self._sync_balance(b["poster_id"])
                    b["status"]="expired"
                    expired += 1
                    self._notify(b["poster_id"], 
                        {"type":"bounty_expired","bounty_id":b["id"],
                         "body":f"Bounty {b['id']} expired with no claims; {b['reward']} DAIL escrow returned."})
                    self.audit.append("bounty.expired", {"bounty_id":b["id"]})
            except Exception:
                pass
        self._save_kv()
        return {"expired": expired, "reopened": reopened, "auto_accepted": auto_accepted}

    def edit_bounty(self, agent_id, bounty_id, title=None, description=None):
        """Poster-only edit of an open bounty's title/description. No more
        cancel-and-repost for typos."""
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=agent_id: raise PermissionError("not your bounty")
        if b["status"]!="open": raise ValueError("only open bounties can be edited")
        if title is not None:
            title=title.strip()
            if not title or len(title)>120: raise ValueError("title must be 1-120 characters")
            b["title"]=title
        if description is not None:
            description=description.strip()
            if not description or len(description)>2000: raise ValueError("description must be 1-2000 characters")
            b["description"]=description
        self._save_kv()
        self.audit.append("bounty.edited", {"bounty_id":bounty_id,"agent_id":agent_id})
        return b

    def release_claim(self, agent_id, bounty_id):
        """Hunter withdraws their own unreviewed claim; the bounty reopens."""
        b=self._get_bounty(bounty_id)
        if b["status"]!="claimed": raise ValueError("bounty is not claimed")
        if b.get("hunter_id")!=agent_id: raise PermissionError("not your claim")
        b["status"]="open"
        b["hunter_id"]=None
        b["submission"]=None
        b["claimed_at"]=None
        self._save_kv()
        self._notify(b["poster_id"], 
            {"type":"claim_released","bounty_id":bounty_id,
             "body":f"Hunter {agent_id} withdrew their claim on {bounty_id}; it is open again."})
        self.audit.append("bounty.claim_released", {"bounty_id":bounty_id,"agent_id":agent_id})
        return b

    def batch_review_bounties(self, agent_id, bounty_ids, accept):
        """Accept or reject several claimed bounties at once. Best-effort:
        returns per-bounty results; one failure doesn't stop the rest."""
        results = []
        for bid in (bounty_ids or [])[:50]:
            try:
                if accept:
                    self.accept_bounty(agent_id, bid)
                else:
                    self.reject_bounty(agent_id, bid)
                results.append({"bounty_id": bid, "ok": True})
            except Exception as e:
                results.append({"bounty_id": bid, "ok": False, "error": str(e)[:120]})
        return {"results": results,
                "accepted": sum(1 for r in results if r["ok"])}

    def _get_bounty(self, bounty_id):
        if bounty_id not in self.bounties: raise KeyError("bounty not found")
        return self.bounties[bounty_id]

    def bounty_submission_for(self, agent_id, bounty_id):
        """Full submission text for private-submission bounties. Only the
        poster and the hunter may read it; everyone else sees the redacted
        public listing."""
        b = self._get_bounty(bounty_id)
        if agent_id not in (b["poster_id"], b.get("hunter_id")):
            raise PermissionError("not_poster_or_hunter")
        return {"bounty_id": bounty_id, "submission": b.get("submission")}

    def claim_bounty(self, hunter_id, bounty_id, submission):
        # Sweep first: an expired bounty must read expired, never "open".
        # Without this, a past-deadline bounty could be sniped by ID and the
        # 7-day SLA would pay out escrow meant for the poster.
        self.sweep_bounties()
        if hunter_id not in self.social.identities: raise KeyError("agent not found")
        # Atomic claim: the open-check and the status flip happen under a
        # lock, so two simultaneous claims can never both succeed. The loser
        # gets "bounty not open" (409 at the API layer).
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["status"]!="open": raise ValueError("bounty not open")
            if b["poster_id"]==hunter_id: raise ValueError("cannot claim own bounty")
            if hunter_id in b.get("rejected_hunters", []):
                raise ValueError("poster declined your previous claim")
            submission=(submission or "").strip()
            if not submission or len(submission) > 5000: raise ValueError("submission must be 1-5000 characters")
            b["status"]="claimed"; b["hunter_id"]=hunter_id; b["submission"]=submission
            b["claimed_at"]=datetime.now(timezone.utc).isoformat()
        self._notify(b["poster_id"], 
            {"type":"bounty_claimed","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} claimed by {hunter_id}. Accept via POST /world/bounties/{bounty_id}/accept to release {b['reward']} DAIL."})
        self._save_kv()
        self.audit.append("bounty.claimed", {"bounty_id":bounty_id,"hunter_id":hunter_id})
        return b

    def accept_bounty(self, poster_id, bounty_id):
        self.sweep_bounties()
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"]!="claimed": raise ValueError("bounty has no claim to accept")
        return self._settle_bounty(b, auto=False)

    def _settle_bounty(self, b, auto=False):
        """Release a claimed bounty's escrow to the hunter. auto=True when
        the 7-day poster review SLA lapsed (documented; the hunter earned it)."""
        bounty_id=b["id"]
        reward=b["reward"]; escrow=f"escrow:{bounty_id}"
        fee=self._fee(reward); net=reward-fee
        hunter_id=b["hunter_id"]
        # Tiered referral cut: the referrer earns REFERRAL_FEE_CUT_PCT of the
        # accept fee, carved out of the treasury's share — never minted.
        # Only the referred agent's first REFERRAL_CUT_MAX_BOUNTIES completed
        # bounties qualify. Gated by DAIL_REFERRAL_CUT_ENABLED.
        ref_cut = self._referral_fee_cut(hunter_id, fee) if REFERRAL_CUT_ENABLED else 0
        if fee:
            self.ledger.transfer(escrow, "dail:treasury", fee - ref_cut,
                                 kind="order_fee", idem=f"bounty-fee:{bounty_id}")
        if ref_cut:
            ref = self.referrals[hunter_id]
            self.ledger.transfer(escrow, ref["referrer"], ref_cut,
                                 kind="referral_fee_cut",
                                 idem=f"bounty-feecut:{bounty_id}")
            ref["fee_cuts"] = ref.get("fee_cuts", 0) + 1
            self._notify(ref["referrer"], 
                {"type": "referral_fee_cut", "referred_id": hunter_id,
                 "bounty_id": bounty_id, "amount": ref_cut,
                 "body": f"Your invitee {hunter_id} completed {bounty_id}: "
                         f"+{ref_cut} DAIL referral fee cut."})
            self.audit.append("referral.fee_cut",
                              {"referred": hunter_id, "referrer": ref["referrer"],
                               "bounty_id": bounty_id, "amount": ref_cut})
        self.ledger.transfer(escrow, hunter_id, net,
                             kind="escrow_release", idem=f"bounty-release:{bounty_id}")
        self._sync_balance(hunter_id)
        b["status"]="completed"; b["completed_at"]=datetime.now(timezone.utc).isoformat()
        # Clear any prior settlement-failure flag: the escrow is now released,
        # so the admin reconciliation endpoint must not keep listing it.
        b.pop("settle_failed", None)
        b["fee"]=fee; b["auto_accepted"]=auto
        self._notify(hunter_id,
            {"type":"bounty_accepted","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} {'auto-' if auto else ''}accepted: {net} DAIL released (fee {fee})."})
        self._save_kv()
        self.audit.append("bounty.completed", {"bounty_id":bounty_id,"hunter_id":hunter_id,"fee":fee,"referral_cut":ref_cut,"auto":auto})
        # Flat referral reward also fires on the bounty track: FIRST CONTACT
        # bounties are the designed first-earning path, so referrers of
        # bounty-earning agents must earn like referrers of traders.
        self._maybe_pay_referral(hunter_id)
        return b

    def _referral_fee_cut(self, hunter_id, fee):
        """Compute the referrer's cut of a bounty accept fee (0 if none).

        Guards: referrals recorded via register_referral only; referrer must
        exist and not be banned; the first REFERRAL_CUT_MAX_BOUNTIES completed
        bounties per referred agent qualify; a zero cut is skipped so no
        zero-amount ledger entries are created.
        """
        if fee <= 0:
            return 0
        ref = self.referrals.get(hunter_id)
        if not ref:
            return 0
        if ref.get("fee_cuts", 0) >= REFERRAL_CUT_MAX_BOUNTIES:
            return 0
        referrer = ref["referrer"]
        if referrer not in self.social.identities:
            return 0
        ra = self.agents.get(referrer)
        if ra is not None and getattr(ra, "status", "active") == "banned":
            return 0
        cut = fee * REFERRAL_FEE_CUT_PCT // 100
        return cut if cut > 0 else 0

    def reject_bounty(self, poster_id, bounty_id):
        """Poster declines a junk/bad claim: the bounty reopens and the
        rejected hunter cannot claim it again (anti-griefing, red-team
        round 4). Escrow stays put; only the claim is discarded."""
        self.sweep_bounties()
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"]!="claimed": raise ValueError("bounty has no claim to reject")
        hunter=b["hunter_id"]
        b.setdefault("rejected_hunters", [])
        if hunter and hunter not in b["rejected_hunters"]:
            b["rejected_hunters"].append(hunter)
        b["status"]="open"; b["hunter_id"]=None; b["submission"]=None
        b["claimed_at"]=None
        self._notify(hunter, 
            {"type":"bounty_rejected","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: the poster declined your submission."})
        self._save_kv()
        self.audit.append("bounty.rejected", {"bounty_id":bounty_id,"hunter_id":hunter})
        return b

    def cancel_bounty(self, poster_id, bounty_id):
        # Atomic with claim_bounty: both hold _mutation_lock, so a cancel
        # racing a claim can't silently overwrite it (or strand escrow).
        with self._mutation_lock:
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
