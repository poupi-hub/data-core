from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.system_status import _collection_readiness_summary
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus


def test_collection_readiness_treats_recovered_collector_errors_as_non_blocking(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(
        CollectionTarget(
            module="pytest-readiness",
            source_name="pytest-source",
            collector_name="pytest.collector",
            target_url="https://example.test/readiness",
            active=True,
        )
    )
    db_session.add(
        CollectorError(
            collector_name="pytest.collector",
            error_type="TemporaryError",
            message="temporary failure",
            created_at=now - timedelta(hours=1),
        )
    )
    db_session.add(
        CollectionRun(
            collector_name="pytest.collector",
            status=RunStatus.success,
            started_at=now,
            finished_at=now,
        )
    )
    db_session.commit()

    summary = _collection_readiness_summary(db_session)

    assert summary["ready"] is True
    assert summary["unresolved_collector_errors"] >= 1
    assert summary["blocking_collector_errors"] == 0
    assert summary["recovered_unresolved_collector_errors"] >= 1


def test_collection_readiness_keeps_unrecovered_collector_errors_blocking(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(
        CollectionTarget(
            module="pytest-readiness",
            source_name="pytest-source",
            collector_name="pytest.blocked_collector",
            target_url="https://example.test/readiness-blocked",
            active=True,
        )
    )
    db_session.add(
        CollectorError(
            collector_name="pytest.blocked_collector",
            error_type="PersistentError",
            message="still failing",
            created_at=now,
        )
    )
    db_session.commit()

    summary = _collection_readiness_summary(db_session)

    assert summary["ready"] is False
    assert summary["blocking_collector_errors"] >= 1
