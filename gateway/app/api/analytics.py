from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import enforce_ip_limit, get_current_user
from app.db.models import RequestLog, User
from app.db.session import get_session

router = APIRouter(
    prefix="/analytics", tags=["analytics"], dependencies=[Depends(enforce_ip_limit)]
)


@dataclass(frozen=True, slots=True)
class LogFilters:
    user_id: UUID
    start: datetime | None
    end: datetime | None
    api_key_id: UUID | None

    def conditions(self) -> list:
        # user_id is always part of the filter: users can only ever see their own data.
        conds = [RequestLog.user_id == self.user_id]
        if self.start:
            conds.append(RequestLog.created_at >= self.start)
        if self.end:
            conds.append(RequestLog.created_at <= self.end)
        if self.api_key_id:
            conds.append(RequestLog.api_key_id == self.api_key_id)
        return conds


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def get_filters(
    user: User = Depends(get_current_user),
    start: datetime | None = Query(
        None, description="Inclusive lower bound (ISO 8601, UTC if no zone)"
    ),
    end: datetime | None = Query(
        None, description="Inclusive upper bound (ISO 8601, UTC if no zone)"
    ),
    api_key_id: UUID | None = Query(None, description="Only requests made with this API key"),
) -> LogFilters:
    start, end = _as_utc(start), _as_utc(end)
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    return LogFilters(user.id, start, end, api_key_id)


class UsageSummary(BaseModel):
    total_requests: int
    successful_requests: int  # status < 400
    failed_requests: int  # status >= 400, excluding gateway rate-limit rejections
    rate_limited_requests: int  # rejected by the gateway with 429
    average_response_time_ms: float


class RequestLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    api_key_id: UUID | None
    method: str
    endpoint: str
    status_code: int
    duration_ms: float
    is_rate_limited: bool
    created_at: datetime


class RequestLogPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[RequestLogOut]


@router.get("/usage", response_model=UsageSummary)
async def usage(
    filters: LogFilters = Depends(get_filters), session: AsyncSession = Depends(get_session)
) -> UsageSummary:
    count = func.count(RequestLog.id)
    row = (
        await session.execute(
            select(
                count,
                count.filter(RequestLog.status_code < 400, RequestLog.is_rate_limited.is_(False)),
                count.filter(RequestLog.status_code >= 400, RequestLog.is_rate_limited.is_(False)),
                count.filter(RequestLog.is_rate_limited.is_(True)),
                func.avg(RequestLog.duration_ms),
            ).where(*filters.conditions())
        )
    ).one()
    total, ok, failed, limited, avg_ms = row
    return UsageSummary(
        total_requests=total,
        successful_requests=ok,
        failed_requests=failed,
        rate_limited_requests=limited,
        average_response_time_ms=round(float(avg_ms), 3) if avg_ms is not None else 0.0,
    )


@router.get("/requests", response_model=RequestLogPage)
async def requests(
    filters: LogFilters = Depends(get_filters),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> RequestLogPage:
    conds = filters.conditions()
    total = (await session.execute(select(func.count(RequestLog.id)).where(*conds))).scalar_one()
    rows = (
        await session.execute(
            select(RequestLog)
            .where(*conds)
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return RequestLogPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[RequestLogOut.model_validate(r) for r in rows],
    )
