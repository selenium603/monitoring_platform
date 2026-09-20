"""Repository for short-lived CLI PKCE authorization codes."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import CliAuthCodeModel


class CliAuthRepository:
    """Persist and atomically consume CLI authorization codes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        code_hash: str,
        user_id: UUID,
        org_id: UUID,
        project_id: UUID,
        project_name: str,
        code_challenge: str,
        label: str,
        expires_days: int,
        expires_at: datetime,
    ) -> None:
        """Persist one short-lived authorization-code binding."""
        self._session.add(
            CliAuthCodeModel(
                code_hash=code_hash,
                user_id=user_id,
                org_id=org_id,
                project_id=project_id,
                project_name=project_name,
                code_challenge=code_challenge,
                label=label,
                expires_days=expires_days,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def get_active(self, code_hash: str) -> CliAuthCodeModel | None:
        """Fetch an unexpired code without consuming it."""
        stmt = select(CliAuthCodeModel).where(
            CliAuthCodeModel.code_hash == code_hash,
            CliAuthCodeModel.expires_at > datetime.now(timezone.utc),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def consume(self, code_hash: str) -> bool:
        """Atomically delete an unexpired code, returning whether it won the race."""
        stmt = (
            delete(CliAuthCodeModel)
            .where(
                CliAuthCodeModel.code_hash == code_hash,
                CliAuthCodeModel.expires_at > datetime.now(timezone.utc),
            )
            .returning(CliAuthCodeModel.code_hash)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
