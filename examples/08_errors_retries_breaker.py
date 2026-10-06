"""08: what fails how: retries, a rate limit's own delay, and the circuit breaker.

Completions retry a status in ``RETRYABLE`` (and transport failures) with jittered backoff,
use a 429's own delay as given, and stop sending once ``circuit_failure_threshold`` calls in
a row have failed. Every error says whether the same call may succeed later. Offline.

    uv run python examples/08_errors_retries_breaker.py
"""

import asyncio

from _fake_gateway import KEY, URL, FakeGateway

from bifrost_sdk import RETRYABLE, Bifrost, CircuitOpen, ServerError


async def main() -> None:
    with FakeGateway() as gw:
        async with Bifrost(
            f"{URL}/v1",
            model="provider/model",
            api_key=KEY,
            max_retries=2,
            backoff_seconds=0.01,  # tiny, so the example is quick
            circuit_failure_threshold=2,
            circuit_open_seconds=60,
        ) as bf:
            gw.fail_next(503)  # one failure, then the retry succeeds
            print("retried, then:", await bf.chat("hello"))

            gw.fail_next(429)  # the delay comes from the body: "Please retry in 0.01s"
            print("after a rate limit:", await bf.chat("hello again"))

            for _ in range(2):  # two calls whose retries are all spent: the circuit opens
                gw.fail_next(500, times=3)
                try:
                    await bf.chat("are you there?")
                except ServerError as exc:
                    print(f"ServerError {exc.status}, retryable={exc.retryable}")

            sent = len(gw.seen)
            try:
                await bf.chat("and now?")
            except CircuitOpen as exc:
                print(f"CircuitOpen: nothing sent, retry in about {exc.retry_after:.0f}s")
            assert len(gw.seen) == sent
            print("retried statuses:", sorted(RETRYABLE))


if __name__ == "__main__":
    asyncio.run(main())
