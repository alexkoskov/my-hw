#!/usr/bin/env python3
"""
Smoke test for feed iteration with error isolation.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from news_bot import job

# Configure logging to capture messages
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

print("Running job with one valid and one invalid feed...")
try:
    job()
except Exception as e:
    print(f"Unexpected exception: {e}")
    sys.exit(1)

print("Smoke test completed. Check logs above for error isolation.")