"""
Database logging handler for Django.

This handler writes log records directly to the AppLog database table,
enabling monitor-internal logging to be captured alongside agent logs.
"""

import logging
import os
import queue
import socket
import sys
import threading
import time


class DbLogHandler(logging.Handler):
    """
    A logging handler that writes log records directly to the AppLog database.

    This is used for monitor-internal logging (MCP actions, SSE events, etc.)
    so they appear in the central logging database alongside agent logs.

    Unlike RestLogHandler which POSTs to the REST API, this handler writes
    directly via Django ORM since it runs inside the Django process.
    """

    _STOP = object()

    def __init__(
        self,
        app_name: str = 'swf-monitor',
        instance_name: str = None,
        queue_size: int = 10000,
    ):
        """
        Initialize the handler.

        Args:
            app_name: Application name for log records (default: 'swf-monitor')
            instance_name: Instance name for log records (default: hostname)
        """
        super().__init__()
        self.app_name = app_name
        self.instance_name = instance_name or socket.gethostname()
        self.queue = queue.Queue(maxsize=queue_size)
        self._dropped = 0
        self._worker = threading.Thread(
            target=self._write_loop,
            name=f"DbLogHandler-{self.app_name}",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Write a log record to the database.

        Args:
            record: The log record to write
        """
        try:
            payload = self._build_payload(record)
        except Exception:
            self.handleError(record)
            return

        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            sys.stderr.write(
                "DbLogHandler: queue full, dropped log "
                f"#{self._dropped}: {record.getMessage()}\n"
            )

    def _build_payload(self, record: logging.LogRecord) -> dict:
        message = self.format(record)

        standard_attrs = {
            'name', 'msg', 'args', 'created', 'filename', 'funcName',
            'levelname', 'levelno', 'lineno', 'module', 'msecs',
            'pathname', 'process', 'processName', 'relativeCreated',
            'stack_info', 'exc_info', 'exc_text', 'thread', 'threadName',
            'message', 'asctime'
        }
        extra_data = {
            k: self._json_safe(v) for k, v in record.__dict__.items()
            if k not in standard_attrs and not k.startswith('_')
        }

        return {
            'app_name': self.app_name,
            'instance_name': self.instance_name,
            'level': record.levelno,
            'levelname': record.levelname,
            'message': message,
            'module': record.module or '',
            'funcname': record.funcName or '',
            'lineno': record.lineno or 0,
            'process': record.process or os.getpid(),
            'thread': record.thread or threading.current_thread().ident or 0,
            'extra_data': extra_data if extra_data else None,
        }

    def _write_loop(self) -> None:
        while True:
            payload = self.queue.get()
            try:
                if payload is self._STOP:
                    return
                self._write_payload(payload)
            finally:
                self.queue.task_done()

    def _write_payload(self, payload: dict) -> None:
        try:
            from django.utils import timezone
            from monitor_app.models import AppLog
            AppLog.objects.create(timestamp=timezone.now(), **payload)
        except Exception as e:
            sys.stderr.write(f"DbLogHandler: Failed to write log to database: {e}\n")
            sys.stderr.write(
                "DbLogHandler: Original message: "
                f"{payload.get('message', '')}\n"
            )

    @staticmethod
    def _json_safe(value):
        try:
            import json
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return repr(value)

    def close(self) -> None:
        try:
            deadline = time.monotonic() + 2.0
            while not self.queue.empty() and time.monotonic() < deadline:
                time.sleep(0.05)
            try:
                self.queue.put_nowait(self._STOP)
            except queue.Full:
                pass
            if threading.current_thread() is not self._worker:
                self._worker.join(timeout=1.0)
        finally:
            super().close()
