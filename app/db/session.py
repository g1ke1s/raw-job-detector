import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from app.db.models import Base

log = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column migrations for existing databases
        from sqlalchemy import text
        for stmt in [
            "ALTER TABLE matches ADD COLUMN IF NOT EXISTS seniority VARCHAR(32)",
            "ALTER TABLE matches ADD COLUMN IF NOT EXISTS job_json JSONB",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                log.warning("Migration skipped: %s", e)
    log.info("Database tables verified/created")
    from app.db.config_store import seed_defaults
    await seed_defaults()