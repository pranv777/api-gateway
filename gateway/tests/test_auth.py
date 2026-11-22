from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models import User
from tests.helpers import PASSWORD, signup


async def test_register_and_login(client):
    user = await signup(client, "Alice@Example.com")
    me = await client.get("/auth/me", headers=user["headers"])
    assert me.json()["email"] == "alice@example.com"  # stored lower-cased

    login = await client.post(
        "/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    body = login.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == client.settings.jwt_expire_minutes * 60


async def test_email_is_normalised_and_unique(client):
    await signup(client, "bob@example.com")
    again = await client.post(
        "/auth/register", json={"email": " BOB@example.com ", "password": PASSWORD}
    )
    assert again.status_code == 409


async def test_password_is_stored_hashed(client, infra):
    await signup(client, "carol@example.com")
    async with infra.sessionmaker() as session:
        stored = (await session.execute(select(User.password_hash))).scalar_one()
    assert stored.startswith("$argon2")
    assert PASSWORD not in stored


async def test_register_validates_input(client):
    bad_email = await client.post("/auth/register", json={"email": "nope", "password": PASSWORD})
    short_pw = await client.post(
        "/auth/register", json={"email": "d@example.com", "password": "short"}
    )
    assert bad_email.status_code == 422
    assert short_pw.status_code == 422


async def test_login_rejects_wrong_password_and_unknown_user_identically(client):
    await signup(client, "erin@example.com")
    wrong = await client.post(
        "/auth/login", json={"email": "erin@example.com", "password": "wrong-password"}
    )
    unknown = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_me_returns_current_user(client):
    user = await signup(client)
    response = await client.get("/auth/me", headers=user["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == user["id"]
    assert "password" not in response.text


async def test_protected_endpoint_without_token_is_401(client):
    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_malformed_token_is_401(client):
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


async def test_expired_token_is_401(client):
    user = await signup(client)
    secret = client.settings.jwt_secret.get_secret_value()
    expired = create_access_token(
        user["id"], secret, expires_minutes=5, now=datetime.now(UTC) - timedelta(hours=1)
    )
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


async def test_token_for_deleted_user_is_401(client):
    secret = client.settings.jwt_secret.get_secret_value()
    token = create_access_token("00000000-0000-0000-0000-000000000000", secret, 5)
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_auth_endpoints_are_ip_throttled(make_client):
    client = await make_client(auth_rate_limit_attempts=3, auth_rate_limit_window_seconds=60)
    body = {"email": "x@example.com", "password": "whatever-it-is"}
    statuses = [(await client.post("/auth/login", json=body)).status_code for _ in range(5)]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429]
    limited = await client.post("/auth/login", json=body)
    assert 1 <= int(limited.headers["retry-after"]) <= 60
