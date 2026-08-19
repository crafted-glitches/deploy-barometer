# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Announce the app on the local network as `<name>.local` over mDNS.

Publishing an A record for `deploy-barometer.local` means the API can be reached
by name from this machine and from anything else on the network, alongside
`localhost` and the raw IP.

This has to run on the host. A container cannot do it: Docker's NAT does not
carry multicast onto the LAN, and the address a container knows about is its own
bridge IP, not the machine's. `scripts/announce.py` covers that case.
"""

from __future__ import annotations

import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

#: Advertised alongside the A record so service browsers list it nicely.
SERVICE_TYPE = "_http._tcp.local."


def local_ip() -> str:
    """Best guess at this machine's address on the network it routes through.

    Opens a UDP socket towards a public address and reads back which local
    interface the kernel would use. No packets are actually sent. A public
    target matters: probing the bar's own address would return the USB
    interface (10.0.4.x) rather than the LAN one.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


class Announcer:
    """Publishes ``<name>.local`` for as long as it is registered.

    Registers both an mDNS service (so the app appears in service browsers) and
    an A record for the hostname (so plain name resolution works, which is what
    ``curl deploy-barometer.local:2323`` actually needs).

    Uses zeroconf's **asyncio** interface throughout. The synchronous API
    performs blocking socket work; called from inside a running event loop it
    refuses to register, warns about blocking I/O, and stalls startup by tens
    of seconds — observed as a ~24 second delay before the server accepted its
    first connection.

    Attributes:
        name: Published name, without the ``.local`` suffix.
        port: Port advertised in the service record.
        address: IPv4 address the name resolves to, resolved at construction.
        _zeroconf: The async zeroconf instance while registered, else ``None``.
        _info: The registered service description while registered.
    """

    def __init__(self, name: str, port: int, address: str = "") -> None:
        """Prepare an announcement without publishing anything yet.

        The address is resolved here rather than at registration, so a caller
        can inspect what would be advertised before committing to it.

        Args:
            name: Name to publish, without ``.local``.
            port: Port to advertise.
            address: IPv4 address to advertise. Empty auto-detects this
                machine's LAN address via :func:`local_ip`.
        """
        self.name = name
        self.port = port
        self.address = address or local_ip()
        self._zeroconf: Any = None
        self._info: Any = None

    @property
    def hostname(self) -> str:
        """The fully qualified name this announcer publishes.

        Returns:
            The name with its ``.local`` suffix, e.g.
            ``deploy-barometer.local``.
        """
        return f"{self.name}.local"

    @property
    def url(self) -> str:
        """The base URL the published name and port resolve to.

        Returns:
            A URL such as ``http://deploy-barometer.local:2323``.
        """
        return f"http://{self.hostname}:{self.port}"

    async def start(self) -> bool:
        """Register the name on the local network.

        Never raises. A network that blocks multicast, a name already claimed
        by another host, or zeroconf being unavailable all cost one convenience
        — the API remains reachable by IP and by localhost regardless — so
        failure is logged and reported rather than propagated. Partial state is
        torn down before returning, so a failed start leaves nothing registered.

        The ``zeroconf`` import is deferred to here so the dependency is only
        loaded when announcing is actually enabled.

        Returns:
            ``True`` if the name was registered, ``False`` otherwise.
        """
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf

            self._zeroconf = AsyncZeroconf()
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(self.address)],
                port=self.port,
                server=f"{self.name}.local.",
                properties={"path": "/check"},
            )
            await self._zeroconf.async_register_service(self._info)
        except Exception as error:
            logger.warning("could not announce %s over mDNS: %r", self.hostname, error)
            await self.stop()
            return False

        logger.info("announced %s/check -> %s", self.url, self.address)
        return True

    async def stop(self) -> None:
        """Withdraw the name and release the mDNS socket.

        Sends an explicit goodbye so resolvers drop the record promptly instead
        of serving a stale name until it expires. Errors during unregistration
        are ignored — this runs during shutdown, where the socket may already
        be gone and nothing useful remains to be done — but the instance is
        always closed and the state cleared, so a failed withdrawal cannot leak
        a socket. Safe to call when not registered, and safe to call twice.
        """
        try:
            if self._zeroconf is not None and self._info is not None:
                await self._zeroconf.async_unregister_service(self._info)
        except Exception:
            pass
        finally:
            if self._zeroconf is not None:
                await self._zeroconf.async_close()
            self._zeroconf = self._info = None
