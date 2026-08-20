# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the framebuffer readback helper.

``scripts/_screen.py`` is development tooling, not part of the running app, but
it is what the font calibration measures through. If its channel handling or
bounding-box arithmetic were wrong, every calibrated metric would be wrong with
it -- and the app's text fitting is built entirely on those numbers.

The channel order is the subtle part. ``GET /api/screen`` is declared
``image/bmp`` but returns base64 raw pixels with no header, keeping BMP's
reversed **BGR** order. That was established on hardware: a PNG with a pure red
left half displays correctly on the panel yet reads back with the halves
swapped. These tests pin the correction so it cannot be "tidied away".
"""

from __future__ import annotations

import base64

import httpx
import pytest
from _screen import FRONT_HEIGHT, FRONT_WIDTH, capture_front, ink_box
from PIL import Image


def client_returning(raw: bytes, status: int = 200) -> httpx.Client:
    """Build a client that returns a fixed base64 framebuffer.

    Args:
        raw: Pixel bytes to encode and serve.
        status: HTTP status to return.

    Returns:
        A client backed by a mock transport, requiring no device.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        """Return the fixed framebuffer, base64-encoded as the device does."""
        return httpx.Response(status, content=base64.b64encode(raw))

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://device")


def framebuffer(pixel: tuple[int, int, int]) -> bytes:
    """Build a full panel of one colour, in the device's byte order.

    Args:
        pixel: The colour as ``(blue, green, red)`` -- device order, not RGB.

    Returns:
        Raw pixel bytes for a full front panel.
    """
    return bytes(pixel) * (FRONT_WIDTH * FRONT_HEIGHT)


class TestCaptureFront:
    """Reading the panel back as an image."""

    def test_returns_panel_sized_rgb_image(self) -> None:
        """Dimensions are supplied by us; the response carries no header."""
        with client_returning(framebuffer((0, 0, 0))) as client:
            image = capture_front(client)
        assert image.size == (FRONT_WIDTH, FRONT_HEIGHT)
        assert image.mode == "RGB"

    def test_red_and_blue_channels_are_swapped(self) -> None:
        """Device bytes are BGR; the returned image must be RGB.

        A device buffer of ``(0, 0, 255)`` is blue-green-red, i.e. pure red,
        and must surface as ``(255, 0, 0)``.
        """
        with client_returning(framebuffer((0, 0, 255))) as client:
            assert capture_front(client).getpixel((0, 0)) == (255, 0, 0)

    def test_blue_is_read_as_blue(self) -> None:
        """The inverse case: device ``(255, 0, 0)`` is pure blue."""
        with client_returning(framebuffer((255, 0, 0))) as client:
            assert capture_front(client).getpixel((0, 0)) == (0, 0, 255)

    def test_green_is_unaffected_by_the_swap(self) -> None:
        """Green sits in the middle channel, which is why the bug hid so long.

        Every early colour test used green, which looks identical either way.
        """
        with client_returning(framebuffer((0, 176, 0))) as client:
            assert capture_front(client).getpixel((0, 0)) == (0, 176, 0)

    def test_swap_is_its_own_inverse(self) -> None:
        """Applying the correction twice restores the original bytes."""
        with client_returning(framebuffer((10, 20, 30))) as client:
            first = capture_front(client)
        blue, green, red = first.split()
        assert Image.merge("RGB", (red, green, blue)).getpixel((0, 0)) == (10, 20, 30)

    def test_http_error_is_raised(self) -> None:
        """A failed read must not be mistaken for a blank panel."""
        with client_returning(framebuffer((0, 0, 0)), status=500) as client:
            with pytest.raises(httpx.HTTPStatusError):
                capture_front(client)

    def test_wrong_payload_size_is_rejected(self) -> None:
        """A short buffer means the assumed pixel format no longer holds."""
        with client_returning(b"\x00" * 10) as client:
            with pytest.raises(ValueError):
                capture_front(client)


class TestInkBox:
    """Measuring the drawn area."""

    def test_blank_panel_measures_zero(self) -> None:
        """An all-black image has no ink, and must not raise."""
        assert ink_box(Image.new("RGB", (FRONT_WIDTH, FRONT_HEIGHT), (0, 0, 0))) == (0, 0)

    def test_measures_the_bounding_box_of_non_black_pixels(self) -> None:
        """Width and height come from the extent of the ink, not the image."""
        image = Image.new("RGB", (FRONT_WIDTH, FRONT_HEIGHT), (0, 0, 0))
        for x in range(4, 12):
            for y in range(2, 7):
                image.putpixel((x, y), (255, 255, 255))
        assert ink_box(image) == (8, 5)

    def test_single_pixel_measures_one_by_one(self) -> None:
        """The smallest possible mark is measured exactly."""
        image = Image.new("RGB", (FRONT_WIDTH, FRONT_HEIGHT), (0, 0, 0))
        image.putpixel((3, 3), (255, 255, 255))
        assert ink_box(image) == (1, 1)

    def test_fully_covered_panel_measures_the_panel(self) -> None:
        """Ink filling the panel reports the panel's own dimensions."""
        image = Image.new("RGB", (FRONT_WIDTH, FRONT_HEIGHT), (255, 255, 255))
        assert ink_box(image) == (FRONT_WIDTH, FRONT_HEIGHT)

    def test_measurement_ignores_colour(self) -> None:
        """Any non-black colour counts as ink, so probe colour is irrelevant."""
        for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (1, 1, 1)):
            image = Image.new("RGB", (FRONT_WIDTH, FRONT_HEIGHT), (0, 0, 0))
            image.putpixel((5, 5), colour)
            assert ink_box(image) == (1, 1), colour
