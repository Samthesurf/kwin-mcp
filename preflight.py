#!/usr/bin/env python3
"""Repo-root shim. Real implementation: kwin_bridge.preflight."""

from kwin_bridge.preflight import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
