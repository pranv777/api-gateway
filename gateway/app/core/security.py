"""Password hashing, JWT handling and API key helpers."""

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

JWT_ALGORITHM = "HS256"
API_KEY_PREFIX = "gw_"

_hasher = PasswordHasher()
# Verified against when a login names an unknown email, so both paths cost about the same.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def hash_password_async(password: str) -> str:
    # Argon2 is deliberately slow (CPU-bound); keep it off the event loop.
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    """Verify a password. With no hash (unknown user) a dummy hash is checked and False returned."""
    if password_hash is None:
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        return False
    return await asyncio.to_thread(verify_password, password, password_hash)


# --------------------------------------------------------------------- JWT
class TokenExpiredError(Exception):
    pass


class TokenInvalidError(Exception):
    pass


def create_access_token(
    user_id: str, secret: str, expires_minutes: int, now: datetime | None = None
) -> str:
    now = now or datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> str:
    """Return the user id (`sub`) of a valid token; raise TokenExpiredError or TokenInvalidError."""
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],  # pinned: rejects alg=none and algorithm confusion
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError from exc
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise TokenInvalidError
    return sub


# ---------------------------------------------------------------- API keys
def hash_api_key(api_key: str) -> str:
    """SHA-256 hex digest. A fast hash is appropriate here (unlike for passwords) because the
    key is 256 bits of randomness, so there is nothing to brute-force, and it is checked on
    every gateway request."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext_key, key_hash). The plaintext must be shown once and never stored."""
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext)
