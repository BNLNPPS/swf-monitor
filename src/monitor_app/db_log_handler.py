"""
Database logging handler for Django.

This handler writes log records directly to the AppLog database table,
enabling monitor-internal logging to be captured alongside agent logs.
"""

import logging
import os
import socket
import threading


class DbLogHandler(logging.Handler):
    """
    A logging handler that writes log records directly to the AppLog database.

    This is used for monitor-internal logging (MCP actions, SSE events, etc.)
    so they appear in the central logging database alongside agent logs.

    Unlike RestLogHandler which POSTs to the REST API, this handler writes
    directly via Django ORM since it runs inside the Django process.
    """

    def __init__(self, app_name: str = 'swf-monitor', instance_name: str = None):
        """
        Initialize the handler.

        Args:
            app_name: Application name for log records (default: 'swf-monitor')
            instance_name: Instance name for log records (default: hostname)
        """
        super().__init__()
        self.app_name = app_name
        self.instance_name = instance_name or socket.gethostname()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Write a log record to the database.

        Args:
            record: The log record to write
        """
        # Import here to avoid circular imports and ensure Django is ready
        try:
            from django.utils import timezone
            from monitor_app.models import AppLog
        except Exception:
            # Django not ready or import failed - fall back to stderr
            import sys
            sys.stderr.write(f"DbLogHandler: Django not ready, cannot log: {record.getMessage()}\n")
            return

        try:
            # Format the message
            message = self.format(record)

            # Extract extra data if present (any attributes not in standard LogRecord)
            standard_attrs = {
                'name', 'msg', 'args', 'created', 'filename', 'funcName',
                'levelname', 'levelno', 'lineno', 'module', 'msecs',
                'pathname', 'process', 'processName', 'relativeCreated',
                'stack_info', 'exc_info', 'exc_text', 'thread', 'threadName',
                'message', 'asctime'
            }
            extra_data = {
                k: v for k, v in record.__dict__.items()
                if k not in standard_attrs and not k.startswith('_')
            }

            AppLog.objects.create(
                app_name=self.app_name,
                instance_name=self.instance_name,
                timestamp=timezone.now(),
                level=record.levelno,
                levelname=record.levelname,
                message=message,
                module=record.module or '',
                funcname=record.funcName or '',
                lineno=record.lineno or 0,
                process=record.process or os.getpid(),
                thread=record.thread or threading.current_thread().ident or 0,
                extra_data=extra_data if extra_data else None,
            )
        except Exception as e:
            # Don't let logging failures crash the application
            import sys
            sys.stderr.write(f"DbLogHandler: Failed to write log to database: {e}\n")
            sys.stderr.write(f"DbLogHandler: Original message: {record.getMessage()}\n")
