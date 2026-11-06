"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    """One database session per request; read-only endpoints never commit."""
    with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
