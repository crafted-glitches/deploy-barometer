# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Show shouldideploy.today's verdict on a BUSY Bar.

An HTTP service that asks `shouldideploy.today <https://shouldideploy.today>`_
whether today is a good day to deploy, then renders the answer on a BUSY Bar's
72x16 LED matrix: the panel floods green or red, the quip is fitted to the
screen, and an 8-bit sound plays. Pressing the bar's physical start/pause
button fetches the next quip.

Modules:
    main: HTTP endpoints and application lifecycle.
    busybar: Device driver -- assets, drawing, animation, button input.
    layout: Decides font, line breaks and whether anything scrolls.
    fonts: Text measurement against metrics calibrated on the hardware.
    sounds: Synthesises the two 8-bit cues as raw PCM.
    verdict: Client for the upstream API.
    discovery: Announces the service as ``deploy-barometer.local``.
    config: Environment-driven settings.

The interesting constraint is the panel: 72x16 pixels, proportional bitmap
fonts, and no way to ask the firmware how wide a glyph is. Every width used
here was measured on the device itself and reproduces its rendering exactly.
See :mod:`barometer.fonts`.
"""
