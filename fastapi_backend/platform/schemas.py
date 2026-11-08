from __future__ import annotations
from datetime import UTC, date, datetime, timedelta
from typing import Any, Optional, List, Annotated, Literal

from pydantic import BaseModel, Field, EmailStr, model_validator, field_validator, BeforeValidator

from platform.enums import ProductType, LaunchStage, PurchaseStatus, Role
from security import validate_password_strength
from infrastructure.settings import settings as s
from platform.validators import _lower_strip, _strip


# Field types (centralized normalization/constraints):
Email = Annotated[EmailStr, BeforeValidator(_lower_strip), Field(max_length=320)]
Name = Annotated[str, BeforeValidator(_strip), Field(min_length=s.MIN_NAME_LENGTH, max_length=200)]

VerificationCode = Annotated[str, Field(min_length=s.VERIFICATION_CODE_LENGTH, max_length=s.VERIFICATION_CODE_LENGTH)]
ResetCode = Annotated[str, Field(min_length=s.RESET_PASSWORD_CODE_LENGTH, max_length=s.RESET_PASSWORD_CODE_LENGTH)]

Token = Annotated[str, Field(max_length=s.MAX_TOKEN_LENGTH)]
OAuthToken = Annotated[str, Field(max_length=s.MAX_OAUTH_TOKEN_LENGTH)]

Password = Annotated[str, Field(min_length=s.MIN_PASSWORD_LENGTH, max_length=s.MAX_PASSWORD_LENGTH)]


# ============================================================================
# AUTHENTICATION SCHEMAS
# ============================================================================

class EmailExistsResponse(BaseModel):
    """
    Response schema for checking if an email exists
    Approach favours UX but allows enumeration (though so do many consumer apps)
    """
    exists: bool = Field(...)
    has_password: Optional[bool] = Field(default=None)
    

class UserLoginRequest(BaseModel):
    email: Email
    password: Password


def _get_cooldown_remaining_seconds(last_sent_at: datetime | None, cooldown_minutes: int) -> int:
    if last_sent_at is None:
        return 0

    cooldown_expires = last_sent_at + timedelta(minutes=cooldown_minutes)
    remaining_seconds = int((cooldown_expires - datetime.now(UTC)).total_seconds())
    return max(remaining_seconds, 0)


class NameChangePolicy(BaseModel):
    # "unlimited" = rename freely; "disabled" = support only; "limited" =
    # `allowance` changes per `interval_days`, `remaining` left before `reset_at`.
    mode: Literal["unlimited", "limited", "disabled"] = Field(...)
    allowance: Optional[int] = Field(default=None)
    interval_days: Optional[int] = Field(default=None)
    remaining: Optional[int] = Field(default=None)
    reset_at: Optional[datetime] = Field(default=None)


class OrganisationSummary(BaseModel):
    """One organisation an account belongs to, as the client sees it.

    Identity ONLY — never authorization. Organisations confer capabilities
    (organisation_service.effective_capabilities), and that conferral already
    reaches the client folded into `UserResponse.capabilities`; nothing here is
    a gate, and no client should treat membership as permission to do anything.

    What it is for is the questions capabilities cannot answer because a grant
    is not parameterised: WHICH organisation, so the frontend can personalise
    for it (the sponsor proposal's return card resolves the firm from `slug`).
    `slug` is the stable external identifier the backend upserts on, so it is
    the field a client should key off — `name` and `logo_url` are presentation.
    """
    slug: str = Field(...)
    name: str = Field(...)
    logo_url: Optional[str] = Field(default=None)

    class Config:
        from_attributes = True


def _organisations_for(data: Any) -> list["OrganisationSummary"]:
    """The ACTIVE organisations this account belongs to, for the user contract.

    Active-only, matching conferral exactly: an inactive organisation confers
    nothing, so surfacing it would let a client personalise for a relation the
    backend no longer honours. Sorted by slug so the payload is stable. Reads
    the memberships collection both auth paths already eager-load, so this adds
    no query. The local import avoids a schemas <-> models cycle."""
    from platform.models import User as UserModel

    if not isinstance(data, UserModel):
        return []

    organisations = [
        membership.organisation
        for membership in (data.organisation_memberships or [])
        if membership.organisation is not None and membership.organisation.is_active
    ]

    return [
        OrganisationSummary(
            slug=organisation.slug,
            name=organisation.name,
            logo_url=organisation.logo_url,
        )
        for organisation in sorted(organisations, key=lambda o: o.slug)
    ]


def _effective_capabilities_for(data: Any) -> list[str]:
    """The account's EFFECTIVE capability grants, for the user contract.

    Resolved through organisation_service.effective_capabilities — the single
    definition the backend also gates on — so a grant conferred by an active
    organisation is indistinguishable from a hand-set one to the client, and
    the frontend contract (and its hasCapability) is unchanged. Sorted so the
    payload is stable. The local import avoids a schemas <-> services cycle."""
    from platform.models import User as UserModel
    from platform.services.organisation_service import effective_capabilities

    if isinstance(data, UserModel):
        return sorted(effective_capabilities(data))
    return sorted(getattr(data, "capabilities", None) or [])


class FlowCompletionSummary(BaseModel):
    """What a question flow's gate needs about a flow this account HAS answered.

    The answers themselves are a separate, explicit fetch
    (GET /flows/{key}/responses/latest) — the user contract carries question IDS,
    never a payload of personal answers, to every caller of GET /user/. Staleness
    is the consumer's policy: the platform reports `completed_at` and takes no view.

    `version` and `completed_at` describe the STORED snapshot and are unchanged.
    The three question fields are additive: `outstanding` is what the account
    still owes under the flow's current definition, so a member who answered
    everything last year and is now missing one newly added question reports
    `complete: false` with that one id — without their earlier answers moving.
    A flow whose definition has been retired reports complete with nothing
    outstanding, because there is no current contract to owe anything against.
    """
    version: int = Field(...)
    completed_at: datetime = Field(...)
    answered: list[str] = Field(default_factory=list)
    outstanding: list[str] = Field(default_factory=list)
    complete: bool = Field(default=True)


class FlowQuestionProgress(BaseModel):
    """Per-question progress for one REGISTERED flow, whether answered or not.

    The sibling of FlowCompletionSummary, and the difference is which flows
    appear: the summary is keyed on what the account answered (so a never-touched
    flow is absent, which is how the completion gate reads "never onboarded"),
    while this covers every flow the instance registers, with a fresh account
    simply owing all of them. A client deciding WHAT TO ASK reads this one.

    `version` here is the flow's CURRENT registered version — what a client
    should render — not the version of any stored row.
    """
    version: int = Field(...)
    # The flow's whole question set, in the order it asks them:
    questions: list[str] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    outstanding: list[str] = Field(default_factory=list)
    complete: bool = Field(default=False)
    # Null when the account has never submitted anything for this flow:
    completed_at: Optional[datetime] = Field(default=None)


class UserResponse(BaseModel):
    id: int = Field(...)
    name: str = Field(...)
    email: str = Field(...)
    # Identity (see the access-control rule); the client derives admin-only
    # identity signals from it:
    role: Role = Field(...)
    # Capability grants (registry: platform/capabilities.py); admins hold all
    # capabilities implicitly, so the client checks grants via hasCapability.
    # This is the EFFECTIVE set — grants held directly plus those conferred by
    # each active organisation the account belongs to:
    capabilities: list[str] = Field(default_factory=list)
    # The ACTIVE organisations this account belongs to — identity, not
    # authorization (see OrganisationSummary). Capabilities say WHAT an account
    # may do; this says WHICH organisation it belongs to, which a capability
    # string cannot express and which personalization needs:
    organisations: list[OrganisationSummary] = Field(default_factory=list)
    is_verified: bool = Field(...)
    marketing_consent: bool = Field(...)
    # Whether the member is listed on the leaderboards (their own setting):
    show_on_leaderboard: bool = Field(default=True)
    avatar_url: Optional[str] = Field(default=None)
    verification_cooldown_seconds: int = Field(default=0)
    reset_password_cooldown_seconds: int = Field(default=0)
    # Instance rename policy so the client picks the matching flow + copy without
    # a hardcoded rule (computed per response from settings + the user's window):
    name_change_policy: NameChangePolicy = Field(...)
    created_at: datetime = Field(...)
    # When the account last made an authenticated request; null = not seen since
    # the column shipped. This is what the users:read admin lookup
    # (GET /admin/users/by-email) exists to show, and it rides on UserResponse
    # rather than a parallel admin-only schema because the shape it belongs to
    # ALREADY carries the sibling account-lifecycle timestamp (created_at) plus
    # the cooldown metadata — "when did this account start" and "when was it last
    # here" are the same class of fact. Self-exposure is a non-issue: the reader
    # is the very account that generated the timestamp, so it tells them nothing
    # they did not just do:
    last_accessed_at: Optional[datetime] = Field(default=None)
    # Completed question flows, keyed by flow key — the gate read (see
    # FlowCompletionSummary). THREE-STATE on purpose: null = "not computed by
    # this endpoint", {} = "computed, nothing completed", populated = the
    # summaries. Only GET /user/ pays for the query and fills it; every other
    # producer of this schema (login, verify, avatar upload, ...) leaves it null,
    # so a client can tell "I don't know yet" from "definitely not onboarded"
    # instead of bouncing a user through onboarding on a login response:
    flows: Optional[dict[str, FlowCompletionSummary]] = Field(default=None)
    # Per-question progress for every REGISTERED flow (see FlowQuestionProgress).
    # Additive alongside `flows` rather than folded into it, because the two are
    # keyed differently on purpose and the difference is load-bearing: `flows`
    # only has a key once the account has answered something, which is exactly
    # what the completion gate reads, so putting never-answered flows in there
    # would tell every gate that a brand-new account had already onboarded. This
    # field carries them, so a client can read the outstanding list for an
    # account with no snapshot at all. Same three-state null contract as `flows`:
    # only GET /user/ computes it, everything else leaves it null:
    flow_questions: Optional[dict[str, FlowQuestionProgress]] = Field(default=None)

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def inject_computed_fields(cls, data: Any):
        if isinstance(data, dict):
            return data

        if hasattr(data, "id") and hasattr(data, "email"):
            verification_cooldown_seconds = 0
            get_verification_cooldown_seconds = getattr(data, "get_verification_cooldown_seconds", None)
            if callable(get_verification_cooldown_seconds):
                verification_cooldown_seconds = get_verification_cooldown_seconds()

            return {
                "id": getattr(data, "id"),
                "name": getattr(data, "name"),
                "email": getattr(data, "email"),
                "role": getattr(data, "role"),
                "capabilities": _effective_capabilities_for(data),
                "organisations": _organisations_for(data),
                "is_verified": getattr(data, "is_verified"),
                "marketing_consent": getattr(data, "marketing_consent"),
                "show_on_leaderboard": getattr(data, "show_on_leaderboard", True),
                "avatar_url": getattr(data, "avatar_url", None),
                "verification_cooldown_seconds": verification_cooldown_seconds,
                "reset_password_cooldown_seconds": _get_cooldown_remaining_seconds(
                    getattr(data, "last_reset_password_email_sent", None),
                    s.RESET_PASSWORD_EMAIL_COOLDOWN,
                ),
                # Computed on the model from settings + the user's window columns:
                "name_change_policy": getattr(data, "name_change_policy", None),
                "created_at": getattr(data, "created_at"),
                "last_accessed_at": getattr(data, "last_accessed_at", None),
                # Set as a transient attribute by the ONE endpoint that computes
                # it (platform/routers/users.read_current_user); absent -> null:
                "flows": getattr(data, "flows_summary", None),
                "flow_questions": getattr(data, "flow_questions", None),
            }

        return data


class DualTokenResponse(BaseModel):
    access_token: str = Field(...)
    access_token_expiry: datetime = Field(...)
    refresh_token_expiry: datetime = Field(...)
    token_type: str = Field(...)
    # Returned only when the client opts into header-mode auth (X-Auth-Mode:
    # header) - contexts where cross-site cookies don't work (previews, native):
    refresh_token: Optional[str] = Field(default=None)


class AccessTokenResponse(BaseModel):
    access_token: str = Field(...)
    access_token_expiry: datetime = Field(...)
    token_type: str = Field(...)
    # Header-mode refresh returns the rotated refresh token in the body
    # (cookie mode keeps it httpOnly and these stay null):
    refresh_token: Optional[str] = Field(default=None)
    refresh_token_expiry: Optional[datetime] = Field(default=None)


class RefreshTokenRequest(BaseModel):
    refresh_token: Token


# ============================================================================
# USER SCHEMAS
# ============================================================================

class CreateUserRequest(BaseModel):
    name: Name
    email: Email
    password: Password
    marketing_consent: bool = Field(default=False)

    # Enforcing password policy (registration schema used by the router):
    @field_validator('password')
    @classmethod
    def validate_password(cls, v, info):
        # Passing user inputs for entropy checks (name/email):
        email = None
        name = None
        data = getattr(info, 'data', {}) or {}
        if hasattr(data, 'get'):
            email = data.get('email')
            name = data.get('name')
        validate_password_strength(v, user_inputs=[email, name])
        return v


class UpdateUserRequest(BaseModel):
    name: Optional[Name] = None
    marketing_consent: Optional[bool] = None
    show_on_leaderboard: Optional[bool] = None


class NameCheckRequest(BaseModel):
    name: Name


class NameCheckResponse(BaseModel):
    # Whether the name passes moderation, plus the deterministically cased form.
    # Lets the client vet a requested name before opening the support/contact
    # flow, so an inappropriate name never reaches a support ticket.
    appropriate: bool = Field(...)
    formatted: str = Field(...)
    message: Optional[str] = Field(default=None)


class UserVerificationRequest(BaseModel):
    code: VerificationCode


# ============================================================================
# PASSWORD RESET SCHEMAS
# ============================================================================

class ResetPasswordRequest(BaseModel):
    """Used for triggering password reset flow with email"""
    email: Email


class ResetPasswordVerifyRequest(BaseModel):
    """Used for verifying password reset code and setting new password"""
    email: Email
    code: ResetCode
    new_password: Password

    # Enforcing password policy for reset:
    @field_validator('new_password')
    @classmethod
    def validate_new_password(cls, v, info):
        email = None
        data = getattr(info, 'data', {}) or {}
        if hasattr(data, 'get'):
            email = data.get('email')
        validate_password_strength(v, user_inputs=[email])
        return v


# ============================================================================
# CONTACT FORM SCHEMAS
# ============================================================================

class ContactSubmissionRequest(BaseModel):
    name: Name
    email: Email
    company: Optional[str] = Field(default=None, max_length=200)
    subject: Optional[str] = Field(default=None, max_length=200)
    message: str = Field(..., min_length=1, max_length=10000)

    # Normalizing email:
    @field_validator('email')
    @classmethod
    def normalize_email(cls, v: EmailStr) -> str:
        return v.lower()


class ContactSubmissionResponse(BaseModel):
    id: int = Field(...)
    name: str = Field(...)
    email: str = Field(...)
    company: Optional[str] = Field(...)
    subject: Optional[str] = Field(default=None)
    message: str = Field(...)
    resolved: bool = Field(...)
    created_at: datetime = Field(...)

    class Config:
        from_attributes = True


# ============================================================================
# QUESTION FLOW SCHEMAS
# ============================================================================

class FlowDefinitionResponse(BaseModel):
    """The public shape of a registered flow — enough for a client to render and
    submit against the right contract, and nothing about anyone's answers.

    `questions` is the flow's full question set in the order it asks them, and
    `required_questions` the subset that can leave an account outstanding. Public
    alongside the version because a client rendering the flow before it has an
    account needs to know what to ask; which of them a PARTICULAR account still
    owes is an authenticated read (`UserResponse.flow_questions`).
    """
    flow_key: str = Field(...)
    version: int = Field(...)
    title: str = Field(...)
    lead_capture: bool = Field(default=False)
    questions: list[str] = Field(default_factory=list)
    required_questions: list[str] = Field(default_factory=list)


class FlowResponseSubmitRequest(BaseModel):
    """One flow submission — any SUBSET of the flow's questions.

    Partial by design. A client sends the questions it just asked; the service
    merges them over what the account already answered and appends the result, so
    answering a newly added question does not require re-answering the rest and
    cannot lose them.

    `version` is the version the CLIENT rendered. It is recorded on nothing and
    gates nothing: a stale client is caught per question instead (an id the flow
    no longer asks, or an answer whose option was retired, is a 422 naming that
    question), which keeps the answers that are still valid. There is no
    `completed_at` field by design — that timestamp is the staleness fact the
    whole feature reads, so it is server-stamped and never client-supplied.
    """
    version: int = Field(..., ge=1)
    # Validated against the flow's own registered answers_schema in the service;
    # left permissive here so schema errors carry the flow's field errors rather
    # than FastAPI's generic body-validation shape:
    answers: dict[str, Any] = Field(...)


class FlowResponseSnapshot(BaseModel):
    """One stored snapshot, as the owning account sees it."""
    flow_key: str = Field(...)
    version: int = Field(...)
    answers: dict[str, Any] = Field(default_factory=dict)
    completed_at: datetime = Field(...)

    class Config:
        from_attributes = True


class FlowLeadRequest(BaseModel):
    """A pre-account, partial answer set (dormant lead module).

    `answers` is optional and intentionally NOT validated against the flow schema
    — a lead is by definition mid-flow. It is size-capped in the service.
    """
    email: Email
    version: int = Field(..., ge=1)
    answers: Optional[dict[str, Any]] = Field(default=None)


# TODO TEMP (QA harness) — remove with POST /flows/{flow_key}/test/reset,
# platform/services/question_flow_service/qa_reset.py and the flows:qa-reset grant:
class FlowResetRequest(BaseModel):
    """Who to reset, and the confirmation for doing it to somebody else.

    Both fields are optional so the common case — resetting your OWN answers —
    is an empty body. `email` targets another account; `confirm` is then
    REQUIRED and must be true, because deleting append-only snapshots belonging
    to a different person is the one genuinely destructive shape this endpoint
    has and must not be reachable by a body typo.
    """
    email: Optional[Email] = Field(default=None)
    confirm: bool = Field(default=False)


class FlowResetResponse(BaseModel):
    """What the reset removed. Counts ONLY — never the deleted answers.

    Zero counts are ambiguous ON PURPOSE: an address with no account, an account
    that never answered, and an already-reset account all report zeros, so the
    endpoint can't be used to test which addresses exist.
    """
    flow_key: str = Field(...)
    responses: int = Field(default=0)
    reminders: int = Field(default=0)
    leads: int = Field(default=0)


class FlowLeadResumeRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=256)


class FlowLeadResumeResponse(BaseModel):
    # Masked, like the unsubscribe confirmation: the link's holder is told which
    # address it belongs to without the response exposing a full address:
    email_masked: str = Field(...)
    version: int = Field(...)
    answers: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# PARTNERSHIP SCHEMAS
# ============================================================================

class PartnershipResponse(BaseModel):
    """One partnership record as every reader sees it.

    Deliberately ONE shape for both audiences (a member reading their own
    organisation, and an operator listing every partnership): the row holds no
    money and no PII, so there is nothing for a second, thinner schema to hide,
    and one shape means the client parses one thing.

    `status`, `season`, `offering_label` and `snapshot_key` are opaque strings
    the backend stores and never interprets — the reader decides what they mean.
    `is_expired` and `days_remaining` are COMPUTED per response from `ends_on`,
    so they are always current and never disagree with the stored status.
    """
    id: int = Field(...)
    organisation_id: int = Field(...)
    # The stable external identifier a client should key off (name is presentation):
    organisation_slug: str = Field(...)
    organisation_name: str = Field(...)

    status: str = Field(...)
    season: str = Field(...)
    offering_label: Optional[str] = Field(default=None)
    snapshot_key: Optional[str] = Field(default=None)

    starts_on: Optional[date] = Field(default=None)
    ends_on: Optional[date] = Field(default=None)
    signed_on: Optional[date] = Field(default=None)

    # Derived from ends_on vs today; null days_remaining = open-ended:
    is_expired: bool = Field(default=False)
    days_remaining: Optional[int] = Field(default=None)

    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def inject_organisation_fields(cls, data: Any):
        if isinstance(data, dict):
            return data

        organisation = getattr(data, "organisation", None)
        return {
            "id": getattr(data, "id"),
            "organisation_id": getattr(data, "organisation_id"),
            "organisation_slug": getattr(organisation, "slug", ""),
            "organisation_name": getattr(organisation, "name", ""),
            "status": getattr(data, "status"),
            "season": getattr(data, "season"),
            "offering_label": getattr(data, "offering_label", None),
            "snapshot_key": getattr(data, "snapshot_key", None),
            "starts_on": getattr(data, "starts_on", None),
            "ends_on": getattr(data, "ends_on", None),
            "signed_on": getattr(data, "signed_on", None),
            # Computed on the model from ends_on, never stored:
            "is_expired": getattr(data, "is_expired", False),
            "days_remaining": getattr(data, "days_remaining", None),
            "created_at": getattr(data, "created_at"),
            "updated_at": getattr(data, "updated_at"),
        }


class PartnershipCreateRequest(BaseModel):
    """Creating a partnership for an organisation, addressed by slug or id.

    `status` is left as a plain string rather than an enum: the registry
    (platform/partnerships.py) is validated in the service on the way to the
    database, so there is exactly one place a status is checked."""
    organisation_slug: Optional[str] = Field(default=None, max_length=200)
    organisation_id: Optional[int] = Field(default=None)

    season: str = Field(..., min_length=1, max_length=64)
    status: Optional[str] = Field(default=None, max_length=32)
    offering_label: Optional[str] = Field(default=None, max_length=200)
    snapshot_key: Optional[str] = Field(default=None, max_length=200)

    starts_on: Optional[date] = Field(default=None)
    ends_on: Optional[date] = Field(default=None)
    signed_on: Optional[date] = Field(default=None)

    @model_validator(mode="after")
    def organisation_identified(self):
        if self.organisation_slug is None and self.organisation_id is None:
            raise ValueError("Provide organisation_slug or organisation_id.")
        return self


class PartnershipUpdateRequest(BaseModel):
    """A partial update — this is how an agent marks a partnership signed.

    Every field is optional and only the ones PRESENT in the request body are
    written (the router forwards `exclude_unset`), so sending just
    `{"status": "signed"}` cannot blank the dates it never mentioned, while an
    explicit null still clears a field on purpose.

    Clearing is only meaningful for the NULLABLE columns, though: `status` and
    `season` are NOT NULL, so an explicit null in either is refused here rather
    than reaching the service. Both nulls are silent bugs otherwise — a null
    status reads as "unset" to the create-path default and quietly resets a
    signed row to draft, and a null season reaches the database and 500s."""
    status: Optional[str] = Field(default=None, max_length=32)
    season: Optional[str] = Field(default=None, min_length=1, max_length=64)
    offering_label: Optional[str] = Field(default=None, max_length=200)
    snapshot_key: Optional[str] = Field(default=None, max_length=200)

    starts_on: Optional[date] = Field(default=None)
    ends_on: Optional[date] = Field(default=None)
    signed_on: Optional[date] = Field(default=None)

    # Optional here means OMITTABLE, never nullable — the fields stay Optional so
    # an absent key is still "unset" under exclude_unset, but a null VALUE is a
    # 422 instead of a write nothing downstream can honour:
    @field_validator("status", "season", mode="before")
    @classmethod
    def reject_explicit_null(cls, v: Any, info) -> Any:
        if v is None:
            raise ValueError(f"{info.field_name} cannot be null; omit the field to leave it unchanged.")
        return v


# ============================================================================
# EMAIL UTILITY SCHEMAS
# ============================================================================

class EmailCooldownResponse(BaseModel):
    cooldown_remaining_seconds: Optional[int] = Field(default=None)


class EmailListResponse(BaseModel):
    count: int = Field(...)
    emails: List[str] = Field(default_factory=list)


# ============================================================================
# PAYMENTS SCHEMAS
# ============================================================================

class CatalogItemResponse(BaseModel):
    """Public shape of a purchasable catalog item (mirrors the Stripe price)"""
    key: str = Field(...)
    name: str = Field(...)
    type: ProductType = Field(...)
    currency: str = Field(...)
    amount_minor: int = Field(...)
    # Optional strikethrough/compare-at price for marketing display:
    compare_at_minor: Optional[int] = Field(default=None)
    # Billing interval for subscriptions (e.g. "month", "year"); null for one-time:
    interval: Optional[str] = Field(default=None)
    launch_stage: LaunchStage = Field(...)
    per_user_limit: Optional[int] = Field(default=None)
    # Purchase window (item available for purchase). Not a discount window:
    purchase_valid_from: Optional[datetime] = Field(default=None)
    purchase_valid_to: Optional[datetime] = Field(default=None)
    requires_shipping: bool = Field(default=False)
    entitlement_feature: Optional[str] = Field(default=None)

    class Config:
        from_attributes = True

# ============================================================================
# ACCESSIBILITY PREFERENCES
# ============================================================================

class AccessibilityPreferencesUpdateRequest(BaseModel):
    """Full accessibility preference set (the client always sends the whole
    object). `enabled` is the explicit master choice; the other fields carry
    the panel's settings even while the master is off so they survive."""
    enabled: bool = Field(...)
    text_scale: Literal["sm", "md", "lg", "xl"] = Field(default="md")
    high_contrast: bool = Field(default=False)
    enhanced_readability: bool = Field(default=False)
    reduce_effects: Literal["system", "on", "off"] = Field(default="system")


class AccessibilityPreferencesResponse(BaseModel):
    # None = the user has never made an explicit master choice (client
    # auto-detects from browser signals like prefers-reduced-motion):
    enabled: Optional[bool] = Field(default=None)
    text_scale: str = Field(default="md")
    high_contrast: bool = Field(default=False)
    enhanced_readability: bool = Field(default=False)
    reduce_effects: str = Field(default="system")

    class Config:
        from_attributes = True


class CheckoutSessionRequest(BaseModel):
    item_key: str = Field(..., min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1, le=100)
    # Same-origin path the buyer came from, echoed back on the result pages:
    return_to: Optional[str] = Field(default=None, max_length=512)


class CheckoutSessionResponse(BaseModel):
    url: str = Field(...)


class PortalSessionResponse(BaseModel):
    url: str = Field(...)


class PurchaseResponse(BaseModel):
    id: int = Field(...)
    item_key: str = Field(...)
    item_type: str = Field(...)
    amount_total_minor: int = Field(...)
    currency: str = Field(...)
    quantity: int = Field(...)
    status: PurchaseStatus = Field(...)
    created_at: datetime = Field(...)

    class Config:
        from_attributes = True


class PaymentsMeResponse(BaseModel):
    entitlements: List[str] = Field(default_factory=list)
    purchases: List[PurchaseResponse] = Field(default_factory=list)


class ReconcileSummaryResponse(BaseModel):
    sessions_seen: int = Field(...)
    purchases_created: int = Field(...)
    purchases_updated: int = Field(...)
    statuses_corrected: int = Field(...)
    entitlements_refreshed: int = Field(...)


class InboxConfigResponse(BaseModel):
    configured: bool = Field(...)
    # Reading can be wired while sending is not; the composer hides on False:
    can_send: bool = Field(default=False)
    # The mailbox identity (non-secret); shown across the inbox UI:
    mailbox_email: str = Field(default="")


class InboxAddress(BaseModel):
    email: str = Field(...)
    name: str = Field(default="")


class InboxAttachmentMeta(BaseModel):
    index: int = Field(...)
    filename: str = Field(...)
    content_type: str = Field(...)
    size: int = Field(...)
    is_inline: bool = Field(default=False)


class InboxMessageSummary(BaseModel):
    id: str = Field(...)
    folder: str = Field(...)
    subject: str = Field(...)
    sender: InboxAddress = Field(...)
    date: Optional[str] = Field(default=None)
    is_unread: bool = Field(...)
    is_flagged: bool = Field(default=False)
    has_attachments: bool = Field(...)
    snippet: str = Field(default="")
    # Threading identity so the client can group summaries into conversations:
    message_id_header: Optional[str] = Field(default=None)
    references: list[str] = Field(default_factory=list)


class InboxMessagePage(BaseModel):
    messages: list[InboxMessageSummary] = Field(...)
    page: int = Field(...)
    page_count: int = Field(...)


class InboxSuggestedReply(BaseModel):
    # Reply-all prefill assembled server-side; the composer may edit it:
    to: list[InboxAddress] = Field(...)
    cc: list[InboxAddress] = Field(...)
    subject: str = Field(...)


class InboxMessageDetail(BaseModel):
    id: str = Field(...)
    folder: str = Field(...)
    subject: str = Field(...)
    sender: InboxAddress = Field(...)
    to: list[InboxAddress] = Field(...)
    cc: list[InboxAddress] = Field(...)
    date: Optional[str] = Field(default=None)
    is_unread: bool = Field(...)
    is_flagged: bool = Field(default=False)
    # Sanitized server-side (nh3) with inline images resolved; the client still
    # renders it inside a sandboxed iframe whose CSP gates remote loads:
    html: str = Field(...)
    attachments: list[InboxAttachmentMeta] = Field(...)
    suggested_reply: InboxSuggestedReply = Field(...)


# Outbound attachments arrive base64-encoded; the cap protects both the API
# body size and the delivery provider's message limits:
_MAX_ATTACHMENT_TOTAL_B64_CHARS = 11_000_000  # ~8MB of binary payload


class InboxOutgoingAttachment(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_base64: str = Field(..., min_length=1)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    # Present = inline image the HTML references as cid:<value>:
    cid: Optional[str] = Field(default=None, max_length=255)


class _InboxOutgoingBase(BaseModel):
    to: list[InboxAddress] = Field(..., min_length=1)
    cc: list[InboxAddress] = Field(default_factory=list)
    bcc: list[InboxAddress] = Field(default_factory=list)
    html: str = Field(..., min_length=1)
    attachments: list[InboxOutgoingAttachment] = Field(default_factory=list)
    # Present = schedule-send: the message waits in the outbox until then:
    send_at: Optional[datetime] = Field(default=None)

    @model_validator(mode="after")
    def send_at_must_be_future(self):
        if self.send_at is not None:
            when = self.send_at if self.send_at.tzinfo else self.send_at.replace(tzinfo=UTC)
            if when <= datetime.now(UTC):
                raise ValueError("send_at must be in the future.")
            self.send_at = when
        return self

    @model_validator(mode="after")
    def enforce_attachment_budget(self):
        total = sum(len(att.content_base64) for att in self.attachments)
        if total > _MAX_ATTACHMENT_TOTAL_B64_CHARS:
            raise ValueError("Attachments exceed the size limit (8MB total).")
        return self


class InboxSendRequest(_InboxOutgoingBase):
    subject: str = Field(..., min_length=1, max_length=998)


class InboxReplyRequest(_InboxOutgoingBase):
    # Blank = keep the server-derived Re: subject:
    subject: str = Field(default="", max_length=998)


class InboxForwardRequest(_InboxOutgoingBase):
    pass


class InboxUpdateMessageRequest(BaseModel):
    # Read-state and starring are shared triage state, part of reading
    # (inbox:read); at least one field must be present:
    is_unread: Optional[bool] = Field(default=None)
    is_flagged: Optional[bool] = Field(default=None)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.is_unread is None and self.is_flagged is None:
            raise ValueError("Provide is_unread or is_flagged.")
        return self


class InboxDraftRequest(BaseModel):
    to: list[InboxAddress] = Field(default_factory=list)
    cc: list[InboxAddress] = Field(default_factory=list)
    bcc: list[InboxAddress] = Field(default_factory=list)
    subject: str = Field(default="", max_length=998)
    html: str = Field(default="")
    attachments: list[InboxOutgoingAttachment] = Field(default_factory=list)


class InboxDraftResponse(BaseModel):
    id: str = Field(...)


class InboxScheduledItem(BaseModel):
    id: int = Field(...)
    subject: str = Field(default="")
    to: list[InboxAddress] = Field(default_factory=list)
    send_at: datetime = Field(...)
    status: str = Field(...)
    # The frozen composer payload, so a pending row can be re-opened in the
    # composer and edited (cancel + recompose) rather than only cancelled:
    kind: str = Field(default="new")
    original_id: Optional[str] = Field(default=None)
    cc: list[InboxAddress] = Field(default_factory=list)
    bcc: list[InboxAddress] = Field(default_factory=list)
    html: str = Field(default="")
    attachments: list[InboxOutgoingAttachment] = Field(default_factory=list)


class InboxMoveRequest(BaseModel):
    folder: str = Field(..., min_length=1, max_length=255)


class InboxConnectRequest(BaseModel):
    # Which client the credential is being taken for (audit metadata):
    connector: str = Field(..., min_length=1, max_length=40)


class InboxConnectionSettings(BaseModel):
    # The full connection material; served only inside the gated, audited flow:
    email: str = Field(...)
    password: str = Field(...)
    imap_host: str = Field(...)
    imap_port: int = Field(...)
    smtp_host: str = Field(...)
    smtp_port: int = Field(...)


class InboxConnectionInteraction(BaseModel):
    id: int = Field(...)
    connector: str = Field(...)
    created_at: datetime = Field(...)


class InboxConnectionGroup(BaseModel):
    # One row per admin, whatever their interaction volume; the aggregation
    # happens in SQL so no admin can page another out of the log:
    user_email: str = Field(default="")
    user_name: str = Field(default="")
    count: int = Field(...)
    latest_at: datetime = Field(...)
    interactions: list[InboxConnectionInteraction] = Field(default_factory=list)


# The batch surface accepts one action applied to many message ids; results
# report per-id success so partial failures stay visible:
_INBOX_BATCH_ACTIONS = {"archive", "spam", "inbox", "trash", "delete", "read", "unread", "star", "unstar"}


class InboxBatchRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=100)
    action: str = Field(...)

    @model_validator(mode="after")
    def action_known(self):
        if self.action not in _INBOX_BATCH_ACTIONS:
            raise ValueError(f"Unknown action; expected one of {sorted(_INBOX_BATCH_ACTIONS)}.")
        return self


class InboxBatchResult(BaseModel):
    id: str = Field(...)
    ok: bool = Field(...)


# ============================================================================
# MANAGED LINKS
# ============================================================================

class ManagedLinkCreateRequest(BaseModel):
    key: str = Field(..., min_length=2, max_length=64)
    destination: str = Field(..., min_length=1, max_length=2048)
    label: Optional[str] = Field(default=None, max_length=200)


class ManagedLinkUpdateRequest(BaseModel):
    destination: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    label: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = Field(default=None)


class ManagedLinkResponse(BaseModel):
    key: str = Field(...)
    destination: str = Field(...)
    label: Optional[str] = Field(default=None)
    is_active: bool = Field(...)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)

    model_config = {"from_attributes": True}


class ManagedLinkResolveResponse(BaseModel):
    key: str = Field(...)
    destination: str = Field(...)
    label: Optional[str] = Field(default=None)
