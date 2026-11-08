import os
from typing import List, Optional

from zxcvbn import zxcvbn
from pydantic_core import PydanticCustomError
from infrastructure.settings import settings as s


def validate_password_strength(password: str, user_inputs: Optional[List[str]] = None) -> None:
    """Validates password meets minimum length and strength requirements"""
    if len(password) < s.MIN_PASSWORD_LENGTH:
        raise PydanticCustomError(
            'password_too_short',
            f"Password must be at least {s.MIN_PASSWORD_LENGTH} characters long."
        )

    result = zxcvbn(password, user_inputs=user_inputs or [])
    if result["score"] < s.PASSWORD_STRENGTH_THRESHOLD:
        feedback = result.get("feedback", {})
        msg_parts = []
        if feedback.get("warning"):
            msg_parts.append(feedback["warning"])
        msg_parts.extend(feedback.get("suggestions", []))
        parts = ["Your password is too weak."] + [p for p in msg_parts if p]
        message = " ".join(parts)
        raise PydanticCustomError(
            'password_too_weak',
            message
        )


