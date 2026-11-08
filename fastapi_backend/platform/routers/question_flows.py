from fastapi import APIRouter, Response, status as st
from starlette.requests import Request

from infrastructure.dependencies import (
    db_dependency,
    require_capability,
    unverified_user_dependency,
)
from infrastructure.rate_limiter import limiter
from platform.capabilities import FLOWS_QA_RESET
from platform.exceptions import (
    FlowNotFoundException,
    FlowResetNotConfirmedException,
    FlowResponseNotFoundException,
)
from platform.question_flows import get_flow
from platform.schemas import (
    FlowDefinitionResponse,
    FlowLeadRequest,
    FlowLeadResumeRequest,
    FlowLeadResumeResponse,
    FlowResetRequest,
    FlowResetResponse,
    FlowResponseSnapshot,
    FlowResponseSubmitRequest,
)
from platform.services import question_flow_service as qfs


router = APIRouter(prefix="/flows", tags=["Question Flows"])


@router.get("/{flow_key}", response_model=FlowDefinitionResponse, status_code=st.HTTP_200_OK)
@limiter.limit("30/minute, 200/hour")
def read_flow_definition(flow_key: str, request: Request, response: Response):
    """The registered contract for a flow. Public and DB-free — a registry read.

    Public because a client may need the questions and the current version before
    it has an account (the pre-auth answer ordering); it exposes what the flow
    ASKS, never anyone's answers. Which questions a particular account still owes
    rides on GET /user/ (`flow_questions`), where it is authenticated.
    """
    flow = get_flow(flow_key)
    if flow is None:
        raise FlowNotFoundException()
    return FlowDefinitionResponse(
        flow_key=flow.key,
        version=flow.version,
        title=flow.title,
        lead_capture=flow.lead_capture,
        questions=list(flow.question_ids),
        required_questions=list(flow.required_question_ids),
    )


@router.post("/{flow_key}/responses", response_model=FlowResponseSnapshot, status_code=st.HTTP_201_CREATED)
@limiter.limit("10/minute, 60/hour")
def submit_flow_response(
    db: db_dependency,
    user: unverified_user_dependency,
    flow_key: str,
    body: FlowResponseSubmitRequest,
    request: Request,
    response: Response,
):
    """Appends one snapshot for the signed-in account, from a PARTIAL answer set.

    `answers` may hold any subset of the flow's questions — the whole set on a
    first run, or the single newly added question for a member who answered the
    rest long ago. The service merges the submission over what the account
    already answered, so nothing is lost by answering one thing.

    UNVERIFIED accounts are allowed on purpose: answers captured before signup
    are flushed the moment the account exists, which is before the email code has
    been entered. Requiring verification here would strand exactly the payload
    this endpoint exists to persist.
    """
    return qfs.submit_response(db, user, flow_key, body.version, body.answers)


@router.get("/{flow_key}/responses/latest", response_model=FlowResponseSnapshot, status_code=st.HTTP_200_OK)
@limiter.limit("60/minute")
def read_latest_flow_response(
    db: db_dependency,
    user: unverified_user_dependency,
    flow_key: str,
    request: Request,
    response: Response,
):
    """The account's most recent snapshot for this flow (its full answers)."""
    snapshot = qfs.latest_response(db, user.id, flow_key)
    if snapshot is None:
        raise FlowResponseNotFoundException()
    return snapshot


# -- TEMP QA harness -----------------------------------------------------------

# TODO TEMP (QA harness) - remove before launch, with reset_flow_state
# (platform/services/question_flow_service/qa_reset.py), the FlowReset* schemas
# and the flows:qa-reset capability.
#
# THIS ROUTE NOW HAS A PRODUCT CONSUMER, so "remove before launch" is no longer
# a self-contained deletion: the admin overlay's "Reset onboarding answers"
# button posts here (next-pwa/product/api/admin.ts -> resetOnboardingFlow).
# Removing the route without removing that button leaves it throwing a 404
# behind an error toast. Delete both together, or keep this route and drop the
# TEMP marker - what must not happen is the button outliving the route silently.
#
# Gated flows:qa-reset and GRANTED TO NOBODY,
# so admins are the only callers: this is the one path that deletes append-only
# snapshots, and self-service "un-answer me" is not a product feature - a person
# who wants their answers gone deletes their account. It exists so a flow can be
# re-run end to end with the same account while it is being built:
flows_qa_reset_dependency = require_capability(FLOWS_QA_RESET)


@router.post("/{flow_key}/test/reset", response_model=FlowResetResponse, status_code=st.HTTP_200_OK)
@limiter.limit("10/minute")
def reset_flow_responses(
    db: db_dependency,
    user: flows_qa_reset_dependency,
    flow_key: str,
    body: FlowResetRequest,
    request: Request,
    response: Response,
):
    """Clears one account's state for one flow so the flow can be taken again.

    Defaults to the CALLER's own account; `email` targets another one and then
    requires `confirm: true`. An unknown flow key still 404s (the same shape
    every other route on this router uses), but an unknown ADDRESS does not:
    every address answers 200 with counts, so a holder of this grant who does
    not also hold users:read gains no way to test which accounts exist. The
    trade-off is accepted knowingly - a typo'd address reports zeros rather than
    an error.
    """
    if get_flow(flow_key) is None:
        raise FlowNotFoundException()

    if body.email is not None:
        # The confirm gate is checked BEFORE the lookup, so a missing
        # confirmation is refused identically whether or not the address exists:
        if not body.confirm:
            raise FlowResetNotConfirmedException()
        target = qfs.find_user_by_email(db, str(body.email))
    else:
        target = user

    counts = qfs.reset_flow_state(db, target, flow_key)
    return FlowResetResponse(flow_key=flow_key, **counts)


# -- Dormant lead module (404 unless the flow opts in) -------------------------

@router.post("/{flow_key}/leads", status_code=st.HTTP_202_ACCEPTED)
@limiter.limit("5/minute, 20/hour, 50/day")
def capture_flow_lead(
    db: db_dependency,
    flow_key: str,
    body: FlowLeadRequest,
    request: Request,
    response: Response,
):
    """Stores a pre-account, partial answer set keyed by email address.

    ALWAYS 202 for a live flow, whatever the address: a public endpoint that
    accepts an email must not report whether that email already has an account
    or a lead, or it becomes an enumeration oracle.

    ⚠️ READ BEFORE SETTING `lead_capture=True` ON ANY FLOW. This is an
    UNAUTHENTICATED, LAST-WRITE-WINS write keyed on an email address the CALLER
    supplies and nothing has confirmed they control. Two consequences follow and
    neither is solved here:
      * ANSWER POISONING — anyone can overwrite the stored partial answers for
        somebody else's address, so a resumed flow may show answers its owner
        never gave. Treat lead answers as untrusted input, never as a fact about
        the person, and never let them drive anything privileged.
      * CONFIRMATION OF ADDRESS — a stored row (and any resume mail built from
        it) is an unverified third-party-submitted address, which is how an
        endpoint like this becomes a way to send mail to strangers.
    Enabling capture is a product decision that has to answer both first — a
    per-address confirmation step, or accepting the exposure knowingly.
    """
    qfs.capture_lead(db, flow_key, str(body.email), body.version, body.answers)
    return Response(status_code=st.HTTP_202_ACCEPTED)


@router.post("/{flow_key}/leads/resume", response_model=FlowLeadResumeResponse, status_code=st.HTTP_200_OK)
@limiter.limit("10/minute, 60/hour")
def resume_flow_lead(
    db: db_dependency,
    flow_key: str,
    body: FlowLeadResumeRequest,
    request: Request,
    response: Response,
):
    """Resolves a resume token back to the stored partial answers (404 on anything else)."""
    return qfs.resume_lead(db, flow_key, body.token)
