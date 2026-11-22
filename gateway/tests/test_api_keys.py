from sqlalchemy import select

from app.core.security import hash_api_key
from app.db.models import ApiKey
from tests.helpers import create_key, key_header, signup


async def test_create_key_returns_plaintext_once(client):
    user = await signup(client)
    created = await create_key(client, user, "ci")
    assert created["key"].startswith("gw_")
    assert created["name"] == "ci" and created["is_active"] is True

    listing = (await client.get("/api-keys", headers=user["headers"])).json()
    assert [k["id"] for k in listing] == [created["id"]]
    assert "key" not in listing[0]
    assert "key_hash" not in listing[0]
    assert created["key"] not in str((await client.get("/api-keys", headers=user["headers"])).text)


async def test_only_the_hash_is_stored(client, infra):
    user = await signup(client)
    created = await create_key(client, user)
    async with infra.sessionmaker() as session:
        row = (await session.execute(select(ApiKey))).scalar_one()
    assert row.key_hash == hash_api_key(created["key"])
    assert row.key_hash != created["key"]
    assert created["key"] not in (row.key_hash, row.name)


async def test_key_management_requires_login(client):
    assert (await client.post("/api-keys", json={"name": "x"})).status_code == 401
    assert (await client.get("/api-keys")).status_code == 401


async def test_valid_key_authenticates_and_updates_last_used(client):
    user = await signup(client)
    key = await create_key(client, user)
    assert key["last_used_at"] is None

    response = await client.get("/api/products", headers=key_header(key))
    assert response.status_code == 200

    listing = (await client.get("/api-keys", headers=user["headers"])).json()
    assert listing[0]["last_used_at"] is not None


async def test_missing_and_invalid_keys_are_rejected(client, upstream):
    missing = await client.get("/api/products")
    invalid = await client.get("/api/products", headers={"X-API-Key": "gw_not-a-real-key"})
    jwt_only = await client.get("/api/products", headers=(await signup(client))["headers"])
    assert missing.status_code == invalid.status_code == jwt_only.status_code == 401
    assert upstream.requests == []  # nothing reached the upstream


async def test_revoked_key_is_rejected(client):
    user = await signup(client)
    key = await create_key(client, user)
    assert (await client.get("/api/products", headers=key_header(key))).status_code == 200

    revoke = await client.delete(f"/api-keys/{key['id']}", headers=user["headers"])
    assert revoke.status_code == 204
    assert (await client.get("/api/products", headers=key_header(key))).status_code == 401

    listing = (await client.get("/api-keys", headers=user["headers"])).json()
    assert listing[0]["is_active"] is False
    # Revoking again is harmless.
    again = await client.delete(f"/api-keys/{key['id']}", headers=user["headers"])
    assert again.status_code == 204


async def test_users_cannot_see_or_revoke_each_others_keys(client):
    alice, bob = await signup(client), await signup(client)
    alice_key = await create_key(client, alice, "alice-key")

    assert (await client.get("/api-keys", headers=bob["headers"])).json() == []
    stolen = await client.delete(f"/api-keys/{alice_key['id']}", headers=bob["headers"])
    assert stolen.status_code == 404
    # Alice's key still works.
    assert (await client.get("/api/products", headers=key_header(alice_key))).status_code == 200
