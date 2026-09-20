"""Orchestration for the CLI OAuth2 Authorization Code + PKCE login flow.

The ``pandaprobe auth login`` CLI obtains a 90-day data-plane API key
without the raw key ever touching the browser. Short-lived authorization
codes are stored as hashes in PostgreSQL and consumed atomically.

Security: PKCE ``S256`` is mandatory, codes are single-use with a ~120s TTL,
and neither the ``code_verifier`` nor the raw key is ever logged.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.repositories.cli_auth_repo import CliAuthRepository
from app.registry.exceptions import AuthenticationError, ValidationError
from app.services.identity_service import IdentityService

_CODE_TTL_SECONDS = 120
_CODE_RANDOM_BYTES = 32
_MAX_KEY_NAME_LEN = 255
_ALLOWED_EXPIRES_DAYS = 90


def verify_pkce(verifier: str, challenge: str) -> bool:
    """Return True iff ``BASE64URL(SHA256(verifier)) == challenge``."""
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def _hash_code(code: str) -> str:
    """Hash an opaque code before it is persisted."""
    return hashlib.sha256(code.encode()).hexdigest()


def _key_name_from_label(label: str) -> str:
    """Derive a human-readable, revocation-friendly API key name from *label*."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    clean_label = (label or "cli").strip() or "cli"
    name = f"pandaprobe-cli — {clean_label} — {today}"
    return name[:_MAX_KEY_NAME_LEN]


class CliAuthService:
    """Issue and exchange single-use CLI authorization codes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._codes = CliAuthRepository(session)
        self._identity = IdentityService(session)

    async def issue_code(
        self,
        *,
        user_id: UUID,
        org_id: UUID,
        project_id: UUID,
        code_challenge: str,
        code_challenge_method: str,
        label: str,
        expires_days: int = 90,
    ) -> tuple[str, int]:
        """Mint a single-use authorization code bound to the PKCE challenge."""
        if code_challenge_method != "S256":
            raise ValidationError("Unsupported code_challenge_method. Only 'S256' is allowed.")
        if not code_challenge or not code_challenge.strip():
            raise ValidationError("code_challenge is required.")
        if expires_days != _ALLOWED_EXPIRES_DAYS:
            raise ValidationError(f"Only a {_ALLOWED_EXPIRES_DAYS}-day key lifetime is supported for the CLI.")

        await self._identity.require_membership(user_id, org_id)
        project = await self._identity.get_project(project_id, org_id=org_id)

        code = secrets.token_urlsafe(_CODE_RANDOM_BYTES)
        await self._codes.create(
            code_hash=_hash_code(code),
            user_id=user_id,
            org_id=org_id,
            project_id=project.id,
            project_name=project.name,
            code_challenge=code_challenge,
            label=label,
            expires_days=expires_days,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=_CODE_TTL_SECONDS),
        )
        return code, _CODE_TTL_SECONDS

    async def exchange(self, *, code: str, code_verifier: str) -> dict:
        """Exchange a single-use code + PKCE verifier for a fresh API key."""
        if not code or not code_verifier:
            raise ValidationError("Both 'code' and 'code_verifier' are required.")

        row = await self._codes.get_active(_hash_code(code))
        if row is None:
            raise AuthenticationError("Authorization code is invalid, expired, or already used.")

        if not verify_pkce(code_verifier, row.code_challenge):
            raise AuthenticationError("PKCE verification failed.")

        if not await self._codes.consume(row.code_hash):
            raise AuthenticationError("Authorization code is invalid, expired, or already used.")
        await self._session.commit()

        await self._identity.require_membership(row.user_id, row.org_id)

        expiration = f"{row.expires_days}d"
        api_key, raw_key = await self._identity.create_api_key(
            org_id=row.org_id,
            name=_key_name_from_label(row.label),
            created_by=row.user_id,
            expiration=expiration,
        )

        return {
            "api_key": raw_key,
            "project_name": row.project_name,
            "org_id": str(row.org_id),
            "user_id": str(row.user_id),
            "key_id": str(api_key.id),
            "key_prefix": api_key.key_prefix,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        }
