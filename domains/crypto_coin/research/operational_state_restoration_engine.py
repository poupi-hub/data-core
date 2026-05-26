"""
operational_state_restoration_engine.py — Phase R R-2

State Persistence & Restoration Engine.

Persiste e restaura todo o estado operacional do sistema para recuperacao
deterministica apos reboot, crash ou novo deploy. Garante continuidade
de sessao sem perda de contexto critico.

Scores produzidos:
  - restoration_integrity_score: integridade da restauracao (0-100)
  - state_consistency_score:     consistencia interna do estado (0-100)
  - replay_recovery_score:       qualidade da recuperacao de replay (0-100)

Estado persistido em data/operational_state.json:
  - governance_state, guardian_state, readiness_state
  - freeze_state, trading_mode, replay_offset
  - scheduler_state, pipeline_state
  - virtual_positions, analytics_checkpoint
  - scores e contadores de sessao

CLI:
  python -m domains.crypto_coin.research.operational_state_restoration_engine
  python -m domains.crypto_coin.research.operational_state_restoration_engine --json
  python -m domains.crypto_coin.research.operational_state_restoration_engine --persist
  python -m domains.crypto_coin.research.operational_state_restoration_engine --restore
"""

from __future__ import annotations

import json
import time
import uuid
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPERATIONAL_STATE = Path("data/operational_state.json")
RESTORATION_LOG   = Path("data/state_restoration_log.jsonl")

# Source logs lidos para popular estado ao persistir
GUARDIAN_LOG      = Path("data/live_guardian_log.jsonl")
GOVERNANCE_LOG    = Path("data/live_governance_summary.jsonl")
READINESS_LOG     = Path("data/live_readiness_revalidation_log.jsonl")
REPLAY_LOG        = Path("data/live_execution_replay_log.jsonl")

STATE_SCHEMA_VERSION = "1.0"

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        restoration_integrity_score as _prom_integrity,
        state_consistency_score     as _prom_consistency,
        replay_recovery_score       as _prom_replay,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Default state ─────────────────────────────────────────────────────────────

DEFAULT_STATE: dict[str, Any] = {
    "schema_version":             STATE_SCHEMA_VERSION,
    "persisted_at":               "",
    "governance_state":           "PAPER_ACTIVE",
    "guardian_state":             "NORMAL",
    "readiness_state":            "GREEN",
    "freeze_state":               False,
    "trading_mode":               "paper",
    "replay_offset":              0,
    "scheduler_state":            "running",
    "pipeline_state":             "active",
    "virtual_positions":          {},
    "analytics_checkpoint":       None,
    "last_governance_score":      0.0,
    "last_readiness_score":       0.0,
    "last_guardian_emergency_level": 0,
    "incident_count":             0,
    "recovery_count":             0,
    "session_start":              "",
    "uptime_seconds":             0.0,
}

# Regras de consistencia: (campo_a, valor_a) → campo_b NAO deve ser valor_b
CONSISTENCY_RULES: list[tuple[str, Any, str, Any, str]] = [
    # (campo, valor, outro_campo, valor_invalido, descricao)
    ("freeze_state", True,  "guardian_state",  "NORMAL",
     "freeze_state=True mas guardian_state=NORMAL (deveria ser FROZEN ou CONTRACTING)"),
    ("freeze_state", True,  "scheduler_state", "running",
     "freeze_state=True mas scheduler_state=running (deveria ser stopped)"),
    ("freeze_state", True,  "pipeline_state",  "active",
     "freeze_state=True mas pipeline_state=active (deveria ser paused)"),
    ("trading_mode", "live", "readiness_state", "RED",
     "trading_mode=live mas readiness_state=RED (unsafe combination)"),
    ("trading_mode", "live", "freeze_state",    True,
     "trading_mode=live mas freeze_state=True (live bloqueado)"),
    ("governance_state", "FROZEN", "scheduler_state", "running",
     "governance_state=FROZEN mas scheduler_state=running (inconsistente)"),
    ("guardian_state", "ROLLBACK", "pipeline_state",  "active",
     "guardian_state=ROLLBACK mas pipeline_state=active (rollback nao completado)"),
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class RestorationReport:
    """Relatorio de restauracao de estado operacional."""
    report_id:                   str
    restoration_integrity_score: float   # 0-100
    state_consistency_score:     float   # 0-100
    replay_recovery_score:       float   # 0-100
    state_found:                 bool
    state_age_seconds:           float
    governance_state_restored:   str
    guardian_state_restored:     str
    readiness_state_restored:    str
    freeze_state_restored:       bool
    trading_mode_restored:       str
    replay_offset_restored:      int
    inconsistencies_found:       list[str]
    restoration_actions:         list[str]
    recommendation:              str
    restored_at:                 str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Engine ────────────────────────────────────────────────────────────────────

class OperationalStateRestorationEngine:
    """
    R-2: Motor de persistencia e restauracao de estado operacional.

    Garante que todos os estados criticos sejam persistidos de forma atomica
    e restaurados corretamente apos qualquer tipo de interrupcao.
    """

    def __init__(
        self,
        state_file:      Path = OPERATIONAL_STATE,
        restoration_log: Path = RESTORATION_LOG,
    ):
        self.state_file      = state_file
        self.restoration_log = restoration_log

    # ── Persist ───────────────────────────────────────────────────────────────

    def persist_state(self, state_overrides: dict | None = None) -> dict:
        """
        Persiste estado operacional atual em operational_state.json.

        Le dados atuais dos JSONLs de runtime e aplica state_overrides.
        Retorna o estado persistido.
        """
        if state_overrides is None:
            state_overrides = {}

        state = dict(DEFAULT_STATE)

        # Popula a partir dos logs de runtime (ultimo registro de cada)
        guardian_rec  = self._read_last(GUARDIAN_LOG)
        gov_rec       = self._read_last(GOVERNANCE_LOG)
        readiness_rec = self._read_last(READINESS_LOG)
        replay_rec    = self._read_last(REPLAY_LOG)

        if guardian_rec:
            state["guardian_state"] = guardian_rec.get("guardian_state", state["guardian_state"])
            state["freeze_state"]   = guardian_rec.get("freeze_active",  state["freeze_state"])
            state["last_guardian_emergency_level"] = int(
                guardian_rec.get("emergency_level", 0)
            )

        if gov_rec:
            state["governance_state"]      = gov_rec.get("governance_state",  state["governance_state"])
            state["last_governance_score"] = float(gov_rec.get("live_governance_score", 0.0))
            state["trading_mode"]          = gov_rec.get("trading_mode", state["trading_mode"])

        if readiness_rec:
            state["readiness_state"]      = readiness_rec.get("readiness_status",   state["readiness_state"])
            state["last_readiness_score"] = float(readiness_rec.get("continuous_live_readiness_score", 0.0))

        if replay_rec:
            state["replay_offset"] = int(replay_rec.get("replay_offset", 0))

        # Aplica overrides
        state.update(state_overrides)

        # Timestamps
        now = datetime.now(timezone.utc).isoformat()
        if not state.get("session_start"):
            state["session_start"] = now
        state["persisted_at"]  = now
        state["schema_version"] = STATE_SCHEMA_VERSION

        # Persiste
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        return state

    # ── Restore ───────────────────────────────────────────────────────────────

    def restore_state(self) -> RestorationReport:
        """
        Restaura estado de operational_state.json e valida consistencia.

        Retorna RestorationReport com scores e lista de inconsistencias.
        """
        report_id   = str(uuid.uuid4())[:12]
        restored_at = datetime.now(timezone.utc).isoformat()

        state_found       = False
        state_age_seconds = 0.0
        state: dict       = {}
        actions: list[str] = []

        if self.state_file.exists():
            state_found = True
            try:
                raw   = self.state_file.read_text(encoding="utf-8")
                state = json.loads(raw)
                actions.append("Estado lido de operational_state.json")

                # Valida schema_version
                schema = state.get("schema_version", "")
                if schema != STATE_SCHEMA_VERSION:
                    actions.append(
                        f"schema_version={schema!r} diferente do esperado {STATE_SCHEMA_VERSION!r} — usando defaults"
                    )
                    # Merge com defaults para campos ausentes
                    merged = dict(DEFAULT_STATE)
                    merged.update(state)
                    state = merged

                # Calcula idade
                persisted_at_str = state.get("persisted_at", "")
                if persisted_at_str:
                    try:
                        persisted_dt  = datetime.fromisoformat(persisted_at_str)
                        now_dt        = datetime.now(timezone.utc)
                        state_age_seconds = (now_dt - persisted_dt).total_seconds()
                    except Exception:
                        state_age_seconds = -1.0

            except (json.JSONDecodeError, OSError) as exc:
                state_found = False
                state       = dict(DEFAULT_STATE)
                actions.append(f"Falha ao ler estado: {exc} — usando defaults (COLD_START)")
        else:
            state = dict(DEFAULT_STATE)
            actions.append("operational_state.json nao encontrado — COLD_START com defaults")

        # Validacao de consistencia
        inconsistencies = self.validate_consistency(state)
        if inconsistencies:
            actions.append(
                f"{len(inconsistencies)} inconsistencia(s) detectada(s) — estado parcialmente restaurado"
            )
        else:
            actions.append("Estado consistente — restauracao completa")

        # Scores
        integrity_score  = self._compute_integrity_score(state_found, inconsistencies)
        consistency_score = max(0.0, round(100.0 - len(inconsistencies) * 15.0, 1))
        replay_score     = self._compute_replay_score(state_found, state)

        recommendation = self._build_recommendation(
            state_found, integrity_score, inconsistencies, state
        )

        report = RestorationReport(
            report_id                   = report_id,
            restoration_integrity_score = integrity_score,
            state_consistency_score     = consistency_score,
            replay_recovery_score       = replay_score,
            state_found                 = state_found,
            state_age_seconds           = round(state_age_seconds, 1),
            governance_state_restored   = state.get("governance_state",  "unknown"),
            guardian_state_restored     = state.get("guardian_state",    "unknown"),
            readiness_state_restored    = state.get("readiness_state",   "unknown"),
            freeze_state_restored       = bool(state.get("freeze_state", False)),
            trading_mode_restored       = state.get("trading_mode",      "paper"),
            replay_offset_restored      = int(state.get("replay_offset", 0)),
            inconsistencies_found       = inconsistencies,
            restoration_actions         = actions,
            recommendation              = recommendation,
            restored_at                 = restored_at,
        )

        self._persist_log(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_integrity.set(integrity_score)
                _prom_consistency.set(consistency_score)
                _prom_replay.set(replay_score)
            except Exception:
                pass

        return report

    # ── Consistency validation ────────────────────────────────────────────────

    def validate_consistency(self, state: dict) -> list[str]:
        """
        Verifica regras de consistencia interna do estado.

        Retorna lista de strings descrevendo cada inconsistencia encontrada.
        """
        issues: list[str] = []
        for (field_a, val_a, field_b, invalid_b, description) in CONSISTENCY_RULES:
            if state.get(field_a) == val_a and state.get(field_b) == invalid_b:
                issues.append(description)
        return issues

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_integrity_score(self, state_found: bool, inconsistencies: list[str]) -> float:
        """Calcula restoration_integrity_score."""
        if not state_found:
            return 0.0
        score = 100.0 - len(inconsistencies) * 10.0
        return round(max(0.0, min(100.0, score)), 1)

    def _compute_replay_score(self, state_found: bool, state: dict) -> float:
        """Calcula replay_recovery_score com base no replay_offset."""
        if not state_found:
            return 0.0
        offset = int(state.get("replay_offset", 0))
        if offset > 0:
            return 100.0
        return 60.0

    def _build_recommendation(
        self,
        state_found:     bool,
        integrity:       float,
        inconsistencies: list[str],
        state:           dict,
    ) -> str:
        if not state_found:
            return (
                "COLD_START: estado operacional nao encontrado. "
                "Sistema iniciara com defaults. "
                "Validar configuracoes antes de ativar execucao."
            )
        if integrity >= 90.0 and not inconsistencies:
            mode    = state.get("trading_mode",  "paper")
            readiness = state.get("readiness_state", "unknown")
            return (
                f"Restauracao completa (integrity={integrity:.0f}/100). "
                f"mode={mode} readiness={readiness}. Sistema pronto."
            )
        if inconsistencies:
            return (
                f"Restauracao com inconsistencias (integrity={integrity:.0f}/100). "
                f"Problemas: {'; '.join(inconsistencies[:2])}{'...' if len(inconsistencies) > 2 else ''}. "
                "Revisar estado antes de operacao live."
            )
        return (
            f"Restauracao parcial (integrity={integrity:.0f}/100). "
            "Monitorar subsistemas nas proximas iteracoes."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_last(self, path: Path) -> dict | None:
        """Le ultimo registro de um JSONL."""
        if not path.exists():
            return None
        last: dict | None = None
        try:
            with open(path, encoding="utf-8") as f:
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

    def _persist_log(self, report: RestorationReport) -> None:
        try:
            self.restoration_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "restored_at":                  report.restored_at,
                "report_id":                    report.report_id,
                "restoration_integrity_score":  report.restoration_integrity_score,
                "state_consistency_score":      report.state_consistency_score,
                "replay_recovery_score":        report.replay_recovery_score,
                "state_found":                  report.state_found,
                "state_age_seconds":            report.state_age_seconds,
                "governance_state_restored":    report.governance_state_restored,
                "guardian_state_restored":      report.guardian_state_restored,
                "readiness_state_restored":     report.readiness_state_restored,
                "freeze_state_restored":        report.freeze_state_restored,
                "trading_mode_restored":        report.trading_mode_restored,
                "replay_offset_restored":       report.replay_offset_restored,
                "inconsistencies_count":        len(report.inconsistencies_found),
            }
            with open(self.restoration_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Operational State Restoration Engine — Phase R R-2"
    )
    parser.add_argument("--json",    action="store_true", help="Saida em JSON")
    parser.add_argument("--persist", action="store_true", help="Persiste estado atual")
    parser.add_argument("--restore", action="store_true", help="Restaura estado salvo")
    args = parser.parse_args()

    engine = OperationalStateRestorationEngine()

    # Ambos podem ser usados juntos: persiste e depois restaura
    if args.persist:
        state = engine.persist_state()
        if args.json:
            print(json.dumps(state, indent=2))
            return
        print(f"\nEstado persistido em {OPERATIONAL_STATE}")
        print(f"  governance_state: {state.get('governance_state')}")
        print(f"  guardian_state:   {state.get('guardian_state')}")
        print(f"  readiness_state:  {state.get('readiness_state')}")
        print(f"  freeze_state:     {state.get('freeze_state')}")
        print(f"  trading_mode:     {state.get('trading_mode')}")
        print(f"  replay_offset:    {state.get('replay_offset')}")
        if not args.restore:
            return

    # Default (ou --restore): restaura e exibe relatorio
    report = engine.restore_state()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    found_icon = "[OK]" if report.state_found else "[--]"
    print(f"\nOperational State Restoration Engine — Phase R R-2")
    print(f"  report_id:                    {report.report_id}")
    print(f"  state_found:                  {found_icon} {'sim' if report.state_found else 'nao (COLD_START)'}")
    if report.state_found:
        print(f"  state_age_seconds:            {report.state_age_seconds:.1f}s")
    print(f"  restoration_integrity_score:  {report.restoration_integrity_score:.1f}/100")
    print(f"  state_consistency_score:      {report.state_consistency_score:.1f}/100")
    print(f"  replay_recovery_score:        {report.replay_recovery_score:.1f}/100")
    print(f"\n  Estado restaurado:")
    print(f"    governance_state: {report.governance_state_restored}")
    print(f"    guardian_state:   {report.guardian_state_restored}")
    print(f"    readiness_state:  {report.readiness_state_restored}")
    print(f"    freeze_state:     {'SIM' if report.freeze_state_restored else 'nao'}")
    print(f"    trading_mode:     {report.trading_mode_restored}")
    print(f"    replay_offset:    {report.replay_offset_restored}")
    if report.inconsistencies_found:
        print(f"\n  Inconsistencias ({len(report.inconsistencies_found)}):")
        for inc in report.inconsistencies_found:
            print(f"    [!] {inc}")
    print(f"\n  Acoes de restauracao:")
    for act in report.restoration_actions:
        print(f"    - {act}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()
