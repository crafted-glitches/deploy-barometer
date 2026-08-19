# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Client for the shouldideploy.today API.

This module is the app's only contact with the outside world's opinion on
deploying. It wraps a single HTTP GET and normalises the response into a
:class:`Verdict`, which the rest of the app treats as the source of truth for
both what to show and what to play.

The upstream API is deliberately tiny. It answers with a boolean and a randomly
chosen quip::

    {"timezone": "Europe/Berlin",
     "date": "2026-08-18T11:29:03.000Z",
     "shouldideploy": true,
     "message": "I don't see why not"}

Two properties of that API shape the design here:

1. There is no third state. The answer is yes or no, which is why the app has
   exactly two colours and two sounds rather than a scale.
2. The quip is re-rolled on every request, even within the same second and for
   the same verdict. Asking again is therefore meaningful, which is what makes
   the bar's start/pause button worth pressing.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import settings


@dataclass(frozen=True)
class Verdict:
    """One answer from shouldideploy.today.

    A frozen dataclass, so an in-flight verdict cannot be mutated by the code
    that renders it. Instances are cheap and are created fresh per request
    rather than cached: the whole point is that the quip changes each time.

    Note that :attr:`background` is resolved from configuration at *access*
    time rather than being stored on the instance. A verdict created before a
    settings change will therefore reflect the new colours, which keeps the
    dataclass purely about the API response and leaves presentation to config.

    Attributes:
        should_deploy: The API's verdict. ``True`` means deploying today is
            fine, ``False`` means it is not. Derived from the response's
            ``shouldideploy`` field.
        message: The human-readable quip, e.g. ``"I don't see why not"`` or
            ``"Tomorrow?"``. Ranges from 2 to ~67 characters in practice and
            may contain non-ASCII characters such as emoji, which are stripped
            later by :func:`barometer.fonts.sanitize` before drawing.
        timezone: The IANA timezone the API resolved the answer in, echoed back
            from the request. Empty string if the response omitted it.
        date: The timestamp the API answered at, as an ISO-8601 string. Empty
            string if the response omitted it.

    Example:
        >>> verdict = Verdict(True, "Go for it", "Europe/Berlin", "")
        >>> verdict.sound
        'go.snd'
    """

    should_deploy: bool
    message: str
    timezone: str
    date: str

    @property
    def sound(self) -> str:
        """Filename of the uploaded sound asset matching this verdict.

        The name is relative to this application's asset directory on the
        device (``/ext/user_assets/<app_name>/``), which is where
        :meth:`barometer.busybar.BusyBarDisplay.ensure_sounds` uploads the
        synthesised waveforms on startup.

        Returns:
            ``"go.snd"`` for a positive verdict, ``"stop.snd"`` otherwise.
        """
        return "go.snd" if self.should_deploy else "stop.snd"

    @property
    def background(self) -> str:
        """Panel background colour matching this verdict.

        Read from configuration on each access, so changing
        ``BAROMETER_COLOR_GO`` / ``BAROMETER_COLOR_STOP`` takes effect without
        rebuilding verdicts.

        Returns:
            An ``#RRGGBBAA`` colour string, as the firmware's draw API expects:
            :attr:`~barometer.config.Settings.color_go` for a positive verdict,
            :attr:`~barometer.config.Settings.color_stop` otherwise.
        """
        return settings.color_go if self.should_deploy else settings.color_stop


async def fetch_verdict(client: httpx.AsyncClient) -> Verdict:
    """Ask shouldideploy.today whether today is a good day to deploy.

    Issues a single ``GET`` against :attr:`~barometer.config.Settings.api_url`.
    The ``tz`` query parameter is only sent when a timezone is configured;
    omitting it lets the API apply its own default rather than forcing one.

    The caller supplies the client so that connections are pooled across
    requests and the app owns its lifecycle — this function never creates or
    closes one.

    Args:
        client: An open async HTTP client. Not closed by this function.

    Returns:
        The parsed verdict.

    Raises:
        httpx.HTTPStatusError: The API answered with a 4xx or 5xx status.
        httpx.HTTPError: The request failed outright — DNS failure, connection
            error, or the 10 second timeout expiring. The caller surfaces this
            as a ``502`` rather than letting it escape.
        KeyError: The response was valid JSON but lacked ``shouldideploy`` or
            ``message``. Treated as fatal because a verdict without those two
            fields cannot be displayed at all, whereas ``timezone`` and ``date``
            are cosmetic and default to an empty string.
        json.JSONDecodeError: The response body was not JSON.
    """
    params = {"tz": settings.timezone} if settings.timezone else {}
    response = await client.get(settings.api_url, params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()
    return Verdict(
        should_deploy=bool(payload["shouldideploy"]),
        message=str(payload["message"]),
        timezone=str(payload.get("timezone", "")),
        date=str(payload.get("date", "")),
    )
