from datetime import datetime, timedelta, UTC

from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy import Column, Integer, String, ForeignKey, Enum, Date, DateTime, Boolean, Text, Index, UniqueConstraint
from sqlalchemy.sql import expression
from platform.enums import PurchaseStatus, Role
from infrastructure.settings import settings as s
from infrastructure.db_types import JSONColumn

from infrastructure.database import Base


# ============================================================================
# USER MODEL
# ============================================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    
    name = Column(String, nullable=False)
    # Start of the current name-change allowance window (null = no change since
    # signup, or the last window has lapsed) and how many changes have been made
    # within it. Together they drive the "N changes per period" limit:
    name_changed_at = Column(DateTime(timezone=True), nullable=True)
    name_change_count = Column(Integer, nullable=False, default=0, server_default="0")
    email = Column(String, index=True, unique=True, nullable=False)

    password = Column(String, nullable=True)

    is_verified = Column[bool](Boolean, nullable=False, default=False, server_default=expression.false(), index=True)
    # Identity, not authorization (see the access-control rule): a normal USER,
    # or ADMIN — the catch-all break-glass identity that implicitly holds every
    # capability. Authorization is the per-account grants in `capabilities`:
    role = Column(
        Enum(Role, values_callable=lambda e: [i.value for i in e], native_enum=False),
        nullable=False,
        default=Role.USER,
        server_default=Role.USER.value,
    )

    # Derived identity accessor so existing reads (`user.is_admin`) and SQL
    # filters (`User.is_admin`) keep working over the single `role` column:
    @hybrid_property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @is_admin.expression
    def is_admin(cls):
        return cls.role == Role.ADMIN

    # The Python-side twin of verify_capability, for service-layer checks (e.g.
    # the learn prerequisite gate). Admin implicitly holds every capability;
    # every other account holds its EFFECTIVE set — grants held directly, plus
    # those conferred by each ACTIVE organisation it belongs to. Conferral is
    # computed here and never materialised onto the row, so removing a
    # membership revokes the grant with nothing left to re-sync. The local
    # import avoids a models <-> organisation_service cycle:
    def has_capability(self, capability: str) -> bool:
        from platform.services.organisation_service import effective_capabilities
        return self.is_admin or capability in effective_capabilities(self)

    # Current display-name change policy for this account (mode / allowance /
    # interval / remaining / reset date), computed from settings + the window
    # columns above. Exposed on UserResponse so the client renders the right
    # confirmation and copy deterministically. Local import avoids a
    # models <-> user_service import cycle.
    @property
    def name_change_policy(self) -> dict:
        from platform.services.user_service import compute_name_change_policy
        return compute_name_change_policy(self, datetime.now(UTC))

    # Per-account capability grants (e.g. "inbox:read"); the registry of valid
    # strings is platform/capabilities.py. Admin implicitly holds every
    # capability; any other account holds only what is listed here (see the
    # access-control rule). This column means grants held DIRECTLY by the
    # account and nothing else — organisation code never writes to it, so a
    # conferred grant stays distinguishable from a direct one without a second
    # column, and revocation is just deleting the membership row (see
    # organisation_service.effective_capabilities):
    capabilities = Column(JSONColumn, nullable=True)

    # Whether the user has given consent to receive marketing communications (DB default false):
    marketing_consent = Column(Boolean, nullable=False, default=False, server_default=expression.false(), index=True)

    # Whether this member appears on the leaderboards. Off removes them from
    # every board's rows AND from its rank counts, so nobody's rank is computed
    # against a person who is not shown; their own position is then withheld
    # too, and the client says so. DB default true: a member is listed until
    # they choose otherwise:
    show_on_leaderboard = Column(Boolean, nullable=False, default=True, server_default=expression.true(), index=True)

    # Profile picture URL (captured from OAuth on first login, or set via upload):
    avatar_url = Column(String, nullable=True)

    verification_code = Column(String, nullable=True, index=True)
    verification_code_expires = Column(DateTime(timezone=True), nullable=True)
    last_verification_email_sent = Column(DateTime(timezone=True), nullable=True)

    reset_password_code = Column(String, nullable=True, index=True)
    reset_password_code_expires = Column(DateTime(timezone=True), nullable=True)
    last_reset_password_email_sent = Column(DateTime(timezone=True), nullable=True)

    # Stripe billing identity and entitlement cache (source of truth is Stripe;
    # the cache is refreshed by entitlement webhooks):
    stripe_customer_id = Column(String, nullable=True, unique=True, index=True)
    entitlements = Column(JSONColumn, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # When this account last made an authenticated request. Deliberately
    # NULLABLE with no default: null means "not seen since the column shipped",
    # which is honest and distinguishable from a genuinely old timestamp — a
    # backfilled created_at would have silently claimed activity that never
    # happened. Stamped in ONE place (infrastructure/dependencies.stamp_last_accessed,
    # on the single dependency every authenticated request resolves through) and
    # throttled there, so an active session writes at most once per window
    # rather than turning every read into a write.
    #
    # NOT indexed, on purpose: nothing reads it as a filter or sort key today,
    # and it is the most write-hot column on the table — an index on it would
    # be rewritten by every stamp and would block HOT updates (index bloat on
    # PostgreSQL) to buy an ordering no query asks for. The moment a real "who
    # has been active" admin query exists, that trade flips and it earns a
    # (probably DESC) index in its own migration.
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)

    # Defining relationship to refresh sessions and relying on DB-level cascade deletes:
    refresh_sessions = relationship(
        "RefreshSession",
        back_populates="user",
        passive_deletes=True,
    )

    # Organisations this account belongs to (see OrganisationMember); the
    # DB-level cascade removes the membership rows when the user is deleted:
    organisation_memberships = relationship(
        "OrganisationMember",
        back_populates="user",
        passive_deletes=True,
    )


    def get_verification_cooldown_seconds(self) -> int:
        """Utility for calculating the remaining verification cooldown for this user"""
        if not self.last_verification_email_sent: return 0
        
        now_utc = datetime.now(UTC)
        cooldown_expires = self.last_verification_email_sent + timedelta(minutes=s.EMAIL_VERIFICATION_COOLDOWN)
        
        if now_utc < cooldown_expires:
            return int((cooldown_expires - now_utc).total_seconds())
        return 0


# ============================================================================
# REFRESH SESSION MODEL
# ============================================================================

class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)

    # The active refresh token's JTI (JWT instance ID)
    # Not PK since sessions span multiple refresh tokens (which change on every rotation):
    current_jti = Column(String, index=True, nullable=False)

    # Previous JTI for a short grace window to avoid false reuse on races:
    previous_jti = Column(String, nullable=True)
    previous_valid_until = Column(DateTime(timezone=True), nullable=True)

    # Expiry and revocation metadata:
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)

    
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationship back to user (no ORM-level delete cascade; DB handles it):
    user = relationship("User", back_populates="refresh_sessions")


# ============================================================================
# CONTACT SUBMISSION MODEL
# ============================================================================

class ContactSubmission(Base):
    __tablename__ = "contact_submissions"

    id = Column(Integer, primary_key=True)
    
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    company = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    
    resolved = Column(Boolean, nullable=False, default=False, server_default=expression.false(), index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


# Audit registry of mail-client credential grants from the inbox connector
# flow: one row per reveal/profile download (who, which connector, when).
# Audit knowledge, not enforcement - the mailbox has one shared credential,
# so revocation is a password rotation that disconnects every device at once:
class MailboxConnection(Base):
    __tablename__ = "mailbox_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connector = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")


# Outbox for schedule-send: the composed message (provider-neutral payload)
# waits here until send_at, when the dispatcher thread sends it through the
# normal send path and removes the row. Status marks in-flight/failed states
# so a crashed dispatch is visible, never silently lost:
class ScheduledMessage(Base):
    __tablename__ = "scheduled_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    payload = Column(JSONColumn, nullable=False)
    send_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")


# Instance-generated key material (e.g. the web-push VAPID keypair) and small
# platform watermarks, generated once at first boot and stored here so no env
# var needs provisioning per instance. Values are secrets or plumbing state -
# never expose the table through an API:
class PlatformKey(Base):
    __tablename__ = "platform_keys"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# One row per browser/device that opted into web-push notifications. The
# endpoint is the push service's unique handle; dead endpoints (404/410 on
# delivery) are pruned automatically:
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(Text, nullable=False)
    auth = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")


# ============================================================================
# PAYMENTS MODELS (STRIPE MIRROR)
# ============================================================================

# Local mirror of completed Stripe Checkout purchases. Stripe is the source of
# truth; rows are written by webhook/reconcile handlers only and exist for fast
# per-user limits and "my purchases" reads:
class Purchase(Base):
    __tablename__ = "purchases"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)

    # Catalog item key (Stripe price lookup_key):
    item_key = Column(String, index=True, nullable=False)
    # Plain string rather than an enum column: historical purchases may reference
    # catalog items (and types) that no longer exist in the authored manifest:
    item_type = Column(String, nullable=False)

    # Stripe object references:
    stripe_checkout_session_id = Column(String, unique=True, index=True, nullable=False)
    stripe_payment_intent_id = Column(String, index=True, nullable=True)
    stripe_subscription_id = Column(String, index=True, nullable=True)

    # Money in minor units to avoid float precision issues:
    amount_total_minor = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    quantity = Column(Integer, nullable=False, default=1, server_default="1")

    # Shipping details captured by Checkout for physical goods:
    shipping = Column(JSONColumn, nullable=True)

    status = Column(
        Enum(PurchaseStatus, values_callable=lambda e: [i.value for i in e], native_enum=False),
        nullable=False,
        index=True,
        server_default=PurchaseStatus.PENDING.value,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# Processed Stripe webhook events for idempotent handling:
class StripeEvent(Base):
    __tablename__ = "stripe_events"

    # Stripe event id (evt_...) as the natural primary key:
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)

    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ============================================================================
# GENERATIVE AI CONVERSATION MODELS
# ============================================================================

class Conversation(Base):
    """User's AI conversation."""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title = Column(String, nullable=True)  # Optional user-provided title
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships:
    user = relationship("User", passive_deletes=True)
    nodes = relationship("ConversationNode", back_populates="conversation", passive_deletes=True)


class ConversationNode(Base):
    """Tree node in conversation (stores message structure, not content)."""
    __tablename__ = "conversation_nodes"
    
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    
    # ID of the parent node:
    parent_id = Column(Integer, ForeignKey("conversation_nodes.id", ondelete="CASCADE"), nullable=True, index=True)
    
    # Message metadata (content stored separately in MessageContent):
    role = Column(String, nullable=False, index=True)  # user, assistant, system, tool
    
    # Summary generated by summarization logic:
    summary = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    
    # Relationships:
    conversation = relationship("Conversation", back_populates="nodes")
    parent = relationship("ConversationNode", remote_side=[id], passive_deletes=True)
    children = relationship("ConversationNode", back_populates="parent", passive_deletes=True)
    contents = relationship("MessageContent", back_populates="node", passive_deletes=True)


class MessageContent(Base):
    """
    Normalized content storage for messages (text/image/audio/tool_call).
    Enables future RAG, querying by type, and embedding storage.
    """
    __tablename__ = "message_contents"
    
    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("conversation_nodes.id", ondelete="CASCADE"), index=True, nullable=False)
    position = Column(Integer, nullable=False)  # Order within message (0, 1, 2...)
    content_type = Column(String, nullable=False, index=True)  # text, image, audio, tool_call, file
    
    # Text content:
    text_content = Column(Text, nullable=True)
    
    # Image content:
    image_url = Column(String, nullable=True)
    image_width = Column(Integer, nullable=True)
    image_height = Column(Integer, nullable=True)
    
    # Audio content:
    audio_data = Column(Text, nullable=True)  # base64
    audio_format = Column(String, nullable=True)  # wav, mp3, opus
    audio_transcript = Column(Text, nullable=True)
    
    # Tool call content:
    tool_call_id = Column(String, nullable=True)
    tool_name = Column(String, nullable=True)
    tool_arguments = Column(Text, nullable=True)  # String (already JSON from OpenAI spec)
    
    # File content (reusing columns where possible):
    # file_url stored in image_url
    # file_id, file_data, filename stored in text fields below:
    file_id = Column(String, nullable=True)
    file_data = Column(Text, nullable=True)
    filename = Column(String, nullable=True)
    
    # Message metadata:
    message_name = Column(String, nullable=True)  # For tool messages (links to function name)
    
    # Future: Vector embedding for RAG
    # embedding = Column(Vector(1536), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships:
    node = relationship("ConversationNode", back_populates="contents")


# ============================================================================
# ACCESSIBILITY PREFERENCES
# ============================================================================

class AccessibilityPreferences(Base):
    """Per-user accessibility preferences (a platform concern, like auth).
    The frontend applies these from localStorage instantly/offline; this row
    is the online cross-device source of truth. One row per user — absence
    means the user has made no explicit choice yet (the client auto-detects
    from browser signals such as prefers-reduced-motion)."""

    __tablename__ = "accessibility_preferences"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    # None = no explicit master choice (client decides from browser signals):
    enabled = Column(Boolean, nullable=True)
    text_scale = Column(String, nullable=False, server_default="md")  # sm|md|lg|xl
    high_contrast = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    enhanced_readability = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    reduce_effects = Column(String, nullable=False, server_default="system")  # system|on|off
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================================
# MARKETING SUPPRESSION
# ============================================================================

class MarketingSuppression(Base):
    """Email-keyed do-not-send list for bulk / marketing mail.

    Keyed by the raw email, not a user id, so it covers recipients who are not
    registered users (a bulk announcement can go to anyone). `User.marketing_consent`
    remains the registered user's stored preference; the two are kept in lockstep
    by marketing_service (unsubscribing mirrors down to consent, and vice versa),
    with this table as the address-level source of truth any sender can consult.
    """

    __tablename__ = "marketing_suppressions"

    id = Column(Integer, primary_key=True)
    # Stored already normalized (trimmed + lowercased) so lookups are exact:
    email = Column(String, nullable=False, unique=True, index=True)
    # unsubscribe | bounce | complaint | manual — provenance, not a gate:
    reason = Column(String, nullable=False, server_default="unsubscribe")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ============================================================================
# ORGANISATION MODELS
# ============================================================================

class Organisation(Base):
    """A company, university or society an account can belong to.

    Organisations are DATA, never authorization: no gate ever asks about an
    organisation (gating stays the one primitive, require_capability). What an
    organisation does is CONFER capabilities — while it is active,
    `granted_capabilities` is unioned onto every member's effective grants at
    CHECK time (organisation_service.effective_capabilities); nothing is ever
    written to `users.capabilities`, so there is no materialised copy to drift
    or to re-sync. "HRT is a sponsor" therefore becomes feature:preview +
    feature:early-access on its people, derived from this relation rather than
    hand-set. Which capabilities a sponsorship tier confers is a product
    decision (product/organisations.py); platform stays generic.

    Gating a surface to ONE named organisation is deliberately not expressible
    here — capabilities are not parameterised. That is a data-scoped ownership
    check over the membership rows, a different pattern (see the plan).
    """

    __tablename__ = "organisations"

    id = Column(Integer, primary_key=True)

    # Stable external identifier ("hudson-river-trading"), the idempotency key
    # create_organisation upserts on:
    slug = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)

    website_url = Column(String, nullable=True)
    logo_url = Column(String, nullable=True)

    # Only an ACTIVE organisation confers its grants; deactivating is how a
    # lapsed sponsorship stops conferring without deleting the relation:
    is_active = Column(Boolean, nullable=False, default=True, server_default=expression.true(), index=True)

    # list[str] of capability strings, every one validated against
    # ALL_CAPABILITIES on write (the typo guard — a typo'd grant gates nothing):
    granted_capabilities = Column(JSONColumn, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    members = relationship(
        "OrganisationMember",
        back_populates="organisation",
        passive_deletes=True,
    )

    # Every partnership this organisation has ever held, across seasons (see
    # Partnership — renewals are new rows, nothing is overwritten or removed):
    partnerships = relationship(
        "Partnership",
        back_populates="organisation",
        passive_deletes=True,
    )


class OrganisationMember(Base):
    """One account's membership of one organisation.

    A user may belong to any number of organisations and an organisation to any
    number of users; the unique constraint keeps the pair to a single row so
    add_member is idempotent. Both foreign keys cascade at the DB level, so
    deleting either side removes the membership — which is the whole of
    revocation, since conferred grants were never copied onto the account.
    """

    __tablename__ = "organisation_members"

    id = Column(Integer, primary_key=True)
    # Constraints are named explicitly so create_all and the migration produce
    # the SAME schema: create_all runs first at boot, so an unnamed FK would take
    # the backend's auto-name and a later op.drop_constraint would miss it:
    organisation_id = Column(
        Integer,
        ForeignKey(
            "organisations.id",
            name="fk_organisation_members_organisation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_organisation_members_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    # Free-text role within the organisation ("University Relations"); purely
    # descriptive, never consulted by any authorization decision:
    title = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organisation = relationship("Organisation", back_populates="members")
    user = relationship("User", back_populates="organisation_memberships")

    __table_args__ = (
        UniqueConstraint("organisation_id", "user_id", name="uq_organisation_members_org_user"),
    )


class Partnership(Base):
    """One organisation's partnership with this instance, for one season.

    STATE, not a catalogue. The backend knows nothing about tiers, benefits or
    prices: it records that an organisation is at some point of a workflow for
    some season, and stores `offering_label` / `snapshot_key` as opaque strings
    it never parses (what they mean is a frontend/invoice decision). Keeping the
    catalogue out of the database is what lets the offering be redesigned without
    a migration.

    HISTORY IS APPEND-ONLY. An organisation has MANY partnerships — a renewal is
    a NEW row for the new season, never an overwrite, and nothing is deleted. No
    unique constraint on (organisation, season) on purpose: one season may carry
    more than one agreement, and the seeder's one-per-season rule is a seeding
    decision, not a schema one.

    Expiry is COMPUTED (is_expired / days_remaining from `ends_on`), never a
    stored flag a job flips: the row then records decisions people made, and the
    "expiring soon" admin question is answered from the dates themselves.
    """

    __tablename__ = "partnerships"

    id = Column(Integer, primary_key=True)
    # Named explicitly for the same reason as the membership FKs — create_all
    # runs at boot, so an unnamed constraint would take the backend's auto-name:
    organisation_id = Column(
        Integer,
        ForeignKey(
            "organisations.id",
            name="fk_partnerships_organisation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    # One of platform/partnerships.PARTNERSHIP_STATUSES, validated in Python on
    # every write (partnership_service). A plain String, never a DB enum:
    status = Column(String, nullable=False, server_default="draft", index=True)

    # Opaque to the backend: the season label ("2026/27"), the free-text label
    # invoices and admin lists show ("Talent Partner"), and the frontend's key
    # for the agreement snapshot it rendered. Stored verbatim, never parsed:
    season = Column(String, nullable=False, index=True)
    offering_label = Column(String, nullable=True)
    snapshot_key = Column(String, nullable=True)

    # Dates, not datetimes: these are contractual days, not instants:
    starts_on = Column(Date, nullable=True)
    ends_on = Column(Date, nullable=True)
    signed_on = Column(Date, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    organisation = relationship("Organisation", back_populates="partnerships")

    @property
    def days_remaining(self) -> int | None:
        """Days until `ends_on`, negative once past it; None when open-ended.

        Computed per read against today, so "expiring soon" needs no job and no
        column that could go stale between runs."""
        if self.ends_on is None:
            return None
        return (self.ends_on - datetime.now(UTC).date()).days

    @property
    def is_expired(self) -> bool:
        """Whether `ends_on` is in the past. Independent of `status` by design:
        a lapsed date does not silently move a partnership to "ended" — that
        transition is an explicit act, recorded as one."""
        remaining = self.days_remaining
        return remaining is not None and remaining < 0


# ============================================================================
# QUESTION FLOW MODELS
# ============================================================================

class QuestionFlowResponse(Base):
    """One COMPLETED snapshot of one account's answers to one question flow.

    Append-only by contract: nothing in the application ever updates or deletes a
    row here (only the user-delete cascade does). A re-take writes a new row and
    reads take the latest, which means the history of what someone answered - and
    when - survives every later edit. Storing the version alongside the answers is
    what makes that history readable years later: the schema that validated a
    snapshot is identified by the pair, so an old snapshot never has to satisfy a
    newer contract.

    `completed_at` is the staleness fact and is always server-stamped at submit.
    It is deliberately independent of `users.created_at`: an account that answers
    two years after signing up has a fresh snapshot, and one that answered on day
    one has a stale one. Consumers own the staleness policy; this table only
    reports the timestamp.

    `flow_key` is a plain string, not a foreign key or an enum - flow definitions
    live in code (platform/question_flows.py) and a retired flow's snapshots must
    outlive its registration rather than block the deploy that removed it.
    """

    __tablename__ = "question_flow_responses"

    id = Column(Integer, primary_key=True)
    # Constraints are named explicitly so create_all and the migration produce
    # the SAME schema (see OrganisationMember above for why that matters):
    user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_question_flow_responses_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    flow_key = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    # The validated + normalised answers (answers_schema.model_dump(mode="json")),
    # size-capped by the service. PII: never logged:
    answers = Column(JSONColumn, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The one read shape: "latest snapshot for this account and flow", plus
        # the completion-summary scan for one account. Descending on completed_at
        # so the latest-wins read is a single index step, not a sort:
        Index(
            "ix_question_flow_responses_user_flow_completed",
            "user_id", "flow_key", completed_at.desc(),
        ),
    )


class QuestionFlowReminder(Base):
    """Sent-record for one reminder stage of one flow to one account.

    This table is the idempotency mechanism for the reminder sweep, not an audit
    log that happens to be useful. The sweep INSERTS the row and commits BEFORE
    it sends, so the unique constraint is what makes a concurrent worker, a retry
    or a crashed tick unable to produce a second email: losing the insert race
    means someone else owns the send. The deliberate consequence is that a send
    that fails after the claim is forfeited rather than retried - a missing nudge
    is a nothing, a duplicate nudge to a stranger's inbox is not.

    `stage` is the ORDINAL into the flow's registered reminder_stages tuple (see
    ReminderStage), so stage order is part of the persisted contract.
    """

    __tablename__ = "question_flow_reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_question_flow_reminders_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    flow_key = Column(String(64), nullable=False)
    stage = Column(Integer, nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The hard at-most-once backstop the claim-first sweep relies on:
        UniqueConstraint(
            "user_id", "flow_key", "stage",
            name="uq_question_flow_reminders_user_flow_stage",
        ),
    )


class QuestionFlowLead(Base):
    """A PARTIAL, pre-account answer set keyed by email address (dormant module).

    Only reachable for a flow whose definition sets lead_capture=True; with no
    such flow registered the endpoints 404 and nothing ever writes here. It exists
    for the ordering where a visitor answers before creating an account, so the
    answers are keyed by the address rather than by a user id.

    Lawful basis is Art 6(1)(b) (steps at the data subject's request prior to
    entering a contract) - NOT consent to marketing, and this table is never a
    marketing list. Two properties keep that honest: unconverted rows are purged
    after LEAD_RETENTION_DAYS by the sweep, and the row is DELETED the moment the
    address becomes an account (the answers move to question_flow_responses,
    where they belong).

    `answers` is nullable and NOT strictly validated - a partial flow is the whole
    point - so nothing may read it without re-validating. `converted_user_id` is
    audit only (SET NULL on user delete): the normal conversion path removes the
    row rather than marking it.
    """

    __tablename__ = "question_flow_leads"

    id = Column(Integer, primary_key=True)
    # Stored already normalized (trimmed + lowercased), like MarketingSuppression:
    email = Column(String, nullable=False, index=True)
    flow_key = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    answers = Column(JSONColumn, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    converted_user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_question_flow_leads_converted_user_id", ondelete="SET NULL"),
        nullable=True,
    )
    converted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The upsert key: one lead row per address per flow:
        UniqueConstraint("email", "flow_key", name="uq_question_flow_leads_email_flow"),
    )


# MANAGED LINKS (NFC / QR / campaign redirects)
# ============================================================================

class ManagedLink(Base):
    """Stable public key → mutable destination for physical/digital campaigns.

    The burned/printed URL is `/links/<key>`; retargeting is a row update, not a
    tag rewrite. Keys are identity; destinations and labels are operator data.
    """

    __tablename__ = "managed_links"

    id = Column(Integer, primary_key=True)
    # Public path segment under /links/<key> — lowercase slug, unique forever:
    key = Column(String(64), nullable=False, unique=True, index=True)
    # Optional operator-facing name ("Freshers leaflet") — never in the public URL:
    label = Column(String(200), nullable=True)
    # Relative same-origin path (/events/foo) or absolute http(s) URL:
    destination = Column(String(2048), nullable=False)
    # Retire without deleting so a key cannot be silently recycled onto a new
    # campaign while old tags still exist in the wild:
    is_active = Column(Boolean, nullable=False, default=True, server_default=expression.true(), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )



# ============================================================================
# PACKAGE-OWNED MODELS (registration only)
# ============================================================================

# Registering the generative AI service's package-owned tables (ai_*) with
# Base.metadata so startup create_all sees them; their definitions stay inside
# the package per its extraction rules (see the package README, D26):
from platform.services.generative_ai_service import models as _generative_ai_models  # noqa: E402,F401
