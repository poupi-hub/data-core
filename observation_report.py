"""
OBSERVATION MODE — Weekly Report
Gera relatório semanal de crescimento, freshness, concentração e source health.
Sem desenvolvimento. Apenas medição.

SUNSET 2026-06-09: Jobs e Real Estate removidos do data-core.
Módulos ativos: ecommerce, crypto.

Uso: python observation_report.py
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from database.session import SessionLocal

ACTIVE_MODULES = ("ecommerce", "crypto")

# ── Helpers ───────────────────────────────────────────────────────────────────
def sep(char="-", n=64):
    return char * n


def hhi(counts):
    total = sum(counts)
    if not total:
        return 0
    return sum((c / total) ** 2 for c in counts) * 10_000


def run_report():
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    d1 = now - timedelta(hours=24)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    lines = []

    def p(*args):
        line = " ".join(str(a) for a in args)
        lines.append(line)
        print(line)

    p()
    p("=" * 64)
    p("OBSERVATION MODE — WEEKLY REPORT")
    p("Gerado em:", now.strftime("%Y-%m-%d %H:%M UTC"))
    p("=" * 64)

    module_totals: dict[str, dict[str, int]] = {}

    for module in ACTIVE_MODULES:
        p()
        p(sep("="))
        p(f"MODULE — {module.upper()}")
        p(sep("="))
        p()
        p("%-30s %8s %8s %8s %8s" % ("source", "total", "+24h", "+7d", "+30d"))
        p(sep())

        rows = db.execute(text(
            "SELECT source_name,"
            " COUNT(*) as total,"
            " SUM(CASE WHEN collected_at >= :d1 THEN 1 ELSE 0 END) as d1,"
            " SUM(CASE WHEN collected_at >= :d7 THEN 1 ELSE 0 END) as d7,"
            " SUM(CASE WHEN collected_at >= :d30 THEN 1 ELSE 0 END) as d30"
            " FROM raw_collections WHERE module=:m"
            " GROUP BY source_name ORDER BY total DESC"
        ), {"m": module, "d1": d1, "d7": d7, "d30": d30}).fetchall()

        totals = {}
        for r in rows:
            totals[r[0]] = r[1]
            p("%-30s %8d %8d %8d %8d" % (r[0], r[1], r[2] or 0, r[3] or 0, r[4] or 0))

        module_totals[module] = totals
        total = sum(totals.values())
        h = hhi(list(totals.values()))
        top = max(totals, key=totals.get) if totals else "N/A"
        top_share = 100 * totals.get(top, 0) / total if total else 0
        p()
        p("  Total     : %d" % total)
        p("  HHI       : %.0f" % h)
        p("  Top source: %s (%.1f%%)" % (top, top_share))

        # Freshness
        p()
        p("FRESHNESS — %s" % module.upper())
        p("%-30s %-20s %-8s" % ("source", "last_collection", "status"))
        p(sep())
        for src in totals:
            row = db.execute(text(
                "SELECT MAX(collected_at) FROM raw_collections"
                " WHERE module=:m AND source_name=:s"
            ), {"m": module, "s": src}).fetchone()
            ultima = row[0]
            if ultima:
                age_h = (now - ultima.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                status = "ACTIVE" if age_h < 48 else ("STALE" if age_h < 336 else "DEAD")
                p("%-30s %-20s %-8s (%.0fh ago)" % (src, str(ultima)[:16], status, age_h))
            else:
                p("%-30s %-20s %-8s" % (src, "NUNCA", "DEAD"))

    # ── SALVAR RELATÓRIO ──────────────────────────────────────────────────────
    report_dir = Path("observation_reports")
    report_dir.mkdir(exist_ok=True)
    filename = report_dir / ("report_%s.txt" % now.strftime("%Y-%m-%d"))
    filename.write_text("\n".join(lines), encoding="utf-8")

    snapshot = {
        "date": now.strftime("%Y-%m-%d"),
        "modules": {m: {"totals": t, "total": sum(t.values())} for m, t in module_totals.items()},
    }
    snap_file = report_dir / ("snapshot_%s.json" % now.strftime("%Y-%m-%d"))
    snap_file.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    db.close()
    p()
    p("Relatorio salvo em:", str(filename))
    p("Snapshot JSON    :", str(snap_file))
    return snapshot


if __name__ == "__main__":
    run_report()
