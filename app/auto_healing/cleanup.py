"""Phase 6A: Safe automated VPS cleanup.

Tasks (in execution order):
1. DanglingImageCleanup     — removes untagged / dangling Docker images
2. OldTaggedImageCleanup    — removes old tagged images not used by any container,
                              respecting retention_days and protected repos
3. BuildCacheCleanup        — clears Docker BuildKit cache (prune_builds)
4. ContainerLogAudit        — measures container log sizes (audit only; no deletion
                               because /var/lib/docker is not mounted inside container)

Safety guarantees (hard-coded, not configurable):
- NEVER removes Docker volumes
- NEVER removes running containers (or any container)
- NEVER removes images used by running OR stopped containers
- NEVER removes images in PROTECTED_REPOS (postgres/redis/grafana/coolify/…)
- NEVER removes heal history file
- NEVER removes images that are the 2 most-recent per repo

Controls:
- DRY_RUN=True  → compute estimate only, zero deletions
- CleanupCooldown       — Redis TTL key, default 6 h between real runs
- CleanupCircuitBreaker — opens after N consecutive errors, auto-resets after 24 h
- CleanupMetrics        — Redis counters in DB2
- CleanupAuditLogger    — Redis list + JSONL file per run
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Repos that must NEVER be removed — ever.
PROTECTED_REPOS: frozenset[str] = frozenset({
    "postgres",
    "redis",
    "grafana",
    "coolify",
    "traefik",
    "prometheus",
    "alertmanager",
    "loki",
    "caddy",
    "nginx",
    "prom",
})

# Paths that must never be deleted by cleanup.
PROTECTED_PATHS: frozenset[str] = frozenset({
    "auto_healing_watchdog.jsonl",   # heal history
    "/var/lib/docker/volumes",       # Docker volumes
})

# Redis key prefix
_CL = "auto_heal:cleanup:"

# Cooldown: minimum hours between real (non-dry) runs
_COOLDOWN_HOURS = 6.0

# Circuit breaker: open after this many consecutive errors
_CB_MAX_ERRORS = 3

# Minimum recent images to KEEP per repo (regardless of age)
_KEEP_MIN_IMAGES_PER_REPO = 2

# Images older than this are candidates for removal (days)
_DEFAULT_RETENTION_DAYS = 3


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    task: str
    dry_run: bool
    items_processed: int = 0
    items_skipped: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bytes_freed_mb"] = round(self.bytes_freed / 1_048_576, 2)
        return d


@dataclass
class CleanupReport:
    run_at: str
    dry_run: bool
    duration_seconds: float
    disk_before_bytes: int
    disk_after_bytes: int
    bytes_freed: int
    tasks: list[TaskResult] = field(default_factory=list)
    errors_total: int = 0
    skipped_total: int = 0
    telegram_sent: bool = False
    circuit_breaker_triggered: bool = False
    cooldown_blocked: bool = False

    @property
    def disk_before_gb(self) -> float:
        return round(self.disk_before_bytes / 1_073_741_824, 2)

    @property
    def disk_after_gb(self) -> float:
        return round(self.disk_after_bytes / 1_073_741_824, 2)

    @property
    def bytes_freed_mb(self) -> float:
        return round(self.bytes_freed / 1_048_576, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["disk_before_gb"] = self.disk_before_gb
        d["disk_after_gb"] = self.disk_after_gb
        d["bytes_freed_mb"] = self.bytes_freed_mb
        return d

    def to_telegram(self) -> str:
        mode = "🔍 DRY RUN" if self.dry_run else "🧹 CLEANUP"
        lines = [
            f"{mode} — AutoHealing Disk Cleanup",
            f"📅 {self.run_at[:19]}Z",
            "",
            f"💾 Antes:     {self.disk_before_gb:.1f} GB usados",
        ]
        if not self.dry_run:
            lines.append(f"💾 Depois:    {self.disk_after_gb:.1f} GB usados")
        freed_label = "Estimado" if self.dry_run else "Liberado"
        lines.append(f"✅ {freed_label}: {self.bytes_freed_mb:.0f} MB")
        if self.errors_total > 0:
            lines.append(f"❌ Erros:     {self.errors_total}")
        lines.append(f"⏱  Duração:   {self.duration_seconds:.1f}s")
        if self.tasks:
            lines.append("")
            for t in self.tasks:
                if t.bytes_freed > 0 or t.errors:
                    mb = round(t.bytes_freed / 1_048_576, 1)
                    err = f" ({len(t.errors)} erros)" if t.errors else ""
                    lines.append(f"  • {t.task}: {mb} MB{err}")
        return "\n".join(lines)


@dataclass
class CleanupStatus:
    last_run_at: str | None
    last_dry_run_at: str | None
    cooldown_active: bool
    cooldown_remaining_seconds: float | None
    circuit_breaker_open: bool
    circuit_consecutive_errors: int
    metrics: dict
    last_report: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Redis helper ──────────────────────────────────────────────────────────────

def _redis():
    import redis as redis_lib  # noqa: PLC0415

    from core.config import settings  # noqa: PLC0415
    return redis_lib.from_url(settings.redis_url, socket_connect_timeout=2, decode_responses=True)


def _disk_used_bytes() -> int:
    """Return bytes used on root filesystem."""
    try:
        return shutil.disk_usage("/").used
    except Exception:
        return 0


# ── Metrics ───────────────────────────────────────────────────────────────────

class CleanupMetrics:
    """Redis counters for cleanup operations."""

    _KEYS = {
        "runs": _CL + "runs_total",
        "bytes": _CL + "bytes_total",
        "skipped": _CL + "skipped_total",
        "errors": _CL + "errors_total",
    }

    def record(self, report: CleanupReport) -> None:
        try:
            r = _redis()
            r.incr(self._KEYS["runs"])
            r.incrby(self._KEYS["bytes"], max(report.bytes_freed, 0))
            r.incrby(self._KEYS["skipped"], report.skipped_total)
            r.incrby(self._KEYS["errors"], report.errors_total)
        except Exception as exc:
            logger.warning("CleanupMetrics.record failed: %s", exc)

    def read(self) -> dict:
        try:
            r = _redis()
            keys = list(self._KEYS.values())
            vals = r.mget(keys)
            return {
                k: int(v or 0)
                for k, v in zip(self._KEYS.keys(), vals, strict=False)
            }
        except Exception:
            return {k: 0 for k in self._KEYS}


# ── Cooldown ──────────────────────────────────────────────────────────────────

class CleanupCooldown:
    """Prevent running real cleanup more than once per COOLDOWN_HOURS."""

    _KEY = _CL + "cooldown"

    def is_active(self) -> bool:
        try:
            r = _redis()
            return r.exists(self._KEY) == 1
        except Exception:
            return False

    def remaining_seconds(self) -> float | None:
        try:
            r = _redis()
            ttl = r.ttl(self._KEY)
            return float(ttl) if ttl > 0 else None
        except Exception:
            return None

    def set(self, hours: float = _COOLDOWN_HOURS) -> None:
        try:
            r = _redis()
            r.setex(self._KEY, int(hours * 3600), "1")
        except Exception as exc:
            logger.warning("CleanupCooldown.set failed: %s", exc)

    def clear(self) -> None:
        """Clear cooldown — for testing or forced re-run."""
        try:
            _redis().delete(self._KEY)
        except Exception:
            pass


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CleanupCircuitBreaker:
    """Opens after N consecutive errors; auto-resets after 24h."""

    _OPEN_KEY = _CL + "circuit_open"
    _ERRORS_KEY = _CL + "circuit_errors"
    _OPEN_TTL = 86400  # 24h auto-reset

    def is_open(self) -> bool:
        try:
            return _redis().exists(self._OPEN_KEY) == 1
        except Exception:
            return False

    def consecutive_errors(self) -> int:
        try:
            return int(_redis().get(self._ERRORS_KEY) or 0)
        except Exception:
            return 0

    def record_success(self) -> None:
        try:
            r = _redis()
            r.delete(self._ERRORS_KEY)
            r.delete(self._OPEN_KEY)
        except Exception:
            pass

    def record_error(self) -> None:
        try:
            r = _redis()
            errors = r.incr(self._ERRORS_KEY)
            if errors >= _CB_MAX_ERRORS:
                r.setex(self._OPEN_KEY, self._OPEN_TTL, "1")
                logger.warning(
                    "cleanup: circuit breaker OPENED after %d consecutive errors", errors
                )
        except Exception as exc:
            logger.warning("CleanupCircuitBreaker.record_error failed: %s", exc)

    def force_close(self) -> None:
        """Manually reset the circuit breaker."""
        try:
            r = _redis()
            r.delete(self._OPEN_KEY)
            r.delete(self._ERRORS_KEY)
        except Exception:
            pass


# ── Audit Logger ──────────────────────────────────────────────────────────────

class CleanupAuditLogger:
    """Writes audit entries to Redis list + JSONL file."""

    _LIST_KEY = _CL + "audit"
    _MAX_ENTRIES = 50
    _LAST_REPORT_KEY = _CL + "last_report"
    _LAST_DRY_KEY = _CL + "last_dry_run_at"
    _LAST_REAL_KEY = _CL + "last_run_at"

    def save(self, report: CleanupReport) -> None:
        try:
            r = _redis()
            entry = json.dumps({
                "run_at": report.run_at,
                "dry_run": report.dry_run,
                "bytes_freed": report.bytes_freed,
                "errors": report.errors_total,
                "duration_seconds": round(report.duration_seconds, 1),
            })
            r.lpush(self._LIST_KEY, entry)
            r.ltrim(self._LIST_KEY, 0, self._MAX_ENTRIES - 1)
            r.set(self._LAST_REPORT_KEY, json.dumps(report.to_dict()))
            if report.dry_run:
                r.set(self._LAST_DRY_KEY, report.run_at)
            else:
                r.set(self._LAST_REAL_KEY, report.run_at)
        except Exception as exc:
            logger.warning("CleanupAuditLogger.save failed: %s", exc)

    def last_run_at(self) -> str | None:
        try:
            return _redis().get(self._LAST_REAL_KEY)
        except Exception:
            return None

    def last_dry_run_at(self) -> str | None:
        try:
            return _redis().get(self._LAST_DRY_KEY)
        except Exception:
            return None

    def last_report(self) -> dict | None:
        try:
            raw = _redis().get(self._LAST_REPORT_KEY)
            return json.loads(raw) if raw else None
        except Exception:
            return None


# ── Task: Dangling Image Cleanup ──────────────────────────────────────────────

class DanglingImageCleanup:
    """Remove untagged (dangling) Docker images."""

    name = "dangling_images"

    def run(self, client, dry_run: bool = True) -> TaskResult:
        result = TaskResult(task=self.name, dry_run=dry_run)
        try:
            dangling = client.images.list(filters={"dangling": True})
            result.items_processed = len(dangling)

            if not dangling:
                result.detail.append("no dangling images found")
                return result

            if dry_run:
                # Estimate size from image metadata
                for img in dangling:
                    size = img.attrs.get("Size", 0)
                    result.bytes_freed += size
                    result.detail.append(
                        f"[DRY] would remove {img.id[:12]} ({_mb(size)} MB)"
                    )
                return result

            # Real prune
            prune_result = client.images.prune(filters={"dangling": True})
            result.bytes_freed = prune_result.get("SpaceReclaimed", 0)
            deleted = prune_result.get("ImagesDeleted") or []
            result.items_processed = len(deleted)
            for d in deleted:
                tag = d.get("Untagged") or d.get("Deleted") or ""
                result.detail.append(f"removed {tag[:40]}")

        except Exception as exc:
            result.errors.append(str(exc))
            logger.error("DanglingImageCleanup failed: %s", exc)
        return result


# ── Task: Old Tagged Image Cleanup ────────────────────────────────────────────

class OldTaggedImageCleanup:
    """Remove tagged images older than retention_days that are not in use."""

    name = "old_tagged_images"

    def __init__(self, retention_days: int = _DEFAULT_RETENTION_DAYS):
        self.retention_days = retention_days

    def run(self, client, dry_run: bool = True) -> TaskResult:
        result = TaskResult(task=self.name, dry_run=dry_run)
        try:
            all_images = client.images.list()
            all_containers = client.containers.list(all=True)

            # Build set of image IDs currently in use (running + stopped)
            in_use_ids: set[str] = {c.image.id for c in all_containers}

            # Build per-repo sorted list of images (newest first) to protect recent ones
            repo_images: dict[str, list] = {}
            for img in all_images:
                for tag in img.tags:
                    repo = tag.rsplit(":", 1)[0]
                    repo_images.setdefault(repo, []).append(img)

            # Sort each repo's images by creation time descending
            for repo in repo_images:
                try:
                    repo_images[repo].sort(
                        key=lambda i: i.attrs.get("Created", ""), reverse=True
                    )
                except Exception:
                    pass  # attrs race — leave order as-is, protected_ids may be incomplete

            # Build set of protected image IDs (most recent N per repo)
            protected_ids: set[str] = set()
            for _repo, imgs in repo_images.items():
                for img in imgs[:_KEEP_MIN_IMAGES_PER_REPO]:
                    protected_ids.add(img.id)

            cutoff_ts = time.time() - self.retention_days * 86400
            candidates: list = []

            for img in all_images:
                try:
                    if not img.tags:
                        continue  # dangling — handled by DanglingImageCleanup

                    if img.id in in_use_ids:
                        result.items_skipped += 1
                        continue

                    if img.id in protected_ids:
                        result.items_skipped += 1
                        continue

                    if _is_protected_repo(img.tags):
                        result.items_skipped += 1
                        continue

                    # Check age via "Created" field (ISO string)
                    # attrs may trigger a Docker API call — guard against 404 race
                    created_str = img.attrs.get("Created", "")
                    if not _older_than(created_str, cutoff_ts):
                        result.items_skipped += 1
                        continue

                    candidates.append(img)
                except Exception:
                    # Image disappeared between list() and attrs access — skip
                    result.items_skipped += 1
                    continue

            result.items_processed = len(candidates)

            for img in candidates:
                size = img.attrs.get("Size", 0)
                tags_str = ", ".join(img.tags[:2])

                if dry_run:
                    result.bytes_freed += size
                    result.detail.append(
                        f"[DRY] would remove {tags_str[:60]} ({_mb(size)} MB)"
                    )
                else:
                    try:
                        client.images.remove(image=img.id, force=False, noprune=False)
                        result.bytes_freed += size
                        result.detail.append(f"removed {tags_str[:60]} ({_mb(size)} MB)")
                    except Exception as exc:
                        result.errors.append(f"{tags_str[:40]}: {exc}")
                        result.items_skipped += 1

        except Exception as exc:
            result.errors.append(str(exc))
            logger.error("OldTaggedImageCleanup failed: %s", exc)
        return result


# ── Task: Build Cache Cleanup ─────────────────────────────────────────────────

class BuildCacheCleanup:
    """Remove Docker BuildKit cache."""

    name = "build_cache"

    def __init__(self, keep_storage_bytes: int = 0):
        # keep_storage=0 means remove all
        self.keep_storage_bytes = keep_storage_bytes

    def run(self, client, dry_run: bool = True) -> TaskResult:
        result = TaskResult(task=self.name, dry_run=dry_run)
        try:
            if dry_run:
                # Estimate build cache size
                try:
                    info = client.df()  # disk usage API
                    build_cache = info.get("BuildCache") or []
                    total_size = sum(
                        c.get("Size", 0)
                        for c in build_cache
                        if not c.get("InUse", False)
                    )
                    result.bytes_freed = total_size
                    result.items_processed = len(build_cache)
                    result.detail.append(
                        f"[DRY] build cache: {len(build_cache)} entries, "
                        f"~{_mb(total_size)} MB reclaimable"
                    )
                except Exception as exc:
                    result.detail.append(f"[DRY] could not estimate build cache: {exc}")
                return result

            # Real prune
            prune_result = client.api.prune_builds(
                keep_storage=self.keep_storage_bytes
            )
            result.bytes_freed = prune_result.get("SpaceReclaimed", 0)
            deleted = prune_result.get("CachesDeleted") or []
            result.items_processed = len(deleted)
            result.detail.append(
                f"cleared {len(deleted)} cache entries, "
                f"reclaimed {_mb(result.bytes_freed)} MB"
            )

        except Exception as exc:
            result.errors.append(str(exc))
            logger.error("BuildCacheCleanup failed: %s", exc)
        return result


# ── Task: Container Log Audit ─────────────────────────────────────────────────

class ContainerLogAudit:
    """Measure container log sizes (audit only — no deletion).

    /var/lib/docker/containers is not accessible from inside the container.
    We report log paths from Docker inspect for operator awareness.
    """

    name = "container_log_audit"

    def run(self, client, dry_run: bool = True) -> TaskResult:
        result = TaskResult(task=self.name, dry_run=dry_run)
        try:
            containers = client.containers.list(all=False)  # running only for audit
            large_logs: list[tuple[str, str, int]] = []

            for c in containers:
                try:
                    info = client.api.inspect_container(c.id)
                    log_path = info.get("LogPath", "")
                    if not log_path:
                        continue
                    # Try to read size (only works if /var/lib/docker is mounted)
                    try:
                        size = os.path.getsize(log_path)
                        if size > 50 * 1_048_576:  # > 50 MB
                            large_logs.append((c.name, log_path, size))
                        result.bytes_freed += 0  # audit only
                    except OSError:
                        # Expected: host path not accessible from container
                        result.detail.append(
                            f"{c.name[:30]}: log at {log_path[:50]} (size unavailable)"
                        )
                except Exception:
                    pass

            result.items_processed = len(containers)
            for name, path, size in large_logs:
                result.detail.append(
                    f"LARGE LOG {name[:30]}: {_mb(size):.0f} MB → {path[:50]}"
                )
                result.items_skipped += 1  # skipped (needs host access)

        except Exception as exc:
            result.errors.append(str(exc))
        return result


# ── Main Runner ───────────────────────────────────────────────────────────────

class CleanupRunner:
    """Orchestrates all cleanup tasks with safety checks, cooldown, circuit breaker."""

    def __init__(
        self,
        dry_run: bool = True,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        cooldown_hours: float = _COOLDOWN_HOURS,
        send_telegram: bool = False,
    ):
        self.dry_run = dry_run
        self.retention_days = retention_days
        self.cooldown_hours = cooldown_hours
        self.send_telegram = send_telegram

        self._metrics = CleanupMetrics()
        self._cooldown = CleanupCooldown()
        self._cb = CleanupCircuitBreaker()
        self._audit = CleanupAuditLogger()

    def status(self) -> CleanupStatus:
        return CleanupStatus(
            last_run_at=self._audit.last_run_at(),
            last_dry_run_at=self._audit.last_dry_run_at(),
            cooldown_active=self._cooldown.is_active(),
            cooldown_remaining_seconds=self._cooldown.remaining_seconds(),
            circuit_breaker_open=self._cb.is_open(),
            circuit_consecutive_errors=self._cb.consecutive_errors(),
            metrics=self._metrics.read(),
            last_report=self._audit.last_report(),
        )

    def run(self) -> CleanupReport:
        """Execute cleanup pipeline. Respects cooldown and circuit breaker."""
        import docker as docker_lib

        start = time.monotonic()
        now_str = datetime.now(tz=timezone.utc).isoformat()
        disk_before = _disk_used_bytes()

        report = CleanupReport(
            run_at=now_str,
            dry_run=self.dry_run,
            duration_seconds=0.0,
            disk_before_bytes=disk_before,
            disk_after_bytes=disk_before,
            bytes_freed=0,
        )

        # ── Safety gate: circuit breaker ───────────────────────────────────────
        if not self.dry_run and self._cb.is_open():
            report.circuit_breaker_triggered = True
            report.tasks.append(TaskResult(
                task="circuit_breaker",
                dry_run=self.dry_run,
                errors=["Circuit breaker is OPEN — cleanup blocked. Use force_reset to clear."],
            ))
            report.errors_total = 1
            report.duration_seconds = round(time.monotonic() - start, 2)
            self._audit.save(report)
            return report

        # ── Safety gate: cooldown ──────────────────────────────────────────────
        if not self.dry_run and self._cooldown.is_active():
            remaining = self._cooldown.remaining_seconds() or 0
            report.cooldown_blocked = True
            report.tasks.append(TaskResult(
                task="cooldown",
                dry_run=self.dry_run,
                detail=[f"Cooldown active — {remaining:.0f}s remaining. Use force to override."],
            ))
            report.skipped_total = 1
            report.duration_seconds = round(time.monotonic() - start, 2)
            self._audit.save(report)
            return report

        # ── Execute tasks ──────────────────────────────────────────────────────
        try:
            client = docker_lib.from_env()
        except Exception as exc:
            report.errors_total = 1
            report.tasks.append(TaskResult(
                task="docker_connect",
                dry_run=self.dry_run,
                errors=[f"Could not connect to Docker: {exc}"],
            ))
            report.duration_seconds = round(time.monotonic() - start, 2)
            self._audit.save(report)
            self._cb.record_error()
            return report

        tasks = [
            DanglingImageCleanup(),
            OldTaggedImageCleanup(retention_days=self.retention_days),
            BuildCacheCleanup(),
            ContainerLogAudit(),
        ]

        has_errors = False
        for task in tasks:
            try:
                task_result = task.run(client=client, dry_run=self.dry_run)
                report.tasks.append(task_result)
                report.bytes_freed += task_result.bytes_freed
                report.skipped_total += task_result.items_skipped
                if task_result.errors:
                    report.errors_total += len(task_result.errors)
                    has_errors = True
                logger.info(
                    "cleanup[%s] dry=%s freed=%dB errors=%d",
                    task.name, self.dry_run,
                    task_result.bytes_freed, len(task_result.errors),
                )
            except Exception as exc:
                err_result = TaskResult(
                    task=task.name if hasattr(task, "name") else "unknown",
                    dry_run=self.dry_run,
                    errors=[str(exc)],
                )
                report.tasks.append(err_result)
                report.errors_total += 1
                has_errors = True
                logger.error("cleanup task %s crashed: %s", getattr(task, "name", "?"), exc)

        # ── Post-run ───────────────────────────────────────────────────────────
        report.disk_after_bytes = _disk_used_bytes()

        # Adjust bytes_freed from actual disk delta if we have it
        if not self.dry_run and report.disk_before_bytes > 0:
            actual_freed = max(
                report.disk_before_bytes - report.disk_after_bytes, 0
            )
            if actual_freed > 0:
                report.bytes_freed = actual_freed

        report.duration_seconds = round(time.monotonic() - start, 2)

        # Circuit breaker update
        if has_errors:
            self._cb.record_error()
        else:
            self._cb.record_success()

        # Cooldown set (real runs only)
        if not self.dry_run:
            self._cooldown.set(hours=self.cooldown_hours)

        # Metrics
        self._metrics.record(report)

        # Audit
        self._audit.save(report)

        # Telegram
        if self.send_telegram:
            report.telegram_sent = self._notify(report)

        return report

    def _notify(self, report: CleanupReport) -> bool:
        """Send Telegram message — fail-silent."""
        try:
            import json as json_lib
            import urllib.request

            from core.config import settings
            if not settings.telegram_enabled or not settings.telegram_bot_token:
                return False
            chat_id = settings.telegram_system_chat_id or settings.telegram_chat_id
            if not chat_id:
                return False
            text = report.to_telegram()
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            body = json_lib.dumps({
                "chat_id": chat_id, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
            }).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.getcode() == 200
        except Exception as exc:
            logger.warning("cleanup: telegram notify failed: %s", exc)
            return False


# ── Utility helpers ────────────────────────────────────────────────────────────

def _mb(b: int) -> float:
    return round(b / 1_048_576, 1)


def _is_protected_repo(tags: list[str]) -> bool:
    """Return True if any tag belongs to a protected repository."""
    for tag in tags:
        # tag format: "repo:version" or "registry/repo:version"
        repo_part = tag.split(":")[0].lower()
        name = repo_part.split("/")[-1]
        full = repo_part
        if any(p in name or p in full for p in PROTECTED_REPOS):
            return True
    return False


def _older_than(created_str: str, cutoff_ts: float) -> bool:
    """Return True if the image creation time is before cutoff_ts."""
    if not created_str:
        return False
    try:
        # Created is like "2026-06-01T12:00:00.123456789Z"
        # datetime.fromisoformat doesn't handle nanoseconds
        ts_str = created_str[:26].rstrip("Z").rstrip("0").rstrip(".")
        if ts_str.endswith("+00:00"):
            pass
        else:
            ts_str += "+00:00"
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp() < cutoff_ts
    except Exception:
        return False
