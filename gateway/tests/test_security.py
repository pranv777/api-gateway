"""Unit tests that need no PostgreSQL or Redis."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import jwt
import pytest
from starlette.requests import Request

from app.core.rate_limit import get_client_ip
from app.core.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)

SECRET = "unit-test-secret-" + "y" * 32


def test_password_hash_is_argon2_and_verifies():
    hashed = hash_password("s3cret-passphrase")
    assert hashed.startswith("$argon2")
    assert "s3cret" not in hashed
    assert verify_password("s3cret-passphrase", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("anything", "not-a-valid-hash")


def test_token_round_trip():
    token = create_access_token("user-1", SECRET, expires_minutes=5)
    assert decode_access_token(token, SECRET) == "user-1"


def test_expired_token_is_rejected():
    past = datetime.now(UTC) - timedelta(hours=2)
    token = create_access_token("user-1", SECRET, expires_minutes=5, now=past)
    with pytest.raises(TokenExpiredError):
        decode_access_token(token, SECRET)


@pytest.mark.parametrize("bad", ["", "garbage", "a.b.c"])
def test_malformed_token_is_rejected(bad):
    with pytest.raises(TokenInvalidError):
        decode_access_token(bad, SECRET)


def test_token_signed_with_other_secret_is_rejected():
    token = create_access_token("user-1", "another-secret-" + "z" * 32, expires_minutes=5)
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, SECRET)


def test_unsigned_alg_none_token_is_rejected():
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode({"sub": "u", "iat": now, "exp": now + 300}, None, algorithm="none")
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, SECRET)


def test_token_without_expiry_is_rejected():
    token = jwt.encode({"sub": "u"}, SECRET, algorithm="HS256")
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, SECRET)


def test_api_keys_are_random_prefixed_and_stored_as_hash():
    key1, hash1 = generate_api_key()
    key2, hash2 = generate_api_key()
    assert key1 != key2 and hash1 != hash2
    assert key1.startswith("gw_") and len(key1) > 40
    assert hash1 == hash_api_key(key1) and len(hash1) == 64
    assert key1 not in hash1


def _request(peer: str | None, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    client = SimpleNamespace(host=peer) if peer else None
    return cast(Request, SimpleNamespace(client=client, headers=headers))


def test_client_ip_ignores_forwarded_header_by_default():
    assert get_client_ip(_request("10.0.0.5", "6.6.6.6")) == "10.0.0.5"


def test_client_ip_with_trusted_proxy_uses_entry_added_by_proxy():
    # One trusted proxy appended the real client; anything the client prepended is ignored.
    assert get_client_ip(_request("172.18.0.2", "6.6.6.6, 203.0.113.9"), 1) == "203.0.113.9"
    assert get_client_ip(_request("172.18.0.2", "203.0.113.9"), 1) == "203.0.113.9"
    # Header missing or shorter than the trusted chain: fall back to the peer address.
    assert get_client_ip(_request("172.18.0.2"), 1) == "172.18.0.2"
    assert get_client_ip(_request("172.18.0.2", "203.0.113.9"), 2) == "172.18.0.2"
    assert get_client_ip(_request(None)) == "unknown"
