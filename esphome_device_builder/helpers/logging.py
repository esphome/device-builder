"""Asyncio-safe queue logging — keeps handler I/O off the event loop."""

from __future__ import annotations

import logging
import logging.handlers
import queue
from typing import Any


class LoggingQueueHandler(logging.handlers.QueueHandler):
    """``QueueHandler`` that tears down its backing listener on ``close()``."""

    listener: logging.handlers.QueueListener | None = None

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Prepare a record for queuing."""
        record = super().prepare(record)
        # Workaround for CPython issue 46755: a non-None ``stack_info``
        # survives the ``copy.copy`` inside ``prepare`` and gets
        # formatted a second time when the listener picks the record up.
        record.stack_info = None
        return record

    def handle(self, record: logging.LogRecord) -> Any:
        """Filter and emit the record."""
        # ``Handler.handle`` acquires ``self.lock`` before delegating
        # to ``emit``, but ``SimpleQueue.put_nowait`` is already
        # thread-safe — the lock just adds contention. See CPython
        # issue 24645.
        return_value = self.filter(record)
        if return_value:
            self.emit(record)
        return return_value

    def close(self) -> None:
        """Close the handler and stop the listener thread."""
        super().close()
        if not self.listener:
            return
        self.listener.stop()
        self.listener = None


def activate_log_queue_handler() -> None:
    """
    Migrate the root logger's handlers behind a thread-backed queue.

    Call once during process startup, after the console and rotating-
    file handlers are configured. Subsequent ``logger.*`` calls return
    immediately instead of formatting and writing inline, so the
    asyncio event loop can't be stalled by log I/O.

    Handlers added to the root logger *after* this call run inline
    again — keep this last in the logging-setup chain. Idempotent:
    repeat calls are no-ops.
    """
    if any(isinstance(h, LoggingQueueHandler) for h in logging.root.handlers):
        return

    simple_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = LoggingQueueHandler(simple_queue)
    logging.root.addHandler(queue_handler)

    migrated_handlers: list[logging.Handler] = []
    for handler in logging.root.handlers[:]:
        if handler is queue_handler:
            continue
        logging.root.removeHandler(handler)
        migrated_handlers.append(handler)

    listener = logging.handlers.QueueListener(simple_queue, *migrated_handlers)
    queue_handler.listener = listener
    listener.start()
