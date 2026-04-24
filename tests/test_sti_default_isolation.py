"""
Regression: STI child Pydantic defaults must not leak onto the shared parent table column.

Root cause: ``_register_sti_columns()`` calls ``get_column_from_field(field_info)``
to build a SQLAlchemy Column from each new STI subclass field. That helper copies
Pydantic's ``default`` onto ``Column.default``. If the column is then appended to
the shared STI parent table unchanged, sibling subclasses that do NOT declare the
field inherit the default at INSERT time: the ORM falls back to ``Column.default``
because the attribute is absent from the sibling instance's ``__dict__``.

Fix: clear ``column.default`` and ``column.server_default`` before
``parent_table.append_column(column)``. The declaring subclass is unaffected
because Pydantic populates the instance attribute during ``__init__`` and the
ORM reads that value directly on flush.

This file covers both layers:
    1. Metadata-level: the shared column carries no SA default after registration.
    2. Behavioural: inserting a sibling row stores NULL (not the peer's default).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests._models import FunctionA, FunctionB, FunctionC, Tool


class TestMetadataDefaultCleared:
    """Pure metadata assertions: no DB required."""

    def test_shared_column_has_no_python_default(self) -> None:
        """``tool.max_files.default`` must be None even though FunctionA set 100."""
        max_files = Tool.__table__.columns["max_files"]  # pyright: ignore[reportAttributeAccessIssue]
        assert max_files.default is None, (
            f"tool.max_files.default should be None (fix cleared the shared SA "
            f"default to prevent leakage). Got: {max_files.default!r}"
        )

    def test_shared_column_has_no_server_default(self) -> None:
        max_files = Tool.__table__.columns["max_files"]  # pyright: ignore[reportAttributeAccessIssue]
        assert max_files.server_default is None

    def test_all_sibling_columns_cleared(self) -> None:
        """Every field declared by a sibling must carry a cleared shared column."""
        for column_name in ("max_files", "timeout"):
            column = Tool.__table__.columns[column_name]  # pyright: ignore[reportAttributeAccessIssue]
            assert column.default is None, (
                f"tool.{column_name}.default should be None. "
                f"Got: {column.default!r}"
            )
            assert column.server_default is None, (
                f"tool.{column_name}.server_default should be None. "
                f"Got: {column.server_default!r}"
            )

    def test_pydantic_field_default_preserved(self) -> None:
        """Fix clears SA-level defaults only; Pydantic field defaults must stay.

        If we accidentally wiped Pydantic's default, the declaring subclass
        would lose its default value at instance construction time.
        """
        assert FunctionA.model_fields["max_files"].default == 100
        assert FunctionB.model_fields["timeout"].default == 30


@pytest.mark.asyncio
class TestSiblingInsertDoesNotPolluteRow:
    """DB-level proof that sibling inserts do not receive the other sibling's default."""

    async def test_function_c_row_has_null_for_both_sibling_fields(
        self,
        session: AsyncSession,
    ) -> None:
        """FunctionC declares no extra fields - both ``max_files`` and ``timeout``
        must be NULL in its row. Without the fix, both would be 100 and 30
        respectively, polluted from FunctionA/FunctionB.
        """
        func_c = FunctionC(name="plain")
        func_c = await func_c.save(session)

        # Raw-SQL check bypasses Pydantic, reading exactly what's on disk.
        result = await session.execute(
            select(
                Tool.__table__.c.max_files,  # pyright: ignore[reportAttributeAccessIssue]
                Tool.__table__.c.timeout,    # pyright: ignore[reportAttributeAccessIssue]
            ).where(Tool.__table__.c.id == func_c.id)  # pyright: ignore[reportAttributeAccessIssue]
        )
        row = result.one()
        assert row.max_files is None, (
            f"FunctionC.max_files should be NULL (sibling field, not declared). "
            f"Got {row.max_files!r} - likely polluted from FunctionA default."
        )
        assert row.timeout is None, (
            f"FunctionC.timeout should be NULL. Got {row.timeout!r} - "
            f"likely polluted from FunctionB default."
        )

    async def test_function_a_row_still_stores_its_own_default(
        self,
        session: AsyncSession,
    ) -> None:
        """Declaring subclass still persists its own Pydantic-provided default.
        Proves the fix didn't break the happy path.
        """
        func_a = FunctionA(name="with-files")
        func_a = await func_a.save(session)

        result = await session.execute(
            select(Tool.__table__.c.max_files)  # pyright: ignore[reportAttributeAccessIssue]
            .where(Tool.__table__.c.id == func_a.id)  # pyright: ignore[reportAttributeAccessIssue]
        )
        stored = result.scalar_one()
        assert stored == 100

    async def test_function_b_row_isolated_from_function_a(
        self,
        session: AsyncSession,
    ) -> None:
        """FunctionB only declares ``timeout``; its ``max_files`` cell must be NULL."""
        func_b = FunctionB(name="with-timeout")
        func_b = await func_b.save(session)

        result = await session.execute(
            select(
                Tool.__table__.c.max_files,  # pyright: ignore[reportAttributeAccessIssue]
                Tool.__table__.c.timeout,    # pyright: ignore[reportAttributeAccessIssue]
            ).where(Tool.__table__.c.id == func_b.id)  # pyright: ignore[reportAttributeAccessIssue]
        )
        row = result.one()
        assert row.max_files is None
        assert row.timeout == 30
