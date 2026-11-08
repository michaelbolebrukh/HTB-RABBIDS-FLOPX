from sqlalchemy.orm import Session

from platform.models import AccessibilityPreferences, User
from platform.schemas import AccessibilityPreferencesUpdateRequest


def get_preferences(db: Session, user: User) -> AccessibilityPreferences:
    """The user's stored preferences, or an unsaved default row (enabled=None
    → the client treats the master toggle as 'no explicit choice'). Defaults
    are set explicitly — server_default only applies at INSERT, and the
    response schema rejects None for the non-nullable fields."""
    row = (
        db.query(AccessibilityPreferences)
        .filter(AccessibilityPreferences.user_id == user.id)
        .first()
    )
    if row is not None:
        return row
    return AccessibilityPreferences(
        user_id=user.id,
        enabled=None,
        text_scale="md",
        high_contrast=False,
        enhanced_readability=False,
        reduce_effects="system",
    )


def upsert_preferences(
    db: Session, user: User, payload: AccessibilityPreferencesUpdateRequest
) -> AccessibilityPreferences:
    row = (
        db.query(AccessibilityPreferences)
        .filter(AccessibilityPreferences.user_id == user.id)
        .first()
    )
    if row is None:
        row = AccessibilityPreferences(user_id=user.id)
        db.add(row)

    row.enabled = payload.enabled
    row.text_scale = payload.text_scale
    row.high_contrast = payload.high_contrast
    row.enhanced_readability = payload.enhanced_readability
    row.reduce_effects = payload.reduce_effects

    db.commit()
    db.refresh(row)
    return row
