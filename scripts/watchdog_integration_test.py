"""
Watchdog Integration Test — 5 cenários manuais end-to-end.

Testa: detecção → alerta Telegram → métricas Prometheus → heartbeat.
Limpa todos os dados inseridos ao final de cada teste.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from prometheus_client import REGISTRY
from sqlalchemy import text

from app.raw.models import RawCollection
from app.scrapers.models import ScraperDriftEvent
from app.watchdog.models import TelegramPublicationEvent, WatchdogRun
from app.watchdog.service import WatchdogService
from database.session import SessionLocal

NOW = datetime.now(tz=timezone.utc)
PASS = "✅"
FAIL = "❌"
SKIP = "⚠️ "

results: list[tuple[str, bool, str]] = []


# ─── Helpers ──────────────────────────────────────────────────────────────────

def check(name: str, condition: bool, detail: str = "") -> bool:
    icon = PASS if condition else FAIL
    print(f"    {icon} {name}" + (f" — {detail}" if detail else ""))
    results.append((name, condition, detail))
    return condition


def section(title: str) -> None:
    print(f"\n{'═' * 62}")
    print(f"  {title}")
    print("═" * 62)


def run_watchdog(db) -> tuple[WatchdogRun, WatchdogService]:
    svc = WatchdogService(db)
    run = svc.run()
    return run, svc


def prometheus_value(metric_name: str, labels: dict | None = None) -> float | None:
    for metric in REGISTRY.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if labels is None or all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return None


def insert_raw(db, source_name: str, age_minutes: int, status: str, extra_checksum: str = "") -> uuid.UUID:
    """Insert a synthetic RawCollection record."""
    row = RawCollection(
        module="ecommerce",
        source_name=source_name,
        collector_name="EcommerceURLScraper",
        checksum=f"test-{source_name}-{age_minutes}-{status}-{extra_checksum}",
        processing_status=status,
        collected_at=NOW - timedelta(minutes=age_minutes),
        metadata_json={},
        collection_metadata_json={},
    )
    db.add(row)
    db.flush()
    return row.id


def insert_drift(db, source_name: str, risk_level: str = "critical") -> int:
    ev = ScraperDriftEvent(
        source_name=source_name,
        collector_name="EcommerceURLScraper",
        module="ecommerce",
        drift_type="field_missing",
        risk_level=risk_level,
        field_name="price",
        detected_at=NOW - timedelta(hours=1),
        resolved_at=None,
    )
    db.add(ev)
    db.flush()
    return ev.id


def insert_telegram_event(db, status: str, age_hours: float = 0.5) -> int:
    ev = TelegramPublicationEvent(
        group_id="poupi-oportunidades",
        marketplace="drogasil",
        price=29.90,
        deal_score=85.0,
        status=status,
        fail_reason="Bot blocked" if status == "failed" else None,
        published_at=NOW - timedelta(hours=age_hours),
        reported_by="test",
    )
    db.add(ev)
    db.flush()
    return ev.id


# ─── TEST 1: Scraper desligado ────────────────────────────────────────────────

def test_scraper_down(db) -> None:
    section("TEST 1 — Scraper desligado (sem coleta nas últimas 3h)")
    print("  Cenário: nenhuma raw_collection recente — scraper efetivamente parado.")

    # Estado atual do DB já representa scraper parado — sem inserções
    run, svc = run_watchdog(db)

    codes = run.alert_codes or []
    check("collection_stale detectado", "collection_stale" in codes, f"codes={codes}")
    check("overall_status = critical", run.overall_status == "critical", run.overall_status)
    check("WatchdogRun persistido", run.id is not None)
    check(
        "telegram_sent = True (alertas críticos enviados)",
        run.telegram_sent is True,
        f"telegram_sent={run.telegram_sent}",
    )

    prom_collection = prometheus_value("operational_watchdog_status", {"check": "collection"})
    check("Prometheus collection = 2 (critical)", prom_collection == 2.0, f"valor={prom_collection}")

    # Heartbeat
    sent = svc.heartbeat()
    check("Heartbeat enviado ao Telegram", sent is True)

    print(f"\n  check_results:")
    for name, r in run.check_results.items():
        print(f"    {name}: {r['status']} — {r['summary'][:70]}")


# ─── TEST 2: Token Telegram quebrado ─────────────────────────────────────────

def test_broken_telegram_token(db) -> None:
    section("TEST 2 — Token Telegram inválido")
    print("  Cenário: TELEGRAM_BOT_TOKEN corrompido — watchdog não deve travar.")

    from app.watchdog import notifier as notifier_module
    from core.config import settings

    original_token = settings.telegram_bot_token
    try:
        # Forçar token inválido
        settings.telegram_bot_token = "9999999999:INVALID_TOKEN_XXXX"
        settings.telegram_enabled = True

        # Recriar notifier com token ruim
        from app.watchdog.notifier import TelegramNotifier
        bad_notifier = TelegramNotifier(
            bot_token="9999999999:INVALID_TOKEN_XXXX",
            chat_id=settings.telegram_chat_id,
        )
        bad_notifier._enabled = True  # força habilitado para testar falha HTTP

        send_result = bad_notifier.send("🔴 teste com token inválido")
        check("send() retorna False com token inválido", send_result is False, f"result={send_result}")

        # Watchdog completo com notifier ruim
        svc = WatchdogService(db)
        svc._notifier = bad_notifier
        run = svc.run()

        check("WatchdogRun criado mesmo com Telegram falhando", run.id is not None)
        check("telegram_sent = False (graceful failure)", run.telegram_sent is False)
        check("overall_status calculado corretamente", run.overall_status in ("ok", "warning", "critical"))
        check("Sem exceção — sistema sobreviveu ao token ruim", True)

    finally:
        settings.telegram_bot_token = original_token

    print("\n  ✔  Sistema é resiliente a falhas do Telegram.")


# ─── TEST 3: Drift estrutural simulado ───────────────────────────────────────

def test_drift_detected(db) -> None:
    section("TEST 3 — Drift estrutural simulado")
    print("  Cenário: inserir evento de drift crítico não resolvido para 'drogasil'.")

    drift_id = insert_drift(db, source_name="drogasil", risk_level="critical")
    db.commit()

    try:
        run, svc = run_watchdog(db)
        codes = run.alert_codes or []
        check("scraper_drift_detected no alert_codes", "scraper_drift_detected" in codes, f"codes={codes}")

        sq_result = run.check_results.get("scraper_quality", {})
        check(
            "scraper_quality status = critical",
            sq_result.get("status") == "critical",
            sq_result.get("status"),
        )

        prom_sq = prometheus_value("operational_watchdog_status", {"check": "scraper_quality"})
        check("Prometheus scraper_quality = 2 (critical)", prom_sq == 2.0, f"valor={prom_sq}")

        sent = svc.heartbeat()
        check("Heartbeat reflete drift crítico", sent is True)

        drift_alert = next(
            (a for a in (run.check_results.get("scraper_quality", {}).get("alerts", []))
             if a["code"] == "scraper_drift_detected"),
            None,
        )
        check(
            "Alert contém source_name=drogasil",
            drift_alert is not None and drift_alert.get("source_name") == "drogasil",
        )

    finally:
        db.execute(text(f"DELETE FROM scraper_drift_events WHERE id = {drift_id}"))
        db.commit()
        print("  🧹 Drift event removido.")


# ─── TEST 4: Backlog de normalização ─────────────────────────────────────────

def test_normalization_backlog(db) -> None:
    section("TEST 4 — Backlog de normalização simulado")
    print("  Cenário: inserir 25 raw records presos em 'normalization_pending' há 90min.")

    inserted_ids: list[uuid.UUID] = []
    try:
        for i in range(25):
            rid = insert_raw(
                db,
                source_name="drogasil",
                age_minutes=95,  # > threshold de 45min → critical (>20 records)
                status="normalization_pending",
                extra_checksum=str(i),
            )
            inserted_ids.append(rid)
        db.commit()

        run, svc = run_watchdog(db)
        codes = run.alert_codes or []
        check("normalization_backlog no alert_codes", "normalization_backlog" in codes, f"codes={codes}")

        norm_result = run.check_results.get("normalization", {})
        norm_alert = next(
            (a for a in norm_result.get("alerts", []) if a["code"] == "normalization_backlog"),
            None,
        )
        check("Severidade = critical (>20 records)", norm_alert and norm_alert.get("severity") == "critical")
        check(
            "Métrica pending_old >= 25",
            norm_result.get("metrics", {}).get("normalization_pending_old", 0) >= 25,
            str(norm_result.get("metrics", {}).get("normalization_pending_old")),
        )

        prom_norm = prometheus_value("operational_watchdog_status", {"check": "normalization"})
        check("Prometheus normalization = 2 (critical)", prom_norm == 2.0, f"valor={prom_norm}")

        sent = svc.heartbeat()
        check("Heartbeat reflete backlog crítico", sent is True)

    finally:
        if inserted_ids:
            id_list = ", ".join(f"'{str(i)}'" for i in inserted_ids)
            db.execute(text(f"DELETE FROM raw_collections WHERE id IN ({id_list})"))
            db.commit()
        print(f"  🧹 {len(inserted_ids)} raw records removidos.")


# ─── TEST 5: Publicação Telegram bloqueada ────────────────────────────────────

def test_publication_blocked(db) -> None:
    section("TEST 5 — Publicação Telegram bloqueada")
    print("  Cenário: 5 falhas de envio sem nenhum sucesso nas últimas 6h.")

    inserted_ids: list[int] = []
    try:
        # 5 falhas recentes, nenhum sucesso
        for i in range(5):
            eid = insert_telegram_event(db, status="failed", age_hours=i * 0.5 + 0.5)
            inserted_ids.append(eid)
        db.commit()

        run, svc = run_watchdog(db)
        codes = run.alert_codes or []
        check(
            "telegram_publish_failing no alert_codes",
            "telegram_publish_failing" in codes,
            f"codes={codes}",
        )

        tg_result = run.check_results.get("telegram", {})
        check(
            "telegram status = critical",
            tg_result.get("status") == "critical",
            tg_result.get("status"),
        )

        prom_tg = prometheus_value("operational_watchdog_status", {"check": "telegram"})
        check("Prometheus telegram = 2 (critical)", prom_tg == 2.0, f"valor={prom_tg}")

        sent = svc.heartbeat()
        check("Heartbeat com Telegram bloqueado enviado", sent is True)

        tg_alert = next(
            (a for a in tg_result.get("alerts", []) if a["code"] == "telegram_publish_failing"),
            None,
        )
        check(
            "Alert cita número de falhas",
            tg_alert is not None and "5" in tg_alert.get("message", ""),
            tg_alert.get("message", "")[:80] if tg_alert else "no alert",
        )

    finally:
        if inserted_ids:
            id_list = ", ".join(str(i) for i in inserted_ids)
            db.execute(text(f"DELETE FROM telegram_publication_events WHERE id IN ({id_list})"))
            db.commit()
        print(f"  🧹 {len(inserted_ids)} telegram events removidos.")


# ─── Resumo final ─────────────────────────────────────────────────────────────

def print_summary() -> None:
    section("RESUMO FINAL")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        icon = PASS if ok else FAIL
        print(f"  {icon} {name}")

    print(f"\n  {'═' * 40}")
    rate = passed / total * 100 if total else 0
    icon = PASS if passed == total else (SKIP if rate >= 80 else FAIL)
    print(f"  {icon} {passed}/{total} checks passaram ({rate:.0f}%)")

    if passed == total:
        print("\n  🏆 Todos os cenários validados com sucesso.")
        print("     O watchdog está operacional e pronto para produção.")
    else:
        failed = [(n, d) for n, ok, d in results if not ok]
        print(f"\n  Falhas ({len(failed)}):")
        for name, detail in failed:
            print(f"    ❌ {name}: {detail}")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🔬 Watchdog Integration Test — 5 cenários end-to-end")
    print(f"   Início: {NOW.strftime('%Y-%m-%d %H:%M UTC')}\n")

    for test_fn in [
        test_scraper_down,
        test_broken_telegram_token,
        test_drift_detected,
        test_normalization_backlog,
        test_publication_blocked,
    ]:
        db = SessionLocal()
        try:
            test_fn(db)
        except KeyboardInterrupt:
            print("\n\n Interrompido pelo usuario.")
            db.close()
            sys.exit(1)
        except Exception as e:
            print(f"\n  ERRO no {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            try:
                db.rollback()
            except Exception:
                pass
            results.append((test_fn.__name__, False, str(e)[:80]))
        finally:
            db.close()

    print_summary()
