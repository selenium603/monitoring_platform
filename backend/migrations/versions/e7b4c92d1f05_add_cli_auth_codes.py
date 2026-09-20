"""add PostgreSQL-backed CLI PKCE authorization codes

Revision ID: e7b4c92d1f05
Revises: df3a7c91b2e4
Create Date: 2026-09-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b4c92d1f05"
down_revision: Union[str, None] = "df3a7c91b2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cli_auth_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("code_challenge", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("expires_days", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_index("ix_cli_auth_codes_expires_at", "cli_auth_codes", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cli_auth_codes_expires_at", table_name="cli_auth_codes")
    op.drop_table("cli_auth_codes")
