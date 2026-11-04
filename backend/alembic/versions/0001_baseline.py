"""Baseline: establishes migration history. Creates no domain tables.

Domain tables (catalogue, inventory, orders, ...) are added from M1 onwards as their own
revisions. This revision exists so every environment has a known starting point and the
health check can confirm the schema is at head.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-04
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
