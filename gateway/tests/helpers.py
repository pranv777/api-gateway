from uuid import uuid4

import httpx

PASSWORD = "correct-horse-battery"


async def signup(client: httpx.AsyncClient, email: str | None = None) -> dict:
    """Register and log in a new user. Returns {id, email, headers} (headers hold the JWT)."""
    email = email or f"user-{uuid4().hex[:10]}@example.com"
    registered = await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {
        "id": registered.json()["id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def create_key(client: httpx.AsyncClient, user: dict, name: str = "test-key") -> dict:
    """Create an API key for `user`. The returned dict includes the plaintext under "key"."""
    response = await client.post("/api-keys", json={"name": name}, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def key_header(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["key"]}
