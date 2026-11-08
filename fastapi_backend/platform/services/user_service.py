import base64
import logging
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import BackgroundTasks
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from infrastructure.service_auth import service_account_email
from infrastructure.settings import settings as s
from platform.exceptions import UserExistsException, UserNotFoundException, InvalidCodeException, VerificationEmailRateLimitException, InvalidAvatarException, NameNotAppropriateException, NameChangeRateLimitException
from platform.services.storage import get_object_storage
from platform.services.name_moderation import is_name_appropriate
from platform.utils.name_format import format_display_name
from platform.schemas import CreateUserRequest, EmailCooldownResponse, EmailListResponse, EmailExistsResponse
from platform.enums import CodeType, EmailTemplateType, UserListFilter
from platform.services.email_services import send_templated_email
from platform.services import question_flow_service
from utils import orm_to_dict
from platform.models import User, RefreshSession
from security import bcrypt_context, generate_numeric_code, generate_alphanumeric_code, hash_code, verify_code as verify_code_hash


logger = logging.getLogger(__name__)


# ============================================================================
# EMAIL UTILITIES
# ============================================================================

def send_user_email(user_data: Dict[str, Any], template_type: EmailTemplateType, **variables) -> None:
    """Sends a templated email to a user with error handling"""
    try:
        send_templated_email(
            template_type=template_type,
            recipient=user_data.get("email"),
            name=user_data.get("name"),
            **variables
        )
    except Exception:
        # By id, never the address: this is logger.error, so it becomes a
        # tracked event, and the observability rule keeps full emails out of
        # them. It matters more since the CV nudge sweep started calling this
        # unattended — one broken provider would otherwise post a batch of
        # members' email addresses into the error tracker.
        logger.error(
            "Failed to send %s email to user %s",
            template_type.value,
            user_data.get("id"),
            exc_info=True,
        )


# ============================================================================
# USER CRUD
# ============================================================================

def create_user(db: Session, user_data: CreateUserRequest, background_tasks: BackgroundTasks) -> User:
    """Creates a new user account and sends verification email"""
    # Rejecting the reserved service-account address (whether or not its row is
    # seeded yet): registering it would squat the identity a service token
    # resolves to. Raising the duplicate-email error so the refusal is
    # indistinguishable from the address already being taken (no enumeration
    # signal added):
    if user_data.email.strip().lower() == service_account_email():
        raise UserExistsException

    # Deterministic casing (McDonald, O'Brien, ...) + appropriateness gate. The
    # signup name doesn't count as a "change", so name_changed_at stays null and
    # the user's first rename is free:
    formatted_name = format_display_name(user_data.name)
    if not is_name_appropriate(formatted_name):
        raise NameNotAppropriateException

    new_user = User(
        name=formatted_name,
        email=user_data.email.lower(),
        password=bcrypt_context.hash(user_data.password),
        marketing_consent=user_data.marketing_consent
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        # Converting any pre-account question-flow leads for this address: the
        # answers now belong to an account, so the address-keyed rows go. Runs
        # after the commit and is fail-open by construction (it swallows and
        # logs its own failures) — housekeeping must never block a signup:
        question_flow_service.on_account_created(db, new_user)

        # Automatically sending verification code:
        generate_and_send_code(db, new_user, CodeType.VERIFICATION, email=True, background_tasks=background_tasks)

        return new_user

    except IntegrityError:
        db.rollback()
        raise UserExistsException


def create_passwordless_user(
    db: Session,
    name: str,
    email: str,
    marketing_consent: bool = False,
    background_tasks: Optional[BackgroundTasks] = None,
) -> User:
    """Creates an account with NO password — the state OAuth accounts already
    use: every password login path is dead by construction until the user sets
    one via the email-code flow. For funnels (e.g. the talent landing page)
    where possession of the mailbox is proven by the verification code rather
    than a chosen password. Sends the verification code immediately."""
    # Same service-account guard as create_user, same non-enumerating refusal:
    if email.strip().lower() == service_account_email():
        raise UserExistsException

    formatted_name = format_display_name(name)
    if not is_name_appropriate(formatted_name):
        raise NameNotAppropriateException

    new_user = User(
        name=formatted_name,
        email=email.lower(),
        password=None,
        marketing_consent=marketing_consent,
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        generate_and_send_code(db, new_user, CodeType.VERIFICATION, email=True, background_tasks=background_tasks)

        return new_user

    except IntegrityError:
        db.rollback()
        raise UserExistsException


def get_user_by_email(db: Session, email: str) -> User:
    user = db.query(User).filter_by(email=email.lower()).first()
    if user is None: raise UserNotFoundException
    return user


def check_display_name(name: str) -> Tuple[bool, str]:
    """Vet a requested display name without changing anything: returns
    (appropriate, deterministically-cased form). Used to gate the support/contact
    hand-off so an inappropriate name never reaches a support ticket."""
    formatted = format_display_name(name)
    return is_name_appropriate(formatted), formatted


def compute_name_change_policy(user: User, now: datetime) -> dict:
    """Current display-name change policy for this account, from settings + the
    user's allowance window. Read-only; the caller decides what to render:

    - mode "unlimited": rename freely, no confirmation.
    - mode "disabled": self-serve renames off; go through support.
    - mode "limited": `allowance` changes per `interval_days`; `remaining` left
      in the active window, which resets at `reset_at`.
    """
    interval = s.NAME_CHANGE_INTERVAL_DAYS
    allowance = max(0, s.NAME_CHANGE_ALLOWANCE)

    if interval is None:
        return {"mode": "unlimited", "allowance": None, "interval_days": None,
                "remaining": None, "reset_at": None}
    if interval == 0:
        return {"mode": "disabled", "allowance": 0, "interval_days": 0,
                "remaining": 0, "reset_at": None}

    window = timedelta(days=interval)
    window_start = user.name_changed_at
    window_active = window_start is not None and now < window_start + window
    if window_active:
        remaining = max(0, allowance - (user.name_change_count or 0))
        reset_at = window_start + window
    else:
        # The window has lapsed (or never started): a full allowance is available,
        # and a change now would open a fresh window closing one interval later.
        remaining = allowance
        reset_at = now + window
    return {"mode": "limited", "allowance": allowance, "interval_days": interval,
            "remaining": remaining, "reset_at": reset_at}


def update_user(
    db: Session,
    user: User,
    name: Optional[str] = None,
    marketing_consent: Optional[bool] = None,
    show_on_leaderboard: Optional[bool] = None,
) -> User:
    if name is not None:
        formatted = format_display_name(name)
        # Only an actual change is moderated, rate-limited, and counted -
        # re-saving the same name (or a marketing-only update) is a free no-op:
        if formatted != user.name:
            if not is_name_appropriate(formatted):
                raise NameNotAppropriateException

            now_utc = datetime.now(UTC)
            interval = s.NAME_CHANGE_INTERVAL_DAYS
            allowance = max(0, s.NAME_CHANGE_ALLOWANCE)
            if interval is not None:
                if interval == 0:
                    raise NameChangeRateLimitException(0, self_serve_disabled=True)

                window = timedelta(days=interval)
                window_active = (
                    user.name_changed_at is not None
                    and now_utc < user.name_changed_at + window
                )
                used = (user.name_change_count or 0) if window_active else 0
                if used >= allowance:
                    reset_at = (user.name_changed_at + window) if window_active else (now_utc + window)
                    raise NameChangeRateLimitException(max(0, int((reset_at - now_utc).total_seconds())))

                # Accepted: open a fresh window on the first change, else advance
                # the counter within the current one.
                if window_active:
                    user.name_change_count = used + 1
                else:
                    user.name_changed_at = now_utc
                    user.name_change_count = 1

            user.name = formatted

    if marketing_consent is not None:
        user.marketing_consent = marketing_consent
    # The leaderboards are uncached reads, so flipping this needs no eviction:
    # the next board request, from anyone, already reflects it.
    if show_on_leaderboard is not None:
        user.show_on_leaderboard = show_on_leaderboard
    db.commit()
    db.refresh(user)
    return user


def set_user_avatar(db: Session, user: User, avatar_url: Optional[str]) -> User:
    user.avatar_url = avatar_url
    db.commit()
    db.refresh(user)
    return user


# Allowed avatar content types mapped to file extensions:
AVATAR_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


# The ceiling for inlining an avatar into the row as a data URI when no durable
# backend is configured. Base64 costs ~33% on top, so this is ~340 KB of text in
# a column that is read on every profile render — small enough to be free, large
# enough to cover a normal profile picture:
INLINE_AVATAR_MAX_BYTES = 256 * 1024


def _inline_data_uri(data: bytes, media_type: str) -> str:
    """The avatar as a self-contained data URI, needing no storage plane."""
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _sniff_image_type(data: bytes) -> Optional[str]:
    """Detecting the real image type from magic bytes (never trusting the
    client-declared content type), returning the canonical media type or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def store_avatar(db: Session, user: User, data: bytes, content_type: Optional[str]) -> User:
    """Validates and stores an uploaded avatar via the configured object storage.

    The stored media type and extension are derived from the file's magic bytes,
    not the client-declared content type, so a spoofed header cannot smuggle a
    non-image payload onto a served URL.
    """
    if not data or len(data) > s.MAX_AVATAR_BYTES:
        max_mb = s.MAX_AVATAR_BYTES // (1024 * 1024)
        raise InvalidAvatarException(f"That image is empty or larger than {max_mb} MB.")

    sniffed = _sniff_image_type(data)
    extension = AVATAR_CONTENT_TYPES.get(sniffed or "")
    if not extension:
        raise InvalidAvatarException("That image type isn't supported. Use a PNG, JPEG, or WEBP file.")

    previous_url = user.avatar_url
    storage = get_object_storage()
    if storage.is_durable or len(data) > INLINE_AVATAR_MAX_BYTES:
        key = f"avatars/{user.id}/{uuid4().hex}.{extension}"
        url = storage.save(data, key, sniffed)
    else:
        # Best-effort degradation. With no durable backend the alternative is a
        # URL into a filesystem the next deploy discards, which renders as a
        # broken image forever. A small avatar inlined as a data URI is instead
        # self-contained: it lives in the row, needs no serving path, survives
        # exactly as long as the account, and is erased with it. Above the cap
        # we fall back to the filesystem rather than pushing megabytes of base64
        # into every row that joins on this user.
        url = _inline_data_uri(data, sniffed)
    updated = set_user_avatar(db, user, url)
    # Deleting the replaced file AFTER the new one is saved and recorded, so a
    # failed upload never orphans the account's current avatar. Best-effort:
    # a storage failure must not fail the upload, but is logged for retry.
    # Only THIS user's own prefix is ever extracted — a foreign avatar_url
    # (e.g. an OAuth picture URL) can never direct a delete at another key:
    if previous_url:
        marker = f"avatars/{user.id}/"
        idx = previous_url.find(marker)
        if idx != -1:
            try:
                get_object_storage().delete_prefix(previous_url[idx:])
            except Exception:
                logger.error("Failed to delete replaced avatar for user %s", user.id, exc_info=True)
    return updated


def email_exists(db: Session, email: str) -> EmailExistsResponse:
    user = db.query(User).filter_by(email=email.lower()).first()
    if not user:
        return EmailExistsResponse(exists=False)

    has_password = bool(user.password)
    return EmailExistsResponse(exists=True, has_password=has_password)


# ============================================================================
# CODE GENERATION & VERIFICATION
# ============================================================================

def generate_and_send_code(
    db: Session,
    user: User,
    code_type: CodeType,
    email: bool = True,
    background_tasks: BackgroundTasks = None
) -> str:
    """Generates and stores a verification or password reset code for a user"""

    now_utc = datetime.now(UTC)
    # Choosing code format based on type
    if code_type == CodeType.VERIFICATION:
        plaintext_code = generate_numeric_code(6)
    else:
        plaintext_code = generate_alphanumeric_code(8)
    hashed_code = hash_code(plaintext_code)

    if code_type == CodeType.VERIFICATION:
        user.verification_code = hashed_code
        user.verification_code_expires = now_utc + timedelta(minutes=s.VERIFICATION_CODE_TTL)
        user.last_verification_email_sent = now_utc

    elif code_type == CodeType.RESET_PASSWORD:
        user.reset_password_code = hashed_code
        user.reset_password_code_expires = now_utc + timedelta(minutes=s.RESET_PASSWORD_CODE_TTL)
        user.last_reset_password_email_sent = now_utc

    db.commit()
    db.refresh(user)

    if email and background_tasks:
        user_data = orm_to_dict(user)
        if code_type == CodeType.VERIFICATION:
            background_tasks.add_task(
                send_user_email,
                user_data,
                EmailTemplateType.USER_VERIFICATION,
                code=plaintext_code,
                expiry_minutes=s.VERIFICATION_CODE_TTL,
            )
        elif code_type == CodeType.RESET_PASSWORD:
            background_tasks.add_task(
                send_user_email,
                user_data,
                EmailTemplateType.PASSWORD_RESET,
                code=plaintext_code,
                expiry_minutes=s.RESET_PASSWORD_CODE_TTL,
            )

    return plaintext_code


def verify_code(user: User, code: str, code_type: CodeType) -> None:
    """Verifies a code against stored hash and checks expiration"""
    # Normalizing code for consistent verification (handling any client case):
    normalized_code = code.strip().upper()

    if code_type == CodeType.VERIFICATION:
        correct_hash = user.verification_code
        expires = user.verification_code_expires
    elif code_type == CodeType.RESET_PASSWORD:
        correct_hash = user.reset_password_code
        expires = user.reset_password_code_expires

    # Ensuring the code is correct and not expired:
    expired = (expires is None) or (expires <= datetime.now(UTC))
    if not correct_hash or expired or (not verify_code_hash(normalized_code, correct_hash)):
        # The three ways this fails are indistinguishable to the caller, and
        # deliberately so - telling someone "that code was right but it expired"
        # confirms a code they hold. They are not indistinguishable to US, and
        # "I typed it correctly and it still failed" is unanswerable without
        # this line: no code on file means it was already spent or superseded,
        # expired means the TTL ran out, and a mismatch means the digits are
        # genuinely wrong. The code itself is never logged, only which branch:
        reason = "no code on file" if not correct_hash else "expired" if expired else "mismatch"
        logger.warning("Code verification failed for user %s (%s): %s", user.id, code_type, reason)
        raise InvalidCodeException

    # If we get here, the code matches and is not expired, so clearing the fields to prevent reuse:
    if code_type == CodeType.VERIFICATION:
        user.is_verified = True
        user.verification_code = None
        user.verification_code_expires = None

    elif code_type == CodeType.RESET_PASSWORD:
        user.reset_password_code = None
        user.reset_password_code_expires = None


# ============================================================================
# USER VERIFICATION
# ============================================================================

def request_user_verification(db: Session, user: User, background_tasks: BackgroundTasks) -> EmailCooldownResponse:
    """Requests a new verification email with cooldown enforcement.

    An already-verified account is answered without sending anything. The
    endpoint's dependency is `unverified_user_dependency`, whose name reads as
    a guard and is not one: it means "verification is not required to CALL
    this", so a fully verified session reaches here as readily as a pending
    one. Without this check such an account can keep asking, and the code path
    happily mints a fresh verification code and emails it, over and over up to
    the cooldown ceiling.

    Nothing is compromised by that, since the code only ever reaches the
    account's own mailbox. What it does is send a stream of "verify your
    account" mail to somebody who verified months ago, which is confusing to
    receive, costs real send volume, and trains a recipient to treat our
    verification mail as noise, which is the one email we most need them to
    trust. Answering successfully rather than raising keeps the call
    idempotent for any caller that retries.
    """
    now_utc = datetime.now(UTC)

    if user.is_verified:
        return EmailCooldownResponse(cooldown_remaining_seconds=0)

    # The cooldown read is check-then-act, so it is taken UNDER A ROW LOCK.
    # Without it two requests that arrive together both see the pre-send
    # timestamp, both pass, and both mint a code - and since minting overwrites
    # `verification_code`, the reader ends up with two emails of which only the
    # later one works. They then type the code that arrived first and are told
    # it is invalid, which is indistinguishable from typing it wrong. A client
    # that fires the request twice (a re-render, a double tap, a retry) is
    # enough; the lock makes the second one wait and correctly see the cooldown.
    locked = db.query(User).filter(User.id == user.id).with_for_update().one()

    # Checking cooldown for sending new emails:
    if locked.last_verification_email_sent:
        cooldown_expires = locked.last_verification_email_sent + timedelta(minutes=s.EMAIL_VERIFICATION_COOLDOWN)

        # The user is on cooldown, raising exception with remaining time:
        if now_utc < cooldown_expires:
            remaining_seconds = int((cooldown_expires - now_utc).total_seconds())
            raise VerificationEmailRateLimitException(remaining_seconds)

    # If sending a new email, generating new code for security:
    # (otherwise we would have to store the plaintext code somewhere).
    # `locked` is the same instance as `user` (identity map), named for the lock
    # it holds - the commit inside is what releases it:
    generate_and_send_code(db, locked, CodeType.VERIFICATION, email=True, background_tasks=background_tasks)

    # Return response with NEW cooldown time (when next email can be sent):
    return EmailCooldownResponse(cooldown_remaining_seconds=s.EMAIL_VERIFICATION_COOLDOWN * 60)


def delete_user(db: Session, user: User) -> None:
    """Deletes a user account and associated data via cascade"""
    user_id = str(user.id)
    # The AI service's tables are a deliberate FK-less island (owner_id is a
    # plain string), so the users.id cascade never reaches them — purging them
    # explicitly in the same transaction keeps the privacy policy's erasure
    # promise (conversations, memories, stored extension credentials):
    from platform.services.generative_ai_service import purge_owner
    purge_owner(db, user_id, commit=False)
    db.delete(user)
    db.commit()
    # Uploaded files live outside the database at user-derived keys, so the
    # cascade never reaches them either. Best-effort AFTER the commit: a
    # storage failure must never block account erasure, but it is logged at
    # error so the orphaned blobs are visible and can be retried:
    try:
        get_object_storage().delete_prefix(f"avatars/{user_id}/")
    except Exception:
        logger.error("Failed to delete stored avatars for deleted user %s", user_id, exc_info=True)
    # The private plane namespaces every user file under users/{id}/ (see the
    # ObjectStorage docstring), so erasure stays generic — the platform needs no
    # knowledge of what product features stored there:
    try:
        get_object_storage().delete_private_prefix(f"users/{user_id}/")
    except Exception:
        logger.error("Failed to delete private files for deleted user %s", user_id, exc_info=True)


def verify_user(db: Session, user: User, code: str) -> User:
    """Verifies a user's email address with provided verification code"""
    verify_code(user, code, CodeType.VERIFICATION)
    db.commit()
    db.refresh(user)
    return user


# ============================================================================
# PASSWORD RESET
# ============================================================================

def request_password_reset(db: Session, email: str, background_tasks: BackgroundTasks) -> EmailCooldownResponse:
    """Initiates password reset with non-enumeration pattern to prevent email discovery attacks"""
    # Non-enumerating user lookup without raising, no-op if not eligible:
    user = db.query(User).filter_by(email=email.lower()).first()
    if not user:
        # Return policy cooldown seconds generically (non-enumerating)
        return EmailCooldownResponse(cooldown_remaining_seconds=s.RESET_PASSWORD_EMAIL_COOLDOWN * 60)

    now_utc = datetime.now(UTC)
    if user.last_reset_password_email_sent:
        cooldown_expires = user.last_reset_password_email_sent + timedelta(minutes=s.RESET_PASSWORD_EMAIL_COOLDOWN)
        if now_utc < cooldown_expires:
            remaining_seconds = int((cooldown_expires - now_utc).total_seconds())
            return EmailCooldownResponse(cooldown_remaining_seconds=remaining_seconds)

    generate_and_send_code(db, user, CodeType.RESET_PASSWORD, email=True, background_tasks=background_tasks)
    # Return full cooldown duration post-send
    return EmailCooldownResponse(cooldown_remaining_seconds=s.RESET_PASSWORD_EMAIL_COOLDOWN * 60)


def reset_password(db: Session, email: str, code: str, new_password: str) -> User:
    """Resets password with code verification and revokes all sessions for security (forces re-login on all devices)"""
    # Non-enumerating treating missing/invalid user as generic failure:
    user = db.query(User).filter_by(email=email.lower()).first()
    if not user:
        raise InvalidCodeException
    verify_code(user, code, CodeType.RESET_PASSWORD)

    # Setting new password:
    user.password = bcrypt_context.hash(new_password)
    # Clearing cooldown to allow immediate new reset cycle if desired:
    user.last_reset_password_email_sent = None

    # Revoking all active refresh sessions for this user in the same transaction:
    try:
        now_utc = datetime.now(UTC)
        sessions = db.query(RefreshSession).filter(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None)
        ).all()
        for session in sessions:
            session.revoked_at = now_utc

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user

# ============================================================================
# ADMIN USER LISTING
# ============================================================================

def list_user_emails(db: Session, flt: UserListFilter) -> EmailListResponse:
    # Creating base query selecting only emails for performance:
    query = db.query(User.email)
    if flt == UserListFilter.UNVERIFIED:
        query = query.filter(User.is_verified.is_(False))
    elif flt == UserListFilter.VERIFIED:
        query = query.filter(User.is_verified.is_(True))
    elif flt == UserListFilter.MARKETING_SUBSCRIBED:
        query = query.filter(User.marketing_consent.is_(True))

    # For ALL, no extra filter
    rows: List[Tuple[str]] = query.all()
    emails = [r[0] for r in rows]
    return EmailListResponse(count=len(emails), emails=emails)
