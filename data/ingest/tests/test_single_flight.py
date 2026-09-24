import asyncio

import pytest

from data.ingest.app.brokers.single_flight import SingleFlight


class VendorCall:
    """A stand-in for a vendor call that can be held open while callers pile up.

    There is no broker and no live vendor connection here, so the guard is exercised
    against a call whose timing the test controls rather than against Alpaca.
    """

    def __init__(self, result='bars', error: Exception | None = None):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.__result = result
        self.__error = error

    async def __call__(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.__error is not None:
            raise self.__error
        return self.__result


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_vendor_call():
    guard = SingleFlight()
    vendor = VendorCall()

    callers = [asyncio.create_task(guard.run('key', vendor)) for _ in range(5)]
    # The leader blocks inside the call, so every follower has attached by the time it
    # is released -- no sleep, and therefore nothing timing-dependent.
    await vendor.started.wait()
    vendor.release.set()
    results = await asyncio.gather(*callers)

    assert vendor.calls == 1
    assert results == ['bars'] * 5
    assert guard.in_flight == 0


@pytest.mark.asyncio
async def test_different_keys_do_not_collapse():
    guard = SingleFlight()
    vendor = VendorCall()

    callers = [asyncio.create_task(guard.run(key, vendor)) for key in ('iex', 'sip')]
    await vendor.started.wait()
    vendor.release.set()
    await asyncio.gather(*callers)

    assert vendor.calls == 2
    assert guard.in_flight == 0


@pytest.mark.asyncio
async def test_failure_reaches_every_caller():
    guard = SingleFlight()
    vendor = VendorCall(error=RuntimeError('vendor unavailable'))

    callers = [asyncio.create_task(guard.run('key', vendor)) for _ in range(4)]
    await vendor.started.wait()
    vendor.release.set()
    results = await asyncio.gather(*callers, return_exceptions=True)

    assert vendor.calls == 1
    assert all(isinstance(result, RuntimeError) for result in results)
    # Released after a failure too, or one bad call would poison the key for good.
    assert guard.in_flight == 0


@pytest.mark.asyncio
async def test_finished_call_is_not_served_again():
    guard = SingleFlight()
    calls = 0

    async def vendor():
        nonlocal calls
        calls += 1
        return calls

    # The guard shares an IN-FLIGHT call and retains nothing, so the second request must
    # reach the vendor again rather than be handed the first one's answer.
    assert await guard.run('key', vendor) == 1
    assert guard.in_flight == 0
    assert await guard.run('key', vendor) == 2


@pytest.mark.asyncio
async def test_cancelled_follower_does_not_cancel_the_shared_call():
    guard = SingleFlight()
    vendor = VendorCall()

    leader = asyncio.create_task(guard.run('key', vendor))
    followers = [asyncio.create_task(guard.run('key', vendor)) for _ in range(2)]
    await vendor.started.wait()
    followers[0].cancel()
    vendor.release.set()
    results = await asyncio.gather(leader, *followers, return_exceptions=True)

    assert vendor.calls == 1
    assert results[0] == 'bars'
    assert isinstance(results[1], asyncio.CancelledError)
    assert results[2] == 'bars'
    assert guard.in_flight == 0


@pytest.mark.asyncio
async def test_cancelled_leader_releases_the_key():
    guard = SingleFlight()
    vendor = VendorCall()

    leader = asyncio.create_task(guard.run('key', vendor))
    follower = asyncio.create_task(guard.run('key', vendor))
    await vendor.started.wait()
    leader.cancel()
    results = await asyncio.gather(leader, follower, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    # A leader that dies without clearing its entry would hang every later request for
    # that key forever -- the classic failure of this kind of code.
    assert guard.in_flight == 0

    vendor.release.set()
    assert await guard.run('key', vendor) == 'bars'
    assert vendor.calls == 2
