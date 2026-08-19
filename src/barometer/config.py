# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Application configuration, resolved once at import time.

Settings are read from environment variables, falling back to a ``.env`` file,
using ``pydantic-settings``. Every field carries a working default except the
device PIN, which is a secret and must be supplied — so the app runs against a
USB-connected bar with almost no configuration, while remaining fully
parameterised.

Nothing here is tied to one particular device. Pointing
``BAROMETER_BUSYBAR_HOST`` at a different bar, over USB or Wi-Fi, is all that
is required to drive it instead.

Every variable takes the ``BAROMETER_`` prefix, so ``busybar_host`` is set with
``BAROMETER_BUSYBAR_HOST``. Unknown variables in the environment are ignored
rather than rejected, so the app coexists with unrelated settings.

Note:
    The ``.env`` file is resolved **relative to the working directory**, not to
    this package. Running from elsewhere silently loses the file, and with it
    the PIN, producing authentication failures against the device. Run from the
    repository root, or pass configuration as real environment variables — which
    is what the container does.

Attributes:
    settings: The single resolved settings instance imported throughout the
        app. Constructed at import time, so an invalid value fails fast at
        startup rather than at first use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: How a message too wide for one line is presented.
#:
#: * ``fit``    -- shrink and wrap so the whole message is on screen at once,
#:                 scrolling downwards only when it is too tall even for the
#:                 smallest font.
#: * ``scroll`` -- keep it on one line and marquee it sideways.
DisplayMode = Literal["fit", "scroll"]


class Settings(BaseSettings):
    """Every knob the application exposes, with validation.

    Grouped by concern: the destination device, the upstream API, presentation,
    network discovery, and the HTTP server itself. Field descriptions are the
    authoritative documentation for each setting and are mirrored in
    ``.env.example``.

    Constraints are enforced by pydantic at construction, so a malformed value
    — a draw priority outside 1–100, a display mode that is neither ``fit`` nor
    ``scroll`` — fails loudly at startup instead of producing puzzling
    behaviour against the device much later.
    """

    model_config = SettingsConfigDict(
        env_prefix="BAROMETER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Destination device ---------------------------------------------------
    busybar_host: str = Field(
        default="10.0.4.20",
        description="Hostname or IP of the BUSY Bar. 10.0.4.20 is the USB address.",
    )
    busybar_token: str = Field(
        default="",
        description="Device API PIN. Required when the bar's HTTP access mode is 'key'.",
    )
    app_name: str = Field(
        default="deploy_barometer",
        description="Application id the bar uses to namespace our assets and drawings.",
    )
    draw_priority: int = Field(
        default=60, ge=1, le=100,
        description="Draw priority. Built-in apps are 10; an active BUSY session is 90.",
    )

    # --- Source API -----------------------------------------------------------
    timezone: str = Field(
        default="",
        description="IANA timezone passed to shouldideploy.today. Empty uses its default.",
    )
    api_url: str = Field(
        default="https://shouldideploy.today/api",
        description="shouldideploy.today API endpoint.",
    )

    # --- Presentation ---------------------------------------------------------
    display_mode: DisplayMode = Field(
        default="fit",
        description=(
            "'fit' shrinks and wraps the message so the whole thing is on screen at "
            "once; 'scroll' keeps it on one line and marquees it sideways."
        ),
    )
    color_go: str = Field(default="#00B000FF", description="Background when deploying is fine.")
    color_stop: str = Field(default="#C00000FF", description="Background when it is not.")
    color_text: str = Field(default="#000000FF", description="Text colour on both backgrounds.")
    display_seconds: int = Field(
        default=60, ge=0,
        description="How long the verdict stays on screen. 0 means until replaced.",
    )
    read_pause_seconds: float = Field(
        default=3.0, ge=0,
        description="Pause before a too-tall message starts scrolling down, so it can be read.",
    )
    sound_enabled: bool = Field(default=True, description="Play the 8-bit verdict sound.")

    # --- Discovery ------------------------------------------------------------
    mdns_enabled: bool = Field(
        default=True,
        description="Announce the API as <mdns_name>.local. Has no effect in a container.",
    )
    mdns_name: str = Field(
        default="deploy-barometer",
        description="Name published over mDNS, without the .local suffix.",
    )
    mdns_address: str = Field(
        default="",
        description="Address to advertise. Empty auto-detects this machine's LAN IP.",
    )

    # --- Server ---------------------------------------------------------------
    host: str = Field(
        default="0.0.0.0",
        description="Address the HTTP server binds to. 0.0.0.0 exposes it on your LAN IP.",
    )
    port: int = Field(default=2323, description="Port the HTTP server binds to.")

    @property
    def busybar_url(self) -> str:
        """Return the bar's base URL, normalising whatever form the host took.

        Accepts a bare hostname or IP (``10.0.4.20``, ``busybar.local``) and
        prefixes the scheme, or passes through a full URL unchanged apart from
        stripping a trailing slash. This means the same variable accepts the
        obvious value a user would type and the explicit one an integration
        might supply, without either being wrong.

        Returns:
            A base URL with no trailing slash, e.g. ``http://10.0.4.20``.
        """
        host = self.busybar_host
        if host.startswith(("http://", "https://")):
            return host.rstrip("/")
        return f"http://{host}"


settings = Settings()
