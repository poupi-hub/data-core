"""Persistent async event loop for APScheduler jobs.

Problem
-------
APScheduler runs jobs in threads. Calling ``asyncio.run()`` inside those
threads creates a *new* event loop on every invocation and destroys it when
the coroutine completes. Each ephemeral loop leaks OS-level resources (SSL
contexts, file descriptors, psycopg connection state) that CPython's garbage
collector does not release promptly. Under normal scheduler load (~20
collectors + pipeline jobs) this produced 6 MB/hr baseline growth with spikes
to 800 MB/hr during bulk collection, eventually triggering OOM-kill.

Solution
--------
A single background thread runs one persistent ``asyncio`` event loop for the
lifetime of the scheduler process. All async jobs are dispatched into it via
``asyncio.run_coroutine_threadsafe()``, which is both thread-safe and avoids
the create/destroy cycle entirely.

Usage
-----
Replace every ``asyncio.run(coro)`` inside a scheduler job with::

    from scheduler.async_runner import run_async
    run_async(coro)

Shutdown
--------
Call ``shutdown_loop()`` once when the scheduler process exits (see
``app/jobs/scheduler_runner.py``). This stops the loop and joins its thread
gracefully within 5 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return the shared persistent event loop, creating it on first call."""
    global _loop, _thread  # noqa: PLW0603
    with _lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_loop.run_forever,
            name="scheduler-async-runner",
            daemon=True,
        )
        _thread.start()
        logger.debug("scheduler/async_runner: persistent event loop started (thread=%s)", _thread.name)
    return _loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* on the shared persistent loop and block until it completes.

    This is the drop-in replacement for ``asyncio.run()`` inside APScheduler
    job functions. Unlike ``asyncio.run()`` it does **not** create or destroy
    an event loop — it submits the coroutine to the long-lived background loop
    and waits for the result in the calling thread.

    Exceptions raised inside *coro* propagate normally to the caller.
    """
    loop = get_or_create_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def shutdown_loop(timeout: float = 5.0) -> None:
    """Stop the persistent loop and join its thread.

    Safe to call multiple times — idempotent if the loop is already stopped.
    Call this once from the scheduler process shutdown hook so the daemon
    thread exits cleanly before the process terminates.
    """
    global _loop, _thread  # noqa: PLW0603
    with _lock:
        if _loop is None:
            return
        loop, thread = _loop, _thread
        _loop = None
        _thread = None

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "scheduler/async_runner: loop thread did not stop within %.1fs — leaving as daemon",
                timeout,
            )
    logger.debug("scheduler/async_runner: persistent event loop stopped")
