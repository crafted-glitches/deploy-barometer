# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the mDNS announcement.

No multicast traffic is produced. ``zeroconf`` is imported lazily inside
:meth:`barometer.discovery.Announcer.start`, which makes it straightforward to
substitute a double in :data:`sys.modules` and assert on what *would* have been
published.

The behaviour that matters most is the failure path: a network that blocks
multicast, or a name already claimed, must cost nothing more than a convenient
hostname. The API stays reachable by IP regardless, so announcing must never be
able to take the app down.
"""

from __future__ import annotations

import socket
import sys
import types
from typing import ClassVar

import pytest

from barometer import discovery


class FakeZeroconf:
    """Records registrations instead of publishing them."""

    instances: ClassVar[list[FakeZeroconf]] = []

    def __init__(self) -> None:
        """Start with empty registration logs and register self for inspection."""
        self.registered: list[object] = []
        self.unregistered: list[object] = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info: object) -> None:
        """Record a registration."""
        self.registered.append(info)

    async def async_unregister_service(self, info: object) -> None:
        """Record a withdrawal."""
        self.unregistered.append(info)

    async def async_close(self) -> None:
        """Mark the instance closed."""
        self.closed = True


class FakeServiceInfo:
    """Captures the service description that would be published."""

    def __init__(self, type_: str, name: str, **kwargs: object) -> None:
        """Capture the service description without publishing it."""
        self.type_ = type_
        self.name = name
        self.kwargs = kwargs


@pytest.fixture
def fake_zeroconf(monkeypatch: pytest.MonkeyPatch):
    """Substitute the zeroconf package with recording doubles."""
    FakeZeroconf.instances.clear()
    root = types.ModuleType("zeroconf")
    root.ServiceInfo = FakeServiceInfo  # type: ignore[attr-defined]
    asyncio_mod = types.ModuleType("zeroconf.asyncio")
    asyncio_mod.AsyncZeroconf = FakeZeroconf  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zeroconf", root)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", asyncio_mod)
    return FakeZeroconf


class TestLocalIp:
    """Discovering which address to advertise."""

    def test_returns_a_dotted_quad(self) -> None:
        """The result must be usable as an IPv4 literal."""
        address = discovery.local_ip()
        assert socket.inet_aton(address)

    def test_probes_a_public_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The target must be public, and no packet is actually sent.

        Probing the bar's own address would return the USB interface
        (``10.0.4.x``) rather than the LAN one, advertising a name that nothing
        else on the network can reach.
        """
        connected: list[tuple[str, int]] = []

        class FakeSocket:
            """A socket that records its target instead of using the network."""

            def connect(self, address: tuple[str, int]) -> None:
                """Record the probe target; no packet is sent."""
                connected.append(address)

            def getsockname(self) -> tuple[str, int]:
                """Return the address the kernel would have chosen."""
                return ("192.168.1.50", 12345)

            def close(self) -> None:
                """Accept closure silently."""

        monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket())
        assert discovery.local_ip() == "192.168.1.50"
        assert not connected[0][0].startswith("10.0.4.")

    def test_socket_is_closed_even_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe must not leak a descriptor when the network is down."""
        closed: list[bool] = []

        class FailingSocket:
            """A socket whose connect fails, to test cleanup."""

            def connect(self, address: tuple[str, int]) -> None:
                """Fail as an unreachable network would."""
                raise OSError("network unreachable")

            def close(self) -> None:
                """Record that the descriptor was released."""
                closed.append(True)

        monkeypatch.setattr(socket, "socket", lambda *a, **k: FailingSocket())
        with pytest.raises(OSError):
            discovery.local_ip()
        assert closed == [True]


class TestAnnouncerNaming:
    """The published identity."""

    def test_hostname_gains_the_local_suffix(self) -> None:
        """``.local`` is what makes the name resolvable over mDNS."""
        assert discovery.Announcer("deploy-barometer", 2323, "1.2.3.4").hostname == (
            "deploy-barometer.local"
        )

    def test_url_combines_name_and_port(self) -> None:
        """Logged at startup so the working address is visible immediately."""
        assert discovery.Announcer("bar", 2323, "1.2.3.4").url == "http://bar.local:2323"

    def test_explicit_address_is_used_verbatim(self) -> None:
        """An override skips auto-detection entirely."""
        assert discovery.Announcer("bar", 1, "10.9.9.9").address == "10.9.9.9"

    def test_empty_address_triggers_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The common case detects the LAN address at construction."""
        monkeypatch.setattr(discovery, "local_ip", lambda: "192.168.0.7")
        assert discovery.Announcer("bar", 1).address == "192.168.0.7"


class TestAnnouncerLifecycle:
    """Registering and withdrawing the name."""

    async def test_start_registers_the_service(self, fake_zeroconf) -> None:
        """A successful start publishes exactly one service."""
        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        assert await announcer.start() is True
        assert len(fake_zeroconf.instances[0].registered) == 1

    async def test_registration_publishes_a_resolvable_hostname(self, fake_zeroconf) -> None:
        """The A record is what ``curl bar.local`` actually needs.

        Publishing only a service would list the app in service browsers while
        leaving the hostname unresolvable.
        """
        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        await announcer.start()
        info = fake_zeroconf.instances[0].registered[0]
        assert info.kwargs["server"] == "bar.local."
        assert info.kwargs["port"] == 2323
        assert info.kwargs["addresses"] == [socket.inet_aton("1.2.3.4")]

    async def test_stop_withdraws_and_closes(self, fake_zeroconf) -> None:
        """An explicit goodbye stops resolvers serving a stale name."""
        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        await announcer.start()
        await announcer.stop()
        instance = fake_zeroconf.instances[0]
        assert len(instance.unregistered) == 1
        assert instance.closed is True

    async def test_stop_without_start_is_safe(self) -> None:
        """Shutdown must not depend on startup having succeeded."""
        await discovery.Announcer("bar", 2323, "1.2.3.4").stop()

    async def test_stop_is_idempotent(self, fake_zeroconf) -> None:
        """Calling it twice must not raise."""
        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        await announcer.start()
        await announcer.stop()
        await announcer.stop()

    async def test_failure_returns_false_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blocked network costs a hostname, never the app.

        Announcing is a convenience; the API is reachable by IP regardless.
        """
        broken = types.ModuleType("zeroconf")

        def explode(*args: object, **kwargs: object) -> None:
            """Fail on any use, as a network blocking multicast would."""
            raise RuntimeError("multicast blocked")

        broken.ServiceInfo = explode  # type: ignore[attr-defined]
        asyncio_mod = types.ModuleType("zeroconf.asyncio")
        asyncio_mod.AsyncZeroconf = explode  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "zeroconf", broken)
        monkeypatch.setitem(sys.modules, "zeroconf.asyncio", asyncio_mod)

        assert await discovery.Announcer("bar", 2323, "1.2.3.4").start() is False

    async def test_failed_start_leaves_nothing_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial state is torn down, so a retry starts clean."""
        class HalfBroken(FakeZeroconf):
            """Constructs successfully but refuses to register.

            Reproduces a name already claimed by another host, which is the
            case that would leave partial state behind.
            """

            async def async_register_service(self, info: object) -> None:
                """Fail as a name collision would."""
                raise RuntimeError("name already claimed")

        root = types.ModuleType("zeroconf")
        root.ServiceInfo = FakeServiceInfo  # type: ignore[attr-defined]
        asyncio_mod = types.ModuleType("zeroconf.asyncio")
        asyncio_mod.AsyncZeroconf = HalfBroken  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "zeroconf", root)
        monkeypatch.setitem(sys.modules, "zeroconf.asyncio", asyncio_mod)

        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        assert await announcer.start() is False
        assert announcer._zeroconf is None
