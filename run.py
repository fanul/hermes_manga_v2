#!/usr/bin/env python3
"""Entry point for manga_auto_search v2 modular package.

Equivalent to: python3 -m modules.orchestrator
But provides a stable entry-point for cron jobs.

Usage:
    python3 /config/manga_auto_search/run.py

This is the canonical cron target. It boots the orchestrator with a fresh
PYTHONPATH entry on /config/manga_auto_search so `modules.*` imports resolve.
"""

import sys
import os

# Ensure the package directory is on path (cron doesn't inherit the cwd we want)
_PKG_PARENT = os.path.dirname(os.path.abspath(__file__))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from modules.orchestrator import main

if __name__ == "__main__":
    main()