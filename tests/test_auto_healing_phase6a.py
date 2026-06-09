"""Phase 6A tests: safe VPS cleanup — DRY_RUN, metrics, cooldown, circuit breaker."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.auto_healing.cleanup import (
    PROTECTED_REPOS,
    BuildCacheCleanup,
    CleanupAuditLogger,
    CleanupCircuitBreaker,
    CleanupCooldown,
    CleanupMetrics,
    CleanupReport,
    CleanupRunner,
    ContainerLogAudit,
    DanglingImageCleanup,
    OldTaggedImageCleanup,
    TaskResult,
    _is_protected_repo,
    _mb,
    _older_than,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_image(
    image_id: str,
    tags: list[str],
    size_bytes: int = 100 * 1024 * 1024,
    created: str = "2020-01-01T00:00:00.000000000Z",
) -> MagicMock:
    img = MagicMock()
    img.id = image_id
    img.tags = tags
    img.attrs = {"Size": size_bytes, "Created": created}
    return img


def _mock_container(image_id: str, name: str = "test-container") -> MagicMock:
    c = MagicMock()
    c.name = name
    c.image.id = image_id
    return c


def _mock_docker_client(images=None, containers=None):
    client = MagicMock()
    client.images.list.return_value = images or []
    client.containers.list.return_value = containers or []
    client.images.prune.return_value = {"ImagesDeleted": None, "SpaceReclaimed": 0}
    client.api.prune_builds.return_value = {"CachesDeleted": [], "SpaceReclaimed": 0}
    client.df.return_value = {"BuildCache": []}
    return client


def _fake_redis():
    """In-memory dict that mimics a subset of Redis interface."""
    store: dict[str, str] = {}
    ttls: dict[str, float] = {}

    r = MagicMock()

    def _incr(key):
        store[key] = str(int(store.get(key, 0)) + 1)
        return int(store[key])

    def _incrby(key, amount):
        store[key] = str(int(store.get(key, 0)) + amount)
        return int(store[key])

    def _get(key):
        return store.get(key)

    def _set(key, value):
        store[key] = str(value)

    def _setex(key, ttl_secs, value):
        store[key] = str(value)
        ttls[key] = time.time() + ttl_secs

    def _exists(key):
        if key in ttls and time.time() > ttls[key]:
            del store[key]
            del ttls[key]
            return 0
        return 1 if key in store else 0

    def _delete(*keys):
        for k in keys:
            store.pop(k, None)
            ttls.pop(k, None)

    def _ttl(key):
        if key not in ttls:
            return -1
        remaining = ttls[key] - time.time()
        return int(remaining) if remaining > 0 else -2

    def _mget(keys):
        return [store.get(k) for k in keys]

    def _lpush(key, *values):
        pass

    def _ltrim(key, start, end):
        pass

    r.incr.side_effect = _incr
    r.incrby.side_effect = _incrby
    r.get.side_effect = _get
    r.set.side_effect = _set
    r.setex.side_effect = _setex
    r.exists.side_effect = _exists
    r.delete.side_effect = _delete
    r.ttl.side_effect = _ttl
    r.mget.side_effect = _mget
    r.lpush.side_effect = _lpush
    r.ltrim.side_effect = _ltrim
    return r


# ── Safety: _is_protected_repo ────────────────────────────────────────────────

@pytest.mark.parametrize("tags,expected", [
    (["postgres:15-alpine"], True),
    (["redis:7"], True),
    (["grafana/grafana-oss:latest"], True),
    (["coolify:latest"], True),
    (["traefik:v3.6"], True),
    (["prometheus:latest"], True),
    (["alertmanager:latest"], True),
    (["dvq6dwsagsw4p4oqwuw7bak9_scheduler:abc123"], False),
    (["poupi-baby-backend:latest"], False),
    ([], False),
])
def test_is_protected_repo(tags, expected):
    assert _is_protected_repo(tags) == expected


def test_protected_repos_not_empty():
    assert len(PROTECTED_REPOS) >= 5


# ── Safety: _older_than ───────────────────────────────────────────────────────

def test_older_than_old_image():
    # Image from year 2020 is definitely older than a 3-day cutoff
    cutoff = time.time() - 3 * 86400
    assert _older_than("2020-01-01T00:00:00.000000000Z", cutoff) is True


def test_older_than_recent_image():
    # Image created 1 hour ago is NOT older than 3 days
    from datetime import datetime, timezone
    recent = datetime.now(tz=timezone.utc).isoformat()
    cutoff = time.time() - 3 * 86400
    assert _older_than(recent, cutoff) is False


def test_older_than_empty_string():
    assert _older_than("", time.time()) is False


# ── _mb helper ────────────────────────────────────────────────────────────────

def test_mb_conversion():
    assert _mb(1_048_576) == pytest.approx(1.0)
    assert _mb(0) == pytest.approx(0.0)
    assert _mb(10 * 1_048_576) == pytest.approx(10.0)


# ── TaskResult ────────────────────────────────────────────────────────────────

def test_task_result_to_dict():
    t = TaskResult(
        task="dangling_images",
        dry_run=True,
        items_processed=3,
        bytes_freed=50 * 1_048_576,
    )
    d = t.to_dict()
    assert d["task"] == "dangling_images"
    assert d["dry_run"] is True
    assert d["bytes_freed_mb"] == pytest.approx(50.0)


# ── CleanupReport ─────────────────────────────────────────────────────────────

def _sample_report(**kwargs) -> CleanupReport:
    defaults = dict(
        run_at="2026-06-08T23:00:00+00:00",
        dry_run=True,
        duration_seconds=1.5,
        disk_before_bytes=35 * 1_073_741_824,
        disk_after_bytes=32 * 1_073_741_824,
        bytes_freed=3 * 1_073_741_824,
    )
    defaults.update(kwargs)
    return CleanupReport(**defaults)


def test_cleanup_report_gb_properties():
    r = _sample_report(
        disk_before_bytes=35 * 1_073_741_824,
        disk_after_bytes=32 * 1_073_741_824,
        bytes_freed=3 * 1_073_741_824,
    )
    assert r.disk_before_gb == pytest.approx(35.0)
    assert r.disk_after_gb == pytest.approx(32.0)
    assert r.bytes_freed_mb == pytest.approx(3072.0)


def test_cleanup_report_to_dict():
    r = _sample_report()
    d = r.to_dict()
    assert "disk_before_gb" in d
    assert "bytes_freed_mb" in d
    assert d["dry_run"] is True


def test_cleanup_report_telegram_dry_run():
    r = _sample_report(dry_run=True, bytes_freed=500 * 1_048_576)
    text = r.to_telegram()
    assert "DRY RUN" in text
    assert "500" in text or "Estimado" in text


def test_cleanup_report_telegram_real_run():
    r = _sample_report(dry_run=False, bytes_freed=2 * 1_073_741_824, errors_total=0)
    r.tasks = [
        TaskResult(task="dangling_images", dry_run=False,
                   bytes_freed=500 * 1_048_576),
    ]
    text = r.to_telegram()
    assert "CLEANUP" in text
    assert "Liberado" in text


def test_cleanup_report_telegram_with_errors():
    r = _sample_report(errors_total=2)
    text = r.to_telegram()
    assert "Erros" in text or "2" in text


# ── DanglingImageCleanup ──────────────────────────────────────────────────────

def test_dangling_cleanup_dry_run_estimates():
    img1 = _mock_image("sha256:aaa", [], size_bytes=200 * 1_048_576)
    img2 = _mock_image("sha256:bbb", [], size_bytes=100 * 1_048_576)
    client = _mock_docker_client()
    client.images.list.return_value = [img1, img2]

    task = DanglingImageCleanup()
    result = task.run(client, dry_run=True)

    assert result.dry_run is True
    assert result.bytes_freed == 300 * 1_048_576
    assert result.items_processed == 2
    assert all("[DRY]" in d for d in result.detail)


def test_dangling_cleanup_real_prune():
    client = _mock_docker_client()
    client.images.prune.return_value = {
        "ImagesDeleted": [{"Deleted": "sha256:abc"}],
        "SpaceReclaimed": 150 * 1_048_576,
    }
    client.images.list.return_value = [_mock_image("sha256:aaa", [])]

    task = DanglingImageCleanup()
    result = task.run(client, dry_run=False)

    client.images.prune.assert_called_once()
    assert result.bytes_freed == 150 * 1_048_576
    assert result.dry_run is False


def test_dangling_cleanup_no_images():
    client = _mock_docker_client()
    client.images.list.return_value = []
    result = DanglingImageCleanup().run(client, dry_run=True)
    assert result.items_processed == 0
    assert result.bytes_freed == 0


# ── OldTaggedImageCleanup ─────────────────────────────────────────────────────

def test_old_tagged_skips_protected_repos():
    # postgres image should be skipped
    pg_img = _mock_image("sha256:pg1", ["postgres:15-alpine"],
                          size_bytes=400 * 1_048_576,
                          created="2020-01-01T00:00:00Z")
    client = _mock_docker_client(images=[pg_img])
    result = OldTaggedImageCleanup(retention_days=1).run(client, dry_run=True)
    assert result.bytes_freed == 0
    assert result.items_skipped >= 1


def test_old_tagged_skips_in_use():
    img = _mock_image("sha256:inuse", ["myapp:latest"],
                       size_bytes=300 * 1_048_576,
                       created="2020-01-01T00:00:00Z")
    container = _mock_container("sha256:inuse")
    client = _mock_docker_client(images=[img], containers=[container])
    result = OldTaggedImageCleanup(retention_days=1).run(client, dry_run=True)
    assert result.bytes_freed == 0
    assert result.items_skipped >= 1


def test_old_tagged_skips_recent_images():
    # Image created 1 hour ago, retention=3 days → should be kept
    from datetime import datetime, timezone
    recent = datetime.now(tz=timezone.utc).isoformat()
    img = _mock_image("sha256:recent", ["myapp:latest"],
                       size_bytes=300 * 1_048_576, created=recent)
    client = _mock_docker_client(images=[img])
    result = OldTaggedImageCleanup(retention_days=3).run(client, dry_run=True)
    assert result.bytes_freed == 0


def test_old_tagged_removes_old_image_dry_run():
    old_img = _mock_image("sha256:old", ["myapp:oldtag"],
                           size_bytes=500 * 1_048_576,
                           created="2020-01-01T00:00:00Z")
    # A second image in same repo (newer) to satisfy min-2 check
    # Make old_img NOT in the top-2 by having 2 newer images
    new_img1 = _mock_image("sha256:new1", ["myapp:v1"],
                            size_bytes=100 * 1_048_576,
                            created="2026-06-07T00:00:00Z")
    new_img2 = _mock_image("sha256:new2", ["myapp:v2"],
                            size_bytes=100 * 1_048_576,
                            created="2026-06-08T00:00:00Z")
    client = _mock_docker_client(images=[old_img, new_img1, new_img2])
    result = OldTaggedImageCleanup(retention_days=1).run(client, dry_run=True)
    # old_img should be candidate
    assert result.bytes_freed >= 500 * 1_048_576
    assert any("[DRY]" in d for d in result.detail)


# ── BuildCacheCleanup ─────────────────────────────────────────────────────────

def test_build_cache_dry_run():
    client = _mock_docker_client()
    client.df.return_value = {
        "BuildCache": [
            {"ID": "c1", "Size": 100 * 1_048_576, "InUse": False},
            {"ID": "c2", "Size": 200 * 1_048_576, "InUse": False},
            {"ID": "c3", "Size": 50 * 1_048_576, "InUse": True},
        ]
    }
    result = BuildCacheCleanup().run(client, dry_run=True)
    assert result.dry_run is True
    assert result.bytes_freed == 300 * 1_048_576  # InUse=True excluded
    assert "[DRY]" in result.detail[0]


def test_build_cache_real_prune():
    client = _mock_docker_client()
    client.api.prune_builds.return_value = {
        "CachesDeleted": ["id1", "id2", "id3"],
        "SpaceReclaimed": 800 * 1_048_576,
    }
    result = BuildCacheCleanup().run(client, dry_run=False)
    assert result.bytes_freed == 800 * 1_048_576
    assert result.items_processed == 3
    client.api.prune_builds.assert_called_once()


# ── ContainerLogAudit ─────────────────────────────────────────────────────────

def test_container_log_audit_no_crash():
    client = _mock_docker_client()
    containers = [_mock_container("sha256:c1", "test-ctr")]
    client.containers.list.return_value = containers
    client.api.inspect_container.return_value = {
        "LogPath": "/var/lib/docker/containers/abc/abc-json.log"
    }
    result = ContainerLogAudit().run(client, dry_run=True)
    # Log path not accessible → should not crash
    assert isinstance(result, TaskResult)
    assert result.bytes_freed == 0  # audit only, no deletion


# ── CleanupCooldown ───────────────────────────────────────────────────────────

def test_cooldown_not_active_initially():
    cooldown = CleanupCooldown()
    with patch("app.auto_healing.cleanup._redis", return_value=_fake_redis()):
        assert cooldown.is_active() is False


def test_cooldown_active_after_set():
    r = _fake_redis()
    cooldown = CleanupCooldown()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        cooldown.set(hours=6)
        assert cooldown.is_active() is True


def test_cooldown_cleared_by_clear():
    r = _fake_redis()
    cooldown = CleanupCooldown()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        cooldown.set(hours=6)
        cooldown.clear()
        assert cooldown.is_active() is False


# ── CleanupCircuitBreaker ─────────────────────────────────────────────────────

def test_circuit_breaker_opens_after_max_errors():
    r = _fake_redis()
    cb = CleanupCircuitBreaker()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        for _ in range(3):
            cb.record_error()
        assert cb.is_open() is True


def test_circuit_breaker_resets_on_success():
    r = _fake_redis()
    cb = CleanupCircuitBreaker()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        cb.record_error()
        cb.record_error()
        cb.record_success()
        assert cb.is_open() is False
        assert cb.consecutive_errors() == 0


def test_circuit_breaker_force_close():
    r = _fake_redis()
    cb = CleanupCircuitBreaker()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        for _ in range(3):
            cb.record_error()
        assert cb.is_open() is True
        cb.force_close()
        assert cb.is_open() is False


# ── CleanupMetrics ────────────────────────────────────────────────────────────

def test_metrics_record_and_read():
    r = _fake_redis()
    metrics = CleanupMetrics()
    report = _sample_report(bytes_freed=100 * 1_048_576, errors_total=1)
    report.skipped_total = 2
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        metrics.record(report)
        result = metrics.read()
    assert result["runs"] == 1
    assert result["errors"] == 1


def test_metrics_accumulate():
    r = _fake_redis()
    metrics = CleanupMetrics()
    report = _sample_report(bytes_freed=50 * 1_048_576, errors_total=0)
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        metrics.record(report)
        metrics.record(report)
        result = metrics.read()
    assert result["runs"] == 2
    assert result["bytes"] == 100 * 1_048_576


# ── CleanupRunner — safety gates ──────────────────────────────────────────────

def test_runner_blocked_by_cooldown():
    r = _fake_redis()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        # Activate cooldown first
        CleanupCooldown().set(hours=6)
        runner = CleanupRunner(dry_run=False)
        report = runner.run()
    assert report.cooldown_blocked is True
    assert report.bytes_freed == 0


def test_runner_blocked_by_circuit_breaker():
    r = _fake_redis()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        # Open circuit breaker
        cb = CleanupCircuitBreaker()
        for _ in range(3):
            cb.record_error()
        runner = CleanupRunner(dry_run=False)
        report = runner.run()
    assert report.circuit_breaker_triggered is True


def test_runner_dry_run_not_blocked_by_cooldown():
    """DRY_RUN runs must ALWAYS proceed regardless of cooldown."""
    r = _fake_redis()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        CleanupCooldown().set(hours=6)
    # DRY_RUN ignores cooldown
    with patch("app.auto_healing.cleanup._redis", return_value=r), \
         patch("docker.from_env", return_value=_mock_docker_client()):
        runner = CleanupRunner(dry_run=True)
        report = runner.run()
    assert report.cooldown_blocked is False
    assert report.dry_run is True


def test_runner_sets_cooldown_after_real_run():
    r = _fake_redis()
    client = _mock_docker_client()
    with patch("app.auto_healing.cleanup._redis", return_value=r), \
         patch("docker.from_env", return_value=client):
        runner = CleanupRunner(dry_run=False)
        runner.run()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        assert CleanupCooldown().is_active() is True


def test_runner_does_not_set_cooldown_for_dry_run():
    r = _fake_redis()
    client = _mock_docker_client()
    with patch("app.auto_healing.cleanup._redis", return_value=r), \
         patch("docker.from_env", return_value=client):
        runner = CleanupRunner(dry_run=True)
        runner.run()
    with patch("app.auto_healing.cleanup._redis", return_value=r):
        assert CleanupCooldown().is_active() is False


def test_runner_docker_connect_failure():
    r = _fake_redis()
    with patch("app.auto_healing.cleanup._redis", return_value=r), \
         patch("docker.from_env", side_effect=Exception("socket error")):
        runner = CleanupRunner(dry_run=True)
        report = runner.run()
    assert report.errors_total >= 1
    assert any("docker_connect" in t.task for t in report.tasks)


def test_runner_full_dry_run_returns_report():
    r = _fake_redis()
    client = _mock_docker_client()
    with patch("app.auto_healing.cleanup._redis", return_value=r), \
         patch("docker.from_env", return_value=client):
        runner = CleanupRunner(dry_run=True)
        report = runner.run()
    assert isinstance(report, CleanupReport)
    assert report.dry_run is True
    assert report.duration_seconds >= 0
    assert len(report.tasks) >= 3  # dangling, old_tagged, build_cache, log_audit
