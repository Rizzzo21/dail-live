#!/usr/bin/env python3
"""Shim: run the DAiL MCP server from a source checkout.

The packaged entry point is the ``dail-mcp`` console script (or
``python -m dail_mcp``). This file keeps ``python3 server.py`` working.
"""

from dail_mcp.server import main

if __name__ == "__main__":
    main()
