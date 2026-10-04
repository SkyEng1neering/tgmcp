import asyncio

import pytest

from tgmcp.limits import BusyError, CallGate, TTLCache


def test_gate_queues_then_times_out():
    async def go():
        gate = CallGate(limit=1, timeout=0.2, max_queue=10)
        order = []

        async def job(i, hold):
            async with gate.slot():
                order.append(i)
                await asyncio.sleep(hold)

        await asyncio.gather(job(1, 0.05), job(2, 0.05), job(3, 0.0))
        assert order == [1, 2, 3]  # FIFO, nothing dropped

        async def blocker():
            async with gate.slot():
                await asyncio.sleep(0.5)

        t = asyncio.create_task(blocker())
        await asyncio.sleep(0.01)
        with pytest.raises(BusyError, match="waited"):
            async with gate.slot():
                pass
        await t

    asyncio.run(go())


def test_gate_rejects_when_queue_full():
    async def go():
        gate = CallGate(limit=1, timeout=5, max_queue=0)

        async def blocker():
            async with gate.slot():
                await asyncio.sleep(0.2)

        t = asyncio.create_task(blocker())
        await asyncio.sleep(0.01)
        with pytest.raises(BusyError, match="queued"):
            async with gate.slot():
                pass
        await t

    asyncio.run(go())


def test_cache_single_flight_and_errors_not_cached():
    async def go():
        cache = TTLCache(ttl=60)
        calls = 0

        async def slow():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return calls

        results = await asyncio.gather(*(cache.get("k", slow) for _ in range(5)))
        assert results == [1] * 5 and calls == 1

        async def boom():
            raise RuntimeError("x")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cache.get("e", boom)
        assert "e" not in cache._data

    asyncio.run(go())


def test_cache_caller_cancellation_does_not_break_other_waiters():
    async def go():
        cache = TTLCache(ttl=60)

        async def slow():
            await asyncio.sleep(0.05)
            return "v"

        first = asyncio.create_task(cache.get("k", slow))
        await asyncio.sleep(0)
        second = asyncio.create_task(cache.get("k", slow))
        await asyncio.sleep(0.01)
        first.cancel()
        assert await second == "v"
        assert await cache.get("k", slow) == "v" and cache.misses == 1

    asyncio.run(go())
