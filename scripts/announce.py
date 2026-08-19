# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Publish `deploy-barometer.local` from this machine.

Only needed when the app runs in Docker: a container's mDNS never reaches the
LAN, because Docker's NAT does not carry multicast and the address the
container knows is its own bridge IP. Run this on the host alongside
`docker compose up` and the name resolves to this machine.

    python scripts/announce.py            # uses .env / defaults
    python scripts/announce.py --name bar --port 2323

When running the app natively you do not need this -- it announces itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from barometer.config import settings  # noqa: E402
from barometer.discovery import Announcer  # noqa: E402


async def run() -> int:
    """Publish the name and hold it until interrupted.

    The announcement only exists while this process does: mDNS records are not
    persisted anywhere, so the process must stay alive for the name to keep
    resolving. It idles on an event that is never set, waking only to shut
    down, so holding the name costs effectively nothing.

    Command-line arguments default to the application's own configuration, so
    running this bare announces exactly what the app would have announced
    itself.

    Returns:
        ``0`` after a clean shutdown, ``1`` if the name could not be registered
        — typically because another host already claims it, or the network
        blocks multicast.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=settings.mdns_name)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--address", default=settings.mdns_address,
                        help="Address to advertise; defaults to this machine's LAN IP.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    announcer = Announcer(args.name, args.port, args.address)
    if not await announcer.start():
        return 1

    print(f"  {announcer.url}/check")
    print("Ctrl-C to stop.")
    try:
        await asyncio.Event().wait()          # run until interrupted
    finally:
        await announcer.stop()
    return 0


def main() -> int:
    """Run the announcer, treating Ctrl-C as a normal exit.

    Returns:
        The exit status: ``0`` for a clean stop or a user interrupt, ``1`` if
        registration failed.
    """
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
