"""Tool calls run off the event loop, and note writes still serialise.

Regression for 2026-11-01: a 133 MB publish_site ran synchronously on the event
loop and froze the server for every client -- claude.ai timed out after 30 s
while the scheduled routine and Desktop both needed the vault.
"""

from __future__ import annotations

import asyncio
import time

from mcp.server.mcpserver import MCPServer

from obsidian_mcp.telemetry import instrumented


def _server():
    mcp = MCPServer(name="concurrency")
    counter = {"value": 0}

    @mcp.tool()
    @instrumented("slow_read")
    async def slow_read() -> str:
        time.sleep(0.6)  # blocking work, as every real tool body does
        return "slow"

    @mcp.tool()
    @instrumented("fast_read")
    async def fast_read() -> str:
        return "fast"

    @mcp.tool()
    @instrumented("increment", mutation=True)
    async def increment() -> int:
        # A read-modify-write with a window in the middle: unserialised, two
        # concurrent calls both read 0 and one update is lost.
        seen = counter["value"]
        time.sleep(0.05)
        counter["value"] = seen + 1
        return counter["value"]

    @mcp.tool()
    @instrumented("long_publish", mutation=True, exclusive=False)
    async def long_publish() -> str:
        time.sleep(0.6)
        return "published"

    return mcp, counter


async def _timed(mcp, name):
    await mcp.call_tool(name, {})
    return name, time.perf_counter()


def test_a_slow_tool_does_not_block_others() -> None:
    mcp, _ = _server()

    async def scenario():
        start = time.perf_counter()
        slow = asyncio.create_task(_timed(mcp, "slow_read"))
        await asyncio.sleep(0.05)  # the slow call is now running
        _, fast_done = await _timed(mcp, "fast_read")
        _, slow_done = await slow
        return fast_done - start, slow_done - start

    fast, slow = asyncio.run(scenario())
    assert fast < 0.3, f"fast call waited {fast:.2f}s behind the slow one"
    assert slow >= 0.6


def test_concurrent_mutations_do_not_lose_updates() -> None:
    mcp, counter = _server()

    async def scenario():
        await asyncio.gather(*(mcp.call_tool("increment", {}) for _ in range(5)))

    asyncio.run(scenario())
    assert counter["value"] == 5


def test_writes_proceed_during_a_long_publish() -> None:
    mcp, counter = _server()

    async def scenario():
        start = time.perf_counter()
        publish = asyncio.create_task(_timed(mcp, "long_publish"))
        await asyncio.sleep(0.05)
        _, write_done = await _timed(mcp, "increment")
        await publish
        return write_done - start

    assert asyncio.run(scenario()) < 0.3
    assert counter["value"] == 1


def test_publish_site_is_registered_non_exclusive() -> None:
    import inspect

    from obsidian_mcp.tools import publish

    source = inspect.getsource(publish)
    assert '@instrumented("publish_site", mutation=True, exclusive=False)' in source
