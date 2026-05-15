"""Tests for scheduler/circuit_breaker.py.

Uses a real PostgreSQL session (skipped if DB unavailable) and patches
send_webhook + Prometheus counter to avoid side effects.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from scheduler.circuit_breaker import (
    CIRCUIT_OPEN_ERROR_TYPE,
    CIRCUIT_REOPEN_ERROR_TYPE,
    CONSECUTIVE_FAILURES_THRESHOLD,
    check_source_circuit,
    reopen_source_circuit,
)

MODULE = "ecommerce"
SOURCE = "cb_test_source"


def _run(db, status: RunStatus, finished_at=None) -> CollectionRun:
    run = CollectionRun(
        collector_name="test_collector",
        module=MODULE,
        source_name=SOURCE,
        status=status,
        finished_at=finished_at or datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def _target(db, active: bool = True) -> CollectionTarget:
    import uuid
    target = CollectionTarget(
        module=MODULE,
        source_name=SOURCE,
        collector_name="test_collector",
        target_url=f"https://example.com/{uuid.uuid4()}",
        active=active,
    )
    db.add(target)
    db.flush()
    return target


@pytest.fixture(autouse=True)
def _cleanup_circuit_test_records(db_session):
    db_session.query(CollectorError).filter(CollectorError.collector_name == SOURCE).delete(synchronize_session=False)
    db_session.query(CollectionTarget).filter(
        CollectionTarget.module == MODULE,
        CollectionTarget.source_name == SOURCE,
    ).delete(synchronize_session=False)
    db_session.query(CollectionRun).filter(
        CollectionRun.module == MODULE,
        CollectionRun.source_name == SOURCE,
    ).delete(synchronize_session=False)
    db_session.commit()
    yield
    db_session.query(CollectorError).filter(CollectorError.collector_name == SOURCE).delete(synchronize_session=False)
    db_session.query(CollectionTarget).filter(
        CollectionTarget.module == MODULE,
        CollectionTarget.source_name == SOURCE,
    ).delete(synchronize_session=False)
    db_session.query(CollectionRun).filter(
        CollectionRun.module == MODULE,
        CollectionRun.source_name == SOURCE,
    ).delete(synchronize_session=False)
    db_session.commit()


@pytest.fixture(autouse=True)
def _patch_side_effects():
    with (
        patch("scheduler.circuit_breaker.send_webhook") as mock_webhook,
        patch("scheduler.circuit_breaker.circuit_breaker_opens_total") as mock_counter,
    ):
        mock_counter.labels.return_value = MagicMock()
        yield mock_webhook, mock_counter


def test_circuit_not_triggered_with_insufficient_history(db_session):
    threshold = CONSECUTIVE_FAILURES_THRESHOLD
    for _ in range(threshold - 1):
        _run(db_session, RunStatus.failed)
    db_session.commit()

    result = check_source_circuit(db_session, module=MODULE, source_name=SOURCE)

    assert result is False


def test_circuit_not_triggered_when_some_runs_succeeded(db_session):
    threshold = CONSECUTIVE_FAILURES_THRESHOLD
    for i in range(threshold):
        status = RunStatus.failed if i < threshold - 1 else RunStatus.success
        _run(db_session, status)
    db_session.commit()

    result = check_source_circuit(db_session, module=MODULE, source_name=SOURCE)

    assert result is False


def test_circuit_opens_after_consecutive_failures(db_session, _patch_side_effects):
    mock_webhook, mock_counter = _patch_side_effects
    threshold = CONSECUTIVE_FAILURES_THRESHOLD
    for _ in range(threshold):
        _run(db_session, RunStatus.failed)
    target = _target(db_session)
    db_session.commit()

    result = check_source_circuit(db_session, module=MODULE, source_name=SOURCE, threshold=threshold)

    assert result is True

    # Targets should be deactivated
    db_session.refresh(target)
    assert target.active is False

    # A CollectorError of type CircuitOpen should exist
    error = (
        db_session.query(CollectorError)
        .filter(
            CollectorError.collector_name == SOURCE,
            CollectorError.error_type == CIRCUIT_OPEN_ERROR_TYPE,
            CollectorError.resolved_at.is_(None),
        )
        .first()
    )
    assert error is not None
    assert "consecutive" in error.message.lower() or "circuit" in error.message.lower()

    # Prometheus counter and webhook should fire
    mock_counter.labels.assert_called_once_with(module=MODULE, source_name=SOURCE)
    mock_counter.labels.return_value.inc.assert_called_once()
    mock_webhook.assert_called_once()
    payload = mock_webhook.call_args[0][0]
    assert payload["event"] == "circuit_opened"
    assert payload["source_name"] == SOURCE


def test_circuit_open_is_idempotent(db_session, _patch_side_effects):
    mock_webhook, _ = _patch_side_effects
    threshold = 3
    for _ in range(threshold):
        _run(db_session, RunStatus.failed)
    _target(db_session)
    db_session.commit()

    first = check_source_circuit(db_session, module=MODULE, source_name=SOURCE, threshold=threshold)
    second = check_source_circuit(db_session, module=MODULE, source_name=SOURCE, threshold=threshold)

    assert first is True
    assert second is False  # already open — no duplicate action
    assert mock_webhook.call_count == 1  # webhook fires only once


def test_reopen_circuit_reactivates_targets_and_resolves_errors(db_session):
    threshold = 3
    for _ in range(threshold):
        _run(db_session, RunStatus.failed)
    target = _target(db_session, active=True)
    db_session.commit()

    with (
        patch("scheduler.circuit_breaker.send_webhook"),
        patch("scheduler.circuit_breaker.circuit_breaker_opens_total") as mock_counter,
    ):
        mock_counter.labels.return_value = MagicMock()
        check_source_circuit(db_session, module=MODULE, source_name=SOURCE, threshold=threshold)

    db_session.refresh(target)
    assert target.active is False

    reactivated = reopen_source_circuit(db_session, module=MODULE, source_name=SOURCE)

    db_session.refresh(target)
    assert target.active is True
    assert reactivated == 1

    # Original CircuitOpen error should be resolved
    open_error = (
        db_session.query(CollectorError)
        .filter(
            CollectorError.collector_name == SOURCE,
            CollectorError.error_type == CIRCUIT_OPEN_ERROR_TYPE,
            CollectorError.resolved_at.is_(None),
        )
        .first()
    )
    assert open_error is None

    # A CircuitAutoReopened event should be recorded
    reopen_event = (
        db_session.query(CollectorError)
        .filter(
            CollectorError.collector_name == SOURCE,
            CollectorError.error_type == CIRCUIT_REOPEN_ERROR_TYPE,
        )
        .first()
    )
    assert reopen_event is not None


def test_circuit_does_not_affect_other_sources(db_session, _patch_side_effects):
    threshold = 3
    other_source = "other_source"
    for _ in range(threshold):
        _run(db_session, RunStatus.failed)
    other_target = CollectionTarget(
        module=MODULE,
        source_name=other_source,
        collector_name="test_collector",
        target_url="https://example.com/other",
        active=True,
    )
    db_session.add(other_target)
    db_session.commit()

    check_source_circuit(db_session, module=MODULE, source_name=SOURCE, threshold=threshold)

    db_session.refresh(other_target)
    assert other_target.active is True  # other source untouched
