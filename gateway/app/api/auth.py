from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import enforce_auth_ip_limit, enforce_ip_limit, get_current_user
from app.core.security import create_access_token, hash_password_async, verify_password_async
from app.db.models import User
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class _EmailMixin(BaseModel):
    email: str = Field(max_length=254, pattern=_EMAIL_PATTERN, examples=["alice@example.com"])

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class RegisterRequest(_EmailMixin):
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(_EmailMixin):
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")


@router.post(
    "/register",
    status_code=201,
    response_model=UserOut,
    dependencies=[Depends(enforce_auth_ip_limit)],
)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> User:
    user = User(email=body.email, password_hash=await hash_password_async(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # unique(email); also covers two simultaneous registrations
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    return user


@router.post("/login", response_model=TokenOut, dependencies=[Depends(enforce_auth_ip_limit)])
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    # Always run a password verification (against a dummy hash if the user is unknown) so
    # response time does not reveal whether the email exists.
    valid = await verify_password_async(body.password, user.password_hash if user else None)
    if user is None or not valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = request.app.state.settings
    token = create_access_token(
        str(user.id), settings.jwt_secret.get_secret_value(), settings.jwt_expire_minutes
    )
    return TokenOut(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.get(
    "/me",
    response_model=UserOut,
    dependencies=[Depends(enforce_ip_limit)],
)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
