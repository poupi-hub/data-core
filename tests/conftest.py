import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://data_core:data_core@localhost:5433/data_core"),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app


@pytest.fixture()
def db_session():
    """Provides a SQLAlchemy session that rolls back all changes after each test.

    Uses join_transaction_mode="create_savepoint" (SQLAlchemy 2.0):
    every session.commit() inside the test only releases a SAVEPOINT.
    The outer connection transaction is rolled back in teardown — no data persists.
    """
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture()
def api_client():
    with TestClient(create_app()) as client:
        yield client
