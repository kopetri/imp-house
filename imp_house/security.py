import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()
# Verified against when the email is unknown so login timing does not reveal accounts.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

DEVICE_TOKEN_PREFIX = "imp_"  # noqa: S105


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def create_session_token(user_id: int, secret: str, days: int) -> str:
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(days=days)}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_session_token(token: str, secret: str) -> int | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        return None


def new_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
