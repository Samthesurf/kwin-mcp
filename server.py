#!/usr/bin/env python3
"""Repo-root shim so `python server.py` works during local development.

The real implementation lives in `kwin_bridge/server.py` so it installs
cleanly as a package (and the `uvx`/`pip` console script points there too).
This file just re-exports for convenience.
"""

from kwin_bridge.server import main, mcp  # noqa: F401

if __name__ == "__main__":
    main()
