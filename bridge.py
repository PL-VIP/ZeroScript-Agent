#!/usr/bin/env python3
"""Backward-compatible entry point for ZeroScript Workspace Agent 2.x."""

import asyncio

from agent_bridge import async_main, parse_args


if __name__ == "__main__":
    try:
        asyncio.run(async_main(parse_args()))
    except KeyboardInterrupt:
        pass
