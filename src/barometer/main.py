# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""HTTP surface and application wiring.

Exposes deliberately few endpoints: one to trigger a reading, one to report
health. Everything else — the button watcher, the mDNS announcement, asset
upload — is lifecycle managed by the ASGI lifespan and needs no route of its
own.

The physical start/pause button runs the *same* refresh path as the HTTP
trigger, so there is exactly one code path that can put a verdict on the bar,
whatever set it off. The only difference is a ``source`` field recording which
did.

``/check`` accepts both ``GET`` and ``POST``. ``POST`` is the semantically
correct verb, since the call has a side effect; ``GET`` is accepted as well so
browser bookmarks, home-automation triggers and a bare ``curl`` all work
without ceremony.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .busybar import BusyBarDisplay
from .discovery import Announcer
from .config import settings
from .verdict import fetch_verdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("barometer")


class Barometer:
    """Coordinates fetching a verdict and rendering it, one reading at a time.

    Owns the two long-lived resources — the HTTP client used for the upstream
    API and the device driver — and guarantees that only one refresh runs at a
    time.

    That serialisation matters: a button press and an API call arriving
    together would otherwise interleave their draws, and because a scrolling
    verdict keeps drawing frames after its request returns, the result could be
    two animations fighting over the panel. The lock makes each reading
    atomic from the display's point of view.

    Attributes:
        bar: The device driver.
        http: Shared client for the upstream API, so connections are pooled
            across readings.
        last_reading: The most recent successful reading, or ``None`` before
            the first one. Surfaced by the health endpoint. Failed refreshes
            leave the previous value in place, since it still describes what is
            actually on the panel.
    """

    def __init__(self) -> None:
        """Create the driver, HTTP client and refresh lock.

        No connections are opened and no I/O is performed; both clients connect
        lazily on first use.
        """
        self.bar = BusyBarDisplay()
        self.http = httpx.AsyncClient()
        self._lock = asyncio.Lock()
        self.last_reading: dict[str, Any] | None = None

    async def refresh(self, source: str) -> dict[str, Any]:
        """Fetch the next verdict and render it on the bar.

        The whole operation is held under a lock, so concurrent callers queue
        rather than interleave. Each call fetches afresh — there is no caching,
        deliberately, because the upstream API re-rolls its quip on every
        request and that variety is the entire reason for asking again.

        Args:
            source: What triggered this reading — ``"api"`` or ``"button"``.
                Recorded in the result and the log line; has no effect on
                behaviour.

        Returns:
            The reading: the verdict's fields, the ``source``, a UTC
            ``shown_at`` timestamp, and a ``display`` description of exactly
            what was rendered.

        Raises:
            httpx.HTTPError: The upstream API could not be reached.
            Exception: Propagated from the device if the draw failed. Note that
                :attr:`last_reading` is only updated after both the fetch and
                the draw succeed, so it never describes a verdict that failed
                to reach the panel.
        """
        async with self._lock:
            verdict = await fetch_verdict(self.http)
            rendered = await self.bar.show(verdict)
            reading = {
                "should_deploy": verdict.should_deploy,
                "message": verdict.message,
                "timezone": verdict.timezone,
                "date": verdict.date,
                "source": source,
                "shown_at": datetime.now(timezone.utc).isoformat(),
                "display": rendered,
            }
            self.last_reading = reading
            logger.info(
                "%s -> %s | %s",
                source,
                "DEPLOY" if verdict.should_deploy else "DO NOT DEPLOY",
                rendered["text"],
            )
            return reading

    async def aclose(self) -> None:
        """Close the HTTP client and the device connection.

        Called once during shutdown. The HTTP client is closed first because
        the device driver's teardown also cancels any running animation, and
        doing that last keeps the panel under control for as long as possible.
        """
        await self.http.aclose()
        await self.bar.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage everything that lives as long as the application does.

    On startup, in order: build the coordinator, upload the sound assets, start
    the button watcher, and announce the service over mDNS. On shutdown the
    same resources are released in reverse.

    Two startup steps are deliberately non-fatal. A failed asset upload only
    costs sound, and a failed mDNS announcement only costs a convenient
    hostname; neither justifies refusing to serve, since the display — the
    actual point of the app — works without either. A failure to reach the
    device at all is likewise not fatal here: the bar may simply be asleep, and
    the watcher will reconnect when it returns.

    The button watcher is cancelled and awaited before the clients close, so no
    in-flight refresh outlives the transport it would write to.

    Args:
        app: The application instance. The coordinator is attached to
            ``app.state`` so request handlers can reach it.

    Yields:
        Control to the server for the lifetime of the application.
    """
    barometer = Barometer()
    app.state.barometer = barometer

    try:
        await barometer.bar.ensure_sounds()
    except Exception as error:
        logger.warning("could not upload sounds to the bar: %s", error)

    watcher = asyncio.create_task(
        barometer.bar.watch_start_button(lambda: barometer.refresh("button")),
        name="start-button-watcher",
    )
    logger.info("listening for the start/pause button on %s", settings.busybar_host)

    announcer: Announcer | None = None
    if settings.mdns_enabled:
        announcer = Announcer(settings.mdns_name, settings.port, settings.mdns_address)
        if not await announcer.start():
            announcer = None

    try:
        yield
    finally:
        if announcer is not None:
            await announcer.stop()
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        await barometer.aclose()


app = FastAPI(
    title="Deploy Barometer",
    description="Shows shouldideploy.today on a BUSY Bar, with 8-bit sound.",
    version="1.1.0",
    lifespan=lifespan,
)


@app.api_route("/check", methods=["GET", "POST"])
async def check() -> JSONResponse:
    """Fetch the current verdict and display it on the bar.

    The trigger endpoint. Accepts ``GET`` as well as ``POST`` so that a plain
    ``curl``, a browser bookmark or a ``GET``-only webhook all work.

    Both failure modes answer ``502``, since in each case this service is a
    working intermediary reporting that something it depends on failed. They
    are distinguished by the ``error`` field, and separated in the log: an
    unreachable upstream API is an ordinary external blip logged as an error,
    while a display failure is logged with a traceback because it more often
    indicates a real problem here — a wedged device, or a draw rejected for
    insufficient priority.

    Returns:
        ``200`` with the full reading, or ``502`` with ``error`` and ``detail``
        describing what failed.
    """
    barometer: Barometer = app.state.barometer
    try:
        return JSONResponse(await barometer.refresh("api"))
    except httpx.HTTPError as error:
        logger.error("shouldideploy.today unreachable: %s", error)
        return JSONResponse(
            {"error": "could not reach shouldideploy.today", "detail": str(error)},
            status_code=502,
        )
    except Exception as error:
        logger.exception("could not display the verdict")
        return JSONResponse(
            {"error": "could not display on the bar", "detail": str(error)},
            status_code=502,
        )


@app.get("/health")
async def health() -> JSONResponse:
    """Report whether the application and the bar are both alive.

    The application is alive by definition if this responds, so the meaningful
    signal is the device. An unreachable bar yields ``503`` with the reason
    attached, which is what the container's health check acts on.

    The last successful reading is included so the endpoint doubles as "what is
    on the panel right now?" without needing a third route.

    Returns:
        ``200`` when the device answered, ``503`` when it did not. The body
        carries ``status``, a ``device`` summary and ``last_reading``.
    """
    barometer: Barometer = app.state.barometer
    try:
        device = await barometer.bar.ping()
    except Exception as error:
        device = {"reachable": False, "host": settings.busybar_host, "error": str(error)}

    healthy = bool(device.get("reachable"))
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "device": device,
            "last_reading": barometer.last_reading,
        },
        status_code=200 if healthy else 503,
    )


def main() -> None:
    """Run the application under uvicorn. Console-script entry point.

    Binds to the configured host and port, which default to all interfaces on
    2323 — so the API answers on localhost, on the machine's LAN address, and
    on its announced ``.local`` name.

    uvicorn is imported here rather than at module scope so that importing this
    module — to mount the ASGI app under another server, or to test a handler —
    does not pull in a server the caller may not want.
    """
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
