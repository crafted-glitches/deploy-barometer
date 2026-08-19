# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Read the bar's front framebuffer back as an image (development tooling).

`GET /api/screen` is declared as `image/bmp` in the firmware's OpenAPI spec but
actually returns base64-encoded raw pixels with no header -- 72*16*3 bytes for
the front panel. It keeps BMP's channel order, which is **BGR, not RGB**, so
the channels are swapped here.

Verified on device: a PNG with a pure-red left half and pure-blue right half
displays correctly on the panel but reads back with the halves swapped. The
panel itself is fine; only this endpoint's byte order is reversed.
"""

from __future__ import annotations

import base64

import httpx
from PIL import Image

FRONT_WIDTH, FRONT_HEIGHT = 72, 16


def capture_front(client: httpx.Client) -> Image.Image:
    """Read the front panel's current contents as a true-colour RGB image.

    Fetches the raw framebuffer and corrects its channel order. The endpoint
    returns no image header of any kind, so the dimensions and pixel format are
    supplied here rather than parsed.

    Args:
        client: HTTP client with the device as its base URL.

    Returns:
        A 72x16 RGB image of exactly what the panel is showing, with channels
        in the order Pillow expects.

    Raises:
        httpx.HTTPStatusError: The device rejected the request.
        ValueError: The response was not the expected 3,456 bytes, which would
            mean the pixel format assumed here no longer holds.
    """
    response = client.get("/api/screen", params={"display": 0})
    response.raise_for_status()
    raw = base64.b64decode(response.content)
    image = Image.frombytes("RGB", (FRONT_WIDTH, FRONT_HEIGHT), raw)
    blue, green, red = image.split()
    return Image.merge("RGB", (red, green, blue))


def ink_box(image: Image.Image) -> tuple[int, int]:
    """Measure the extent of everything that is not black.

    Used against probe renders drawn as white text on a black flood, where the
    non-black region is precisely the glyph ink. Channel order is irrelevant
    here — black is black either way — so this is equally correct on a raw or
    corrected capture.

    Args:
        image: A captured panel image.

    Returns:
        ``(width, height)`` of the ink's bounding box in pixels, or ``(0, 0)``
        for a fully black image.
    """
    box = image.getbbox()
    if box is None:
        return 0, 0
    return box[2] - box[0], box[3] - box[1]
