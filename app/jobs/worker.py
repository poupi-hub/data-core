import argparse
import signal
import time

from core.config import settings
from logs.config import configure_logging
from app.runtime.heartbeat import write_worker_heartbeat
from scheduler.jobs import analytics_job, normalize_job


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Data Core pipeline worker.")
    parser.add_argument("--interval", type=int, default=settings.worker_pipeline_interval_seconds)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    configure_logging()
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    write_worker_heartbeat(status="starting", details={"interval_seconds": args.interval})
    while running:
        cycle_started = time.time()
        write_worker_heartbeat(status="running", details={"phase": "normalization"})
        normalize_job()
        write_worker_heartbeat(status="running", details={"phase": "analytics"})
        analytics_job()
        write_worker_heartbeat(
            status="idle",
            details={
                "interval_seconds": args.interval,
                "last_cycle_duration_seconds": round(time.time() - cycle_started, 3),
            },
        )
        if args.once:
            break
        time.sleep(args.interval)
    write_worker_heartbeat(status="stopped")


if __name__ == "__main__":
    main()
