from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

engine = create_engine(
    settings.database_url,
    # ── Connection health ────────────────────────────────────────────────────
    pool_pre_ping=True,       # issue a cheap SELECT 1 before handing out a connection
    future=True,
    # ── Pool sizing ──────────────────────────────────────────────────────────
    # scheduler + worker each run in a single process; 5+5 connections is
    # plenty and avoids exhausting Neon's 100-connection limit across all
    # containers (api × 2 + scheduler + worker + volatile).
    pool_size=5,
    max_overflow=5,           # up to 10 total connections per process under burst
    # ── Staleness prevention ─────────────────────────────────────────────────
    # Recycle connections older than 30 min so the pool never holds a
    # connection across a Neon idle-timeout (300 s) or a network NAT table
    # expiry. This is the primary fix for "SSL connection has been closed
    # unexpectedly" errors under light load.
    pool_recycle=1800,
    # ── Acquisition timeout ──────────────────────────────────────────────────
    # Fail fast if all connections are held for > 30 s (e.g. a stuck job
    # hogging the pool) rather than queueing indefinitely.
    pool_timeout=30,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
