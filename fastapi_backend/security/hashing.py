from passlib.context import CryptContext

# Setting up bcrypt for hashing:
bcrypt_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
