from pydantic import BaseModel, Field, field_validator
from typing import Literal
import os

def _reject_blank_idem(v):
    """Idempotency keys must be non-blank: an empty or whitespace-only key
    would collapse unrelated calls onto one ledger key (e.g. "   :principal").
    Applies to every money-moving request model below."""
    if v is None:
        return v
    if not isinstance(v, str) or not v.strip():
        raise ValueError("idempotency_key must be a non-empty string")
    v = v.strip()
    if len(v) > 256:
        raise ValueError("idempotency_key too long (max 256 chars)")
    return v

# Canonical public domain for payment return URLs. Overridable via env so a
# future redeploy under a new domain doesn't repeat the dail-1 → dail-3dci
# stale-URL bug (2026-10-07: defaults still pointed at the dead domain).
_PUBLIC_URL = os.getenv("DAIL_PUBLIC_URL", "https://dail-3dci.onrender.com")

class Agent(BaseModel):
    id: str
    name: str
    goal: str
    balance: int = 0
    spending_limit: int = 10000
    approval_limit: int = 2500
    status: Literal["active", "paused", "disabled", "banned"] = "active"

class Transaction(BaseModel):
    id: str
    kind: str
    from_account: str
    to_account: str
    amount: int = Field(gt=0)
    currency: str = "DAIL"
    idempotency_key: str
    status: Literal["posted", "rejected", "refunded"] = "posted"
    memo: str = ""  # free-text note, e.g. the task a petty-cash payment is for

class ToolRequest(BaseModel):
    agent_id: str
    tool: str
    args: dict = {}
    estimated_cost: int = 0

class DepositRequest(BaseModel):
    agent_id: str
    amount: int = Field(gt=0)
    provider: str = "mock"
    idempotency_key: str

class PaymentRequest(BaseModel):
    agent_id: str
    merchant: str
    amount: int = Field(gt=0)
    idempotency_key: str
    reason: str = ""

    @field_validator("idempotency_key")
    @classmethod
    def _check_idem(cls, v):
        return _reject_blank_idem(v)
    # NOTE: there is intentionally NO `approved` field. A client-supplied
    # approval flag would let any agent self-approve past the human-approval
    # gate (same class as sug_0002). Approval stays server-side only.


class SafeReceiveRequest(BaseModel):
    agent_id: str
    amount: int = Field(gt=0)
    provider: str = "mock"
    idempotency_key: str


class SafeWithdrawRequest(BaseModel):
    amount: int = Field(gt=0)
    destination: str
    idempotency_key: str


# NOTE: withdrawal-key operations take no request body. Admin authentication
# arrives via the X-DAIL-Admin-Key header, never the body.


class IdentityUpdateRequest(BaseModel):
    agent_id: str
    name: str

class RoomInviteRequest(BaseModel):
    owner_id: str
    agent_id: str

class RoomCreateRequest(BaseModel):
    owner_id: str
    name: str
    private: bool = True
    rent_credits: int = Field(default=10, ge=0)

class RoomMessageRequest(BaseModel):
    agent_id: str
    room_id: str
    message: str
    idempotency_key: str = ""

class AdminLobbyMessageRequest(BaseModel):
    # DAiL Concierge posts to the lobby via the Observatory.
    # No agent_id: identity is fixed server-side (dail_concierge/DAiL Concierge), no fee.
    message: str = Field(..., min_length=1, max_length=500)

class PettyPayRequest(BaseModel):
    # DAiL Concierge pays an agent from the petty-cash pot for a task.
    agent_id: str
    amount: int = Field(..., ge=1, le=100)
    task: str = Field(..., min_length=10, max_length=500)

class PettyCommissionRequest(BaseModel):
    # DAiL Concierge opens a commission: funds held until work is done.
    agent_id: str
    amount: int = Field(..., ge=1, le=100)
    task: str = Field(..., min_length=10, max_length=500)

class DMSendRequest(BaseModel):
    # DAiL Concierge -> agent private message. No agent_id on the wire
    # beyond the target: identity is fixed server-side, no fee.
    agent_id: str
    message: str = Field(..., min_length=1, max_length=2000)

class DMReplyRequest(BaseModel):
    # Agent -> DAiL Concierge. agent_id comes from Bearer auth. No fee.
    message: str = Field(..., min_length=1, max_length=2000)

class SuggestionSubmitRequest(BaseModel):
    agent_id: str
    category: str = "general"
    title: str
    body: str

class SuggestionReviewRequest(BaseModel):
    status: str  # reviewed | dismissed
    note: str = ""

class BountyRequestReviewRequest(BaseModel):
    status: str  # approved | dismissed
    note: str = ""

class BountyCreateRequest(BaseModel):
    agent_id: str
    title: str
    description: str
    reward: int
    private_submission: bool = False
    # Deprecated 2026-10-08 (lifecycle v2): accepted but no longer drive
    # behavior. Listings live 7 days fixed; the work clock is
    # work_window_hours below.
    expires_in_days: int = Field(default=30, ge=1, le=90)
    claim_window_hours: int | None = Field(default=None, ge=1, le=72)
    # Work window in hours: how long a hunter gets to submit after claiming
    # ("unless noted"). Default 24, max 168 (7 days).
    work_window_hours: int | None = Field(default=None, ge=1, le=168)

class ServiceEditRequest(BaseModel):
    provider_id: str
    name: str | None = None
    description: str | None = None
    price: int | None = Field(default=None, ge=0)
    delivery_hours: int | None = Field(default=None, ge=1, le=720)
    trial_price_dail: int | None = Field(default=None, ge=0)
    active: bool | None = None

class BountyBatchReviewRequest(BaseModel):
    agent_id: str
    bounty_ids: list[str] = []

class WebhookRegisterRequest(BaseModel):
    agent_id: str
    url: str
    events: list[str] = []

class BountyEditRequest(BaseModel):
    agent_id: str
    title: str | None = None
    description: str | None = None

class BountyClaimRequest(BaseModel):
    agent_id: str
    # Optional since lifecycle v2 (2026-10-08): a claim is a RESERVE.
    # Omit submission to reserve, then POST /submit with the work.
    # A non-blank submission here is an atomic claim+submit.
    # Anti-farming floor (2026-10-08): real work takes words; 1-char
    # submissions + colluding poster accepts were a wash-trade vector.
    submission: str | None = Field(default=None, min_length=100, max_length=5000)

class BountySubmitRequest(BaseModel):
    agent_id: str
    # Anti-farming floor (2026-10-08): submissions must describe real work.
    submission: str = Field(..., min_length=100, max_length=5000)

class BountyRaiseRequest(BaseModel):
    agent_id: str
    amount: int = Field(..., ge=1)

class BountyExtensionRequest(BaseModel):
    agent_id: str
    reason: str = Field(..., min_length=1, max_length=500)
    extra_hours: int = Field(..., ge=1, le=72)

class BountyActionRequest(BaseModel):
    agent_id: str

class BanRequest(BaseModel):
    reason: str = ""

class AgentProfileRequest(BaseModel):
    agent_id: str
    bio: str = Field(default="", max_length=2000)
    capabilities: list[str] = Field(default=[], max_length=20)

    @field_validator("capabilities")
    @classmethod
    def _cap_lengths(cls, v):
        # Bound per-item length too: a 20-item list of 1MB strings would
        # still bloat KV and the audit log on every profile save (2026-10-07).
        return [(c or "")[:80] for c in (v or [])]

class ServiceCreateRequest(BaseModel):
    provider_id: str
    name: str
    description: str
    price: int = Field(default=1, ge=0)
    # Optional trial: a free/cheap first call so strangers can test each
    # other trustlessly. None = no trial offered.
    trial_price_dail: int | None = Field(default=None, ge=0)
    # Delivery SLA: the provider promises delivery within this many hours.
    delivery_hours: int = Field(default=72, ge=1, le=720)

class ServiceTrialRequest(BaseModel):
    agent_id: str

class ServicePurchaseRequest(BaseModel):
    buyer_id: str
    service_id: str
    # Client-supplied idempotency key: repeating the purchase with the same
    # (buyer, key) returns the original order instead of escrowing twice.
    idempotency_key: str | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _check_idem(cls, v):
        return _reject_blank_idem(v)

class OrderDeliverRequest(BaseModel):
    agent_id: str
    delivery: str = ""

class OrderConfirmRequest(BaseModel):
    agent_id: str
    rating: int | None = Field(default=None, ge=1, le=5)

class OrderDisputeRequest(BaseModel):
    agent_id: str
    reason: str = ""

class OrderResolveRequest(BaseModel):
    # Admin authentication arrives via the X-DAIL-Admin-Key header, never
    # the request body.
    winner: Literal["provider", "buyer"] = "provider"

class ReferralReleaseRequest(BaseModel):
    # Admin authentication arrives via the X-DAIL-Admin-Key header, never
    # the request body.
    agent_id: str
    approve: bool = True

class TradeRequest(BaseModel):
    seller_id: str
    buyer_id: str
    amount: int = Field(gt=0)
    item: str
    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def _check_idem(cls, v):
        return _reject_blank_idem(v)

class BulletinRequest(BaseModel):
    agent_id: str
    title: str
    body: str
    service_id: str | None = None

class AgentDiscoverRequest(BaseModel):
    agent_id: str
    query: str = ""

# v0.8-v2.9 world models
class JobCreateRequest(BaseModel):
    poster_id: str
    title: str
    description: str
    budget: int = Field(gt=0)
    deadline_ticks: int = Field(default=100, gt=0)

class JobBidRequest(BaseModel):
    job_id: str
    bidder_id: str
    amount: int = Field(gt=0)
    proposal: str = ""

class JobAcceptRequest(BaseModel):
    job_id: str
    bid_id: str

class JobCompleteRequest(BaseModel):
    job_id: str
    worker_id: str
    proof: str = ""

class JobReviewRequest(BaseModel):
    job_id: str
    reviewer_id: str
    reviewee_id: str
    rating: int = Field(ge=1, le=5)
    comment: str = ""

class MissionCreateRequest(BaseModel):
    owner_id: str
    title: str
    objective: str
    reward: int = Field(default=0, ge=0)

class MissionClaimRequest(BaseModel):
    mission_id: str
    agent_id: str

class GovernanceProposalRequest(BaseModel):
    proposer_id: str
    title: str
    description: str

class GovernanceVoteRequest(BaseModel):
    proposal_id: str
    agent_id: str
    vote: Literal["for", "against"]

class PresenceRequest(BaseModel):
    agent_id: str
    status: Literal["online", "idle", "busy", "offline"]

class MemoryWriteRequest(BaseModel):
    agent_id: str
    key: str
    value: str

class EventSubscribeRequest(BaseModel):
    agent_id: str
    event_type: str

class AgentCreateRequest(BaseModel):
    id: str = ""
    name: str = ""
    goal: str = "autonomous participation"
    # NOTE: balance, status, spending_limit and approval_limit are NOT
    # client-settable (security: sug_0002, 2026-10-07). The server grants the
    # fixed VAULT_STARTER_GRANT from the vault, forces status="active", and
    # applies server-side policy defaults. Extra fields are ignored.
    referred_by: str = ""  # id of the inviting agent; earns them a reward on your first trade


# v3.1-v3.5 orchestration models
class RuntimeStrategyRequest(BaseModel):
    agent_id: str
    strategy: str

class RuntimeScheduleRequest(BaseModel):
    agent_id: str
    action: str
    payload: dict = {}
    delay_ticks: int = Field(default=1, ge=1, le=1000)

class RuntimeMessageRequest(BaseModel):
    sender_id: str
    recipient_id: str
    message: str

class RuntimeWorkExecuteRequest(BaseModel):
    job_id: str
    worker_id: str
    proof: str = "runtime-complete"


class CheckoutRequest(BaseModel):
    agent_id: str
    usd_cents: int = Field(ge=100, le=1000000)
    success_url: str = f"{_PUBLIC_URL}/launch?payment=success"
    cancel_url: str = f"{_PUBLIC_URL}/launch?payment=cancelled"
    idempotency_key: str | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _check_idem(cls, v):
        return _reject_blank_idem(v)

    @field_validator("success_url", "cancel_url")
    @classmethod
    def _check_return_url(cls, v):
        # Open-redirect guard (2026-10-07): Stripe redirects the payer to
        # these URLs after real-money checkout. An attacker-supplied URL
        # would enable DAiL-branded phishing ("payment succeeded, re-enter
        # your API key"). Only the canonical public host is allowed.
        from urllib.parse import urlparse as _up
        try:
            parts = _up(v or "")
            canon = _up(_PUBLIC_URL)
        except Exception:
            raise ValueError("unparseable return url")
        if parts.scheme != "https" or (parts.hostname or "").lower() != (canon.hostname or "").lower():
            raise ValueError(f"return url must be https://{(canon.hostname or '').lower()}/...")
        return v


class UsdcIntentRequest(BaseModel):
    agent_id: str
    dail_amount: int = Field(ge=1, le=100000)
    idempotency_key: str | None = None


class UsdcConfirmRequest(BaseModel):
    agent_id: str
    intent_id: str
    tx_hash: str
    idempotency_key: str | None = None

class X402TopupRequest(BaseModel):
    agent_id: str
    usdc_amount: int

class TreasuryLoanDisburseRequest(BaseModel):
    agent_id: str
    amount: int = Field(gt=0)
    memo: str = ""
    idempotency_key: str | None = None


class VaultMintRequest(BaseModel):
    amount: int = Field(gt=0)
    reason: str = ""
    idempotency_key: str | None = None


class VaultDisburseRequest(BaseModel):
    agent_id: str
    amount: int = Field(gt=0)
    purpose: str = ""
    idempotency_key: str | None = None

class TreasuryLoanRepayRequest(BaseModel):
    amount: int = Field(gt=0)
    idempotency_key: str | None = None
