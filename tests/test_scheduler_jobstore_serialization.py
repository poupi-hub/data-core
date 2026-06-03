from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from scheduler import service


def _noop_boot_heartbeat() -> None:
    return None


def _sqlalchemy_jobstore(tmp_path):
    return {"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'scheduler_jobs.sqlite'}")}


def _shutdown_scheduler(scheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def test_all_scheduler_jobs_register_with_sqlalchemy_jobstore(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "boot_heartbeat", _noop_boot_heartbeat)

    scheduler = service.create_scheduler(
        jobstores=_sqlalchemy_jobstore(tmp_path),
        start_paused_for_persistence=True,
    )
    try:
        jobs = scheduler.get_jobs()
        non_serializable = [
            job.id for job in jobs if "<lambda>" in job.func_ref or "<locals>" in job.func_ref
        ]

        assert jobs
        assert non_serializable == []
    finally:
        _shutdown_scheduler(scheduler)


def test_persistent_bootstrap_preserves_existing_next_run_time(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "boot_heartbeat", _noop_boot_heartbeat)

    jobstores = _sqlalchemy_jobstore(tmp_path)
    first = service.create_scheduler(
        jobstores=jobstores,
        start_paused_for_persistence=True,
    )
    try:
        preserved_next_run_time = datetime.now(first.timezone) + timedelta(hours=6)
        first.get_job("pipeline:analytics").modify(next_run_time=preserved_next_run_time)
    finally:
        _shutdown_scheduler(first)

    second = service.create_scheduler(
        jobstores=jobstores,
        start_paused_for_persistence=True,
    )
    try:
        restored = second.get_job("pipeline:analytics")
        assert restored is not None
        assert int(restored.next_run_time.timestamp()) == int(preserved_next_run_time.timestamp())
    finally:
        _shutdown_scheduler(second)
