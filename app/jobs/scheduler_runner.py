import signal
import time

from logs.config import configure_logging
from app.runtime.scheduler_watchdog import append_scheduler_lifecycle_event, start_scheduler_watchdog_probe
from scheduler.service import create_scheduler, start_scheduler, stop_scheduler


def main() -> None:
    configure_logging()
    append_scheduler_lifecycle_event("scheduler_runner_starting")
    scheduler = create_scheduler()
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    watchdog_stop = start_scheduler_watchdog_probe()
    start_scheduler(scheduler)
    append_scheduler_lifecycle_event("scheduler_started")
    try:
        while running:
            time.sleep(1)
    finally:
        append_scheduler_lifecycle_event("scheduler_stopping")
        watchdog_stop.set()
        stop_scheduler(scheduler)
        append_scheduler_lifecycle_event("scheduler_stopped")


if __name__ == "__main__":
    main()
