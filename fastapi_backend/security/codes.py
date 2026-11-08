import secrets
from typing import Optional

from .hashing import bcrypt_context


_DEFAULT_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O, I, L, 0, 1


def generate_numeric_code(length: int) -> str:
    """Generates a random numeric code of specified length"""
    return str(secrets.randbelow(10 ** length)).zfill(length)


def generate_alphanumeric_code(length: int, alphabet: str = _DEFAULT_ALPHA) -> str:
    """Generates a random alphanumeric code using unambiguous characters"""
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_code(plaintext: str) -> str:
    """Hashes a verification code using bcrypt"""
    return bcrypt_context.hash(plaintext)


def verify_code(plaintext: str, hashed: Optional[str]) -> bool:
    """Verifies a plaintext code against its bcrypt hash"""
    if not plaintext or not hashed:
        return False
    return bcrypt_context.verify(plaintext, hashed)


