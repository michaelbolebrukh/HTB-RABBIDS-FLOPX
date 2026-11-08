from fastapi import HTTPException
from fastapi import status as st


# AUTHENTICATION & USER EXCEPTIONS:
class InvalidCredentialsException(HTTPException):
    def __init__(self, detail="Please check your email and password and try again."):
        super().__init__(status_code=st.HTTP_401_UNAUTHORIZED, detail=detail)


class PasswordLoginUnavailableException(HTTPException):
    def __init__(self, detail="This account doesn’t have a password set. Use OAuth or set a password first."):
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=detail)


class UserNotFoundException(HTTPException):
    def __init__(self, detail="We couldn't find an account with this email. Please sign up instead."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class UserExistsException(HTTPException):
    def __init__(self, detail="An account with this email already exists. Please log in instead."):
        super().__init__(status_code=st.HTTP_409_CONFLICT, detail=detail)


class UnverifiedUserException(HTTPException):
    def __init__(self, detail="This account is not verified. Please verify your account to continue.", cooldown_seconds: int = 0):
        # Returning structured detail to surface cooldown_seconds to clients:
        structured_detail = {"message": detail, "cooldown_seconds": cooldown_seconds}
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=structured_detail)

        # For internal use only:
        self.cooldown_seconds = cooldown_seconds


class InvalidCodeException(HTTPException):
    def __init__(self, detail="This verification code is incorrect. Please try again or request a new one."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class OAuthProviderNotConfiguredException(HTTPException):
    def __init__(self, provider: str):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=f"{provider.title()} sign-in is not available. Please use email or try another sign-in method.")


class JWTException(HTTPException):
    def __init__(self, detail="Your session has ended. Please sign in again."):
        super().__init__(status_code=st.HTTP_401_UNAUTHORIZED, detail=detail)


class RefreshTokenMissingException(HTTPException):
    # Separate from above exception for clarity, identical client-facing detail:
    def __init__(self, detail="Your session has ended. Please sign in again."):
        super().__init__(status_code=st.HTTP_401_UNAUTHORIZED, detail=detail)


class CSRFException(HTTPException):
    def __init__(self, detail="For your security, we couldn’t verify this action. Please refresh the page and try again."):
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=detail)


class RefreshTokenInvalidException(HTTPException):
    def __init__(self, detail="Your session is no longer valid. Please sign in again."):
        super().__init__(status_code=st.HTTP_401_UNAUTHORIZED, detail=detail)


class RefreshTokenReuseException(HTTPException):
    def __init__(self, detail="We detected unusual activity and signed you out to keep your account safe. Please sign in again."):
        super().__init__(status_code=st.HTTP_401_UNAUTHORIZED, detail=detail)


# AUTHORIZATION EXCEPTIONS:
# The single authorization denial: every gate is a capability check (see
# platform/capabilities.py), so a missing grant is the one 403 shape. Clients
# key on the status code, never the detail string:
class CapabilityException(HTTPException):
    def __init__(self, detail="You are not authorized to access this resource. Please contact us if you believe this is an error."):
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=detail)


# The typo guard, raised when a capability string outside the registry
# (platform/capabilities.py) is about to be written to the database — an
# unrecognised grant would silently gate nothing, so writing one is refused:
class UnknownCapabilityException(HTTPException):
    def __init__(self, capability: str):
        super().__init__(
            status_code=st.HTTP_400_BAD_REQUEST,
            detail=f"Unknown capability '{capability}'. Valid grants are defined in platform/capabilities.py.",
        )
        # For internal use only:
        self.capability = capability


# DOCUMENT UPLOAD EXCEPTIONS:
class DocumentTooLargeException(HTTPException):
    """413 rather than a generic 400: the limit is part of the contract, so the
    refusal says which one it is. Raised before the body is fully buffered (see
    the talent router's _read_capped) so an oversized upload costs the process
    almost nothing.

    The default detail names no file type and no number: this is platform code,
    and the caller knows both. The talent router passes a PDF-specific message
    whose size is DERIVED from the setting that enforces it, so the two cannot
    drift apart."""

    def __init__(self, detail="That file is too large."):
        super().__init__(status_code=st.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=detail)


class StaleConsentTermsException(HTTPException):
    """409: the client affirmed a consent statement built against a wording
    version the server no longer stamps. The grant is refused rather than
    recorded under the current version, because the words on that client's
    screen were not the current words. The fix on the client is a reload."""

    def __init__(self, detail="The terms shown were out of date. Refresh the page and try again."):
        super().__init__(status_code=st.HTTP_409_CONFLICT, detail=detail)


# ORGANISATION & PARTNERSHIP EXCEPTIONS:
class OrganisationNotFoundException(HTTPException):
    def __init__(self, detail="We couldn't find that organisation."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class PartnershipNotFoundException(HTTPException):
    def __init__(self, detail="We couldn't find that partnership."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


# The typo guard for partnership state, the sibling of UnknownCapabilityException:
# a status outside platform/partnerships.PARTNERSHIP_STATUSES would be stored
# happily and read as an unknown state forever, so the write is refused:
class UnknownPartnershipStatusException(HTTPException):
    def __init__(self, status: str, valid: tuple[str, ...] = ()):
        super().__init__(
            status_code=st.HTTP_400_BAD_REQUEST,
            detail=f"Unknown partnership status '{status}'. Valid statuses are {list(valid)}.",
        )
        # For internal use only:
        self.status = status


# INBOX EXCEPTIONS:
class DurableStorageUnavailableException(HTTPException):
    """Raised when a blob the product undertakes to KEEP is uploaded to a
    deployment with no durable storage configured. Refusing is deliberate: the
    alternative is accepting the file onto a filesystem the next deploy wipes,
    which leaves the database row behind and makes the loss invisible."""

    def __init__(self, detail="Uploads are temporarily unavailable. Please try again later."):
        super().__init__(status_code=st.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


class InboxNotConfiguredException(HTTPException):
    def __init__(self, detail="The inbox is not connected yet."):
        super().__init__(status_code=st.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


class InboxUnavailableException(HTTPException):
    def __init__(self, detail="The inbox could not be reached. Please try again."):
        super().__init__(status_code=st.HTTP_502_BAD_GATEWAY, detail=detail)


class InboxSendNotConfiguredException(HTTPException):
    def __init__(self, detail="Sending is not configured for this inbox yet."):
        super().__init__(status_code=st.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


class InboxSendException(HTTPException):
    def __init__(self, detail="The message could not be sent. Please try again."):
        super().__init__(status_code=st.HTTP_502_BAD_GATEWAY, detail=detail)


class InboxMessageNotFoundException(HTTPException):
    def __init__(self, detail="That message no longer exists."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class InboxAttachmentNotFoundException(HTTPException):
    def __init__(self, detail="That attachment no longer exists."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


# CONTACT FORM EXCEPTIONS:
class ContactFormUnavailableException(HTTPException):
    def __init__(self, detail="There was an error processing your message. Please try again or contact support directly."):
        super().__init__(status_code=st.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


class ContactSubmissionNotFoundException(HTTPException):
    def __init__(self, detail="We couldn't find that contact submission. Please check the ID or submit a new message."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


# EMAIL SERVICE EXCEPTIONS:
class EmailSendException(HTTPException):
    def __init__(self, detail="We were unable to send an email at this time. Please try again or contact support directly."):
        super().__init__(status_code=st.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


class VerificationEmailRateLimitException(HTTPException):
    def __init__(self, remaining_seconds: int):
        minutes = remaining_seconds // 60
        seconds = remaining_seconds % 60

        detail = f"Please wait {f'{minutes} minutes' if minutes > 0 else f'{seconds} seconds'} before requesting another verification email."
            
        # Returning structured detail and including Retry-After header for clients:
        structured_detail = {"message": detail, "cooldown_seconds": remaining_seconds}
        super().__init__(
            status_code=st.HTTP_429_TOO_MANY_REQUESTS,
            detail=structured_detail,
            headers={"Retry-After": str(remaining_seconds)}
        )
        self.cooldown_seconds = remaining_seconds


# EXTERNAL API EXCEPTIONS:
class APIRequestException(HTTPException):
    def __init__(self, detail="The service is temporarily unavailable. Please try again in a few moments."):
        super().__init__(status_code=st.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


# PAYMENTS EXCEPTIONS:
class PaymentsNotConfiguredException(HTTPException):
    def __init__(self, detail="Payments are not available right now. Please try again later or contact support."):
        super().__init__(status_code=st.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


class CatalogItemNotFoundException(HTTPException):
    def __init__(self, detail="This item is not available."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class PurchaseNotAllowedException(HTTPException):
    def __init__(self, detail="This item can't be purchased right now."):
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=detail)


class PurchaseLimitReachedException(HTTPException):
    def __init__(self, detail="You've reached the purchase limit for this item."):
        super().__init__(status_code=st.HTTP_409_CONFLICT, detail=detail)


class EntitlementRequiredException(HTTPException):
    def __init__(self, detail="Your current plan doesn't include this feature. Please upgrade to continue."):
        super().__init__(status_code=st.HTTP_403_FORBIDDEN, detail=detail)


class InvalidWebhookException(HTTPException):
    def __init__(self, detail="Invalid webhook payload or signature."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class InvalidUnsubscribeTokenException(HTTPException):
    def __init__(self, detail="This unsubscribe link is invalid or no longer usable."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class InvalidAvatarException(HTTPException):
    def __init__(self, detail="Invalid avatar image"):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class InvalidDocumentException(HTTPException):
    def __init__(self, detail="Invalid document upload"):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class DocumentDisarmFailedException(HTTPException):
    """Raised when fail-closed PDF rebuild cannot produce a safe share artefact.

    Distinct from InvalidDocumentException (bad input shape) so clients can tell
    "wrong file type" from "this PDF could not be safely processed"."""

    def __init__(
        self,
        detail="This PDF could not be processed safely. Please export a new PDF and try again.",
    ):
        super().__init__(status_code=st.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


class BillingProfileNotFoundException(HTTPException):
    def __init__(self, detail="No billing profile found for this account yet. Complete a purchase first."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class NameNotAppropriateException(HTTPException):
    def __init__(self, detail="That display name isn't allowed. Please choose a different name."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


# QUESTION FLOW EXCEPTIONS:
# One 404 shape for "no such flow", used both for a genuinely unknown key and for
# a flow whose optional lead module is dormant — a dormant surface must be
# indistinguishable from an absent one, or the 404/403 split would advertise
# which flows exist but are switched off:
class FlowNotFoundException(HTTPException):
    def __init__(self, detail="We couldn't find that questionnaire."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


# There is deliberately NO version-conflict exception. A flow submission used to
# 409 when the client's version did not match the registry, which refused the
# whole body — including answers that were still perfectly valid — the moment a
# question was added behind an open tab. Completion is now computed per question
# (platform/services/question_flow_service/questions.py), so a stale client is
# caught by the answers it actually sent: an id the flow no longer asks, or an
# answer whose option was retired, is one entry in the 422 below naming that
# question. Do not reintroduce a whole-submission version gate.


class FlowAnswersInvalidException(HTTPException):
    """The submitted answers don't satisfy the flow's schema (or exceed its size cap).

    `errors` carries field LOCATIONS and error TYPES only, never the submitted
    values: answers are PII and must not be echoed into logs, error trackers, or
    a response body a proxy might record.
    """
    def __init__(self, errors: list[dict] | None = None,
                 detail="Some of those answers weren't valid. Please try again."):
        structured_detail = {"message": detail, "errors": errors or []}
        super().__init__(status_code=st.HTTP_422_UNPROCESSABLE_ENTITY, detail=structured_detail)
        # For internal use only:
        self.errors = errors or []


class FlowResponseNotFoundException(HTTPException):
    def __init__(self, detail="You haven't completed that questionnaire yet."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


# TODO TEMP (QA harness) — remove with POST /flows/{flow_key}/test/reset. Raised
# ONLY for the missing confirmation, never for "no such account": the reset
# answers identically for every address, so this 400 can only ever be produced by
# the caller's own body and leaks nothing about who exists:
class FlowResetNotConfirmedException(HTTPException):
    def __init__(self, detail="Resetting another account's answers requires confirm=true."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


# 404 rather than 400/401 deliberately: a resume link points at one stored lead,
# and an expired, purged, converted or forged token must all look the same, so a
# probe learns nothing about which addresses have answered:
class InvalidResumeTokenException(HTTPException):
    def __init__(self, detail="This link is invalid or has expired."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class NameChangeRateLimitException(HTTPException):
    def __init__(self, remaining_seconds: int, self_serve_disabled: bool = False):
        if self_serve_disabled:
            # Interval 0: renames are support-only on this instance.
            detail = "Name changes are handled by our support team. Please contact support to change your name."
            reason = "name_change_disabled"
        else:
            days = remaining_seconds // 86400
            hours = (remaining_seconds % 86400) // 3600
            if days > 0:
                wait = f"{days} day{'s' if days != 1 else ''}"
            elif hours > 0:
                wait = f"{hours} hour{'s' if hours != 1 else ''}"
            else:
                wait = "a little while"
            detail = f"You can only change your name once per period. Please wait {wait}, or contact support to change it sooner."
            reason = "name_change_rate_limit"
        # Structured detail + Retry-After so the client can offer the support flow:
        structured_detail = {"message": detail, "cooldown_seconds": remaining_seconds, "reason": reason}
        super().__init__(
            status_code=st.HTTP_429_TOO_MANY_REQUESTS,
            detail=structured_detail,
            headers={"Retry-After": str(remaining_seconds)},
        )


class ManagedLinkNotFoundException(HTTPException):
    def __init__(self, detail="That link was not found."):
        super().__init__(status_code=st.HTTP_404_NOT_FOUND, detail=detail)


class ManagedLinkExistsException(HTTPException):
    def __init__(self, detail="A link with that key already exists. Choose a different key."):
        super().__init__(status_code=st.HTTP_409_CONFLICT, detail=detail)


class InvalidManagedLinkKeyException(HTTPException):
    def __init__(self, detail="That link key is not allowed. Use letters, numbers, and hyphens only."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)


class InvalidManagedLinkDestinationException(HTTPException):
    def __init__(self, detail="That destination is not allowed. Use a site path like /events/welcome or an https:// URL."):
        super().__init__(status_code=st.HTTP_400_BAD_REQUEST, detail=detail)
