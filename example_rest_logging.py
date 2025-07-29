#!/usr/bin/env python3
"""
Example agent using REST logging for swf-monitor.

To run this example, first install swf-common-lib:
    pip install swf-common-lib

Or if developing locally:
    pip install -e /path/to/swf-common-lib
"""

import logging
from swf_common_lib.rest_logging import setup_rest_logging

# Setup logging - this is all you need!
logger = setup_rest_logging(
    app_name='my_agent',
    instance_name='agent_001'
)

# Now just use standard Python logging
logger.info("Agent starting up")
logger.debug("Connecting to data source")  
logger.info("Processing workflow step 1")
logger.warning("Step took longer than expected")
logger.error("Failed to process item 42")
logger.info("Agent shutting down")