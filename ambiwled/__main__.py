"""Entry point.  Comes up and serves the UI even if the TV is unreachable."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from . import config as config_mod
from .bridge import Bridge
from .server import Server

log = logging.getLogger("ambiwled")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def main() -> None:
    cfg = config_mod.load()
    _setup_logging(cfg.get("log_level", "INFO"))

    problems = config_mod.validate(cfg)
    if problems:
        log.error("config validation: %s", "; ".join(problems))
    else:
        log.info("config valid: %d pixels, %d edges",
                 cfg["led"]["count"], len(cfg["mapping"]["edges"]))

    bridge = Bridge(cfg)
    server = Server(bridge)

    runner = await server.start()
    await bridge.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("shutting down")
    # Persist first: everything below is network teardown that can take seconds,
    # and a container stop will SIGKILL us partway through it.
    server.flush_config()
    await bridge.stop()
    await server.stop(runner)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
