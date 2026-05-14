"""Custom Prometheus metrics for data-core.

All counters and histograms are singletons — import from here to avoid
re-registration errors when the module is imported multiple times.
"""

from prometheus_client import Counter, Gauge, Histogram

price_feed_requests_total = Counter(
    "price_feed_requests_total",
    "Total number of /price-feed requests",
    ["cursor_used"],  # label: 'yes' | 'no'
)

price_feed_items_served_total = Counter(
    "price_feed_items_served_total",
    "Total number of price-feed items returned to consumers",
    ["store_name"],
)

price_feed_response_size = Histogram(
    "price_feed_response_size_items",
    "Distribution of item counts returned per price-feed request",
    buckets=[0, 1, 10, 50, 100, 200, 500, 1000],
)

job_dead_letters_total = Counter(
    "job_dead_letters_total",
    "Total number of scheduler jobs that exhausted retries and wrote a dead letter",
    ["job_name"],
)


def _unresolved_job_dead_letter_count() -> int:
    from database.models import CollectorError
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(CollectorError)
            .filter(
                CollectorError.error_type == "JobDeadLetter",
                CollectorError.resolved_at.is_(None),
            )
            .count()
        )
    except Exception:
        return 0
    finally:
        db.close()


job_dead_letters_unresolved = Gauge(
    "job_dead_letters_unresolved",
    "Current number of unresolved scheduler JobDeadLetter records",
)
job_dead_letters_unresolved.set_function(_unresolved_job_dead_letter_count)

circuit_breaker_opens_total = Counter(
    "circuit_breaker_opens_total",
    "Total number of times a source circuit was opened",
    ["module", "source_name"],
)


def _open_circuit_count() -> int:
    from database.models import CollectorError
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(CollectorError)
            .filter(
                CollectorError.error_type == "CircuitOpen",
                CollectorError.resolved_at.is_(None),
            )
            .count()
        )
    except Exception:
        return 0
    finally:
        db.close()


circuit_breaker_open_sources = Gauge(
    "circuit_breaker_open_sources",
    "Current number of sources with an open circuit breaker",
)
circuit_breaker_open_sources.set_function(_open_circuit_count)
