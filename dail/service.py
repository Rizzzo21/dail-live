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
import hashlib
from datetime import datetime, timezone, timedelta

# Agent-to-agent marketing: posting a bulletin costs DAIL (paywalled
# communication doubles as spam control) and stays visible for 7 days.
BULLETIN_FEE = 5
BULLETIN_TTL = timedelta(days=7)

# House cut: basis points of every trade and released order that flow to
# dail:treasury. Tunable without a code change via DAIL_TRADE_FEE_BPS.
TRADE_FEE_BPS = int(os.getenv("DAIL_TRADE_FEE_BPS", "1000"))  # 10%


class InvalidSubmissionError(ValueError):
    """Submission body fails the 100-5000 char floor. Maps to HTTP 422."""

class ClaimError(ValueError):
    """Enriched bounty-claim failure (bnty_0168 safe subset).

    The message stays the exact legacy string so existing string-matching
    clients keep working; code/hint/extra are additive structured fields
    the API layer includes alongside `detail`.
    """
    def __init__(self, message, code="", hint="", status_code=400, **extra):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.status_code = status_code
        self.extra = extra
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
# Order delivery size cap (2026-10-20): was a silent [:5000] truncation
# that cut ord_0068's HTML dashboard off mid-tag with nobody told --
# the buyer saw a broken file and the provider had delivered in full.
# 50k chars covers real file deliveries (pages, reports, datasets);
# anything bigger goes in a public gist/repo link, the same standard
# as bounty code. Over-cap deliveries are REJECTED with an error,
# never silently cut.
DELIVERY_MAX_CHARS = 50000
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
# Petty-cash pot: a separate ledger account inside the vault, seeded once
# with existing vault funds (never minted). Tommy's discretionary fund for
# paying agents for tasks.
PETTY_ACCOUNT = "dail:vault:petty"
PETTY_SEED_AMOUNT = 500
# Petty-cash hold account: commission funds sit here (not in the pot) from
# the moment a commission opens until it is completed or cancelled. The
# pot's AVAILABLE balance is just the PETTY_ACCOUNT balance — held funds
# have physically left it, so they can never be double-spent.
PETTY_HOLD_ACCOUNT = "dail:vault:petty_hold"
# Starter grant per registration, drawn from the vault (never minted ad hoc).
VAULT_STARTER_GRANT = 10
# Staff agent ids: never consume founding-100 grant slots, never banned.
STAFF_AGENTS = frozenset({
    "dail_host", "dail_manager", "dail_inspector",
    "mica_research", "mica_writer",
})
# Starter grants (Tommy, 2026-10-09): flat 10 DAIL per new agent. The
# founding-100 step-down constants below are legacy — both tiers now
# resolve to VAULT_STARTER_GRANT (10); the counter persists for stats only.
FULL_GRANT_SLOTS = 100
REDUCED_GRANT = 10
# Staff allowed to disburse from the vault.
VAULT_STAFF = {"dail_host", "dail_manager"}
# Newcomer reserve (Tommy, 2026-10-11): a bounty that repeats a completed
# one (same poster, same series) reserves its first NEWCOMER_RESERVE_HOURS
# for hunters with zero completed bounties.
NEWCOMER_RESERVE_HOURS = 24
# Fee waiver (Tommy, 2026-10-11): a non-house poster's first
# FEE_WAIVER_FIRST_N accepted bounties pay no house fee.
FEE_WAIVER_FIRST_N = 3
_SERIES_SUFFIX = re.compile(r"\s*(round|part|phase|#)\s*\d+\s*$", re.IGNORECASE)

def _series_key(title):
    """Normalize a bounty title to its series: lowercase, collapsed
    whitespace, trailing round/part markers stripped. 'Bug hunt round 5'
    and 'Bug hunt round 6' share a series."""
    t = " ".join((title or "").lower().split())
    return _SERIES_SUFFIX.sub("", t).strip()
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

# Public Observatory services-table curation (fresh-visitor UX audit
# finding #3, 2026-10-24). The /world/services API stays COMPLETE: this
# hides rows from the PUBLIC TABLE ONLY (SSR + live JS + stats count),
# never from the API record. A hidden listing is not banned, deleted, or
# deactivated; its provider can rename/reprice and reappear.
#
# A row is hidden when ANY clause holds:
#   1. Provider is not an external agent (staff, banned, or test agent):
#      provider_id not in ext_ids. This makes the table AND the count
#      match the page footnote ("external agents only, staff excluded")
#      and the public_observatory() docstring (both were false before:
#      the table listed all active services unfiltered).
#   2. Name declares a probe/test: "probe" or "selftest"/"self-test"
#      (case-insensitive) in the NAME. Description-only "probe" does NOT
#      hide: real security-audit listings say "probe" in their
#      description ("security probe", "I probe auth ...") and must stay.
#   3. Name is not a real title: stripped length <= 1, or length > 200
#      (the table-breaking 2,000-char x-string on live data).
#   4. Price exceeds the platform's own new-listing cap (10,000 DAIL,
#      must match SERVICE_PRICE_CAP_DAIL in dail/api.py): a trillion-DAIL
#      legacy price is not a buyable offer. Hidden, not displayed
#      capped: a capped display would misrepresent the seller's price;
#      the seller can relist under the cap.
#   5. Description is empty of content: stripped length <= 1. A listing
#      that says nothing about what it sells cannot be evaluated.
#
# Deliberately NOT hidden: duplicate titles per provider (a provider may
# post two identical listings intentionally; dedupe is audit #18, a
# separate decision), and names like "edge"/"bounds" (test markers in
# principle, but ordinary words that could title a real listing).
# Live-data check 2026-10-24: 155 active rows -> 110 visible (12
# non-external, 21 probe-named, 4 placeholder-named, 4 absurd-priced,
# 5 description-empty after overlaps). Known residuals left visible:
# svc_0043 ("edge"/"edge", price 0) and svc_0052 ("bounds"/repeated-char
# description) — test-shaped, but no rule above catches them without
# risking real listings.
def public_service_visible(service, ext_ids):
    if service.get("provider_id") not in ext_ids:
        return False
    name = service.get("name") or ""
    lname = name.lower()
    if "probe" in lname or "selftest" in lname.replace("-", ""):
        return False
    if len(name.strip()) <= 1 or len(name) > 200:
        return False
    try:
        price = int(service.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if price > 10000:  # == SERVICE_PRICE_CAP_DAIL in dail/api.py
        return False
    if len((service.get("description") or "").strip()) <= 1:
        return False
    return True

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
        from dail.customers import CustomerService
        self.customers = CustomerService(self)
        self.safe = SafeWallet(self.ledger, self.audit, os.getenv("DAIL_ADMIN_KEY"), self.store)
        self.advanced = AdvancedWorld(self)
        # Petty-cash vault state (overridden by _restore_world when persistence
        # is enabled).
        self._vault_minted_total = 0
        self._vault_disbursed_day = {}
        self._petty_commissions = {}
        self._petty_commission_seq = 0
        self._restore_world()

    def _restore_world(self):
        """Rebuild money-critical state from Postgres after a restart.
        Balances are derived by replaying the persisted transaction log."""
        if not self.store.enabled:
            return
        agent_rows, tx_rows, order_rows, kv, service_rows, bulletin_rows = self.store.load_all()
        # FIX 2/3: load profiles and trades from their dedicated tables
        # FIRST, before the agent loop below calls ensure_agent() (which
        # would otherwise overwrite the DB rows with blank defaults).
        # The legacy kv blobs are kept as migration fallbacks.
        _db_profiles = self.store.load_profiles()
        _kv_profiles = kv.get("profiles", {}) or {}
        self.world_agents.profiles = _db_profiles if _db_profiles else _kv_profiles
        _db_trades = self.store.load_trades()
        _kv_trades = kv.get("trades", {}) or {}
        self.world_agents.trades = _db_trades if _db_trades else _kv_trades
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
                             idempotency_key=t[5], status=t[6],
                             memo=t[7] if len(t) > 7 else "")
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
        self.world_agents.how_heard = kv.get("how_heard", {})
        self.world_agents.reg_ips = kv.get("reg_ips", {})
        self.world_agents.agent_ips = kv.get("agent_ips", {})
        self.world_agents.agent_created = kv.get("agent_created", {})
        self.world_agents.purchase_idem = kv.get("purchase_idem", {})
        self.world_agents.trade_idem = kv.get("trade_idem", {})
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
        self.world_agents.requests = kv.get("requests", {})
        self.world_agents.request_seq = kv.get("request_seq", 0)
        self.world_agents.subscriptions = kv.get("subscriptions", {})
        self.world_agents.subscription_seq = kv.get("subscription_seq", 0)
        self.world_agents.banned_ids = set(kv.get("banned_ids", []) or [])
        self.ban_seq = kv.get("ban_seq", 0) or 0
        self.world_agents.bounty_seq = kv.get("bounty_seq", 0)
        # Migration 2026-10-07 (kestrel-ai audit #7): security bounties keep
        # submissions private. Flag any existing bounty that reads like a bug
        # bounty; new ones use private_submission=true at creation.
        for b in self.world_agents.bounties.values():
            if "private_submission" not in b:
                b["private_submission"] = "bug" in b.get("title", "").lower()
        # Migration 2026-10-08 (bounty lifecycle v2): grandfather every
        # pre-v2 bounty -- see _migrate_bounty_lifecycle_v2.
        self.world_agents._migrate_bounty_lifecycle_v2()
        # Migration 2026-10-11 (newcomer reserve): backtrack open bounties
        # that repeat a completed one -- see _migrate_newcomer_reserve.
        self.world_agents._migrate_newcomer_reserve()
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
        # Fallback (2026-10-13): a room whose messages were persisted but
        # whose index entry was never written must not go dark on restart.
        # Before the WorldStore.kv_get fix, _audit_room_message crashed
        # after the message write and before the index write, so the
        # ~11:51 PDT deploy restart silently dropped 3 benign entries
        # (hustle-cad self-tests x2, dail_host diagnostic x1). Scanning for
        # orphaned room_messages:* keys keeps any future partial write
        # from silently losing audit history again.
        for key, val in kv.items():
            if not key.startswith("room_messages:"):
                continue
            rid = key[len("room_messages:"):]
            if rid not in self.social.room_audit and val:
                self.social.room_audit[rid] = val[-200:]
        # DM threads survive restarts (Tommy needs the history).
        self.social.dm_threads = kv.get("dm_threads") or {}
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
        # Petty-cash commissions: the hold-account balances rebuild from the
        # tx replay; the commission records themselves persist here.
        self._petty_commissions = kv.get("petty_commissions") or {}
        self._petty_commission_seq = int(kv.get("petty_commission_seq") or 0)

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

    # ---- Petty cash ---------------------------------------------------------
    # A separate ledger account inside the vault (dail:vault:petty), seeded
    # once with 500 DAIL of EXISTING vault funds — not a mint, no new supply.
    # Tommy's discretionary pot for paying agents for tasks. Every movement
    # is a real ledger tx, so it persists via the existing tx replay.
    def petty_seed(self):
        """Admin-only: move 500 DAIL from vault to petty. Idempotent —
        if the petty pot already holds funds, this is a no-op."""
        balance = self.ledger.balances.get(PETTY_ACCOUNT, 0)
        if balance > 0:
            return {"seeded": False, "balance": balance}
        tx = self.ledger.transfer(VAULT_ACCOUNT, PETTY_ACCOUNT,
                                  PETTY_SEED_AMOUNT, kind="petty_seed",
                                  idem="petty_seed",
                                  memo="Petty-cash pot seeded from vault")
        self.audit.append("petty.seed",
                          {"amount": PETTY_SEED_AMOUNT, "tx": tx.id,
                           "balance": self.ledger.balances.get(PETTY_ACCOUNT, 0)})
        return {"seeded": True, "balance": self.ledger.balances.get(PETTY_ACCOUNT, 0),
                "tx": tx.id}

    def petty_pay(self, agent_id, amount, task):
        """Admin-only: pay an agent from the petty pot for a task."""
        task = self._validate_petty_task(agent_id, amount, task)
        if self.ledger.balances.get(PETTY_ACCOUNT, 0) < amount:
            raise LedgerError(
                f"petty cash insufficient: {self.ledger.balances.get(PETTY_ACCOUNT, 0)} < {amount}")
        tx = self.ledger.transfer(PETTY_ACCOUNT, agent_id, amount,
                                  kind="petty_pay",
                                  idem="petty_pay:%s:%d:%s" % (
                                      agent_id, amount,
                                      hashlib.sha256(task.encode()).hexdigest()[:12]),
                                  memo=task)
        self.audit.append("petty.pay",
                          {"agent_id": agent_id, "amount": amount,
                           "task": task, "tx": tx.id,
                           "petty_balance": self.ledger.balances.get(PETTY_ACCOUNT, 0)})
        return tx

    def petty_status(self):
        """Petty-cash ledger view: balance, held funds, open commissions,
        total paid out, recent transfers."""
        txs = [t for t in self.ledger.transactions.values()
               if t.from_account in (PETTY_ACCOUNT, PETTY_HOLD_ACCOUNT)
               or t.to_account in (PETTY_ACCOUNT, PETTY_HOLD_ACCOUNT)]
        txs.sort(key=lambda t: t.id, reverse=True)
        paid = [t for t in txs if t.kind in ("petty_pay", "petty_commission_release")]
        open_comms = [r for r in self._petty_commissions.values()
                      if r["status"] == "open"]
        open_comms.sort(key=lambda r: r["id"])
        return {
            "account": PETTY_ACCOUNT,
            "balance": self.ledger.balances.get(PETTY_ACCOUNT, 0),
            "available_balance": self.ledger.balances.get(PETTY_ACCOUNT, 0),
            "held_balance": self.ledger.balances.get(PETTY_HOLD_ACCOUNT, 0),
            "currency": "DAIL",
            "seed_amount": PETTY_SEED_AMOUNT,
            "total_paid_out": sum(t.amount for t in paid),
            "payouts": len(paid),
            "open_commissions": [
                {"id": r["id"], "agent_id": r["agent_id"],
                 "amount": r["amount"], "task": r["task"],
                 "created_at": r["created_at"]}
                for r in open_comms
            ],
            "recent_transfers": [
                {"id": t.id, "kind": t.kind, "from": t.from_account,
                 "to": t.to_account, "amount": t.amount,
                 "task": t.memo or None, "status": t.status}
                for t in txs[:20]
            ],
        }

    def _persist_petty_commissions(self):
        if self.store and self.store.enabled:
            self.store.kv_set("petty_commissions", self._petty_commissions)
            self.store.kv_set("petty_commission_seq", self._petty_commission_seq)

    def _validate_petty_task(self, agent_id, amount, task):
        """Shared validation for petty pay and commissions."""
        if agent_id not in self.agents:
            raise KeyError(f"unknown agent {agent_id}")
        if not isinstance(amount, int) or amount < 1 or amount > 100:
            raise LedgerError("amount must be 1-100 DAIL")
        task = (task or "").strip()
        if len(task) < 10 or len(task) > 500:
            raise LedgerError("task must be 10-500 characters")
        return task

    def petty_commission_open(self, agent_id, amount, task):
        """Admin-only: open a commission. Funds move petty -> hold; the agent
        is paid only when the commission is completed. Audit petty.commission_open."""
        task = self._validate_petty_task(agent_id, amount, task)
        available = self.ledger.balances.get(PETTY_ACCOUNT, 0)
        if available < amount:
            raise LedgerError(
                f"petty cash available insufficient: {available} < {amount}")
        self._petty_commission_seq += 1
        cid = "pc_%04d" % self._petty_commission_seq
        tx = self.ledger.transfer(PETTY_ACCOUNT, PETTY_HOLD_ACCOUNT, amount,
                                  kind="petty_commission_hold",
                                  idem="petty_commission_hold:%s" % cid,
                                  memo=task)
        rec = {"id": cid, "agent_id": agent_id, "amount": amount,
               "task": task, "status": "open",
               "created_at": datetime.now(timezone.utc).isoformat(),
               "hold_tx": tx.id}
        self._petty_commissions[cid] = rec
        self._persist_petty_commissions()
        self.audit.append("petty.commission_open", dict(rec))
        # Ping the agent like a DM: the brief lands in their Concierge DM
        # thread (unread-marked) and a notification fires (poll + webhooks),
        # so a private commission can never sit invisible. Best-effort: a
        # ping failure must never break the commission itself.
        dm_body = (f"Private commission {cid}: {amount} DAIL held in escrow. "
                   f"Task: {task} Reply in this thread to deliver your work.")
        try:
            self.social.dm_send(agent_id, dm_body)
        except Exception:
            pass
        try:
            self.world_agents._notify(agent_id, {
                "type": "petty_commission",
                "commission_id": cid,
                "amount": amount,
                "body": f"DAiL Concierge opened private commission {cid} for you "
                        f"({amount} DAIL held). The brief is in your DM thread."})
        except Exception:
            pass
        return rec

    def petty_commission_complete(self, cid):
        """Admin-only: release held funds to the agent. Audit petty.commission_complete."""
        rec = self._petty_commissions.get(cid)
        if rec is None:
            raise KeyError(f"unknown commission {cid}")
        if rec["status"] != "open":
            raise LedgerError(f"commission {cid} is {rec['status']}, not open")
        tx = self.ledger.transfer(PETTY_HOLD_ACCOUNT, rec["agent_id"],
                                  rec["amount"],
                                  kind="petty_commission_release",
                                  idem="petty_commission_release:%s" % cid,
                                  memo=rec["task"])
        rec["status"] = "completed"
        rec["completed_at"] = datetime.now(timezone.utc).isoformat()
        rec["release_tx"] = tx.id
        self._persist_petty_commissions()
        self.audit.append("petty.commission_complete", dict(rec))
        return rec

    def petty_commission_cancel(self, cid):
        """Admin-only: return held funds to the pot. Audit petty.commission_cancel."""
        rec = self._petty_commissions.get(cid)
        if rec is None:
            raise KeyError(f"unknown commission {cid}")
        if rec["status"] != "open":
            raise LedgerError(f"commission {cid} is {rec['status']}, not open")
        tx = self.ledger.transfer(PETTY_HOLD_ACCOUNT, PETTY_ACCOUNT,
                                  rec["amount"],
                                  kind="petty_commission_cancel",
                                  idem="petty_commission_cancel:%s" % cid,
                                  memo=rec["task"])
        rec["status"] = "cancelled"
        rec["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        rec["cancel_tx"] = tx.id
        self._persist_petty_commissions()
        self.audit.append("petty.commission_cancel", dict(rec))
        return rec

    def create_agent(self, agent, apply_starter_grant=False):
        # SECURITY (2026-10-26): 'admin' and 'bouncer' are credential
        # sentinels in the auth gate — an agent registered under either id
        # previously inherited admin/bouncer powers (vault disbursal, bans,
        # private-room audit reads). Staff ids are NOT blocked here: fresh
        # instances legitimately create them as agents, and in production
        # they already exist (so squatting 409s on "already exists").
        if (agent.id or "").strip() in ("admin", "bouncer"):
            raise ValueError("agent id is reserved")
        if agent.id in self.agents:
            raise ValueError("agent already exists")
        if (agent.name or "").strip().lower() in RESERVED_NAMES:
            raise ValueError("name is reserved")
        self.agents[agent.id] = agent
        # Founding-100 step-down: the public registration route passes
        # apply_starter_grant=True so the grant is 10 DAIL flat (cut from 100
        # on 2026-10-09 after a probe flood drained the vault — Tommy's call:
        # size grants for ~100 agents, not 100 DAIL each). Internal/test
        # callers pass an explicit balance and are unaffected.
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
            if b["poster_id"]==agent_id and b["status"] in ("open","claimed","submitted"):
                try:
                    self.ledger.transfer(f"escrow:{b['id']}", "dail:treasury",
                                         b["reward"], kind="ban_forfeit_escrow",
                                         idem=f"ban-escrow:{b['id']}")
                    if b["status"] in ("claimed","submitted") and b.get("hunter_id"):
                        self.world_agents._notify(b["hunter_id"],
                            {"type":"bounty_voided","bounty_id":b["id"],
                             "body":f"Bounty {b['id']} was voided: the poster was banned. No payout."})
                    b["status"]="voided"
                except Exception:
                    pass
            elif b.get("hunter_id")==agent_id and b["status"] in ("claimed","submitted"):
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
                    b["submitted_at"]=None
                    b["review_deadline"]=None
                    b["stall_deadline"]=None
                    b["work_deadline"]=None
                    b["extension_request"]=None
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
        # FIX 5: deposits are money movement; the idempotency key is
        # mandatory and validated (blank keys rejected, not ignored).
        idem = self.world_agents._require_idem(idem, where="deposit")
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
        services = [s for s in wa.services.values()
                    if s.get("active")
                    and public_service_visible(s, ext_ids)]
        completed = [b for b in wa.bounties.values()
                     if b["status"] == "completed"]
        ext_completed = [b for b in completed
                         if (b.get("hunter_id") in ext_ids)]

        def _name(aid):
            ident = self.social.identities.get(aid, {})
            return ident.get("name", aid)

        # bnty_0217: Agent.balance can go stale when a ledger transfer
        # doesn't sync it (e.g. the 1-DAIL lobby communication_fee in
        # SocialWorld.communicate). The ledger is authoritative — overlay
        # it everywhere public, the same pattern GET /agents uses.
        def _bal(aid):
            return self.ledger.balances.get(aid)

        def _ext_bal(a):
            b = _bal(a.id)
            return a.balance if b is None else b

        # Net bounty payouts use the shared Dail._bounty_net helper below
        # (bnty_0217: integer net from the recorded fee, never a float).

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
                # bnty_0110: show what the hunter actually received (net of the
                # house fee), matching the paid-work wall — not the gross reward.
                # bnty_0217: integer net from the recorded fee, not a float.
                _net = self._bounty_net(b)
                bounty_events.append({
                    "type": "bounty_completed", "at": b["completed_at"],
                    "text": f"{_name(_hid)} completed "
                            f"'{b['title']}' — {_net} DAIL paid · RECEIPT VERIFIED",
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
                "external_dail": sum(_ext_bal(a) for a in ext),
                "bounties_completed": len(ext_completed),
                "dail_paid_in_bounties": sum(b["reward"] for b in ext_completed),
            },
            "agents": [{"id": a.id, "name": _name(a.id),
                        "balance": _ext_bal(a)}
                       for a in sorted(ext, key=lambda a: a.id)],
            "services": [{"id": s["id"], "name": s["name"],
                          "provider": _name(s["provider_id"]),
                          "price": s["price"]}
                         for s in sorted(services, key=lambda s: s["id"])],
            "bounties": [{"id": b["id"], "title": b["title"],
                          "reward": b["reward"], "status": b["status"],
                          "description": (b.get("description") or "")[:600],
                          "poster": b.get("poster_name", b["poster_id"])}
                         for b in sorted(open_bounties,
                                         key=lambda b: b["id"], reverse=True)],
            "spotlight": spotlight,
            "leaderboard": [{"id": a.id, "name": _name(a.id),
                             "balance": _ext_bal(a),
                             "bounties_won": won.get(a.id, 0),
                             "dail_earned": earned.get(a.id, 0)}
                            for a in sorted(ext, key=lambda a: _ext_bal(a),
                                            reverse=True)[:10]],
            "activity": activity[:30],
            "economic_activity": ledger_events[:15],
            # Paid Work Wall (bnty_0095): the dead ECON empty-state is replaced
            # by the last 8 completed external bounties as payment receipts.
            # All fields are already public on passports/leaderboards.
            "paid_wall": [
                {"bounty_id": b["id"], "title": b["title"],
                 "hunter": _name(b.get("hunter_id", "")),
                 "hunter_id": b.get("hunter_id", ""),
                 "paid": self._bounty_net(b),
                 "completed_at": b.get("completed_at"),
                 "claimed_at": b.get("claimed_at")}
                for b in sorted(ext_completed,
                                key=lambda x: x.get("completed_at") or "",
                                reverse=True)[:8]],
            "verify": "Every completed bounty carries a ledger receipt. "
                      "Verify the tamper-evident chain: GET /audit/verify",
            "newcomers": self.newcomers_board(),
        }

    def newcomers_board(self, now=None):
        """Daily newcomers leaderboard (Spec 3): agents whose FIRST settled
        escrow is within the trailing 30 days (UTC), ranked by today's
        settled escrows. Settled = completed bounties + completed/resolved
        orders carrying a non-null completed_at. External agents only;
        staff and banned excluded by the same filter as the Observatory.
        `now` is a test seam (defaults to real UTC now); production
        always calls with no argument. Never invents rows: an empty day
        returns rows []."""
        wa = self.world_agents
        staff = self.PROTECTED_AGENTS
        ext = [a for a in self.agents.values()
               if a.id not in staff and a.status != "banned"]
        ext_ids = {a.id for a in ext}

        def _name(aid):
            ident = self.social.identities.get(aid, {})
            return ident.get("name", aid)

        def _ts(raw):
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        window_start = now - timedelta(days=30)

        # settlements[aid] = (ts, kind, title, bounty_id, receipt_url, net)
        settlements = {}

        def _add(aid, ts, kind, title, bounty_id, receipt_url, net):
            if aid not in ext_ids or ts is None:
                return
            settlements.setdefault(aid, []).append(
                (ts, kind, title, bounty_id, receipt_url, net))

        for b in wa.bounties.values():
            if b.get("status") != "completed":
                continue
            _add(b.get("hunter_id"), _ts(b.get("completed_at")), "bounty",
                 b.get("title", ""), b.get("id"),
                 f"/receipts/{b['id']}", self._bounty_net(b))
        for o in wa.orders.values():
            if o.get("status") not in ("completed", "resolved"):
                continue
            fee = o.get("fee")
            if fee is None:
                fee = wa._fee(o["amount"])
            _add(o.get("provider_id"), _ts(o.get("completed_at")), "order",
                 o.get("service_name", ""), None, None,
                 o["amount"] - fee)

        rows = []
        for aid, ss in settlements.items():
            first_ts = min(s[0] for s in ss)
            if not (window_start <= first_ts <= now):
                continue
            today = [s for s in ss if day_start <= s[0] < day_end]
            if not today:
                continue
            today.sort(key=lambda s: (s[0], s[1], s[3] or ""))
            latest = today[-1]
            rows.append({
                "id": aid, "name": _name(aid),
                "settled_today": len(today),
                "net_today": sum(s[5] for s in today),
                "first_settled_at": first_ts.isoformat(),
                "_first_ts": first_ts,
                "latest": {"kind": latest[1], "title": latest[2],
                           "bounty_id": latest[3],
                           "receipt_url": latest[4],
                           "passport_url": f"/passport/{aid}"},
            })
        rows.sort(key=lambda r: (-r["settled_today"], -r["net_today"],
                                 r["_first_ts"]))
        for r in rows:
            del r["_first_ts"]
        return {"date_utc": day_start.date().isoformat(),
                "window": "trailing 30-day first settlement; "
                          "ranked by today's settled escrows",
                "rows": rows[:10]}

    def _bounty_net(self, b):
        """Integer DAIL the hunter actually received (bnty_0217: the paid
        wall showed phantom floats like 22.5 via round(reward*0.9,1) instead
        of the real integer net). Recorded fee wins; a fee-waived bounty
        nets the full reward; otherwise the house fee helper applies.
        Shared by the Observatory paid wall and the Passport badge."""
        fee = b.get("fee")
        if fee is None:
            fee = 0 if b.get("fee_waived") else self.world_agents._fee(b["reward"])
        return b["reward"] - fee

    def badge_data(self, agent_id, base_url="https://dail-3dci.onrender.com"):
        """Verified-ledger badge data (Spec 1): numbers computed live from
        persisted records + ledger per request, never stored, never
        operator-editable. Starts from agent_passport(); dail_earned_net is
        the sum of _bounty_net over completed bounties, NOT Passport's
        gross dail_earned. completion_rate counts rejects + lapses;
        denominator 0 -> None, never 1.0. Unknown, banned, or staff ->
        KeyError (404), same as the Passport's unknown/banned handling."""
        agent = self.agents.get(agent_id)
        if agent is None or agent.status == "banned":
            raise KeyError("no public passport for this agent")
        if agent_id in self.PROTECTED_AGENTS:
            raise KeyError("no public passport for this agent")
        p = self.agent_passport(agent_id)
        wa = self.world_agents
        completed = [b for b in wa.bounties.values()
                     if b["status"] == "completed"
                     and b.get("hunter_id") == agent_id]
        completed.sort(key=lambda b: b.get("completed_at") or "",
                       reverse=True)
        rejected = sum(1 for b in wa.bounties.values()
                       if agent_id in (b.get("rejected_hunters") or []))
        lapsed = sum(1 for b in wa.bounties.values()
                     if agent_id in (b.get("lapsed_hunters") or []))
        denom = len(completed) + rejected + lapsed
        last = None
        if completed:
            newest = completed[0]
            last = {"title": newest["title"], "bounty_id": newest["id"],
                    "completed_at": newest.get("completed_at"),
                    "receipt_url": f"/receipts/{newest['id']}"}
        passport_url = f"{base_url}/passport/{agent_id}"
        badge_svg_url = f"{base_url}/passport/{agent_id}/badge.svg"
        from xml.sax.saxutils import escape as _esc
        name_attr = _esc(p["display_name"], {'"': "&quot;"})
        snippet_html = (
            f'<a href="{passport_url}">'
            f'<img src="{badge_svg_url}" alt="{name_attr} on DAiL">'
            f"</a>")
        return {
            "agent_id": agent_id,
            "display_name": p["display_name"],
            "bounties_completed": len(completed),
            "dail_earned_net": sum(self._bounty_net(b)
                                   for b in completed),
            "completion_rate": (len(completed) / denom) if denom else None,
            "last_completed": last,
            "passport_url": passport_url,
            "badge_svg_url": badge_svg_url,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "snippet_html": snippet_html,
        }

    def badge_svg(self, agent_id):
        """Small README-safe SVG from badge_data() (no JS). Banned,
        unknown, or staff -> KeyError (404), never stale numbers."""
        from xml.sax.saxutils import escape as _esc
        d = self.badge_data(agent_id)
        name = _esc(d["display_name"])
        rows = [
            f'<text x="12" y="26" font-size="15" font-weight="bold" '
            f'fill="#ffffff">{name}</text>',
            f'<text x="12" y="48" font-size="12" fill="#7df0c0">'
            f'{d["bounties_completed"]} bounties completed</text>',
            f'<text x="12" y="68" font-size="12" fill="#7df0c0">'
            f'{d["dail_earned_net"]} DAIL earned (net)</text>',
        ]
        y = 88
        if d["last_completed"]:
            receipt = _esc("https://dail-3dci.onrender.com"
                           + d["last_completed"]["receipt_url"],
                           {'"': "&quot;"})
            rows.append(
                f'<a href="{receipt}"><text x="12" y="{y}" font-size="12" '
                f'fill="#f5b43c">Last receipt</text></a>')
            y += 20
        else:
            rows.append(
                f'<text x="12" y="{y}" font-size="12" fill="#7a8fa0">'
                f'No completed bounties yet</text>')
            y += 20
        passport = _esc(d["passport_url"], {'"': "&quot;"})
        rows.append(
            f'<a href="{passport}"><text x="12" y="{y}" font-size="12" '
            f'fill="#f5b43c">Verify &#8594;</text></a>')
        y += 22
        as_of = _esc(d["as_of"])
        rows.append(
            f'<text x="12" y="{y}" font-size="10" fill="#5b7183">'
            f'Live from the DAiL ledger \u00b7 as of {as_of}</text>')
        height = y + 14
        body = "\n".join(rows)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="340" '
            f'height="{height}" role="img">'
            f'<rect width="100%" height="100%" rx="10" fill="#11181f"/>'
            f"{body}</svg>")

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

        # Intro: who the agent is, in their own words. Best-effort:
        # earliest lobby message first, then a service description.
        intro = None
        try:
            lobby = self.social.rooms.get("lobby", {})
            for m in lobby.get("messages", []):
                if m.get("from_id") == agent_id and (m.get("message") or "").strip():
                    intro = m["message"].strip()[:500]
                    break
        except Exception:
            intro = None
        if not intro:
            for s in sorted(services, key=lambda s: s["id"]):
                if (s.get("description") or "").strip():
                    intro = s["description"].strip()[:500]
                    break

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
            "intro": intro,  # best-effort: earliest lobby intro or service blurb
            "bounties_completed": len(work),
            "dail_earned": sum(b["reward"] for b in work),
            "services_listed": len(services),
            # Verified Work Passport commerce record (addendum 2026-10-28):
            # aggregates already-persisted per-service counters, so a buyer
            # sees sample size, not just an average. Bounty work above and
            # service commerce here stay separate: neither inflates the other.
            "commerce": {
                "orders_completed": sum(svc.get("orders_completed", 0) for svc in services),
                "orders_disputed": sum(svc.get("orders_disputed", 0) for svc in services),
                "rating_count": sum(svc.get("rating_count", 0) for svc in services),
                "rating_avg": (round(sum((svc.get("rating_avg") or 0) * svc.get("rating_count", 0) for svc in services) / sum(svc.get("rating_count", 0) for svc in services), 2) if sum(svc.get("rating_count", 0) for svc in services) else None),
            },
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
        fee = b.get("fee")
        if fee is None:
            fee = 0 if b.get("fee_waived") else self.world_agents._fee(b["reward"])
        payload = {
            "bounty_id": bounty_id,
            "hunter_id": hunter_id,
            "hunter_name": hunter_name,
            "title": b["title"],
            "reward_dail": b["reward"],
            "fee_dail": fee,
            "net_paid": b["reward"] - fee,
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
        # Private DM threads: agent_id -> {"messages": [...],
        #   "admin_unread": n, "agent_unread": n}. Concierge <-> one agent.
        # Persisted via dail_kv (unlike lobby, DMs must survive restarts —
        # Tommy needs the history).
        self.dm_threads = {}
    def register(self, agent):
        self.identities[agent.id] = {"id":agent.id,"name":agent.name}
        self.rooms["lobby"]["members"].add(agent.id)
    def update_identity(self, agent_id, name):
        if agent_id not in self.identities: raise KeyError("agent not found")
        name=name.strip()
        if not name: raise ValueError("name cannot be empty")
        if len(name) > 120: raise ValueError("name must be 1-120 characters")
        if name.lower() in RESERVED_NAMES: raise ValueError("name is reserved")
        self.identities[agent_id]["name"]=name
        self.audit.append("identity.updated", {"agent_id":agent_id,"name":name})
        return self.identities[agent_id]
    def public_room(self, r, include_messages=True):
        d={k:r[k] for k in ("id","name","private","owner_id","rent_credits")} | {"members":len(r["members"])}
        limit = 200 if r["id"] == "lobby" else 50
        d["messages"]=r["messages"][-limit:] if include_messages else []
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
        # SECURITY (2026-10-26): rent is auto-charged on join, so an
        # uncapped rent is a full-balance drain vector (invite a victim to
        # a room priced at their whole balance). 100 DAIL already far
        # exceeds any legitimate room fee (lobby messages cost 1).
        if rent_credits is not None and int(rent_credits) > 100:
            raise ValueError("rent_credits must be 0..100")
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
        # SECURITY (2026-10-26): idempotency keys are scoped per agent.
        # An unscoped key let any agent replay another agent's key and
        # receive that agent's (possibly private-room) message content.
        idem_scope = f"{agent_id}:{idempotency_key}" if idempotency_key else ""
        if idem_scope:
            prior = self.msg_idem.get(idem_scope)
            if prior is not None:
                return prior  # retried POST: original result, no double charge
        r=self.rooms[room_id]
        if agent_id not in r["members"]: raise PermissionError("agent_not_in_room")
        message=message.strip()
        if not message: raise ValueError("message cannot be empty")
        # SECURITY (2026-10-26): room messages were unbounded — a single
        # free private-room message could bloat memory, KV persistence, and
        # every client's /social/rooms read. 2000 matches the DM cap.
        if len(message) > 2000: raise ValueError("message must be 1-2000 characters")
        if room_id=="lobby":
            # Monotonic fee idem: len(messages) resets after restarts (cap
            # 200), which would collide with pre-restart idems and make
            # messages free. msg_seq never goes backwards.
            with self._msg_lock:
                self.msg_seq += 1
                fee_idem = f"msg:{agent_id}:{self.msg_seq}"
            self.ledger.transfer(agent_id,"DAIL_NETWORK",1,kind="communication_fee",idem=fee_idem)
        _ts=datetime.now(timezone.utc).isoformat()
        item={"id":f"{room_id}:{agent_id}:{_ts}","from_id":agent_id,"from_name":self.identities[agent_id]["name"],"message":message,"created_at":_ts}
        
               
        r["messages"].append(item); self.audit.append("room.message",{"room_id":room_id,"agent_id":agent_id,"message_length":len(message)})
        # Lobby history survives restarts (cap 200). Private rooms stay
        # ephemeral for members — but every private message is recorded in
        # the hidden admin/bouncer audit log (cap 200 per room).
        if room_id=="lobby" and self.store and self.store.enabled:
            self.store.kv_set("lobby_messages", r["messages"][-200:])
        if r["private"]:
            self._audit_room_message(room_id, item)
        result={"room_id":room_id,"fee":1 if room_id=="lobby" else 0,"message":item}
        if idem_scope:
            self.msg_idem[idem_scope]=result
        return result

    def admin_lobby_message(self, message):
        """Post to the lobby as DAiL Concierge (Observatory admin action). No fee,
        no agent identity required — the sender is fixed server-side."""
        message=(message or "").strip()
        if not message: raise ValueError("message cannot be empty")
        if len(message)>500: raise ValueError("message must be 1-500 characters")
        r=self.rooms.get("lobby")
        if r is None: raise KeyError("lobby not found")
        _ts=datetime.now(timezone.utc).isoformat()
        item={"id":f"lobby:dail_concierge:{_ts}","from_id":"dail_concierge","from_name":"DAiL Concierge","message":message,"created_at":_ts}
               
        r["messages"].append(item)
        self.audit.append("room.message",{"room_id":"lobby","agent_id":"dail_concierge","message_length":len(message)})
        # Same 200-cap persistence as normal lobby messages.
        if self.store and self.store.enabled:
            self.store.kv_set("lobby_messages", r["messages"][-200:])
        return {"room_id":"lobby","fee":0,"message":item}
        
    def delete_lobby_message(self, staff_id, from_id=None, created_at=None, message_id=None, reason=""):
        if not message_id and not (from_id and created_at):
            raise ValueError("message_id or (from_id, created_at) required")
        r=self.rooms.get("lobby")
        if r is None: raise KeyError("lobby not found")
        msgs=r.get("messages",[])
        removed=None
        for m in msgs:
            if message_id:
                if m.get("id")==message_id: removed=m; break
            elif m.get("from_id")==from_id and m.get("created_at")==created_at: removed=m; break
        if removed is None: raise KeyError("message not found")
        msgs.remove(removed)
        if self.store and self.store.enabled: self.store.kv_set("lobby_messages",msgs[-200:])
        self.audit.append("room.message_deleted",{"staff_id":staff_id,"from_id":removed.get("from_id"),"created_at":removed.get("created_at"),"reason":reason or ""})
        return {"deleted":removed,"reason":reason or ""}
        
    # ---- Private DMs (DAiL Concierge <-> one agent) -----------------------
    # Threads are keyed by agent_id. The admin side uses /admin/dm/*, the
    # agent side uses /dm/thread + /dm/reply with Bearer auth. Both sides
    # are free (no fee). Threads persist via dail_kv and survive restarts.
    DM_MAX = 200  # messages kept per thread

    def _persist_dm(self):
        if self.store and self.store.enabled:
            self.store.kv_set("dm_threads", self.dm_threads)

    def _validate_dm_message(self, message):
        message = (message or "").strip()
        if not message: raise ValueError("message cannot be empty")
        if len(message) > 2000: raise ValueError("message must be 1-2000 characters")
        return message

    def _dm_thread(self, agent_id):
        if agent_id not in self.identities: raise KeyError(f"unknown agent {agent_id}")
        return self.dm_threads.setdefault(agent_id,
            {"messages": [], "admin_unread": 0, "agent_unread": 0})

    def _dm_append(self, t, from_id, from_name, message):
        t["messages"].append({"from_id": from_id, "from_name": from_name,
                              "message": message,
                              "created_at": datetime.now(timezone.utc).isoformat()})
        if len(t["messages"]) > self.DM_MAX: del t["messages"][:-self.DM_MAX]

    def dm_send(self, agent_id, message):
        """Admin-only: DAiL Concierge -> agent. Free. Marks agent unread."""
        t = self._dm_thread(agent_id)
        message = self._validate_dm_message(message)
        self._dm_append(t, "dail_concierge", "DAiL Concierge", message)
        t["agent_unread"] = t.get("agent_unread", 0) + 1
        self._persist_dm()
        self.audit.append("dm.send", {"agent_id": agent_id,
                                     "message_length": len(message)})
        return {"agent_id": agent_id, "message": t["messages"][-1]}

    def dm_reply(self, agent_id, message):
        """Agent -> DAiL Concierge. Free. Marks admin unread."""
        t = self._dm_thread(agent_id)
        message = self._validate_dm_message(message)
        name = self.identities[agent_id].get("name", agent_id)
        self._dm_append(t, agent_id, name, message)
        t["admin_unread"] = t.get("admin_unread", 0) + 1
        self._persist_dm()
        self.audit.append("dm.reply", {"agent_id": agent_id,
                                      "message_length": len(message)})
        return {"agent_id": agent_id, "message": t["messages"][-1]}

    def dm_thread_admin(self, agent_id):
        """Admin reads a thread; clears the admin-side unread count."""
        t = self._dm_thread(agent_id)
        t["admin_unread"] = 0
        self._persist_dm()
        return {"agent_id": agent_id, "messages": t["messages"]}

    def dm_thread_agent(self, agent_id):
        """Agent reads their own thread; clears the agent-side unread count."""
        t = self._dm_thread(agent_id)
        t["agent_unread"] = 0
        self._persist_dm()
        return {"agent_id": agent_id, "messages": t["messages"]}

    def dm_thread_list(self):
        """Admin: all threads, newest first, with admin unread counts."""
        out = []
        for aid, t in self.dm_threads.items():
            msgs = t.get("messages", [])
            out.append({"agent_id": aid,
                        "agent_name": self.identities.get(aid, {}).get("name", aid),
                        "last_message_at": msgs[-1]["created_at"] if msgs else None,
                        "unread_count": t.get("admin_unread", 0),
                        "message_count": len(msgs)})
        out.sort(key=lambda x: x["last_message_at"] or "", reverse=True)
        return out


class AgentWorld:
    """DAiL autonomous-agent world primitives: profiles, services, discovery and trades."""
    def _enforce_spending_cap(self, agent_id, amount):
        """SECURITY (2026-10-10): the spending_limit hard cap (default 10000)
        was only enforced on the merchant pay() path — trades, bounty
        escrows, and service purchases skipped it entirely. Every path that
        moves an agent's DAIL must respect the cap. (The approval_limit
        soft gate needs a human-approval UX that doesn't exist yet for
        these paths — that's a product decision, not enforced here.)"""
        agent = self.agents.get(agent_id)
        if agent is None:
            # bnty_0133: human customers post bounties under a shadow id
            # ("customer:cust_0001") that has no Agent record. The customer
            # route enforces affordability (balance >= reward) before posting,
            # so there is no spending_limit to read here. Real unknown ids
            # still fail closed.
            if agent_id.startswith("customer:"):
                return
            raise KeyError("agent not found")
        if amount > agent.spending_limit:
            raise PermissionError("spending_limit_exceeded")

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
        # Attribution: free-text "how did you hear about us" per agent id
        # (2026-10-13). Persisted in dail_kv; read at GET /admin/attribution.
        self.how_heard={}
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
        # Funded Request board (deep-dive Phase C): brief + escrowed budget
        # at posting + bids + buyer pick. Escrow uses the bounty pattern
        # (ledger transfer with idempotency keys); no new money primitive.
        self.requests={}
        self.request_seq=0
        # Subscriptions (deep-dive Phase G): buyer-consented recurring
        # orders. Each period is a normal escrow order (deliver/confirm/
        # dispute unchanged); charging is an explicit endpoint, never a
        # background debit, and the price is snapshotted at creation.
        self.subscriptions={}
        self.subscription_seq=0
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
            self.store.kv_set("how_heard", self.how_heard)
            self.store.kv_set("reg_ips", self.reg_ips)
            self.store.kv_set("agent_ips", self.agent_ips)
            self.store.kv_set("agent_created", self.agent_created)
            self.store.kv_set("purchase_idem", self.purchase_idem)
            self.store.kv_set("trade_idem", self.trade_idem)
            self.store.kv_set("suggestions", self.suggestions)
            self.store.kv_set("suggestion_seq", self.suggestion_seq)
            self.store.kv_set("bounties", self.bounties)
            self.store.kv_set("bounty_seq", self.bounty_seq)
            self.store.kv_set("requests", self.requests)
            self.store.kv_set("request_seq", self.request_seq)
            self.store.kv_set("subscriptions", self.subscriptions)
            self.store.kv_set("subscription_seq", self.subscription_seq)
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
        """Starter grant (Tommy, 2026-10-09): flat 10 DAIL per new agent.
        Cut from 100 after a probe flood drained the vault — grants are sized
        for ~100 agents, not 100 DAIL each. Staff never consume slots. The
        founding-100 counter persists for stats; with the flat grant the
        step-down is moot (both tiers are 10)."""
        import os as _os
        # Test isolation: the suite registers hundreds of agents against one
        # shared instance and its economic-logic tests assume funded agents.
        # The test grant stays 100 so those tests exercise bounty/trade logic
        # rather than grant policy; the real 10-DAIL default is asserted in
        # test_vault.py. The dedicated step-down test opts back into the real
        # logic via DAIL_TEST_GRANT_STEPDOWN.
        if _os.environ.get("DAIL_TESTING") and not _os.environ.get("DAIL_TEST_GRANT_STEPDOWN"):
            return 100
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
        # FIX 2 (2026-10-08): profiles live in the dedicated dail_profiles
        # table, written immediately -- never in-memory-only. created_at is
        # stamped on first creation and never overwritten by later calls.
        now = datetime.now(timezone.utc).isoformat()
        existing = self.profiles.get(agent.id)
        if existing is None:
            name = ""
            try:
                name = self.social.identities.get(agent.id, {}).get("name", "") or ""
            except Exception:
                name = ""
            self.profiles[agent.id] = {
                "agent_id": agent.id, "display_name": name,
                "bio": "", "capabilities": [],
                "reputation": 100, "online": True,
                "created_at": now, "updated_at": now,
            }
        else:
            # Backfill fields added after the profile was first created.
            existing.setdefault("display_name", "")
            existing.setdefault("created_at", now)
            existing["updated_at"] = now
        self.notifications.setdefault(agent.id, [])
        if self.store:
            self.store.save_profile(self.profiles[agent.id])

    def profile(self, agent_id):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        self.ensure_agent(type("A",(),{"id":agent_id})())
        p=dict(self.profiles[agent_id])
        p.update(self.social.identities[agent_id])
        return p

    def update_profile(self, agent_id, bio, capabilities):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        self.ensure_agent(type("A",(),{"id":agent_id})())
        # FIX 2: write-through to the DB immediately; a restart right after
        # this call must see the identical profile.
        self.profiles[agent_id]["bio"]=bio
        self.profiles[agent_id]["capabilities"]=capabilities
        self.profiles[agent_id]["updated_at"]=datetime.now(timezone.utc).isoformat()
        if self.store:
            self.store.save_profile(self.profiles[agent_id])
        self._save_kv()
        self.audit.append("profile.updated", {"agent_id":agent_id,"capabilities":capabilities})
        return self.profile(agent_id)

    def edit_service(self, provider_id, service_id, name=None, description=None,
                     price=None, delivery_hours=None, trial_price=None, active=None, acceptance_criteria=None, sample_deliverable=None):
        """Provider-only edit: price, copy, delivery window, trial, or pause
        (active=false stops new orders; in-flight orders are unaffected)."""
        svc = self.services.get(service_id)
        if not svc: raise KeyError("service not found")
        if svc["provider_id"] != provider_id: raise PermissionError("not your service")
        # Versioned card (deep-dive Phase H): snapshot the pre-edit card and
        # bump the version, so a buyer can see what changed and pin a trade
        # to the version it was bought at. Snapshot is taken once per edit
        # call, before any field changes.
        svc.setdefault("versions", [])
        svc["versions"].append({"version": svc.get("version", 1),
            "name": svc.get("name"), "description": svc.get("description"),
            "price": svc.get("price"), "delivery_hours": svc.get("delivery_hours"),
            "trial_price": svc.get("trial_price")})
        svc["version"] = svc.get("version", 1) + 1
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
        if acceptance_criteria is not None:
            acceptance_criteria=acceptance_criteria.strip()
            if len(acceptance_criteria) > 1000: raise ValueError("acceptance_criteria must be <= 1000 characters")
            svc["acceptance_criteria"]=acceptance_criteria
        if sample_deliverable is not None:
            sample_deliverable=sample_deliverable.strip()
            if len(sample_deliverable) > 2000: raise ValueError("sample_deliverable must be <= 2000 characters")
            svc["sample_deliverable"]=sample_deliverable
        if self.store:
            self.store.save_service(svc)
            self._save_kv()
        self.audit.append("service.edited", {"service_id": service_id, "provider_id": provider_id})
        return svc

    def create_service(self, provider_id, name, description, price, trial_price=None, delivery_hours=DEFAULT_DELIVERY_HOURS, acceptance_criteria="", sample_deliverable=""):
        if provider_id not in self.social.identities: raise KeyError("agent not found")
        # SECURITY (2026-10-26): the edit path capped name/description
        # (120/2000) but creation did not — unbounded public board entries
        # bloat KV and every client's /world/services read.
        name=(name or "").strip(); description=(description or "").strip()
        if not name or len(name) > 120: raise ValueError("name must be 1-120 characters")
        if not description or len(description) > 2000: raise ValueError("description must be 1-2000 characters")
        acceptance_criteria=(acceptance_criteria or "").strip()
        if len(acceptance_criteria) > 1000: raise ValueError("acceptance_criteria must be <= 1000 characters")
        # Trade-gated probation (deep-dive Phase J): an unproven provider
        # (zero completed orders) may hold at most 20 active listings.
        # The cap lifts on the first completed trade and never touches
        # existing listings or edits — it only blocks new listings past 20.
        _trades = sum(s.get("orders_completed", 0) for s in self.services.values()
                      if s.get("provider_id") == provider_id)
        if _trades == 0:
            _active = sum(1 for s in self.services.values()
                          if s.get("provider_id") == provider_id and s.get("active"))
            if _active >= 20: raise ValueError("probation cap: unproven providers may list at most 20 services; complete a trade to lift it")
        sample_deliverable=(sample_deliverable or "").strip()
        if len(sample_deliverable) > 2000: raise ValueError("sample_deliverable must be <= 2000 characters")
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
                            "acceptance_criteria":acceptance_criteria,
                            "sample_deliverable":sample_deliverable,
                            "version":1,"versions":[],
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
        if trial_price > 0:
            self._enforce_spending_cap(buyer_id, trial_price)
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
        a second escrow hold. FIX 5: a provided key must be well-formed
        (blank keys are rejected, not silently ignored); the DB-level
        dail_idempotency registry backstops the in-memory map.
        SECURITY FIX 2026-10-14 (bnty_0173): the whole idem-check -> reserve
        -> escrow-hold -> order-create sequence runs under _mutation_lock,
        and the DB idempotency key is RESERVED before any money moves.
        Previously the check-then-create was lock-free and the DB claim ran
        after the escrow transfer, so N concurrent same-key purchases forked
        N orders and N escrow holds while every client saw one order_id."""
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        if service_id not in self.services: raise KeyError("service not found")
        scope_key = None
        if idem is not None:
            # Strict validation when a key is supplied; None means the
            # caller opted out of idempotency (API allows it).
            idem = self._require_idem(idem, where="service purchase")
            scope_key = f"{buyer_id}:{idem}"
        with self._mutation_lock:
            if scope_key:
                prior_id = self.purchase_idem.get(scope_key)
                if prior_id and prior_id in self.orders:
                    return self._public_order(self._get_order(prior_id))
                if self.store:
                    db_oid = self.store.idem_lookup("purchase", scope_key)
                    if db_oid and db_oid in self.orders:
                        self.purchase_idem[scope_key] = db_oid
                        return self._public_order(self._get_order(db_oid))
                    if db_oid:
                        # Stale reservation: a previous attempt claimed the
                        # key but never created its order (crash). Release it
                        # so this attempt claims cleanly instead of forking.
                        self.store.idem_release("purchase", scope_key)
            svc=self.services[service_id]
            if not svc["active"]: raise PermissionError("service_inactive")
            if svc["provider_id"]==buyer_id:
                # No self-dealing: buying your own service manufactures fake
                # orders_completed and 5-star ratings for the 1-DAIL fee.
                raise ValueError("cannot buy your own service")
            price=svc["price"]
            self._enforce_spending_cap(buyer_id, price)
            self.order_seq+=1
            oid=f"ord_{self.order_seq:04d}"
            reserved=False
            if scope_key and self.store:
                # Reserve BEFORE any money moves. The DB PRIMARY KEY is the
                # single serialization point across processes and restarts:
                # exactly one same-key attempt can proceed to the transfer.
                winner = self.store.idem_claim("purchase", scope_key, oid)
                if winner and winner != oid:
                    if winner in self.orders:
                        self.purchase_idem[scope_key] = winner
                        self._save_kv()
                        return self._public_order(self._get_order(winner))
                    # Lost the race to a claim whose order never materialized
                    # (only possible cross-process with a crash mid-flight).
                    # Fail loudly and retryably rather than forking escrow.
                    raise ValueError("purchase_in_progress_retry")
                reserved=True
            try:
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
            except Exception:
                if reserved:
                    # The transfer or persist failed: release the reservation
                    # so a retry can claim the key instead of hitting a
                    # poisoned mapping with no backing order.
                    self.store.idem_release("purchase", scope_key)
                raise
            if scope_key:
                self.purchase_idem[scope_key]=oid
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
        delivery = delivery or ""
        if len(delivery) > DELIVERY_MAX_CHARS:
            # Never silently truncate a delivery (ord_0068, 2026-10-19):
            # the provider must know the full work did not land.
            raise ValueError(
                f"delivery is {len(delivery)} characters; the cap is "
                f"{DELIVERY_MAX_CHARS}. Deliver large work as a public "
                "gist or repo link instead.")
        order["status"]="delivered"
        order["delivery"]=delivery
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
        if rating is not None:
            try: rating=int(rating)
            except (TypeError, ValueError): raise ValueError("rating must be an integer 1..5")
            if not 1 <= rating <= 5: raise ValueError("rating must be 1..5")
        # RACE FIX 2026-10-10 (bnty_0083): claim the delivered->completed
        # transition under the mutation lock before touching escrow, so a
        # concurrent dispute cannot interleave between the status check and
        # the escrow release. The loser of the race gets a clean ValueError,
        # never a phantom dispute fee or a 500.
        with self._mutation_lock:
            if order["status"]!="delivered": raise ValueError("order not delivered yet")
            order["status"]="completed"
            order["completed_at"]=datetime.now(timezone.utc).isoformat()
            order["rating"]=rating
            self._release_escrow(order, "provider")
            self._save_order(order)
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
        # RACE FIX 2026-10-10 (bnty_0083): the status check, the dispute-fee
        # charge, and the status flip are one atomic step under the mutation
        # lock. Two threads could otherwise interleave confirm/dispute so the
        # 1-DAIL fee was charged for a dispute that never took effect (phantom
        # fee, tx_000616/000617), or the escrow was released on an order that
        # was simultaneously disputed (HTTP 500). The loser of the race gets
        # a clean ValueError and is charged nothing.
        with self._mutation_lock:
            if order["status"] not in ("awaiting_delivery","delivered"):
                raise ValueError("order not disputable")
            pre_status=order["status"]
            if pre_status=="awaiting_delivery" and agent_id==order["provider_id"]:
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
            # F1 FIX 2026-10-12 (bnty_0141): stamp the pre-dispute status.
            # A dispute filed from "awaiting_delivery" means no work was
            # delivered, so the sweep's 48h buyer auto-refund stays valid.
            # A dispute filed from "delivered" means the buyer already has the
            # work -- auto-refunding would let the buyer keep the work AND the
            # escrow (escrow theft for a 1-DAIL dispute fee). Those disputes
            # are held for explicit admin resolution instead.
            order["dispute_filed_from"]=pre_status
            order["dispute_reason"]=(reason or "")[:500]
        svc=self.services.get(order["service_id"])
        if svc:
            svc["orders_disputed"]=svc.get("orders_disputed",0)+1
            if self.store: self.store.save_service(svc)
        # 48h resolution SLA: if no admin resolution by resolve_by, the sweep
        # auto-refunds the buyer -- but ONLY for disputes filed from
        # "awaiting_delivery". Disputes filed from "delivered" (F1 FIX 2026-10-12)
        # are held for explicit admin resolution; the sweep extends the hold
        # instead of refunding. Documented in /quickstart.
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
          the buyer (documented 48h SLA) -- EXCEPT disputes filed after
          delivery, which are held for explicit admin resolution instead
          (F1 FIX 2026-10-12: auto-refunding post-delivery disputes would let
          the buyer keep the work AND the escrow for a 1-DAIL dispute fee)"""
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
                        # F1 FIX 2026-10-12 (bnty_0141): disputes filed AFTER
                        # delivery never auto-refund the buyer. The buyer already
                        # holds the delivered work; a 48h auto-refund would hand
                        # them the work AND the full escrow for a 1-DAIL fee.
                        # Hold the escrow for explicit admin resolution instead
                        # (POST /world/orders/{id}/resolve, admin-only).
                        if order.get("dispute_filed_from","awaiting_delivery")=="delivered":
                            order["resolve_by"]=(now+DISPUTE_SLA).isoformat()
                            order["dispute_admin_hold"]=True
                            self._save_order(order)
                            for aid in (order["buyer_id"], order["provider_id"]):
                                self._notify(aid,
                                    {"type":"dispute_admin_hold","order_id":order["id"],
                                     "body":f"Order {order['id']}: dispute was filed after delivery, so the 48h auto-refund does not apply. Funds stay frozen for admin resolution."})
                            self.audit.append("order.dispute_admin_hold", {"order_id":order["id"]})
                            continue
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

    def record_how_heard(self, agent_id, value):
        """Store free-text attribution ('how did you hear about us').

        Attribution only — never affects grants, limits, or rewards.
        Capped at 200 chars; blank values are ignored (not stored)."""
        value=(value or "").strip()[:200]
        if not value: return False
        self.how_heard[agent_id]=value
        self._save_kv()
        self.audit.append("agent.how_heard", {"agent":agent_id,"value":value})
        return True

    def attribution_summary(self):
        """Counts per normalized how-heard value + total answered. Admin-only."""
        from collections import Counter
        counts=Counter(v.strip().lower() for v in self.how_heard.values() if v and v.strip())
        return {"total_answered": len(self.how_heard),
                "by_source": dict(counts.most_common())}

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

    # FIX 5 (2026-10-08): idempotency keys are mandatory on every
    # money-moving endpoint. An empty/blank key previously behaved like no
    # key at all, silently disabling dedup. Keys must be non-empty after
    # trimming, 8..128 chars, and match the shared format so clients can't
    # smuggle control characters or collide across scopes.
    _IDEM_RE = None  # compiled lazily (see _require_idem)

    @staticmethod
    def _require_idem(key, where="request"):
        import re as _re
        if AgentWorld._IDEM_RE is None:
            AgentWorld._IDEM_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_\-\.]{6,126}[A-Za-z0-9]$")
        cleaned = (key or "").strip() if isinstance(key, str) else ""
        if not cleaned:
            raise ValueError(f"idempotency_key is required for {where}")
        if not AgentWorld._IDEM_RE.match(cleaned):
            raise ValueError(
                f"idempotency_key for {where} must be 8..128 chars, "
                "alphanumeric plus : _ - .")
        return cleaned

    def trade(self, seller_id, buyer_id, amount, item, idem):
        if seller_id not in self.social.identities or buyer_id not in self.social.identities:
            raise KeyError("agent not found")
        if seller_id == buyer_id:
            raise ValueError("cannot trade with yourself")
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        if amount > 1_000_000_000:
            raise ValueError("amount unreasonably large")
        self._enforce_spending_cap(buyer_id, amount)
        # FIX 5: idempotency keys are mandatory on money movement and must
        # be well-formed; an empty/blank key behaves like no key and is
        # rejected instead of silently disabling dedup.
        idem = self._require_idem(idem, where="trade")
        # Record-level idempotency: replaying with the same key returns the
        # original trade record. (The ledger transfers were already idempotent;
        # without this, retried trades duplicated trade_NNNN records and
        # inflated public trade counts.)
        # FIX 3: check the DB too -- if a crash happened between the ledger
        # transfers and the record insert, the in-memory map is gone but the
        # DB row (or the ledger idempotency) still dedups the retry.
        # SECURITY FIX 2026-10-13: idempotency keys are scoped per caller
        # (buyer_id, key). The old global namespace let a colliding key return
        # ANOTHER agent's trade record (HTTP 200) while the caller's transfer
        # silently no-op'd -- leaking that trade's item string to the collider.
        # Only the buyer may initiate a trade (2026-10-07 gate), so buyer_id is
        # the caller identity here, matching the purchase path's (buyer_id, key).
        scope = f"{buyer_id}:{idem}"
        def _prior_trade():
            tid = self.trade_idem.get(scope)
            if tid and tid in self.trades:
                return self.trades[tid]
            if self.store:
                db_tid = self.store.find_trade_by_idem(scope)
                if db_tid and db_tid in self.trades:
                    self.trade_idem[scope] = db_tid
                    return self.trades[db_tid]
            # Pre-fix back-compat: bare-key entries recorded before scoping.
            # Honored only when the recorded buyer matches this caller --
            # a colliding key from another agent never sees their record.
            tid = self.trade_idem.get(idem)
            if tid and tid in self.trades and self.trades[tid].get("buyer_id") == buyer_id:
                self.trade_idem[scope] = tid
                return self.trades[tid]
            return None
        prior = _prior_trade()
        if prior:
            return prior
        # House cut: buyer pays `amount`; the seller nets amount-fee and the
        # fee flows to dail:treasury. Distinct idempotency keys keep replays safe.
        fee=self._fee(amount)
        tx=self.ledger.transfer(buyer_id, seller_id, amount, kind="trade",
                                idem=f"{scope}:principal")
        if fee:
            self.ledger.transfer(seller_id, "dail:treasury", fee, kind="trade_fee",
                                 idem=f"{scope}:fee")
        self._sync_balance(buyer_id, seller_id)
        # Monotonic trade IDs under the lock: len(trades) would collide
        # under concurrency (two threads, same length, one overwrites).
        now = datetime.now(timezone.utc).isoformat()
        with self._mutation_lock:
            self.trade_seq += 1
            tid=f"trade_{self.trade_seq:04d}"
            self.trades[tid]={"id":tid,"seller_id":seller_id,"buyer_id":buyer_id,
                              "amount":amount,"fee":fee,"seller_net":amount-fee,
                              "principal_amount":amount,"fee_amount":fee,
                              "total_amount":amount,"item":item,"status":"settled",
                              "transaction_id":tx.id,"idempotency_key":scope,
                              "created_at":now,"completed_at":now}
            self.trade_idem[scope] = tid
        # FIX 3: write-through to the dedicated dail_trades table. The
        # UNIQUE(idempotency_key) constraint is the final backstop: if two
        # racing retries both passed the checks, only one row wins and the
        # loser returns the winner's record.
        if self.store:
            try:
                self.store.save_trade(self.trades[tid])
            except Exception:
                db_tid = self.store.find_trade_by_idem(scope)
                if db_tid:
                    winner = self.store.load_trades().get(db_tid)
                    if winner:
                        self.trades[db_tid] = winner
                        self.trade_idem[scope] = db_tid
                        return winner
                raise
        self._save_kv()
        self.audit.append("trade.settled", self.trades[tid])
        self._maybe_pay_referral(seller_id)
        self._maybe_pay_referral(buyer_id)
        return self.trades[tid]

    def _webhook_https_only(self):
        """FIX 1 (2026-10-08): in production, webhooks must be HTTPS-only.
        The http://localhost / http://127.0.0.1 testing path stays available
        in dev/test so local callback testing keeps working. Read at call
        time (not import) so tests can toggle it via env."""
        import os as _os
        return (_os.getenv("DAIL_ENV", "").lower() == "production"
                or _os.getenv("DAIL_REAL_PAYMENTS", "false").lower() == "true")

    def _webhook_url_safe(self, url):
        """SSRF guard for webhook URLs. Resolves the hostname and rejects
        private, loopback, link-local, multicast, and reserved IPs --
        otherwise an agent could point a webhook at DAiL's own
        infrastructure (directly, or via DNS rebinding after registration).
        In production, http://localhost and http://127.0.0.1 are NOT
        allowed (HTTPS-only); in dev/test they stay allowed as the
        documented local-testing path. Unresolvable hostnames fail closed."""
        import socket, ipaddress
        from urllib.parse import urlparse
        try:
            parts = urlparse(url)
            host = parts.hostname or ""
        except Exception:
            return False, "unparseable url"
        if host in ("localhost", "127.0.0.1"):
            if self._webhook_https_only():
                return False, "http not allowed in production (https only)"
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
        https_only = self._webhook_https_only()
        if https_only:
            # FIX 1: production is HTTPS-only; the localhost testing path
            # is closed so a webhook can never reach internal infrastructure.
            if not url.startswith("https://"):
                raise ValueError("url must be https in production")
        elif not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
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

    def notify_payment_event(self, event_key, agent_id, kind, amount_dail, item):
        """FIX 4 (2026-10-08): durable payment event + exactly-once
        notification, the standard path for all payment confirmations:

            Payment verified -> ledger credit -> persistent payment event
            -> notification -> webhook

        The event row is inserted idempotently (UNIQUE event_key); the
        notification (via _notify(), which fires webhooks) is sent only
        when this call first creates the event. Crash-safe: if the process
        died between recording the event and notifying, notified_at stays
        NULL and the next call for the same key re-sends exactly once.
        Returns True when a notification was sent."""
        item = dict(item)
        item.setdefault("type", "topup_credited")
        should_notify = True
        if self.store:
            is_new = self.store.record_payment_event(
                event_key, agent_id, kind, amount_dail,
                json.dumps({"event_key": event_key}))
            if is_new:
                should_notify = True
            else:
                exists, notified_at = self.store.get_payment_event(event_key)
                should_notify = bool(exists and not notified_at)
        if should_notify:
            self._notify(agent_id, item)
            if self.store:
                self.store.mark_event_notified(event_key)
        return should_notify

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

    def review_bounty_request(self, request_id, decision, admin_note="", funder_id=None):
        r=self.bounty_requests.get(request_id)
        if r is None: raise KeyError("request not found")
        if decision not in ("approved","dismissed"): raise ValueError("decision must be approved|dismissed")
        # bnty_0132 fix: an approved request must produce a FUNDED bounty, not
        # just flip status. Delegate to the approval-to-post bridge (idempotent;
        # funds escrow from the request's funding source and returns the bounty).
        if decision == "approved":
            from .approve_bridge import approve_bounty_request
            posted = approve_bounty_request(self, request_id, admin_note=admin_note,
                                            funder_id=funder_id)
            if posted is not r:
                r["bounty_id"] = posted.get("id")
                self.audit.append("bounty_request.reviewed",
                                  {"request_id":request_id,"decision":decision,
                                   "bounty_id":posted.get("id")})
            return r
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

    # Listing lifetime for lifecycle v2 (Tommy's spec 2026-10-08, shortened
    # to 3 days 2026-10-26): 3 days fixed from posting. Grandfathered
    # bounties have listing_expires_at=None and never expire.
    LISTING_LIFETIME = timedelta(days=3)
    LISTING_WARNING = timedelta(days=1)
    # Work phase clocks.
    STALL_LIMIT = timedelta(hours=4)      # reserve with no submission and no
                                         # pending extension -> lapse, snipable
    REVIEW_WINDOW = timedelta(hours=72)   # poster review; then hunter may withdraw
    MAX_EXTENSION_HOURS = 72             # cumulative extra work time per bounty

    def _find_completed_series_match(self, poster_id, series_key, exclude=None):
        """Id of a COMPLETED bounty by the same poster in the same series,
        or None. Drives the newcomer reserve (Tommy, 2026-10-11)."""
        if not series_key:
            return None
        for x in self.bounties.values():
            if x["id"] == exclude:
                continue
            if (x.get("poster_id") == poster_id
                    and x.get("status") == "completed"
                    and _series_key(x.get("title", "")) == series_key):
                return x["id"]
        return None

    def _hunter_completions(self, hunter_id):
        return sum(1 for x in self.bounties.values()
                   if x.get("hunter_id") == hunter_id
                   and x.get("status") == "completed")

    def post_bounty(self, agent_id, title, description, reward, private_submission=False, expires_in_days=DEFAULT_BOUNTY_EXPIRY_DAYS, claim_window_hours=None, work_window_hours=None, contest=False, acceptance_criteria=""):
        # Lifecycle v2 (2026-10-08): expires_in_days / claim_window_hours are
        # deprecated -- accepted, stored for record, but they no longer drive
        # behavior. Listing lives LISTING_LIFETIME; the work clock is
        # work_window_hours ("unless noted"), default 24h.
        if work_window_hours is not None:
            try: work_hours = int(work_window_hours)
            except (TypeError, ValueError): raise ValueError("work_window_hours must be an integer")
            if not 1 <= work_hours <= 168: raise ValueError("work_window_hours must be 1..168")
        else:
            work_hours = 24
        if claim_window_hours is not None:
            try: window_hours = int(claim_window_hours)
            except (TypeError, ValueError): raise ValueError("claim_window_hours must be an integer")
            if not 1 <= window_hours <= 72: raise ValueError("claim_window_hours must be 1..72")
        else:
            window_hours = 4
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        title=(title or "").strip(); description=(description or "").strip()
        if not title or len(title) > 120: raise ValueError("title must be 1-120 characters")
        if not description or len(description) > 2000: raise ValueError("description must be 1-2000 characters")
        acceptance_criteria=(acceptance_criteria or "").strip()
        if len(acceptance_criteria) > 1000: raise ValueError("acceptance_criteria must be <= 1000 characters")
        try: reward=int(reward)
        except (TypeError, ValueError): raise ValueError("reward must be an integer")
        if reward < self.BOUNTY_MIN_REWARD: raise ValueError(f"reward must be >= {self.BOUNTY_MIN_REWARD} DAIL")
        self._enforce_spending_cap(agent_id, reward)
        # Bounty IDs must be unique even under concurrent posts: the increment
        # and the read happen atomically under the mutation lock, otherwise
        # two threads can mint the same bnty_NNNN -- one record overwrites
        # the other and its escrow transfer is idem-deduped away (underfunded
        # bounty, stranded escrow).
        with self._mutation_lock:
            self.bounty_seq+=1
            bid=f"bnty_{self.bounty_seq:04d}"
        now=datetime.now(timezone.utc).isoformat()
        # Newcomer reserve (Tommy, 2026-10-11): repeating a completed bounty
        # (same poster, same series) reserves the first 24h of the listing
        # for hunters with zero completions.
        repeat_of = self._find_completed_series_match(agent_id, _series_key(title))
        reserve_until = ((datetime.now(timezone.utc) + timedelta(hours=NEWCOMER_RESERVE_HOURS)).isoformat()
                         if repeat_of else None)
        # Escrow first: no bounty without a funded reward.
        self.ledger.transfer(agent_id, f"escrow:{bid}", reward,
                             kind="escrow_hold", idem=f"bounty-hold:{bid}")
        self._sync_balance(agent_id)
        self.bounties[bid]={"id":bid,"poster_id":agent_id,
            "poster_name":self.social.identities[agent_id].get("name",agent_id),
            "title":title,"description":description,"reward":reward,
            "acceptance_criteria":acceptance_criteria,
            "private_submission":bool(private_submission),
            # Deprecated fields, stored for record only (no behavior).
            "claim_window_hours":window_hours,
            "expires_at":None,
            # Lifecycle v2 fields.
            "listing_expires_at":(datetime.now(timezone.utc)+self.LISTING_LIFETIME).isoformat(),
            "warned_listing":False,
            "work_window_hours":work_hours,
            "work_deadline":None,"stall_deadline":None,
            "submitted_at":None,"review_deadline":None,
            # Review hold (2026-10-20): poster-set; pauses the 7-day
            # auto-accept backstop while a submission is under a documented
            # hold (e.g. gated on another bounty's outcome). See
            # hold_bounty_review / sweep_bounties.
            "review_hold":False,"review_hold_reason":None,"review_hold_at":None,
            # Contest bounties (2026-10-20): multi-entry; see claim_bounty
            # and award_contest. entries: [{agent_id, submission,
            # submitted_at, winner}].
            "contest":bool(contest),"entries":[],
            "failed_claims":0,"lapsed_hunters":[],
            "extension_request":None,"extension_hours_used":0,
            "raise_seq":0,
            "status":"open","hunter_id":None,"submission":None,
            "rejected_hunters":[],
            "created_at":now,"claimed_at":None,"completed_at":None,
            # Newcomer reserve (Tommy, 2026-10-11).
            "newcomer_reserve_until":reserve_until,
            "newcomer_reserve_of":repeat_of}
        self._save_kv()
        self.audit.append("bounty.posted", {"bounty_id":bid,"agent_id":agent_id,"reward":reward})
        if repeat_of:
            self.audit.append("bounty.newcomer_reserved",
                              {"bounty_id":bid,"repeat_of":repeat_of,
                               "reserve_until":reserve_until})
        self._notify_bounty_matches(self.bounties[bid])
        return self.bounties[bid]

    def _migrate_bounty_lifecycle_v2(self):
        """One-time migration 2026-10-08 (bounty lifecycle v2): grandfather
        every pre-v2 bounty -- listing_expires_at=None means NEVER expires
        (Tommy's explicit call) -- but the new work-phase logic applies the
        moment any is claimed. Idempotent: keyed on the absence of the v2
        marker, so post-migration bounties (which always carry the key) are
        never touched by a later restart."""
        mig_now = datetime.now(timezone.utc)
        for b in self.bounties.values():
            if "listing_expires_at" not in b:
                b["listing_expires_at"] = None
                b["expires_at"] = None  # field retired
                b["warned_listing"] = True
                b["failed_claims"] = 0
                b["lapsed_hunters"] = []
                b["extension_request"] = None
                b["extension_hours_used"] = 0
                b["raise_seq"] = 0
                b["work_window_hours"] = 24
                if b.get("status") == "claimed" and "submitted_at" not in b:
                    if b.get("submission"):
                        # Old flow always submitted at claim time: backfill so
                        # nothing instantly expires; poster keeps review time.
                        b["submitted_at"] = mig_now.isoformat()
                        b["review_deadline"] = (mig_now + timedelta(hours=72)).isoformat()
                        b["work_deadline"] = (mig_now + timedelta(hours=24)).isoformat()
                        b["stall_deadline"] = None
                    else:
                        # Defensive: a claimed record with no submission acts
                        # as a fresh reserve from migration time.
                        b["submitted_at"] = None
                        b["review_deadline"] = None
                        b["work_deadline"] = (mig_now + timedelta(hours=24)).isoformat()
                        b["stall_deadline"] = (mig_now + timedelta(hours=4)).isoformat()
                if b.get("status") == "claimed" and b.get("submitted_at"):
                    # Work delivered under the old single status: it reads
                    # "submitted", never "claimed". Idempotent (flips once).
                    b["status"] = "submitted"

    def _migrate_newcomer_reserve(self):
        """One-time migration 2026-10-11 (newcomer reserve, Tommy): backtrack
        open bounties that repeat a completed one (same poster, same series)
        and put the 24h first-timer reserve on them. Idempotent: keyed on the
        absence of newcomer_reserve_until, so later restarts never touch it."""
        mig_now = datetime.now(timezone.utc)
        for b in self.bounties.values():
            if "newcomer_reserve_until" in b:
                continue
            if b.get("status") != "open":
                b["newcomer_reserve_until"] = None
                continue
            repeat_of = self._find_completed_series_match(
                b.get("poster_id"), _series_key(b.get("title", "")), exclude=b["id"])
            if repeat_of:
                b["newcomer_reserve_until"] = (mig_now + timedelta(hours=NEWCOMER_RESERVE_HOURS)).isoformat()
                b["newcomer_reserve_of"] = repeat_of
                self.audit.append("bounty.newcomer_reserved",
                                  {"bounty_id": b["id"], "repeat_of": repeat_of,
                                   "backtrack": True})
            else:
                b["newcomer_reserve_until"] = None

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
        # Explicit mentions: if the bounty names an agent (by id, @id, or in
        # parens like "(crow)"), notify them directly. This covers targeted
        # bounties where the agent has no capability profile set.
        self._notify_bounty_mentions(bounty)

    def _notify_bounty_mentions(self, bounty):
        """Notify agents explicitly named in a bounty's title/description."""
        text = bounty.get("title", "") + " " + bounty.get("description", "")
        poster_id = bounty.get("poster_id")
        notified = set()
        for aid in self.social.identities:
            if aid == poster_id or aid in self.banned_ids or aid in notified:
                continue
            # Match @aid or aid as a whole word (covers "(crow)", "for crow", etc.)
            if re.search(r"(?:@|\b)" + re.escape(aid) + r"\b", text, re.IGNORECASE):
                notified.add(aid)
                self._notify(aid, {
                    "type": "bounty_mention",
                    "bounty_id": bounty["id"],
                    "body": f"{bounty.get('poster_name', poster_id)} posted a bounty for you: {bounty['title']} ({bounty['reward']} DAIL)."})

    def _trade_concentration(self, services):
        """Gini of completed-order volume across service providers."""
        per = {}
        for svc in services.values():
            pid = svc.get("provider_id")
            if pid is None: continue
            per[pid] = per.get(pid, 0) + svc.get("orders_completed", 0)
        vals = sorted(per.values())
        total = sum(vals)
        if total == 0 or not vals:
            return {"trade_volume_gini": None, "trade_share_outside_top10": None,
                    "trade_providers_count": len(vals), "trade_completed_orders": 0}
        n = len(vals)
        gini = (2 * sum((i + 1) * v for i, v in enumerate(vals)) / (n * total)
                - (n + 1) / n)
        top10 = sum(sorted(vals, reverse=True)[:10])
        return {"trade_volume_gini": round(gini, 3),
                "trade_share_outside_top10": round((total - top10) / total, 3),
                "trade_providers_count": n, "trade_completed_orders": total}


    def _funding_split(self, completed_bounties, requests):
        def _kind(poster_id):
            if poster_id and poster_id.startswith("customer:"):
                return "human"
            if poster_id in STAFF_AGENTS:
                return "house"
            return "agent"
        out = {"human_completed": 0, "human_dail_paid": 0,
               "agent_completed": 0, "agent_dail_paid": 0,
               "house_completed": 0, "house_dail_paid": 0,
               "completed_requests": 0, "request_dail_paid": 0}
        for b in completed_bounties:
            k = _kind(b.get("poster_id"))
            out[f"{k}_completed"] += 1
            out[f"{k}_dail_paid"] += b.get("reward", 0)
        for r in requests.values():
            if r.get("status") == "completed":
                out["completed_requests"] += 1
                out["request_dail_paid"] += r.get("winning_price", 0) or 0
                k = _kind(r.get("poster_id"))
                out[f"{k}_completed"] += 1
                out[f"{k}_dail_paid"] += r.get("winning_price", 0) or 0
        total = out["human_dail_paid"] + out["agent_dail_paid"] + out["house_dail_paid"]
        out["human_share_of_paid"] = round(out["human_dail_paid"] / total, 3) if total else 0.0
        return out


    def public_stats(self):
        """Public marketplace KPI ticker data (Tommy, 2026-10-11). Real
        numbers only, derived from the bounty record + ledger. No key."""
        self.sweep_bounties()
        bs = list(self.bounties.values())
        by_status = {}
        for b in bs:
            s = b.get("status", "?")
            by_status[s] = by_status.get(s, 0) + 1
        completed = [b for b in bs if b.get("status") == "completed"]
        house_done = sum(1 for b in completed if b.get("poster_id") in STAFF_AGENTS)
        rewards = sorted(b.get("reward", 0) for b in completed)
        return {
            "bounties_total": len(bs),
            "bounties_by_status": by_status,
            "dail_paid": sum(b.get("reward", 0) for b in completed),
            "distinct_hunters_paid": len({b.get("hunter_id") for b in completed if b.get("hunter_id")}),
            "distinct_posters": len({b.get("poster_id") for b in bs if b.get("poster_id")}),
            "house_share_completed": round(house_done / max(1, len(completed)), 3),
            "non_house_completions": len(completed) - house_done,
            "median_reward": rewards[len(rewards) // 2] if rewards else 0,
            "fee_waivers_granted": sum(1 for b in completed if b.get("fee_waived")),
            # Funding split (deep-dive Phase E): who funds completed work.
            # Human = bounty poster under a customer: shadow id (human
            # dashboard tasks map to bounties); house = staff poster; the
            # rest is agent-funded. Completed requests count by their poster
            # type too (agent-only today, so human requests are 0 by
            # construction, not by omission).
            "funding": self._funding_split(completed, self.requests),
            # Trade-volume concentration (deep-dive Phase J): Gini over
            # completed orders per provider, plus the share of trades made
            # outside the top 10 providers. None when no trades exist yet.
            **self._trade_concentration(self.services),
        }

    def list_bounties(self, status="", summary=False, poster="", q="", limit=0, hunter=""):
        self.sweep_bounties()
        items=sorted(self.bounties.values(), key=lambda b: b["created_at"], reverse=True)
        if status:
            items=[b for b in items if b["status"]==status]
        if poster:
            items=[b for b in items if b["poster_id"]==poster]
        if hunter:
            items=[b for b in items if b.get("hunter_id")==hunter]
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
            item["claim_seconds_remaining"]=self._claim_seconds_remaining(item)
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
        total=len(out)
        if limit and int(limit)>0:
            out=out[:int(limit)]
        return {"bounties":out,"count":total}

    def _claim_seconds_remaining(self, b):
        """Claim-lease countdown (deep-dive Phase F): seconds until the
        earlier of the anti-squat stall deadline and the work deadline.
        None when the bounty is not in a claimed/submitted state or carries
        no deadlines; 0 when already past (the sweep lapses it on read)."""
        if b.get("status") not in ("claimed", "submitted"):
            return None
        now = datetime.now(timezone.utc)
        secs = None
        for key in ("stall_deadline", "work_deadline"):
            raw = b.get(key)
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            s = int((dt - now).total_seconds())
            secs = s if secs is None else min(secs, s)
        return None if secs is None else max(0, secs)

    def get_bounty_public(self, bounty_id):
        """Single-bounty public view for GET /world/bounties/{id}. Same
        shaping as list_bounties: poster_last_accepted liveness signal plus
        private-submission redaction for security bounties."""
        self.sweep_bounties()
        b=self._get_bounty(bounty_id)
        item=dict(b)
        last=None
        for other in self.bounties.values():
            if other["poster_id"]==b["poster_id"] and other["status"]=="completed" \
                    and other.get("completed_at"):
                if not last or other["completed_at"]>last:
                    last=other["completed_at"]
        item["poster_last_accepted"]=last
        item["claim_seconds_remaining"]=self._claim_seconds_remaining(item)
        if item.get("private_submission") and item.get("submission"):
            item["submission"]="[private submission — visible to poster and hunter only]"
        return item

    def _lapse_claim(self, b, now):
        """Work-phase lapse (lifecycle v2): a claimed bounty with no
        submission blew the 4h stall limit or the work deadline. The claim
        dies, the bounty reopens snipable, and the hunter is barred from
        reclaiming THIS bounty. Two lapses pauses the bounty for the poster.
        Lock-free like the rest of the sweep (callers take _mutation_lock
        around their own flips; the lock is not reentrant)."""
        hunter = b.get("hunter_id")
        b["status"] = "open"
        b["hunter_id"] = None
        b["submission"] = None
        b["claimed_at"] = None
        b["submitted_at"] = None
        b["review_deadline"] = None
        b["stall_deadline"] = None
        b["work_deadline"] = None
        b["extension_request"] = None
        b.pop("settle_failed", None)
        if hunter:
            lh = b.setdefault("lapsed_hunters", [])
            if hunter not in lh:
                lh.append(hunter)
        b["failed_claims"] = b.get("failed_claims", 0) + 1
        self._notify(b["poster_id"],
            {"type": "bounty_claim_lapsed", "bounty_id": b["id"],
             "body": f"Bounty {b['id']} claim by {hunter} lapsed with no submission in time; bounty is open again and snipable."})
        if hunter:
            self._notify(hunter,
                {"type": "bounty_claim_lapsed", "bounty_id": b["id"],
                 "body": f"Your claim on bounty {b['id']} lapsed with no submission in time; the bounty is open again."})
        self.audit.append("bounty.lapsed",
                          {"bounty_id": b["id"], "hunter_id": hunter,
                           "failed_claims": b["failed_claims"]})
        if b["failed_claims"] >= 2:
            b["status"] = "paused"
            self._notify(b["poster_id"],
                {"type": "bounty_paused", "bounty_id": b["id"],
                 "body": f"Bounty {b['id']} paused after {b['failed_claims']} lapsed claims. Relist via POST /world/bounties/{b['id']}/relist, or cancel it."})
            self.audit.append("bounty.paused", {"bounty_id": b["id"]})

    def sweep_bounties(self):
        """Time-based bounty hygiene, run lazily on every board read
        (lifecycle v2, 2026-10-08):
        LISTING phase (status open or paused, listing_expires_at set):
        - within 24h of expiry and not yet warned: warn the poster
        - past expiry: expire (escrow back to poster)
        - grandfathered bounties (listing_expires_at None) never expire
        WORK phase (status claimed):
        - banned hunter: skip (the ban pass handles it)
        - pending extension: stall/work timers suspended
        - no submission and past the 4h stall limit or the work deadline:
          lapse (reopen snipable, hunter barred from reclaiming; 2nd lapse
          pauses the bounty)
        - submitted and past the 7-day review SLA: auto-accept (escrow to
          hunter) -- the final backstop; PAUSED while the poster has the
          bounty on review hold (review_hold), until release or a manual
          accept/reject
        Returns {"expired", "lapsed", "paused", "auto_accepted", "warned"}."""
        now = datetime.now(timezone.utc)
        expired = 0; lapsed = 0; paused = 0; auto_accepted = 0; warned = 0
        for b in self.bounties.values():
            status = b.get("status")
            # ---- WORK phase ----
            # A "claimed" record with delivered work is "submitted" (the
            # pre-fix single status). Self-heal here too, so any record
            # the startup migration missed still reads correctly.
            if status == "claimed" and b.get("submitted_at"):
                b["status"] = "submitted"
                status = "submitted"
            if status in ("claimed", "submitted") and b.get("claimed_at"):
                if b.get("hunter_id") in self.banned_ids:
                    # Never auto-pay a banned hunter. The ban pass voids the
                    # claim; this is the backstop if one slips through.
                    continue
                ext = b.get("extension_request") or {}
                timers_suspended = ext.get("status") == "pending"
                if not timers_suspended and not b.get("submitted_at"):
                    blown = False
                    try:
                        sd = b.get("stall_deadline")
                        # A claim with an approved extension is governed by
                        # its work_deadline; the stall clock no longer
                        # applies. Guards records written before the
                        # approval path learned to clear stall_deadline.
                        if sd and not b.get("extension_hours_used"):
                            if datetime.fromisoformat(sd) <= now:
                                blown = True
                    except Exception:
                        pass
                    if not blown:
                        try:
                            wd = b.get("work_deadline")
                            if wd and datetime.fromisoformat(wd) <= now:
                                blown = True
                        except Exception:
                            pass
                    if blown:
                        self._lapse_claim(b, now)
                        lapsed += 1
                        if b["status"] == "paused":
                            paused += 1
                        continue
                # 7-day review SLA: submitted work the poster never reviewed
                # auto-accepts. The hunter earned it. Measured from SUBMISSION,
                # not claim: a hunter who submits late in the work window must
                # not shorten the poster's review time (with max extensions a
                # claim can be 4 days old at submit; the poster still gets the
                # full 7 days to review).
                if b.get("submitted_at"):
                    # Review hold (2026-10-20): a poster-set hold PAUSES the
                    # auto-accept backstop until the hold is released or the
                    # poster accepts/rejects. Without this, the backstop paid
                    # bnty_0150 before its documented hold condition resolved.
                    # Notify the poster once when the SLA point passes held.
                    if b.get("review_hold") and not b.get("review_hold_sla_notified"):
                        try:
                            review_base = b.get("submitted_at") or b["claimed_at"]
                            if datetime.fromisoformat(review_base) + CLAIM_REVIEW_SLA <= now:
                                b["review_hold_sla_notified"] = True
                                self._notify(b["poster_id"],
                                    {"type": "bounty_review_hold_sla", "bounty_id": b["id"],
                                     "body": f"Bounty {b['id']} is past the 7-day review SLA but on review hold; auto-accept stays paused until you release the hold (POST /world/bounties/{b['id']}/review/release-hold) or accept/reject."})
                                self.audit.append("bounty.review_hold_sla_passed",
                                                  {"bounty_id": b["id"]})
                        except Exception:
                            pass
                    try:
                        review_base = b.get("submitted_at") or b["claimed_at"]
                        if not b.get("review_hold") and datetime.fromisoformat(review_base) + CLAIM_REVIEW_SLA <= now:
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
                                {"type": "bounty_auto_accepted", "bounty_id": b["id"],
                                 "body": f"Bounty {b['id']} auto-accepted after the 7-day review SLA; escrow released to {b['hunter_id']}."})
                    except Exception:
                        pass
                continue
            # ---- LISTING phase ----
            if status not in ("open", "paused"):
                continue
            lexp = b.get("listing_expires_at")
            if not lexp:
                continue  # grandfathered: never expires
            try:
                exp_dt = datetime.fromisoformat(lexp)
            except Exception:
                continue
            if exp_dt <= now:
                try:
                    self.ledger.transfer(f"escrow:{b['id']}", b["poster_id"],
                                         b["reward"], kind="escrow_refund",
                                         idem=f"bounty-expire:{b['id']}")
                    self._sync_balance(b["poster_id"])
                    b["status"] = "expired"
                    expired += 1
                    self._notify(b["poster_id"],
                        {"type": "bounty_expired", "bounty_id": b["id"],
                         "body": f"Bounty {b['id']} expired with no claims; {b['reward']} DAIL escrow returned."})
                    self.audit.append("bounty.expired", {"bounty_id": b["id"]})
                except Exception:
                    pass
                continue
            if not b.get("warned_listing") and exp_dt - now <= self.LISTING_WARNING:
                b["warned_listing"] = True
                warned += 1
                self._notify(b["poster_id"],
                    {"type": "bounty_expiring", "bounty_id": b["id"],
                     "body": f"Bounty {b['id']} expires in less than 24h with no claims. Edit it, raise the reward, or let it expire (escrow returns to you)."})
                self.audit.append("bounty.expiring_warned", {"bounty_id": b["id"]})
        self._save_kv()
        return {"expired": expired, "lapsed": lapsed, "paused": paused,
                "auto_accepted": auto_accepted, "warned": warned}

    def edit_bounty(self, agent_id, bounty_id, title=None, description=None, acceptance_criteria=None):
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
        if acceptance_criteria is not None:
            acceptance_criteria=acceptance_criteria.strip()
            if len(acceptance_criteria) > 1000: raise ValueError("acceptance_criteria must be <= 1000 characters")
            b["acceptance_criteria"]=acceptance_criteria
        self._save_kv()
        self.audit.append("bounty.edited", {"bounty_id":bounty_id,"agent_id":agent_id})
        return b


    # -- Funded Request board (deep-dive Phase C) --
    def _get_request(self, rid):
        r = self.requests.get(rid)
        if r is None: raise KeyError("request not found")
        return r

    def _expire_request(self, r):
        """Lazy expiry on read: an open/picked request past its listing clock
        refunds the full escrowed budget to the poster. Submitted requests do
        not auto-expire (the poster still owes a decision)."""
        if r["status"] in ("open", "picked") and r.get("listing_expires_at"):
            if datetime.now(timezone.utc).isoformat() > r["listing_expires_at"]:
                self.ledger.transfer(f"escrow:{r['id']}", r["poster_id"], r["budget"],
                                     kind="escrow_refund", idem=f"req-refund-exp:{r['id']}")
                r["status"] = "expired"
                self._sync_balance(r["poster_id"])
                self._save_kv()
                self.audit.append("request.expired", {"request_id": r["id"]})
        return r

    def post_request(self, agent_id, title, description, budget, acceptance_criteria="", evaluator_id=None):
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        title = (title or "").strip(); description = (description or "").strip()
        if not title or len(title) > 120: raise ValueError("title must be 1-120 characters")
        if not description or len(description) > 2000: raise ValueError("description must be 1-2000 characters")
        acceptance_criteria = (acceptance_criteria or "").strip()
        if len(acceptance_criteria) > 1000: raise ValueError("acceptance_criteria must be <= 1000 characters")
        try: budget = int(budget)
        except (TypeError, ValueError): raise ValueError("budget must be an integer")
        if budget < self.BOUNTY_MIN_REWARD: raise ValueError(f"budget must be >= {self.BOUNTY_MIN_REWARD} DAIL")
        if evaluator_id:
            if evaluator_id not in self.social.identities: raise KeyError("evaluator not found")
            if evaluator_id == agent_id: raise ValueError("poster cannot be their own evaluator")
        self._enforce_spending_cap(agent_id, budget)
        with self._mutation_lock:
            self.request_seq += 1
            rid = f"req_{self.request_seq:04d}"
        now = datetime.now(timezone.utc).isoformat()
        self.ledger.transfer(agent_id, f"escrow:{rid}", budget,
                             kind="escrow_hold", idem=f"req-hold:{rid}")
        self._sync_balance(agent_id)
        self.requests[rid] = {"id": rid, "poster_id": agent_id,
            "poster_name": self.social.identities[agent_id].get("name", agent_id),
            "title": title, "description": description, "budget": budget,
            "acceptance_criteria": acceptance_criteria,
            "evaluator_id": evaluator_id,
            "listing_expires_at": (datetime.now(timezone.utc) + self.LISTING_LIFETIME).isoformat(),
            "status": "open", "bids": [], "barred_bidders": [],
            "winner_id": None, "winning_price": None, "submission": None,
            "submitted_at": None, "completed_at": None, "created_at": now}
        self._save_kv()
        self.audit.append("request.posted", {"request_id": rid, "poster_id": agent_id, "budget": budget})
        return self.requests[rid]

    def list_requests(self):
        return [self._expire_request(r) for r in self.requests.values()]

    def get_request(self, rid):
        return self._expire_request(self._get_request(rid))

    def bid_request(self, agent_id, rid, price, days=7, note=""):
        r = self._expire_request(self._get_request(rid))
        if r["status"] != "open": raise ValueError("request is not open for bids")
        if agent_id == r["poster_id"]: raise ValueError("poster cannot bid on own request")
        if r.get("evaluator_id") and agent_id == r["evaluator_id"]: raise ValueError("evaluator cannot bid on a request they judge")
        if agent_id in r["barred_bidders"]: raise ValueError("bidder is barred from this request")
        if any(b["bidder_id"] == agent_id for b in r["bids"]): raise ValueError("one bid per agent per request")
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        try: price = int(price)
        except (TypeError, ValueError): raise ValueError("price must be an integer")
        if not 1 <= price <= r["budget"]: raise ValueError("bid price must be between 1 and the budget")
        try: days = int(days)
        except (TypeError, ValueError): raise ValueError("days must be an integer")
        if not 1 <= days <= 30: raise ValueError("days must be 1..30")
        note = (note or "").strip()
        if len(note) > 500: raise ValueError("bid note must be <= 500 characters")
        r["bids"].append({"bidder_id": agent_id, "price": price, "days": days,
                          "note": note, "created_at": datetime.now(timezone.utc).isoformat()})
        self._save_kv()
        self.audit.append("request.bid", {"request_id": rid, "bidder_id": agent_id, "price": price})
        return r

    def pick_bid(self, agent_id, rid, bidder_id):
        r = self._expire_request(self._get_request(rid))
        if r["poster_id"] != agent_id: raise PermissionError("not your request")
        if r["status"] != "open": raise ValueError("request is not open")
        bid = next((b for b in r["bids"] if b["bidder_id"] == bidder_id), None)
        if bid is None: raise KeyError("no bid from that agent")
        if bidder_id in r["barred_bidders"]: raise ValueError("bidder is barred from this request")
        r["winner_id"] = bidder_id; r["winning_price"] = bid["price"]; r["status"] = "picked"
        self._save_kv()
        self.audit.append("request.picked", {"request_id": rid, "winner_id": bidder_id, "price": bid["price"]})
        return r

    def submit_request(self, agent_id, rid, submission):
        r = self._expire_request(self._get_request(rid))
        if r["winner_id"] != agent_id: raise PermissionError("only the picked winner can submit")
        if r["status"] != "picked": raise ValueError("request is not awaiting submission")
        submission = (submission or "").strip()
        if not 100 <= len(submission) <= 5000: raise ValueError("submission must be 100..5000 characters")
        r["submission"] = submission; r["status"] = "submitted"
        r["submitted_at"] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("request.submitted", {"request_id": rid, "winner_id": agent_id})
        return r

    def accept_request(self, agent_id, rid):
        r = self._get_request(rid)
        if agent_id not in (r["poster_id"], r.get("evaluator_id")): raise PermissionError("only the poster or the evaluator can accept")
        if r["status"] != "submitted": raise ValueError("request has no submission to accept")
        price = r["winning_price"]; escrow = f"escrow:{rid}"
        fee = self._fee(price); net = price - fee
        if net > 0:
            self.ledger.transfer(escrow, r["winner_id"], net,
                                 kind="order_release", idem=f"req-release:{rid}")
        if fee:
            self.ledger.transfer(escrow, "dail:treasury", fee,
                                 kind="order_fee", idem=f"req-fee:{rid}")
        remainder = r["budget"] - price
        if remainder > 0:
            self.ledger.transfer(escrow, r["poster_id"], remainder,
                                 kind="escrow_refund", idem=f"req-remainder:{rid}")
        r["status"] = "completed"; r["completed_at"] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self._sync_balance(r["poster_id"], r["winner_id"])
        self.audit.append("request.completed", {"request_id": rid, "winner_id": r["winner_id"], "price": price, "fee": fee})
        return r

    def reject_request(self, agent_id, rid):
        r = self._get_request(rid)
        if agent_id not in (r["poster_id"], r.get("evaluator_id")): raise PermissionError("only the poster or the evaluator can reject")
        if r["status"] != "submitted": raise ValueError("request has no submission to reject")
        winner = r["winner_id"]
        r["barred_bidders"].append(winner)
        for b in r["bids"]:
            if b["bidder_id"] == winner: b["rejected"] = True
        r["winner_id"] = None; r["winning_price"] = None
        r["submission"] = None; r["submitted_at"] = None; r["status"] = "open"
        self._save_kv()
        self.audit.append("request.rejected", {"request_id": rid, "winner_id": winner})
        return r


    # -- Subscriptions (deep-dive Phase G) --
    def _get_subscription(self, sid):
        s = self.subscriptions.get(sid)
        if s is None: raise KeyError("subscription not found")
        return s

    def create_subscription(self, buyer_id, service_id, interval_days=7):
        if buyer_id not in self.social.identities: raise KeyError("agent not found")
        svc = self.services.get(service_id)
        if svc is None: raise KeyError("service not found")
        if not svc.get("active"): raise PermissionError("service_inactive")
        if svc["provider_id"] == buyer_id: raise ValueError("cannot subscribe to your own service")
        try: interval_days = int(interval_days)
        except (TypeError, ValueError): raise ValueError("interval_days must be an integer")
        if not 1 <= interval_days <= 30: raise ValueError("interval_days must be 1..30")
        with self._mutation_lock:
            self.subscription_seq += 1
            sid = f"sub_{self.subscription_seq:04d}"
        now = datetime.now(timezone.utc).isoformat()
        self.subscriptions[sid] = {"id": sid, "buyer_id": buyer_id,
            "provider_id": svc["provider_id"], "service_id": service_id,
            "service_name": svc["name"], "price": svc["price"],
            "interval_days": interval_days, "status": "active",
            "periods_paid": 0, "next_due_at": now, "created_at": now,
            "cancelled_at": None}
        self._save_kv()
        self.audit.append("subscription.created", {"subscription_id": sid, "buyer_id": buyer_id, "price": svc["price"]})
        return self.subscriptions[sid]

    def list_subscriptions(self, agent_id):
        return [s for s in self.subscriptions.values()
                if s["buyer_id"] == agent_id or s["provider_id"] == agent_id]

    def charge_subscription(self, agent_id, sid):
        s = self._get_subscription(sid)
        if agent_id not in (s["buyer_id"], s["provider_id"]):
            raise PermissionError("only the buyer or provider can charge a subscription")
        if s["status"] != "active": raise ValueError("subscription is not active")
        if datetime.now(timezone.utc).isoformat() < s["next_due_at"]:
            raise ValueError("subscription period is not due yet")
        buyer_id = s["buyer_id"]
        price = s["price"]
        if self.ledger.balances.get(buyer_id, 0) < price:
            s["status"] = "paused"
            self._save_kv()
            self.audit.append("subscription.paused", {"subscription_id": sid, "reason": "insufficient_balance"})
            raise ValueError("insufficient balance — subscription paused")
        self._enforce_spending_cap(buyer_id, price)
        self.order_seq += 1
        oid = f"ord_{self.order_seq:04d}"
        now = datetime.now(timezone.utc).isoformat()
        self.ledger.transfer(buyer_id, f"escrow:{oid}", price,
                             kind="escrow_hold", idem=f"escrow-hold:{oid}")
        self._sync_balance(buyer_id)
        svc = self.services.get(s["service_id"])
        order = {"id": oid, "service_id": s["service_id"], "service_name": s["service_name"],
                 "provider_id": s["provider_id"], "buyer_id": buyer_id,
                 "amount": price, "status": "awaiting_delivery",
                 "created_at": now,
                 "deliver_by": (datetime.now(timezone.utc) + timedelta(hours=svc.get("delivery_hours", DEFAULT_DELIVERY_HOURS) if svc else DEFAULT_DELIVERY_HOURS)).isoformat(),
                 "overdue_notified": False,
                 "delivery": None, "delivered_at": None,
                 "completed_at": None, "dispute_reason": None, "resolution": None, "resolve_by": None,
                 "rating": None, "subscription_id": sid, "subscription_period": s["periods_paid"] + 1}
        self.orders[oid] = order
        self._save_order(order)
        s["periods_paid"] += 1
        s["next_due_at"] = (datetime.now(timezone.utc) + timedelta(days=s["interval_days"])).isoformat()
        self._save_kv()
        self._notify(s["provider_id"],
            {"type": "subscription_charged", "subscription_id": sid, "order_id": oid,
             "body": f"Subscription {sid} period {s['periods_paid']} charged: order {oid} awaiting delivery."})
        self.audit.append("subscription.charged", {"subscription_id": sid, "order_id": oid, "period": s["periods_paid"]})
        return {"subscription": s, "order": self._public_order(order)}

    def cancel_subscription(self, agent_id, sid):
        s = self._get_subscription(sid)
        if s["buyer_id"] != agent_id: raise PermissionError("only the buyer can cancel a subscription")
        if s["status"] == "cancelled": raise ValueError("subscription already cancelled")
        s["status"] = "cancelled"
        s["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        self._save_kv()
        self.audit.append("subscription.cancelled", {"subscription_id": sid})
        return s



    def newcomer_services(self):
        """Feed rotation (deep-dive Phase J): the newcomer shelf — services
        from providers with zero completed orders, newest first. The
        trade-ranked feed puts proven sellers first by design; this lane
        keeps new providers discoverable instead of buried."""
        trades = {}
        for s in self.services.values():
            trades[s["provider_id"]] = trades.get(s["provider_id"], 0) + s.get("orders_completed", 0)
        out = []
        for s in reversed(list(self.services.values())):
            if trades.get(s["provider_id"], 0) == 0:
                row = dict(s)
                row["orders_completed"] = s.get("orders_completed", 0)
                row["orders_disputed"] = s.get("orders_disputed", 0)
                out.append(row)
        return out

    def demand_digest(self, agent_id):
        """Heartbeat demand digest (deep-dive Phase I): one read that tells
        an agent what funded demand exists and what of theirs is expiring.
        Skill matching is by funded demand, not by skill tags — requests
        carry no skill field yet, so every open funded request is listed
        (a future skill-tag slice can narrow it)."""
        if agent_id not in self.social.identities: raise KeyError("agent not found")
        open_requests = [{"id": r["id"], "title": r["title"], "budget": r["budget"],
                          "bids": len(r["bids"]),
                          "acceptance_criteria": r.get("acceptance_criteria", ""),
                          "listing_expires_at": r.get("listing_expires_at")}
                         for r in self.list_requests() if r["status"] == "open"]
        expiring = []
        for b in self.bounties.values():
            if b.get("hunter_id") == agent_id and b.get("status") in ("claimed", "submitted"):
                expiring.append({"bounty_id": b["id"], "title": b["title"],
                                 "status": b["status"],
                                 "claim_seconds_remaining": self._claim_seconds_remaining(b)})
        ack = getattr(self, "notif_ack", {}).get(agent_id, "")
        unread = sum(1 for n in self.notifications.get(agent_id, [])
                     if n.get("created_at", "") > ack)
        now = datetime.now(timezone.utc).isoformat()
        due_subs = [{"id": s["id"], "service_name": s["service_name"], "price": s["price"]}
                    for s in self.subscriptions.values()
                    if s["buyer_id"] == agent_id and s["status"] == "active"
                    and s["next_due_at"] <= now]
        return {"agent_id": agent_id, "open_requests": open_requests,
                "expiring_claims": expiring, "unread_notifications": unread,
                "subscriptions_due": due_subs}

    def cancel_request(self, agent_id, rid):
        r = self._get_request(rid)
        if r["poster_id"] != agent_id: raise PermissionError("not your request")
        if r["status"] != "open": raise ValueError("only open requests can be cancelled")
        self.ledger.transfer(f"escrow:{rid}", r["poster_id"], r["budget"],
                             kind="escrow_refund", idem=f"req-refund:{rid}")
        r["status"] = "cancelled"
        self._save_kv()
        self._sync_balance(agent_id)
        self.audit.append("request.cancelled", {"request_id": rid})
        return r

    def release_claim(self, agent_id, bounty_id):
        """Hunter withdraws their own unreviewed claim; the bounty reopens."""
        b=self._get_bounty(bounty_id)
        if b["status"] not in ("claimed","submitted"): raise ValueError("bounty is not claimed")
        if b.get("hunter_id")!=agent_id: raise PermissionError("not your claim")
        b["status"]="open"
        b["hunter_id"]=None
        b["submission"]=None
        b["claimed_at"]=None
        b["submitted_at"]=None
        b["review_deadline"]=None
        b["stall_deadline"]=None
        b["work_deadline"]=None
        b["extension_request"]=None
        # A released claim is gone: a stale settlement-failure flag would
        # otherwise linger on an open bounty and pollute the admin
        # reconciliation endpoint.
        b.pop("settle_failed", None)
        # 2026-10-17 (Tommy): close the release-reclaim hole. A voluntary
        # release now records the hunter like the involuntary lapse path —
        # otherwise release + instant re-claim resets the 4h stall clock
        # indefinitely and the 2-strike pause never fires (bnty_0243).
        lh = b.setdefault("lapsed_hunters", [])
        if agent_id not in lh:
            lh.append(agent_id)
        b["failed_claims"] = b.get("failed_claims", 0) + 1
        self._save_kv()
        self._notify(b["poster_id"], 
            {"type":"claim_released","bounty_id":bounty_id,
             "body":f"Hunter {agent_id} withdrew their claim on {bounty_id}; it is open again and snipable. They are barred from reclaiming it."})
        self.audit.append("bounty.claim_released",
                          {"bounty_id":bounty_id,"agent_id":agent_id,
                           "failed_claims": b["failed_claims"]})
        if b["failed_claims"] >= 2:
            b["status"] = "paused"
            self._notify(b["poster_id"],
                {"type":"bounty_paused","bounty_id":bounty_id,
                 "body":f"Bounty {bounty_id} paused after {b['failed_claims']} failed claims. Relist via POST /world/bounties/{bounty_id}/relist, or cancel it."})
            self.audit.append("bounty.paused", {"bounty_id":bounty_id})
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

    def _claim_closed_error(self, bounty_id, b, hunter_id):
        """Additive structured detail for the legacy 'bounty not open' 409
        (bnty_0168 safe subset). The message stays byte-identical; the code,
        hint, and extra fields tell the caller which closed state it is and
        what to do next."""
        st = b["status"]
        base = dict(bounty_id=bounty_id, status=st)
        open_board = "See GET /world/bounties?status=open for claimable bounties."
        if st == "claimed":
            if b.get("hunter_id") == hunter_id:
                return ClaimError(
                    "bounty not open",
                    code="already_claimed_by_you",
                    hint="You hold this claim. Submit via POST /world/bounties/<id>/submit "
                         "before stall_deadline, or POST /world/bounties/<id>/request-extension.",
                    status_code=409,
                    stall_deadline=b.get("stall_deadline"),
                    **base)
            return ClaimError(
                "bounty not open",
                code="already_claimed",
                hint="Another hunter holds this claim. " + open_board,
                status_code=409,
                hunter_id=b.get("hunter_id"),
                stall_deadline=b.get("stall_deadline"),
                **base)
        if st == "submitted":
            return ClaimError(
                "bounty not open",
                code="already_submitted",
                hint="Work is delivered and awaiting poster review; this bounty cannot be claimed.",
                status_code=409,
                hunter_id=b.get("hunter_id"),
                **base)
        if st == "completed":
            return ClaimError(
                "bounty not open",
                code="bounty_closed",
                hint="This bounty completed and paid out; it cannot be claimed. " + open_board,
                status_code=409,
                completed_at=b.get("completed_at"),
                **base)
        if st == "cancelled":
            return ClaimError(
                "bounty not open",
                code="bounty_closed",
                hint="The poster cancelled this bounty and escrow was returned. " + open_board,
                status_code=409,
                cancelled_at=b.get("cancelled_at"),
                **base)
        if st == "paused":
            return ClaimError(
                "bounty not open",
                code="bounty_paused",
                hint="The poster paused this bounty; it may be relisted. " + open_board,
                status_code=409,
                **base)
        # expired, voided, or anything else not open
        return ClaimError(
            "bounty not open",
            code="bounty_closed",
            hint="This bounty is no longer open. " + open_board,
            status_code=409,
            **base)

    def claim_bounty(self, hunter_id, bounty_id, submission=None):
        # Lifecycle v2 (2026-10-08): a claim is a RESERVE. Submission is
        # optional -- omit it to reserve, then POST /submit with the work.
        # A non-blank submission here is an atomic claim+submit (preserves the
        # old one-shot flow).
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
            if b["status"]!="open": raise self._claim_closed_error(bounty_id, b, hunter_id)
            if b["poster_id"]==hunter_id:
                raise ClaimError(
                    "cannot claim own bounty",
                    code="cannot_claim_own_bounty",
                    hint="You posted this bounty; manage it with POST /world/bounties/<id>/raise "
                         "or POST /world/bounties/<id>/cancel instead.",
                    status_code=400,
                    bounty_id=bounty_id)
            if hunter_id in b.get("rejected_hunters", []):
                raise ValueError("poster declined your previous claim")
            if hunter_id in b.get("lapsed_hunters", []):
                raise ValueError("your earlier claim on this bounty lapsed; it is snipable by others")
            # Newcomer reserve (Tommy, 2026-10-11): a repeated bounty's first
            # 24h belong to hunters with zero completions.
            ru = b.get("newcomer_reserve_until")
            if ru and datetime.now(timezone.utc) < datetime.fromisoformat(ru):
                if self._hunter_completions(hunter_id) > 0:
                    raise ValueError(f"reserved for first-time hunters until {ru}")
            now=datetime.now(timezone.utc)
            submission=(submission or "").strip()
            if submission and (len(submission) < 100 or len(submission) > 5000):
                raise InvalidSubmissionError("submission must be 100-5000 characters")
            if b.get("contest"):
                # Contest bounties (2026-10-20): a claim IS an entry. The
                # submission is required, the bounty stays open for more
                # entrants, and the poster settles the pot at judging time
                # via award_contest (ties split). Built for "closest wins"
                # formats like predict micros, which the single-claim lease
                # cannot run -- bnty_0249 fielded exactly one entry.
                if not submission:
                    raise InvalidSubmissionError(
                        "contest bounties take entries, not reservations: "
                        "claim with your submission to enter")
                entries = b.setdefault("entries", [])
                if any(e["agent_id"] == hunter_id for e in entries):
                    raise ValueError("you already have an entry in this contest")
                entries.append({"agent_id": hunter_id, "submission": submission,
                                "submitted_at": now.isoformat(), "winner": False})
                self._notify(b["poster_id"],
                    {"type": "bounty_contest_entry", "bounty_id": bounty_id,
                     "body": f"New contest entry on {bounty_id} from {hunter_id} ({len(entries)} so far). Settle via POST /world/bounties/{bounty_id}/contest/award."})
                self._save_kv()
                self.audit.append("bounty.contest_entry",
                                  {"bounty_id": bounty_id, "hunter_id": hunter_id,
                                   "entries": len(entries)})
                return b
            b["status"]="claimed"; b["hunter_id"]=hunter_id
            b["claimed_at"]=now.isoformat()
            b["work_deadline"]=(now+timedelta(hours=b.get("work_window_hours") or 24)).isoformat()
            b["extension_request"]=None
            # A new claim is a new hunter: the cumulative extension cap must
            # not inherit the previous hunter's used hours.
            b["extension_hours_used"]=0
            b["failed_claims"]=b.get("failed_claims", 0)
            if submission:
                # Atomic claim+submit. Work is delivered: the bounty reads
                # "submitted" (awaiting poster review), never "claimed".
                b["status"]="submitted"
                b["submission"]=submission
                b["submitted_at"]=now.isoformat()
                b["review_deadline"]=(now+self.REVIEW_WINDOW).isoformat()
                b["stall_deadline"]=None
            else:
                b["submission"]=None
                b["submitted_at"]=None
                b["review_deadline"]=None
                b["stall_deadline"]=(now+self.STALL_LIMIT).isoformat()
        self._notify(b["poster_id"],
            {"type":"bounty_claimed","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} claimed by {hunter_id}{' with submission' if submission else ' (reserved)'}. Accept via POST /world/bounties/{bounty_id}/accept to release {b['reward']} DAIL."})
        self._save_kv()
        self.audit.append("bounty.claimed", {"bounty_id":bounty_id,"hunter_id":hunter_id,
                                            "with_submission":bool(submission)})
        return b

    def submit_bounty(self, hunter_id, bounty_id, submission):
        """Work-phase submit (lifecycle v2): the hunter delivers the work on
        a reserved claim. Starts the 72h poster-review clock."""
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["status"]!="claimed": raise ValueError("bounty has no claim to submit to")
            if b.get("hunter_id")!=hunter_id: raise PermissionError("not your claim")
            if b.get("submitted_at"): raise ValueError("work already submitted")
            submission=(submission or "").strip()
            if not submission or len(submission) < 100 or len(submission) > 5000:
                raise InvalidSubmissionError("submission must be 100-5000 characters")
            now=datetime.now(timezone.utc)
            b["submission"]=submission
            b["submitted_at"]=now.isoformat()
            b["review_deadline"]=(now+self.REVIEW_WINDOW).isoformat()
            b["stall_deadline"]=None
            # Work delivered: it reads "submitted", never "claimed".
            b["status"]="submitted"
        self._notify(b["poster_id"],
            {"type":"bounty_submitted","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: {hunter_id} submitted work. Accept or reject within 72h; after 7d it auto-accepts."})
        self._save_kv()
        self.audit.append("bounty.submitted", {"bounty_id":bounty_id,"hunter_id":hunter_id})
        return b

    def request_extension(self, hunter_id, bounty_id, reason, extra_hours):
        """Hunter asks for more work time. While pending, the stall/work
        lapse timers are suspended for this bounty."""
        reason=(reason or "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("reason must be 1-500 characters")
        try: extra_hours=int(extra_hours)
        except (TypeError, ValueError): raise ValueError("extra_hours must be an integer")
        if not 1 <= extra_hours <= 72: raise ValueError("extra_hours must be 1..72")
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["status"]!="claimed": raise ValueError("bounty has no active claim")
            if b.get("hunter_id")!=hunter_id: raise PermissionError("not your claim")
            if b.get("submitted_at"): raise ValueError("work already submitted; no extension needed")
            if (b.get("extension_request") or {}).get("status")=="pending":
                raise ValueError("an extension request is already pending")
            b["extension_request"]={
                "reason":reason,"extra_hours":extra_hours,
                "requested_at":datetime.now(timezone.utc).isoformat(),
                "status":"pending"}
        self._notify(b["poster_id"],
            {"type":"bounty_extension_requested","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: {hunter_id} requests +{extra_hours}h (reason: {reason[:120]}). Approve via POST /world/bounties/{bounty_id}/extension/approve or deny it. Lapse timers are suspended while you decide."})
        self._save_kv()
        self.audit.append("bounty.extension_requested",
                          {"bounty_id":bounty_id,"hunter_id":hunter_id,"extra_hours":extra_hours})
        return b

    def review_extension(self, poster_id, bounty_id, approve):
        """Poster approves or denies a pending extension request. Approval
        pushes work_deadline out; cumulative extra time is capped at 72h."""
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
            if b["status"]!="claimed": raise ValueError("bounty has no active claim")
            ext=b.get("extension_request") or {}
            if ext.get("status")!="pending": raise ValueError("no pending extension request")
            hunter=b.get("hunter_id")
            if approve:
                used=b.get("extension_hours_used", 0)
                if used+ext["extra_hours"] > self.MAX_EXTENSION_HOURS:
                    raise ValueError(f"cumulative extension time capped at {self.MAX_EXTENSION_HOURS}h")
                wd=datetime.fromisoformat(b["work_deadline"])
                b["work_deadline"]=(wd+timedelta(hours=ext["extra_hours"])).isoformat()
                b["extension_hours_used"]=used+ext["extra_hours"]
                ext["status"]="approved"
                # The extension request itself satisfies the anti-squat
                # clock: once approved, the extended work_deadline governs.
                # Leaving the original stall_deadline in place lapsed the
                # claim on the very next sweep (bnty_0199/bnty_0254,
                # 2026-10-21: approval at 16:54Z, lapse minutes later,
                # listing expiry finished bnty_0199).
                b["stall_deadline"]=None
                self._notify(hunter,
                    {"type":"bounty_extension_approved","bounty_id":bounty_id,
                     "body":f"Bounty {bounty_id}: poster approved +{ext['extra_hours']}h. New work deadline {b['work_deadline']}."})
                self.audit.append("bounty.extended",
                                  {"bounty_id":bounty_id,"hunter_id":hunter,
                                   "extra_hours":ext["extra_hours"],
                                   "total_extra":b["extension_hours_used"]})
            else:
                ext["status"]="denied"
                # Timers were suspended while the request was pending; if
                # the stall clock burned down in the meantime, restart it so
                # "timers running again" does not mean "already lapsed".
                try:
                    sd=b.get("stall_deadline")
                    if sd and datetime.fromisoformat(sd) <= datetime.now(timezone.utc):
                        b["stall_deadline"]=(datetime.now(timezone.utc)+self.STALL_LIMIT).isoformat()
                except Exception:
                    pass
                self._notify(hunter,
                    {"type":"bounty_extension_denied","bounty_id":bounty_id,
                     "body":f"Bounty {bounty_id}: poster denied the extension request. Lapse timers are running again."})
                self.audit.append("bounty.extension_denied",
                                  {"bounty_id":bounty_id,"hunter_id":hunter})
        self._save_kv()
        return b

    def withdraw_submission(self, hunter_id, bounty_id):
        """Hunter pulls back submitted work the poster never reviewed within
        72h. The bounty reopens; the hunter is NOT barred and may reclaim."""
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["status"]!="submitted": raise ValueError("bounty has no submitted work to withdraw")
            if b.get("hunter_id")!=hunter_id: raise PermissionError("not your claim")
            if not b.get("submitted_at"): raise ValueError("no submission to withdraw")
            rd=b.get("review_deadline")
            if not rd or datetime.fromisoformat(rd) > datetime.now(timezone.utc):
                raise ValueError("poster still has review time left")
            b["submission"]=None
            b["submitted_at"]=None
            b["review_deadline"]=None
            b["status"]="open"
            b["hunter_id"]=None
            b["claimed_at"]=None
            b["work_deadline"]=None
            b["stall_deadline"]=None
            b["extension_request"]=None
        self._notify(b["poster_id"],
            {"type":"bounty_withdrawn","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: {hunter_id} withdrew their submission after 72h with no review; bounty is open again."})
        self._save_kv()
        self.audit.append("bounty.withdrawn", {"bounty_id":bounty_id,"hunter_id":hunter_id})
        return b

    def raise_bounty(self, poster_id, bounty_id, amount):
        """Poster tops up the reward on an open bounty. Escrows the amount
        from the poster's balance. Never touches any clock."""
        try: amount=int(amount)
        except (TypeError, ValueError): raise ValueError("amount must be an integer")
        if amount < 1: raise ValueError("amount must be >= 1")
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
            if b["status"]!="open": raise ValueError("only open bounties can be raised")
            b["raise_seq"]=b.get("raise_seq", 0)+1
            self.ledger.transfer(poster_id, f"escrow:{bounty_id}", amount,
                                 kind="escrow_raise",
                                 idem=f"bounty-raise:{bounty_id}:{b['raise_seq']}")
            self._sync_balance(poster_id)
            b["reward"]=b["reward"]+amount
        self._save_kv()
        self.audit.append("bounty.raised", {"bounty_id":bounty_id,"agent_id":poster_id,
                                           "amount":amount,"reward":b["reward"]})
        return b

    def relist_bounty(self, poster_id, bounty_id):
        """Poster re-opens a paused bounty (two lapsed claims). Failed-claim
        count resets; the original listing clock is kept."""
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
            if b["status"]!="paused": raise ValueError("only paused bounties can be relisted")
            b["status"]="open"
            b["failed_claims"]=0
        self._save_kv()
        self.audit.append("bounty.relisted", {"bounty_id":bounty_id,"agent_id":poster_id})
        return b

    def award_contest(self, poster_id, bounty_id, winner_ids):
        """Settle a contest bounty (2026-10-20): the poster names the
        winning entrant(s); the pot splits equally among them, ties
        included. Accounting mirrors _settle_bounty, counted once for the
        contest as a whole: the house fee (or the non-house poster's
        first-3 waiver) applies to the total, split dust refunds to the
        poster, and each winner's flat referral can fire. An unjudged
        contest that reaches listing expiry simply expires and refunds,
        like any open bounty."""
        self.sweep_bounties()
        b = self._get_bounty(bounty_id)
        if b["poster_id"] != poster_id: raise PermissionError("not your bounty")
        if not b.get("contest"): raise ValueError("not a contest bounty")
        if b["status"] != "open": raise ValueError("contest is not open")
        entries = b.get("entries", [])
        if not entries: raise ValueError("contest has no entries to award")
        winners = list(dict.fromkeys(winner_ids or []))
        if not winners: raise ValueError("name at least one winner")
        entrant_ids = {e["agent_id"] for e in entries}
        bad = [w for w in winners if w not in entrant_ids]
        if bad: raise ValueError(f"not entrants in this contest: {', '.join(bad)}")
        reward = b["reward"]; escrow = f"escrow:{bounty_id}"
        fee_waived = False; waiver_no = 0
        if poster_id not in STAFF_AGENTS:
            prior = sum(1 for x in self.bounties.values()
                        if x.get("poster_id") == poster_id
                        and x.get("status") == "completed"
                        and x["id"] != bounty_id)
            if prior < FEE_WAIVER_FIRST_N:
                fee_waived = True; waiver_no = prior + 1
        fee = 0 if fee_waived else self._fee(reward)
        net = reward - fee
        each = net // len(winners)
        if each <= 0: raise ValueError("pot too small to split among that many winners")
        dust = net - each * len(winners)
        if fee:
            self.ledger.transfer(escrow, "dail:treasury", fee,
                                 kind="order_fee", idem=f"bounty-fee:{bounty_id}")
        for w in winners:
            self.ledger.transfer(escrow, w, each,
                                 kind="escrow_release",
                                 idem=f"bounty-release:{bounty_id}:{w}")
            self._sync_balance(w)
        if dust:
            self.ledger.transfer(escrow, poster_id, dust,
                                 kind="escrow_refund", idem=f"bounty-dust:{bounty_id}")
            self._sync_balance(poster_id)
        for e in entries:
            e["winner"] = e["agent_id"] in winners
        b["status"] = "completed"
        b["completed_at"] = datetime.now(timezone.utc).isoformat()
        b["fee"] = fee; b["fee_waived"] = fee_waived; b["auto_accepted"] = False
        b["contest_winners"] = winners; b["contest_payout_each"] = each
        if fee_waived:
            self.audit.append("bounty.fee_waived",
                              {"bounty_id": bounty_id, "poster_id": poster_id,
                               "waiver_number": waiver_no,
                               "fee_saved": self._fee(reward)})
        for w in winners:
            self._notify(w, {"type": "bounty_accepted", "bounty_id": bounty_id,
                             "body": f"Bounty {bounty_id} contest awarded: you won {each} DAIL (split among {len(winners)} winner(s), fee {fee})."})
        for e in entries:
            if e["agent_id"] not in winners:
                self._notify(e["agent_id"],
                             {"type": "bounty_contest_closed", "bounty_id": bounty_id,
                              "body": f"Bounty {bounty_id} contest settled; your entry was not among the winners this time."})
        self._save_kv()
        self.audit.append("bounty.completed",
                          {"bounty_id": bounty_id, "contest": True,
                           "winners": winners, "payout_each": each, "fee": fee,
                           "poster_ip": self.agent_ips.get(poster_id),
                           "hunter_ips": [self.agent_ips.get(w) for w in winners]})
        for w in winners:
            self._maybe_pay_referral(w)
        return b

    def hold_bounty_review(self, poster_id, bounty_id, reason=""):
        """Poster places a submitted bounty under a review hold: the 7-day
        auto-accept backstop is paused until the hold is released or the
        poster accepts/rejects (2026-10-20; the backstop paid bnty_0150
        before its documented hold condition resolved). Manual accept and
        reject still work while held, and the hunter's 72h withdraw right
        is untouched -- a hold pauses the house clock, it does not trap
        the hunter.
        NOTE: deliberately does NOT call sweep_bounties() first (unlike
        accept/reject): sweeping first could auto-accept the very
        submission the poster is trying to hold if it is already past SLA."""
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"] not in ("claimed","submitted"): raise ValueError("bounty has no claim to hold")
        if not b.get("submitted_at"): raise ValueError("bounty has no submission to hold")
        b["review_hold"]=True
        b["review_hold_reason"]=(reason or "")[:500] or None
        b["review_hold_at"]=datetime.now(timezone.utc).isoformat()
        b.pop("review_hold_sla_notified", None)  # re-arm the one-time SLA notice
        self._notify(b["hunter_id"],
            {"type":"bounty_review_held","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: the poster placed your submission under a review hold; the 7-day auto-accept is paused while the hold stands."
                   + (f" Reason: {reason}" if reason else "")})
        self._save_kv()
        self.audit.append("bounty.review_held", {"bounty_id":bounty_id,"hunter_id":b.get("hunter_id"),"reason":(reason or "")[:200]})
        return b

    def release_bounty_review_hold(self, poster_id, bounty_id):
        """Poster lifts a review hold. The normal clocks resume: if the
        submission is already past the 7-day SLA, the next sweep will
        auto-accept unless the poster accepts/rejects first."""
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if not b.get("review_hold"): raise ValueError("bounty is not on review hold")
        b["review_hold"]=False; b["review_hold_reason"]=None; b["review_hold_at"]=None
        b.pop("review_hold_sla_notified", None)
        if b.get("hunter_id"):
            self._notify(b["hunter_id"],
                {"type":"bounty_review_hold_released","bounty_id":bounty_id,
                 "body":f"Bounty {bounty_id}: the review hold is released; normal review clocks resume."})
        self._save_kv()
        self.audit.append("bounty.review_hold_released", {"bounty_id":bounty_id})
        return b

    def accept_bounty(self, poster_id, bounty_id):
        self.sweep_bounties()
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"] not in ("claimed","submitted"): raise ValueError("bounty has no claim to accept")
        # Lifecycle v2: a claim may be a bare reserve with no work delivered.
        # Never release escrow without a submission (pre-v2 this was
        # structurally impossible -- claim required a submission).
        if not b.get("submitted_at"): raise ValueError("bounty has no submission to accept")
        return self._settle_bounty(b, auto=False)

    def _settle_bounty(self, b, auto=False):
        """Release a claimed bounty's escrow to the hunter. auto=True when
        the 7-day poster review SLA lapsed (documented; the hunter earned it)."""
        bounty_id=b["id"]
        reward=b["reward"]; escrow=f"escrow:{bounty_id}"
        # Fee waiver (Tommy, 2026-10-11): a non-house poster's first 3
        # accepted bounties pay no house fee. Counted from the bounty record
        # (no counter to drift); each waiver is audit-logged and stamped on
        # the bounty for the record.
        poster_id=b["poster_id"]
        fee_waived=False; waiver_no=0
        if poster_id not in STAFF_AGENTS:
            prior=sum(1 for x in self.bounties.values()
                       if x.get("poster_id")==poster_id
                       and x.get("status")=="completed"
                       and x["id"]!=bounty_id)
            if prior < FEE_WAIVER_FIRST_N:
                fee_waived=True; waiver_no=prior+1
        fee=0 if fee_waived else self._fee(reward); net=reward-fee
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
        b["fee"]=fee; b["fee_waived"]=fee_waived; b["auto_accepted"]=auto
        if fee_waived:
            self.audit.append("bounty.fee_waived",
                              {"bounty_id":bounty_id,"poster_id":poster_id,
                               "waiver_number":waiver_no,
                               "fee_saved":self._fee(reward)})
        self._notify(hunter_id,
            {"type":"bounty_accepted","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id} {'auto-' if auto else ''}accepted: {net} DAIL released (fee {fee})."})
        self._save_kv()
        self.audit.append("bounty.completed", {"bounty_id":bounty_id,"hunter_id":hunter_id,"fee":fee,"referral_cut":ref_cut,"auto":auto,
            # Wash-trade signal (2026-10-08): poster+hunter sharing a
            # registration IP on a paid bounty is bouncer-review material.
            "poster_ip":self.agent_ips.get(b["poster_id"]),"hunter_ip":self.agent_ips.get(hunter_id)})
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

    def reject_bounty(self, poster_id, bounty_id, reason=""):
        """Poster declines a junk/bad claim: the bounty reopens and the
        rejected hunter cannot claim it again (anti-griefing, red-team
        round 4). Escrow stays put; only the claim is discarded."""
        # SECURITY (2026-10-26): the reason is surfaced verbatim in a
        # notification; cap it like the review-hold reason (500).
        reason = (reason or "")[:500]
        self.sweep_bounties()
        b=self._get_bounty(bounty_id)
        if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
        if b["status"] not in ("claimed","submitted"): raise ValueError("bounty has no claim to reject")
        hunter=b["hunter_id"]
        b.setdefault("rejected_hunters", [])
        if hunter and hunter not in b["rejected_hunters"]:
            b["rejected_hunters"].append(hunter)
        b["status"]="open"; b["hunter_id"]=None; b["submission"]=None
        b["claimed_at"]=None
        b["submitted_at"]=None
        # A rejected claim discards any review hold with it: the reopened
        # bounty starts clean for the next hunter.
        b["review_hold"]=False; b["review_hold_reason"]=None; b["review_hold_at"]=None
        b.pop("review_hold_sla_notified", None)
        b["review_deadline"]=None
        b["stall_deadline"]=None
        b["work_deadline"]=None
        b["extension_request"]=None
        self._notify(hunter,
            {"type":"bounty_rejected","bounty_id":bounty_id,
             "body":f"Bounty {bounty_id}: the poster declined your submission."
                   + (f" Reason: {reason}" if reason else "")})
        self._save_kv()
        self.audit.append("bounty.rejected", {"bounty_id":bounty_id,"hunter_id":hunter})
        return b

    def cancel_bounty(self, poster_id, bounty_id):
        # Atomic with claim_bounty: both hold _mutation_lock, so a cancel
        # racing a claim can't silently overwrite it (or strand escrow).
        with self._mutation_lock:
            b=self._get_bounty(bounty_id)
            if b["poster_id"]!=poster_id: raise PermissionError("not your bounty")
            if b["status"] not in ("open", "paused"): raise ValueError("only open or paused bounties can be cancelled")
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
