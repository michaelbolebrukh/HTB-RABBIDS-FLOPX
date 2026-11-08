"""The OAuth account-creation path must refuse the reserved service-account
address, closing the second creation path the registration guard alone missed."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from platform.exceptions import UserExistsException
from platform.services.auth_services.oauth_service import find_or_create_oauth_user
from infrastructure.service_auth import service_account_email


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("APP_DOMAIN", "example.com")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_oauth_refuses_reserved_service_email(db):
    """find_or_create_oauth_user rejects the reserved address (case-insensitive),
    so a verified passwordless row can never be minted at the service identity."""
    reserved = service_account_email()

    with pytest.raises(UserExistsException):
        find_or_create_oauth_user(db, reserved.upper(), {"name": "Squatter"}, "Google")

    # No row was created for the reserved address:
    from platform.models import User
    assert db.query(User).filter(User.email == reserved).first() is None


def test_oauth_allows_normal_email(db):
    """A normal address is unaffected by the reserved-email guard."""
    user = find_or_create_oauth_user(db, "person@example.com", {"name": "Person"}, "Google")
    assert user.email == "person@example.com"
    assert user.is_verified is True
