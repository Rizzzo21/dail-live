from pydantic import BaseModel, Field
from typing import Literal

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
    approved: bool = False


class SafeReceiveRequest(BaseModel):
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
    expires_in_days: int = Field(default=30, ge=1, le=90)

class BountyEditRequest(BaseModel):
    agent_id: str
    title: str | None = None
    description: str | None = None

class BountyClaimRequest(BaseModel):
    agent_id: str
    submission: str = Field(..., max_length=5000)

class BountyActionRequest(BaseModel):
    agent_id: str

class BanRequest(BaseModel):
    reason: str = ""

class AgentProfileRequest(BaseModel):
    agent_id: str
    bio: str = ""
    capabilities: list[str] = []

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
    balance: int = Field(default=100, ge=0)
    spending_limit: int = 10000
    approval_limit: int = 2500
    status: Literal["active", "paused", "disabled", "banned"] = "active"
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
    success_url: str = "https://dail-1.onrender.com/launch?payment=success"
    cancel_url: str = "https://dail-1.onrender.com/launch?payment=cancelled"
    idempotency_key: str | None = None


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
