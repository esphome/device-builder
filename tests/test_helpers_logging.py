"""Tests for ``helpers/logging.py`` — asyncio-safe queue logging."""

from __future__ import annotations

import logging
import logging.handlers
import queue
from collections.abc import Generator

import pytest

from esphome_device_builder.helpers.logging import (
    LoggingQueueHandler,
    activate_log_queue_handler,
)


@pytest.fixture
def isolated_root_logger() -> Generator[None]:
    """
    Snapshot ``logging.root`` and restore it after the test.

    Each case in this file mutates the root logger's handler list,
    so without this fixture they leak handlers between tests (and
    into the rest of the suite).
    """
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    logging.root.handlers = []
    try:
        yield
    finally:
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        for handler in saved_handlers:
            logging.root.addHandler(handler)
        logging.root.setLevel(saved_level)


def test_migrates_existing_handlers_behind_a_queue_listener(
    isolated_root_logger: None,
) -> None:
    """``activate_log_queue_handler`` moves existing handlers into the listener."""
    stream = logging.StreamHandler()
    file_like = logging.NullHandler()
    logging.root.addHandler(stream)
    logging.root.addHandler(file_like)

    activate_log_queue_handler()

    # Root now holds exactly one ``LoggingQueueHandler``; the
    # originals migrated into the listener it owns. (Pytest may
    # have re-added its own ``LogCaptureHandler`` to root after our
    # fixture's reset, hence subset rather than equality on the
    # listener handlers.)
    queue_handlers = [h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler)]
    assert len(queue_handlers) == 1
    queue_handler = queue_handlers[0]
    assert queue_handler.listener is not None
    assert {stream, file_like}.issubset(set(queue_handler.listener.handlers))


def test_is_idempotent(isolated_root_logger: None) -> None:
    """A second call after activation does not nest another queue handler."""
    logging.root.addHandler(logging.NullHandler())

    activate_log_queue_handler()
    first_handler = next(h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler))
    first_listener = first_handler.listener

    activate_log_queue_handler()

    queue_handlers = [h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler)]
    assert queue_handlers == [first_handler]
    assert first_handler.listener is first_listener


def test_close_stops_listener_thread(isolated_root_logger: None) -> None:
    """Closing the queue handler shuts the backing listener down."""
    logging.root.addHandler(logging.NullHandler())
    activate_log_queue_handler()
    queue_handler = next(h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler))
    listener = queue_handler.listener
    assert listener is not None
    listener_thread = listener._thread
    assert listener_thread is not None
    assert listener_thread.is_alive()

    queue_handler.close()

    assert queue_handler.listener is None
    listener_thread.join(timeout=2.0)
    assert not listener_thread.is_alive()


def test_records_reach_migrated_handlers(isolated_root_logger: None) -> None:
    """Records emitted after activation flow through the queue to the migrated handlers."""

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    capture = _Capture()
    logging.root.addHandler(capture)
    logging.root.setLevel(logging.DEBUG)

    activate_log_queue_handler()

    logger = logging.getLogger("esphome_device_builder.test_helpers_logging")
    logger.warning("hello")

    queue_handler = next(h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler))
    listener = queue_handler.listener
    assert listener is not None
    # Listener is on a separate thread — drain by stopping it, which
    # blocks until the queue is empty and the worker has exited.
    listener.stop()
    queue_handler.listener = None

    assert any(r.getMessage() == "hello" for r in capture.records)


def test_prepare_clears_stack_info() -> None:
    """``prepare`` strips ``stack_info`` so the listener can't double-format it."""
    handler = LoggingQueueHandler(queue.SimpleQueue())
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="msg",
        args=None,
        exc_info=None,
    )
    record.stack_info = "fake stack"

    prepared = handler.prepare(record)

    assert prepared.stack_info is None
