"""Mail-client connector flow: audited credential grants + Apple profile.

The OAuth-shaped onboarding for standard mail clients: an inbox:credentials holder
asks for the mailbox connection material, we verify, AUDIT-LOG, record the
grant in mailbox_connections, and hand it over - either as the raw settings
(revealed in place, never sent anywhere) or as a one-tap Apple configuration
profile with the account embedded.

Security model: the credential grants nothing beyond what the caller's grants
already allow (full mailbox read/send through the admin UI), so revealing it
to that caller changes where they can use their access, not how much they
have. Every grab is on the record. Revocation = rotating the mailbox password
(one shared credential; all devices disconnect at once).
"""
import logging
import plistlib
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from infrastructure.settings import settings as s
from platform.exceptions import InboxNotConfiguredException
from platform.models import MailboxConnection, User


logger = logging.getLogger(__name__)

# The Forward Email endpoints every factory mailbox lives on (implicit TLS):
IMAP_HOST = "imap.forwardemail.net"
IMAP_PORT = 993
SMTP_HOST = "smtp.forwardemail.net"
SMTP_PORT = 465

CONNECTORS = {"apple", "outlook", "android", "other"}
_INTERACTIONS_PER_USER = 20


def _require_credential() -> tuple[str, str]:
    if not (s.INBOX_IMAP_EMAIL and s.INBOX_IMAP_PASSWORD):
        raise InboxNotConfiguredException
    return s.INBOX_IMAP_EMAIL, s.INBOX_IMAP_PASSWORD


def record_connection(db: Session, user: User, connector: str) -> None:
    """Writes the audit row and log line for one credential grant"""
    connection = MailboxConnection(user_id=user.id, connector=connector)
    db.add(connection)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    # Who took the credential and how - never the credential itself:
    logger.info("Inbox credential granted (user %s, connector %s).", user.id, connector)


def list_connection_groups(db: Session) -> list[dict]:
    """One aggregated row per admin, newest activity first.

    Counts and latest-activity come from a SQL group-by over the whole table,
    so a high-volume admin can never page another admin out of the log; only
    the per-user interaction detail is capped (most recent few grants).
    """
    totals = (
        db.query(
            MailboxConnection.user_id,
            func.count(MailboxConnection.id).label("count"),
            func.max(MailboxConnection.created_at).label("latest_at"),
        )
        .group_by(MailboxConnection.user_id)
        .all()
    )

    groups = []
    for user_id, count, latest_at in totals:
        recent = (
            db.query(MailboxConnection)
            .options(joinedload(MailboxConnection.user))
            .filter(MailboxConnection.user_id == user_id)
            .order_by(MailboxConnection.created_at.desc(), MailboxConnection.id.desc())
            .limit(_INTERACTIONS_PER_USER)
            .all()
        )
        user = recent[0].user if recent else None
        groups.append({
            "user_email": user.email if user else "",
            "user_name": user.name if user else "",
            "count": count,
            "latest_at": latest_at,
            "interactions": [
                {"id": row.id, "connector": row.connector, "created_at": row.created_at}
                for row in recent
            ],
        })

    groups.sort(key=lambda group: group["latest_at"], reverse=True)
    return groups


def reveal_settings(db: Session, user: User, connector: str) -> dict:
    """Returns the full connection material and records the grant"""
    email, password = _require_credential()
    record_connection(db, user, connector if connector in CONNECTORS else "other")
    return {
        "email": email,
        "password": password,
        "imap_host": IMAP_HOST,
        "imap_port": IMAP_PORT,
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
    }


def build_apple_profile(db: Session, user: User) -> bytes:
    """One-tap Apple .mobileconfig with the mailbox account embedded.

    Generated on demand inside the gated session and recorded like any other
    grant; iOS/macOS install the account directly from it.
    """
    email, password = _require_credential()
    record_connection(db, user, "apple")

    account_name = s.SENDER_NAME or email
    domain = email.split("@")[-1]
    # Title case: this is the profile/account NAME iOS shows in Settings and in
    # Mail's account list, not prose. The product half comes from SENDER_NAME
    # (derived from APP_DOMAIN), so it stays correct in every clone.
    payload_org = f"{account_name} Mailbox"

    mail_payload = {
        "PayloadType": "com.apple.mail.managed",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"com.{domain}.mailbox.account",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": payload_org,
        "EmailAccountDescription": payload_org,
        "EmailAccountName": account_name,
        "EmailAccountType": "EmailTypeIMAP",
        "EmailAddress": email,
        "IncomingMailServerAuthentication": "EmailAuthPassword",
        "IncomingMailServerHostName": IMAP_HOST,
        "IncomingMailServerPortNumber": IMAP_PORT,
        "IncomingMailServerUseSSL": True,
        "IncomingMailServerUsername": email,
        "IncomingPassword": password,
        "OutgoingMailServerAuthentication": "EmailAuthPassword",
        "OutgoingMailServerHostName": SMTP_HOST,
        "OutgoingMailServerPortNumber": SMTP_PORT,
        "OutgoingMailServerUseSSL": True,
        "OutgoingMailServerUsername": email,
        "OutgoingPasswordSameAsIncomingPassword": True,
        "PreventAppSheet": False,
        "PreventMove": False,
        "SMIMEEnabled": False,
    }
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"com.{domain}.mailbox.profile",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": payload_org,
        "PayloadDescription": f"Adds the {email} mailbox to Mail.",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [mail_payload],
    }
    return plistlib.dumps(profile)
