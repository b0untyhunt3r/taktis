import asyncio
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

# Force in-memory SQLite for tests -- must be set before any taktis import.
os.environ["TAKTIS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Provide a single event-loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Golden DB: built once per session, copied per test for fast Taktis init
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def _golden_db_path():
    """Create a fully-initialized DB file once per session.

    Contains all tables, migrations, experts (232+), agent templates (7),
    and pipeline templates.  Each function-scoped fixture copies this file
    instead of re-running load_builtins() (which reads 232+ .md files).
    """
    import taktis.db as db_mod
    from taktis.db import init_db, init_pool, close_pool, get_session

    tmpdir = tempfile.mkdtemp(prefix="golden_db_")
    golden_path = os.path.join(tmpdir, "golden.db")

    original_path = db_mod.DATABASE_PATH
    db_mod.DATABASE_PATH = golden_path

    # Full init: tables + migrations + seed pipeline templates
    await init_db()
    # Need a pool for the registries to use get_session()
    await init_pool()

    try:
        from taktis.core.experts import ExpertRegistry
        from taktis.core.agent_templates import AgentTemplateRegistry

        expert_reg = ExpertRegistry(db_session_factory=get_session)
        await expert_reg.load_builtins()

        template_reg = AgentTemplateRegistry(db_session_factory=get_session)
        await template_reg.load_builtins()
    finally:
        await close_pool()

    db_mod.DATABASE_PATH = original_path

    yield golden_path

    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Raw DB connection fixture (for repository-level tests)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_conn():
    """Provide a fresh in-memory DB connection with tables already created.

    Because each aiosqlite.connect(":memory:") creates a *separate* in-memory
    database, we create the tables on the same connection that the test will
    use.  This avoids the issue where ``init_db()`` creates tables on one
    connection and the test operates on another.
    """
    import aiosqlite
    from taktis.db import _CREATE_TABLES_SQL

    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(_CREATE_TABLES_SQL)
    await db.commit()
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Taktis fixture (integration tests)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def taktis_engine(tmp_path, _golden_db_path):
    """Provide an initialised Taktis backed by a temporary on-disk DB.

    Copies the pre-built golden DB (with experts, templates, etc. already
    loaded) so that load_builtins() can be skipped, saving ~232 file reads
    and DB inserts per test.
    """
    import taktis.db as db_mod
    from taktis.core.engine import Taktis

    db_file = str(tmp_path / "test.db")
    shutil.copy2(_golden_db_path, db_file)

    original_path = db_mod.DATABASE_PATH
    db_mod.DATABASE_PATH = db_file

    orch = Taktis()
    with patch(
        "taktis.core.experts.ExpertRegistry.load_builtins",
        new_callable=AsyncMock,
    ), patch(
        "taktis.core.agent_templates.AgentTemplateRegistry.load_builtins",
        new_callable=AsyncMock,
    ):
        await orch.initialize()

    try:
        yield orch
    finally:
        await orch.shutdown()
        db_mod.DATABASE_PATH = original_path
