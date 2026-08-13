#!/usr/bin/env python3
"""Create a minimal delayed wired CORE network and verify connectivity."""

from core.emulator.coreemu import CoreEmu
from core.emulator.data import IpPrefixes, LinkOptions
from core.emulator.enumerations import EventTypes
from core.nodes.base import CoreNode


def main() -> None:
    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)

    try:
        prefixes = IpPrefixes(ip4_prefix="10.250.0.0/24")
        alice = session.add_node(CoreNode, name="alice-classical")
        bob = session.add_node(CoreNode, name="bob-classical")

        alice_iface = prefixes.create_iface(alice)
        bob_iface = prefixes.create_iface(bob)
        link = LinkOptions(delay=5_000, bandwidth=100_000_000)
        session.add_link(alice.id, bob.id, alice_iface, bob_iface, link)
        session.instantiate()

        output = alice.cmd("ping -c 3 -W 2 10.250.0.2")
        print(output)
        if "0% packet loss" not in output:
            raise RuntimeError("CORE smoke test did not achieve lossless connectivity")

        print("[CORE_SMOKE] OK direct link delayUs=5000 bandwidthBps=100000000")
    finally:
        session.shutdown()
        coreemu.shutdown()


if __name__ == "__main__":
    main()
