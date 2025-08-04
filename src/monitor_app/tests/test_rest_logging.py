#!/usr/bin/env python3
"""
Test script for REST API logging functionality.

This script demonstrates how agents can send log messages to the swf-monitor
database via the REST API endpoint. It provides both direct REST calls and
a custom logging handler that integrates with Python's logging system.
"""

import requests
import logging
import json
import os
import sys
from datetime import datetime
from typing import Dict, Any, Optional


class RestLogHandler(logging.Handler):
    """
    Custom logging handler that sends log records to the swf-monitor REST API.
    
    This handler formats Python log records and sends them to the /api/logs/
    endpoint for storage in the database.
    """
    
    def __init__(self, base_url: str, app_name: str, instance_name: str, 
                 timeout: int = 10):
        """
        Initialize the REST logging handler.
        
        Args:
            base_url: Base URL of the swf-monitor API (e.g., 'http://localhost:8000')
            app_name: Name of the application sending logs
            instance_name: Instance identifier for this application
            timeout: Request timeout in seconds
        """
        super().__init__()
        self.logs_url = f"{base_url.rstrip('/')}/api/logs/"
        self.app_name = app_name
        self.instance_name = instance_name
        self.timeout = timeout
        self.session = requests.Session()
        
    def emit(self, record: logging.LogRecord) -> None:
        """
        Send a log record to the REST API.
        
        Args:
            record: Python LogRecord to send
        """
        try:
            log_data = self._format_log_record(record)
            response = self.session.post(
                self.logs_url, 
                json=log_data, 
                timeout=self.timeout,
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()
            
        except requests.exceptions.RequestException as e:
            # Handle network/API errors - print to stderr but don't raise
            # to avoid disrupting the application
            print(f"Failed to send log to REST API: {e}", file=sys.stderr)
        except Exception as e:
            # Handle any other errors in log formatting/sending
            print(f"Error in REST log handler: {e}", file=sys.stderr)
    
    def _format_log_record(self, record: logging.LogRecord) -> Dict[str, Any]:
        """
        Convert a Python LogRecord to the format expected by the REST API.
        
        Args:
            record: Python LogRecord
            
        Returns:
            Dictionary suitable for JSON serialization and REST API
        """
        return {
            'app_name': self.app_name,
            'instance_name': self.instance_name,
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelno,
            'levelname': record.levelname,
            'message': record.getMessage(),
            'module': record.module or 'unknown',
            'funcname': record.funcName or 'unknown',
            'lineno': record.lineno or 0,
            'process': record.process or 0,
            'thread': record.thread or 0,
            'extra_data': {
                'pathname': record.pathname,
                'filename': record.filename,
                'created': record.created,
                'msecs': record.msecs,
            }
        }


def send_direct_log_message(base_url: str, app_name: str, instance_name: str,
                          level: str, message: str, **kwargs) -> bool:
    """
    Send a log message directly to the REST API without using Python logging.
    
    Args:
        base_url: Base URL of the swf-monitor API
        app_name: Application name
        instance_name: Instance identifier  
        level: Log level (INFO, WARNING, ERROR, etc.)
        message: Log message text
        **kwargs: Additional fields for the log record
        
    Returns:
        True if successful, False otherwise
    """
    logs_url = f"{base_url.rstrip('/')}/api/logs/"
    
    # Map level names to integers
    level_mapping = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO, 
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    
    log_data = {
        'app_name': app_name,
        'instance_name': instance_name,
        'timestamp': datetime.now().isoformat(),
        'level': level_mapping.get(level.upper(), logging.INFO),
        'levelname': level.upper(),
        'message': message,
        'module': kwargs.get('module', 'test_script'),
        'funcname': kwargs.get('funcname', 'send_direct_log_message'),
        'lineno': kwargs.get('lineno', 0),
        'process': kwargs.get('process', os.getpid()),
        'thread': kwargs.get('thread', 0),
        'extra_data': kwargs.get('extra_data', {})
    }
    
    try:
        response = requests.post(
            logs_url,
            json=log_data,
            timeout=10,
            headers={'Content-Type': 'application/json'}
        )
        response.raise_for_status()
        print(f"✅ Successfully sent {level} log: {message}")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Failed to send log: {e}")
        return False


def test_rest_logging(base_url: str = "http://localhost:8000") -> None:
    """
    Comprehensive test of REST API logging functionality.
    
    Tests both direct REST calls and the custom logging handler.
    
    Args:
        base_url: Base URL of the swf-monitor service
    """
    app_name = "test_logging_script"
    instance_name = f"test_instance_{os.getpid()}"
    
    print(f"🧪 Testing REST logging against {base_url}")
    print(f"📝 App: {app_name}, Instance: {instance_name}")
    print("-" * 60)
    
    # Test 1: Direct REST API calls
    print("1️⃣ Testing direct REST API calls...")
    
    test_messages = [
        ("INFO", "Application started successfully"),
        ("WARNING", "Configuration file not found, using defaults"),
        ("ERROR", "Failed to connect to external service"),
        ("DEBUG", "Processing item 42 of 100"),
        ("CRITICAL", "System out of memory, shutting down")
    ]
    
    success_count = 0
    for level, message in test_messages:
        if send_direct_log_message(base_url, app_name, instance_name, level, message):
            success_count += 1
    
    print(f"Direct API calls: {success_count}/{len(test_messages)} successful\n")
    
    # Test 2: Python logging handler integration
    print("2️⃣ Testing Python logging handler integration...")
    
    # Create logger with REST handler
    logger = logging.getLogger('test_rest_logger')
    logger.setLevel(logging.DEBUG)
    
    # Add our custom REST handler
    rest_handler = RestLogHandler(base_url, app_name, f"{instance_name}_handler")
    logger.addHandler(rest_handler)
    
    # Send various log levels through Python logging
    logger.debug("Debug message from Python logging")
    logger.info("Info message with data: %s", {"key": "value"})
    logger.warning("Warning about deprecated function")
    logger.error("Error processing request ID %d", 12345)
    logger.critical("Critical system failure detected")
    
    print("Python logging handler tests completed\n")
    
    # Test 3: Bulk logging simulation
    print("3️⃣ Testing bulk logging (simulating real agent usage)...")
    
    for i in range(10):
        logger.info(f"Processing workflow step {i+1}/10")
        if i % 3 == 0:
            logger.debug(f"Step {i+1} details: processing file batch")
        if i == 7:
            logger.warning(f"Step {i+1} took longer than expected")
    
    logger.info("Workflow processing completed successfully")
    print("Bulk logging simulation completed\n")
    
    print("✅ All REST logging tests completed!")
    print("Check the swf-monitor database/UI to verify log entries were created.")


def main():
    """Main entry point for the test script."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Test REST API logging functionality for swf-monitor"
    )
    parser.add_argument(
        '--url', '-u',
        default='http://localhost:8000',
        help='Base URL of swf-monitor service (default: http://localhost:8000)'
    )
    parser.add_argument(
        '--direct-only',
        action='store_true',
        help='Only test direct REST calls, skip Python logging handler'
    )
    
    args = parser.parse_args()
    
    if args.direct_only:
        # Quick test of direct API
        print("🧪 Quick REST API logging test...")
        success = send_direct_log_message(
            args.url, 
            "quick_test", 
            "test_instance",
            "INFO", 
            "Quick test log message"
        )
        sys.exit(0 if success else 1)
    else:
        # Full test suite
        test_rest_logging(args.url)


if __name__ == "__main__":
    main()