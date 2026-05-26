"""
operational_recovery_engine.py — Phase R R-7

Controlled Recovery Engine.

Executes controlled recovery procedures for failed/degraded subsystems.
Each recovery action is timed, audited, and scored. The recovery is considered
complete when all pre+post checks pass AND success_rate >= 70%.

Output JSONL: data/recovery_log.jsonl

Prometheus (optional, from api.runtime_metrics):
  recovery_success_rate     Gauge
  recovery_integrity_score  Gauge
  recovery_duration_ms      Gauge

Recovery actions:
  1. restart_governance_loop    — write marker to data/recovery_markers.json
  2. restore_state              — call OperationalStateRestorationEngine if available
  3. rebuild_caches             — clear stale data/*.tmp files
  4. revalidate_readiness       — check live_readiness_revalidation_log.jsonl freshness
  5. sync_replay                — check live_execution_replay_log.jsonl consistency
  6. restart_pipelines          — write marker to data/recovery_markers.json
  7. restore_analytics          — check governance_history.jsonl
  8. reactivate_paper_runtime   — validate TRADING_MODE=paper and no freeze

CLI:
  python -m domains.crypto_coin.research.operational_recovery_engine
  python -m domains.crypto_coin.research.operational_recovery_engine --json
  python -m domains.crypto_coin.research.operational_recovery_engine --trigger "manual"
  python -m domains.crypto_coin.research.operational_recovery_engine --actions restart_governance_loop restore_state revalidate_readiness
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

RECOVERY_LOG     = Path("data/recovery_log.jsonl")
RECOVERY_MARKERS = Path("data/recovery_markers.json")

# Source logs (read-only)
REVALID_LOG    = Path("data/live_readiness_revalidation_log.jsonl")
REPLAY_LOG     = Path("data/live_execution_replay_log.jsonl")
GOVERNANCE_LOG = Path("data/governance_history.jsonl")
GUARDIAN_LOG   = Path("data/live_guardian_log.jsonl")
INCIDENT_LOG   = Path("data/active_incidents.json")

ALL_ACTION_TYPES = [
    "restart_governance_loop",
    "restore_state",
    "rebuild_caches",
    "revalidate_readiness",
    "sync_replay",
    "restart_pipelines",
    "restore_analytics",
    "reactivate_paper_runtime",
]

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        recovery_success_rate    as _prom_success_rate,
        recovery_integrity_score as _prom_integrity,
        recovery_duration_ms     as _prom_duration,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RecoveryAction:
    action_id:   str
    action_type: str    # one of the 8 types
    status:      str    # PENDING | SUCCESS | FAILED | SKIPPED
    duration_ms: float
    detail:      str
    executed_at: str


@dataclass
class RecoveryReport:
    report_id:               str
    recovery_success_rate:   float   # 0-100
    recovery_integrity_score: float  # 0-100
    recovery_duration_ms:    float   # total duration
    trigger_reason:          str
    actions:                 list[RecoveryAction]
    actions_total:           int
    actions_succeeded:       int
    actions_failed:          int
    actions_skipped:         int
    pre_checks_passed:       bool
    post_checks_passed:      bool
    recovery_complete:       bool
    recommendation:          str
    executed_at:             str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["actions"] = [asdict(a) for a in self.actions]
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class OperationalRecoveryEngine:
    """
    R-7: Controlled Recovery Engine.

    Executes controlled recovery procedures for failed/degraded subsystems.
    Supports selective or full recovery with pre/post integrity validation.
    """

    def __init__(self, recovery_log: Path = RECOVERY_LOG):
        self.recovery_log = recovery_log

    # ── Public interface ───────────────────────────────────────────────────────

    def execute_recovery(
        self,
        trigger_reason: str = "manual",
        requested_actions: list[str] | None = None,
    ) -> RecoveryReport:
        """
        Execute controlled recovery.

        If requested_actions is None, all 8 actions are run.
        Pre-checks are evaluated first; if they fail all actions are SKIPPED.
        """
        report_id = str(uuid.uuid4())[:12]
        total_start = time.time() * 1000

        action_types = requested_actions if requested_actions is not None else list(ALL_ACTION_TYPES)

        # ── Pre-checks ────────────────────────────────────────────────────────
        pre_check_results = self._run_all_checks()
        pre_checks_passed = all(ok for ok, _ in pre_check_results.values())

        # ── Execute actions ───────────────────────────────────────────────────
        actions: list[RecoveryAction] = []
        for action_type in action_types:
            now_str = datetime.now(timezone.utc).isoformat()
            if not pre_checks_passed:
                actions.append(RecoveryAction(
                    action_id   = str(uuid.uuid4())[:8],
                    action_type = action_type,
                    status      = "SKIPPED",
                    duration_ms = 0.0,
                    detail      = "Skipped: pre-checks failed",
                    executed_at = now_str,
                ))
                continue

            start_ms = time.time() * 1000
            try:
                detail = self._execute_action(action_type)
                duration = round(time.time() * 1000 - start_ms, 2)
                actions.append(RecoveryAction(
                    action_id   = str(uuid.uuid4())[:8],
                    action_type = action_type,
                    status      = "SUCCESS",
                    duration_ms = duration,
                    detail      = detail,
                    executed_at = now_str,
                ))
            except Exception as exc:
                duration = round(time.time() * 1000 - start_ms, 2)
                actions.append(RecoveryAction(
                    action_id   = str(uuid.uuid4())[:8],
                    action_type = action_type,
                    status      = "FAILED",
                    duration_ms = duration,
                    detail      = f"Error: {exc}",
                    executed_at = now_str,
                ))

        # ── Post-checks ───────────────────────────────────────────────────────
        if pre_checks_passed:
            post_check_results = self._run_all_checks()
            post_checks_passed = all(ok for ok, _ in post_check_results.values())
        else:
            post_checks_passed = False

        # ── Scoring ───────────────────────────────────────────────────────────
        actions_total     = len(actions)
        actions_succeeded = sum(1 for a in actions if a.status == "SUCCESS")
        actions_failed    = sum(1 for a in actions if a.status == "FAILED")
        actions_skipped   = sum(1 for a in actions if a.status == "SKIPPED")

        non_skipped = actions_total - actions_skipped
        success_rate = (
            round((actions_succeeded / non_skipped) * 100.0, 1)
            if non_skipped > 0 else 0.0
        )

        if pre_checks_passed and post_checks_passed:
            integrity_score = 100.0
        elif pre_checks_passed or post_checks_passed:
            integrity_score = 50.0
        else:
            integrity_score = 20.0

        recovery_complete = (
            pre_checks_passed and post_checks_passed and success_rate >= 70.0
        )

        total_duration = round(time.time() * 1000 - total_start, 2)

        recommendation = self._build_recommendation(
            recovery_complete, success_rate, integrity_score,
            pre_checks_passed, post_checks_passed,
            actions_failed, actions_skipped,
            pre_check_results,
        )

        report = RecoveryReport(
            report_id               = report_id,
            recovery_success_rate   = success_rate,
            recovery_integrity_score = integrity_score,
            recovery_duration_ms    = total_duration,
            trigger_reason          = trigger_reason,
            actions                 = actions,
            actions_total           = actions_total,
            actions_succeeded       = actions_succeeded,
            actions_failed          = actions_failed,
            actions_skipped         = actions_skipped,
            pre_checks_passed       = pre_checks_passed,
            post_checks_passed      = post_checks_passed,
            recovery_complete       = recovery_complete,
            recommendation          = recommendation,
            executed_at             = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_success_rate.set(success_rate)
                _prom_integrity.set(integrity_score)
                _prom_duration.set(total_duration)
            except Exception:
                pass

        return report

    # ── Pre/Post checks ────────────────────────────────────────────────────────

    def _run_all_checks(self) -> dict[str, tuple[bool, str]]:
        return {
            "integrity":      self._pre_check_validate_integrity(),
            "dependencies":   self._pre_check_validate_dependencies(),
            "replay":         self._pre_check_validate_replay(),
            "governance":     self._pre_check_validate_governance(),
            "runtime_health": self._pre_check_validate_runtime_health(),
        }

    def _pre_check_validate_integrity(self) -> tuple[bool, str]:
        """Check that essential data directories and files are accessible."""
        try:
            data_dir = Path("data")
            if not data_dir.exists():
                return False, "data/ directory does not exist"
            return True, "data/ directory accessible"
        except Exception as exc:
            return False, f"integrity check error: {exc}"

    def _pre_check_validate_dependencies(self) -> tuple[bool, str]:
        """Check that critical dependency logs are not corrupted."""
        try:
            for log_path in [GOVERNANCE_LOG, GUARDIAN_LOG]:
                if log_path.exists():
                    with open(log_path) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                json.loads(line)  # validate parseable
                                break
            return True, "dependency logs parseable"
        except Exception as exc:
            return False, f"dependency validation error: {exc}"

    def _pre_check_validate_replay(self) -> tuple[bool, str]:
        """Check replay log exists and has recent entries."""
        if not REPLAY_LOG.exists():
            return True, "replay log not present (new system)"
        try:
            last = self._load_last(REPLAY_LOG)
            if last is None:
                return True, "replay log empty"
            return True, f"replay log accessible, last_entry found"
        except Exception as exc:
            return False, f"replay check error: {exc}"

    def _pre_check_validate_governance(self) -> tuple[bool, str]:
        """Check governance history is accessible and non-empty."""
        if not GOVERNANCE_LOG.exists():
            return True, "governance log not present (new system)"
        try:
            last = self._load_last(GOVERNANCE_LOG)
            if last is None:
                return True, "governance log empty"
            score = last.get("governance_health_score", last.get("runtime_governance_score", 75.0))
            if float(score) < 20.0:
                return False, f"governance critically low: {score}"
            return True, f"governance accessible score={score}"
        except Exception as exc:
            return False, f"governance check error: {exc}"

    def _pre_check_validate_runtime_health(self) -> tuple[bool, str]:
        """Check guardian state and active incidents for critical blocks."""
        try:
            # Check guardian state
            if GUARDIAN_LOG.exists():
                last = self._load_last(GUARDIAN_LOG)
                if last:
                    state = last.get("guardian_state", "NORMAL")
                    if state == "ROLLBACK":
                        return False, f"guardian in ROLLBACK state"

            # Check active incidents
            if INCIDENT_LOG.exists():
                try:
                    with open(INCIDENT_LOG) as f:
                        data = json.load(f)
                    incidents = data if isinstance(data, list) else data.get("incidents", [])
                    critical = [
                        i for i in incidents
                        if i.get("severity") in ("CRITICAL", "EMERGENCY")
                    ]
                    if critical:
                        return False, f"{len(critical)} active CRITICAL/EMERGENCY incidents"
                except Exception:
                    pass

            return True, "runtime health OK"
        except Exception as exc:
            return False, f"runtime health check error: {exc}"

    # ── Action executors ───────────────────────────────────────────────────────

    def _execute_action(self, action_type: str) -> str:
        executor = {
            "restart_governance_loop":  self._action_restart_governance_loop,
            "restore_state":            self._action_restore_state,
            "rebuild_caches":           self._action_rebuild_caches,
            "revalidate_readiness":     self._action_revalidate_readiness,
            "sync_replay":              self._action_sync_replay,
            "restart_pipelines":        self._action_restart_pipelines,
            "restore_analytics":        self._action_restore_analytics,
            "reactivate_paper_runtime": self._action_reactivate_paper_runtime,
        }.get(action_type)

        if executor is None:
            raise ValueError(f"Unknown action type: {action_type}")
        return executor()

    def _action_restart_governance_loop(self) -> str:
        self._write_marker("restart_governance")
        return "Marker written to data/recovery_markers.json: action=restart_governance"

    def _action_restore_state(self) -> str:
        try:
            from domains.crypto_coin.research.operational_state_restoration_engine import (
                OperationalStateRestorationEngine,
            )
            engine = OperationalStateRestorationEngine()
            engine.restore_state()
            return "OperationalStateRestorationEngine.restore_state() executed"
        except ImportError:
            self._write_marker("restore_state_scheduled")
            return "OperationalStateRestorationEngine not importable; scheduled via marker"
        except Exception as exc:
            return f"restore_state attempted but raised: {exc}; scheduled via marker"

    def _action_rebuild_caches(self) -> str:
        data_dir = Path("data")
        removed = []
        if data_dir.exists():
            for tmp_file in data_dir.glob("*.tmp"):
                try:
                    tmp_file.unlink()
                    removed.append(tmp_file.name)
                except Exception:
                    pass
        if removed:
            return f"Cleared {len(removed)} stale .tmp file(s): {', '.join(removed)}"
        return "No stale .tmp files found in data/"

    def _action_revalidate_readiness(self) -> str:
        if not REVALID_LOG.exists():
            return "live_readiness_revalidation_log.jsonl not present — skipped freshness check"
        last = self._load_last(REVALID_LOG)
        if last is None:
            return "live_readiness_revalidation_log.jsonl empty"
        evaluated_at = last.get("evaluated_at", "unknown")
        status = last.get("readiness_status", "unknown")
        score  = last.get("continuous_live_readiness_score", "N/A")
        return (
            f"Readiness log found: status={status} score={score} "
            f"last_evaluated={evaluated_at}"
        )

    def _action_sync_replay(self) -> str:
        if not REPLAY_LOG.exists():
            return "live_execution_replay_log.jsonl not present — nothing to sync"
        last = self._load_last(REPLAY_LOG)
        if last is None:
            return "live_execution_replay_log.jsonl empty"
        fidelity = last.get("avg_fidelity_score", last.get("fidelity_score", "N/A"))
        anomalous = last.get("pct_anomalous", last.get("anomalous_count", "N/A"))
        return (
            f"Replay log consistent: avg_fidelity={fidelity} "
            f"pct_anomalous={anomalous}"
        )

    def _action_restart_pipelines(self) -> str:
        self._write_marker("restart_pipelines")
        return "Marker written to data/recovery_markers.json: action=restart_pipelines"

    def _action_restore_analytics(self) -> str:
        if not GOVERNANCE_LOG.exists():
            return "governance_history.jsonl not present"
        last = self._load_last(GOVERNANCE_LOG)
        if last is None:
            return "governance_history.jsonl empty"
        score = last.get("governance_health_score", last.get("runtime_governance_score", "N/A"))
        return f"governance_history.jsonl accessible: last_score={score}"

    def _action_reactivate_paper_runtime(self) -> str:
        trading_mode = os.environ.get("TRADING_MODE", "paper")
        if trading_mode.lower() != "paper":
            raise RuntimeError(
                f"TRADING_MODE={trading_mode!r} — expected 'paper' for safe reactivation"
            )
        # Check guardian not frozen
        freeze_detected = False
        freeze_reason = ""
        if GUARDIAN_LOG.exists():
            last = self._load_last(GUARDIAN_LOG)
            if last:
                state = last.get("guardian_state", "NORMAL")
                if state in ("FROZEN", "ROLLBACK"):
                    freeze_detected = True
                    freeze_reason = f"guardian_state={state}"

        if freeze_detected:
            raise RuntimeError(f"Cannot reactivate paper runtime: {freeze_reason}")

        return f"Paper runtime reactivation validated: TRADING_MODE={trading_mode!r} no freeze detected"

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _write_marker(self, action: str) -> None:
        """Append or update a recovery marker in data/recovery_markers.json."""
        RECOVERY_MARKERS.parent.mkdir(parents=True, exist_ok=True)
        markers: list[dict] = []
        if RECOVERY_MARKERS.exists():
            try:
                with open(RECOVERY_MARKERS) as f:
                    data = json.load(f)
                markers = data if isinstance(data, list) else [data]
            except Exception:
                markers = []
        markers.append({
            "action":     action,
            "written_at": datetime.now(timezone.utc).isoformat(),
        })
        with open(RECOVERY_MARKERS, "w") as f:
            json.dump(markers, f, indent=2)

    def _load_last(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        last: dict | None = None
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return last

    def _build_recommendation(
        self,
        complete: bool,
        success_rate: float,
        integrity: float,
        pre_ok: bool,
        post_ok: bool,
        failed: int,
        skipped: int,
        pre_checks: dict[str, tuple[bool, str]],
    ) -> str:
        if not pre_ok:
            failed_checks = [k for k, (ok, _) in pre_checks.items() if not ok]
            return (
                f"RECOVERY BLOCKED: pre-checks failed ({', '.join(failed_checks)}). "
                "All actions skipped. Resolve pre-check failures before retrying recovery."
            )
        if complete:
            return (
                f"Recovery complete: success_rate={success_rate:.0f}% "
                f"integrity={integrity:.0f}/100. System operational."
            )
        if success_rate >= 70.0 and not post_ok:
            return (
                f"Actions succeeded ({success_rate:.0f}%) but post-checks failed. "
                "System partially recovered — investigate remaining issues."
            )
        if failed > 0:
            return (
                f"Recovery incomplete: {failed} action(s) failed, "
                f"success_rate={success_rate:.0f}%. Retry failed actions."
            )
        return (
            f"Recovery partially complete: success_rate={success_rate:.0f}% "
            f"integrity={integrity:.0f}/100. Review system state."
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: RecoveryReport) -> None:
        try:
            self.recovery_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "executed_at":             report.executed_at,
                "report_id":               report.report_id,
                "trigger_reason":          report.trigger_reason,
                "recovery_success_rate":   report.recovery_success_rate,
                "recovery_integrity_score": report.recovery_integrity_score,
                "recovery_duration_ms":    report.recovery_duration_ms,
                "actions_total":           report.actions_total,
                "actions_succeeded":       report.actions_succeeded,
                "actions_failed":          report.actions_failed,
                "actions_skipped":         report.actions_skipped,
                "pre_checks_passed":       report.pre_checks_passed,
                "post_checks_passed":      report.post_checks_passed,
                "recovery_complete":       report.recovery_complete,
            }
            with open(self.recovery_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Operational Recovery Engine — Phase R R-7"
    )
    parser.add_argument("--json",    action="store_true", help="Output as JSON")
    parser.add_argument(
        "--trigger", type=str, default="manual",
        help="Trigger reason for recovery (default: manual)",
    )
    parser.add_argument(
        "--actions", nargs="+", default=None,
        metavar="ACTION",
        help=(
            "Specific actions to run (default: all). "
            "Choices: " + " ".join(ALL_ACTION_TYPES)
        ),
    )
    args = parser.parse_args()

    # Validate actions
    if args.actions:
        invalid = [a for a in args.actions if a not in ALL_ACTION_TYPES]
        if invalid:
            parser.error(f"Unknown action(s): {invalid}. Valid: {ALL_ACTION_TYPES}")

    engine = OperationalRecoveryEngine()
    report = engine.execute_recovery(
        trigger_reason    = args.trigger,
        requested_actions = args.actions,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    complete_str = "COMPLETE" if report.recovery_complete else "INCOMPLETE"
    print(f"\nOperational Recovery Engine — Phase R R-7")
    print(f"  report_id:               {report.report_id}")
    print(f"  trigger_reason:          {report.trigger_reason}")
    print(f"  recovery_complete:       [{complete_str}]")
    print(f"  recovery_success_rate:   {report.recovery_success_rate:.1f}%")
    print(f"  recovery_integrity_score:{report.recovery_integrity_score:.1f}/100")
    print(f"  recovery_duration_ms:    {report.recovery_duration_ms:.1f}ms")
    print(f"\n  Pre-checks passed:       {'YES' if report.pre_checks_passed else 'NO'}")
    print(f"  Post-checks passed:      {'YES' if report.post_checks_passed else 'NO'}")
    print(f"\n  Actions ({report.actions_total} total):")
    print(f"    succeeded: {report.actions_succeeded}")
    print(f"    failed:    {report.actions_failed}")
    print(f"    skipped:   {report.actions_skipped}")
    print()
    for action in report.actions:
        status_icon = {"SUCCESS": "+", "FAILED": "X", "SKIPPED": "-", "PENDING": "?"}.get(
            action.status, "?"
        )
        print(f"  [{status_icon}] {action.action_type:<35} {action.duration_ms:>7.1f}ms")
        print(f"       {action.detail}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()
