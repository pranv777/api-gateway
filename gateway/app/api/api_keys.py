from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import enforce_ip_limit, get_current_user
from app.core.security import generate_api_key
from app.db.models import ApiKey, User
from app.db.session import get_session

router = APIRouter(prefix="/api-keys", tags=["api-keys"], dependencies=[Depends(enforce_ip_limit)])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["ci-pipeline"])


class ApiKeyOut(BaseModel):
    """Public view of a key. Neither the hash nor the plaintext is ever included."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The plaintext API key. It is shown only once, so store it now.")


@router.post("", status_code=201, response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    plaintext, key_hash = generate_api_key()
    api_key = ApiKey(user_id=user.id, key_hash=key_hash, name=body.name, last_used_at=None)
    session.add(api_key)
    await session.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(api_key).model_dump(), key=plaintext)


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[ApiKey]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars())


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    # Scoped by user_id: another user's key looks exactly like a missing one (404).
    api_key = (
        await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    ).scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.is_active:  # revoking twice is a no-op
        api_key.is_active = False
        await session.commit()
    return Response(status_code=204)
