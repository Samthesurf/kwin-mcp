#!/usr/bin/env python3
"""Convenience entry point: python run.py [--http PORT].

Launches the kwin-mcp server. Defaults to stdio transport (for MCP clients
like Claude/Codex/Hermes). Use --http to serve over Streamable HTTP.
"""
import sys
from server import main

if __name__ == "__main__":
    sys.exit(main())
