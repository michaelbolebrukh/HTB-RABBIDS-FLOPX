import os
import sys


# Setting minimal env vars before infrastructure imports settings during test collection:
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("HASH_KEY", "test-hash-key")
os.environ.setdefault("HASH_ALGORITHM", "HS256")
os.environ.setdefault("APP_DOMAIN", "localhost")
os.environ.setdefault("SENDER_NAME", "Test")

# Ensuring the backend root is importable when pytest runs from the repo root:
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)
