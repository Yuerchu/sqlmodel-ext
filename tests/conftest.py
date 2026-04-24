"""
Pytest fixtures for sqlmodel-ext tests.

Uses aiosqlite (in-memory) so tests never touch a real database.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import configure_mappers
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel_ext import (
    register_sti_column_properties_for_all_subclasses,
    register_sti_columns_for_all_subclasses,
)

# Import test models so the STI metaclass queues them before phase 1 runs.
from tests import _models  # noqa: F401  -- imported for side effects


@pytest.fixture(scope="session", autouse=True)
def _register_sti() -> None:
    """Run the STI two-phase registration exactly once per session.

    Phase 1 must run before ``configure_mappers()``, phase 2 after.
    Calling this more than once is idempotent because the queue is drained
    but SA mapper configuration short-circuits already-configured mappers.
    """
    register_sti_columns_for_all_subclasses()
    configure_mappers()
    register_sti_column_properties_for_all_subclasses()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Fresh in-memory async SQLite engine per test.

    Each test gets a brand-new database so state never leaks between tests.
    """
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Async session bound to the fresh engine."""
    async with AsyncSession(engine) as s:
        yield s
