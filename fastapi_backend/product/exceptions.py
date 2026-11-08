"""FLOPX exceptions. HTTP-facing ones subclass HTTPException so routers can let
them propagate; the domain-level ones carry a machine-readable `code` the
frontend maps to copy (the backend ships no user-facing prose)."""

from fastapi import HTTPException, status


class FlopxDomainError(Exception):
    """Base for domain refusals; `code` is a stable token, `details` is data."""

    code = "flopx.error"

    def __init__(self, code: str | None = None, **details):
        self.code = code or self.code
        self.details = details
        super().__init__(self.code)


class MixedProvenanceError(FlopxDomainError):
    """An aggregate was asked to combine sample rows with live/archived rows."""
    code = "flopx.mixed-provenance"


class InsufficientHistoryError(FlopxDomainError):
    """A forecast horizon was requested that the history cannot support."""
    code = "flopx.insufficient-history"


class UnknownSeriesError(FlopxDomainError):
    code = "flopx.unknown-series"


class UnknownEntityError(FlopxDomainError):
    code = "flopx.unknown-entity"


class SampleDataInvalidError(FlopxDomainError):
    """A sample dataset file failed validation at load time."""
    code = "flopx.sample-data-invalid"


class PaperOrderError(FlopxDomainError):
    """An order could not be placed or acted on; `reason` names why."""
    code = "flopx.paper-order"


class ConfirmationRequiredError(FlopxDomainError):
    """A destructive paper action (close-all) was sent without `confirm: true`."""
    code = "flopx.confirmation-required"


class SignInRequiredError(FlopxDomainError):
    """A read that exists only per account was made without a session. A 401, not a 404,
    so the client's transport refreshes the session and retries instead of reporting the
    record missing (falsifier V3 F13)."""
    code = "flopx.sign-in-required"


class DecisionConflictError(FlopxDomainError):
    """A decision another request claimed first (two accepts racing for one proposal)."""
    code = "flopx.paper-order"


# -- HTTP translations -----------------------------------------------------------

class FlopxNotFoundException(HTTPException):
    def __init__(self, code: str = "flopx.not-found", **details):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code, **details})


class FlopxUnprocessableException(HTTPException):
    def __init__(self, code: str, **details):
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": code, **details})


class FlopxUnauthorizedException(HTTPException):
    def __init__(self, code: str, **details):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": code, **details})


class FlopxConflictException(HTTPException):
    def __init__(self, code: str, **details):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail={"code": code, **details})


def to_http(exc: FlopxDomainError) -> HTTPException:
    """Maps a domain refusal onto the HTTP status its class deserves."""
    if isinstance(exc, (UnknownSeriesError, UnknownEntityError)):
        return FlopxNotFoundException(exc.code, **exc.details)
    if isinstance(exc, SignInRequiredError):
        return FlopxUnauthorizedException(exc.code, **exc.details)
    if isinstance(exc, (ConfirmationRequiredError, DecisionConflictError)):
        return FlopxConflictException(exc.code, **exc.details)
    return FlopxUnprocessableException(exc.code, **exc.details)
