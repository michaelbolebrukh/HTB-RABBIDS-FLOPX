import logging
from datetime import UTC
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from platform.schemas import ContactSubmissionRequest
from platform.enums import EmailTemplateType
from platform.exceptions import ContactFormUnavailableException, ContactSubmissionNotFoundException
from platform.models import ContactSubmission, User
from platform.services.email_services import send_templated_email
from utils import orm_to_dict
from infrastructure.settings import settings as s


# Setting up logger:
logger = logging.getLogger(__name__)


# ============================================================================
# EMAIL NOTIFICATIONS
# ============================================================================

def send_contact_admin_notification(contact_data: Dict[str, Any]):
    """Sends admin notification email for new contact form submission"""
    try:
        # Using the DB timestamp (already timezone-aware) to avoid mismatch between label and value:
        created_at = contact_data.get("created_at")
        formatted_timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if created_at else None

        send_templated_email(
            template_type=EmailTemplateType.CONTACT_ADMIN_NOTIFICATION,
            recipient=s.CONTACT_EMAIL,
            # A user-supplied subject becomes the email subject; otherwise the
            # template's generic "New contact message from ..." stays:
            subject_override=contact_data.get("subject"),
            reply_to=contact_data.get("email"),
            name=contact_data.get("name"),
            email=contact_data.get("email"),
            company=contact_data.get("company") or "Not provided",
            message=contact_data.get("message"),
            contact_id=contact_data.get("id"),
            # Signed-in account identity, independent of the (editable) form fields:
            user_account=contact_data.get("user_account") or "Not signed in",
            timestamp=formatted_timestamp
        )
    except Exception as e:
        logger.error(f"Error sending contact admin notification: {e}")
        raise ContactFormUnavailableException


# ============================================================================
# CONTACT SUBMISSION CRUD
# ============================================================================

def create_contact_submission(db: Session, submission_data: ContactSubmissionRequest) -> ContactSubmission:
    contact_submission = ContactSubmission(**submission_data.model_dump())

    db.add(contact_submission)
    db.commit()
    db.refresh(contact_submission)

    return contact_submission


def get_contact_submissions(db: Session, resolved: Optional[bool] = None) -> List[ContactSubmission]:
    """Retrieves contact submissions with optional resolved status filter"""
    query = db.query(ContactSubmission)

    if resolved is not None:
        query = query.filter(ContactSubmission.resolved == resolved)

    # Ordering newest first:
    return query.order_by(ContactSubmission.created_at.desc()).all()


def get_contact_submission(db: Session, contact_id: int) -> ContactSubmission:
    contact_submission = db.query(ContactSubmission).filter(ContactSubmission.id == contact_id).first()

    if not contact_submission:
        raise ContactSubmissionNotFoundException

    return contact_submission


def resolve_contact_submission(db: Session, contact_id: int) -> ContactSubmission:
    contact_submission = get_contact_submission(db, contact_id)
    contact_submission.resolved = True
    db.commit()
    db.refresh(contact_submission)

    return contact_submission


def delete_contact_submission(db: Session, contact_id: int) -> None:
    contact_submission = get_contact_submission(db, contact_id)
    db.delete(contact_submission)
    db.commit()


def submit_contact_form(
    db: Session,
    contact_data: ContactSubmissionRequest,
    background_tasks: Optional[BackgroundTasks] = None,
    user: Optional[User] = None,
) -> ContactSubmission:
    """Processes contact form submission and sends admin notification"""

    # Creating new contact submission:
    contact_submission = ContactSubmission(**contact_data.model_dump())

    db.add(contact_submission)
    db.commit()
    db.refresh(contact_submission)

    contact_dict = orm_to_dict(contact_submission)

    # Attaching the signed-in account identity (token-derived, not client-supplied)
    # so the admin notification identifies the user even when fields were edited:
    if user is not None:
        contact_dict["user_account"] = f"User #{user.id} ({user.email})"

    # 'Not none' check is needed, could be provided but empty:
    if background_tasks is not None:
        # If background tasks provided, queuing email and returning immediately:
        background_tasks.add_task(send_contact_admin_notification, contact_dict)
    else:
        # If no background tasks provided, sending synchronously and waiting for completion:
        send_contact_admin_notification(contact_dict)

    return contact_submission
