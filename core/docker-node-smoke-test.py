#!/usr/bin/env python3
"""Verify that CORE can create, connect and remove real Docker nodes."""

import os
import re
import subprocess

from core.emulator.coreemu import CoreEmu
from core.emulator.data import IpPrefixes, LinkOptions
from core.emulator.enumerations import EventTypes
from core.nodes.docker import DockerNode, DockerOptions


NODE_IMAGE = os.environ.get("CORE_NODE_IMAGE", "qkdnetsim-core-node:22.04")
DELAY_US = 5_000
BANDWIDTH_BPS = 100_000_000
PING_COUNT = 3


def container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    suffix = str(os.getpid())
    alice_name = f"qkd-core-smoke-alice-{suffix}"
    bob_name = f"qkd-core-smoke-bob-{suffix}"
    node_names = (alice_name, bob_name)

    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)

    try:
        prefixes = IpPrefixes(ip4_prefix="10.251.0.0/24")
        node_options = DockerOptions(image=NODE_IMAGE)
        alice = session.add_node(
            DockerNode, name=alice_name, options=node_options
        )
        bob = session.add_node(
            DockerNode,
            name=bob_name,
            options=DockerOptions(image=NODE_IMAGE),
        )

        alice_iface = prefixes.create_iface(alice)
        bob_iface = prefixes.create_iface(bob)
        link_options = LinkOptions(delay=DELAY_US, bandwidth=BANDWIDTH_BPS)
        session.add_link(
            alice.id,
            bob.id,
            alice_iface,
            bob_iface,
            link_options,
        )
        session.instantiate()

        if not alice.alive() or not bob.alive():
            raise RuntimeError("CORE did not keep both Docker nodes running")

        alice_interfaces = alice.cmd("ip -brief address show")
        bob_interfaces = bob.cmd("ip -brief address show")
        print(f"[{alice_name}]\n{alice_interfaces}")
        print(f"[{bob_name}]\n{bob_interfaces}")
        if "10.251.0.1/24" not in alice_interfaces:
            raise RuntimeError("Alice did not receive its CORE-managed address")
        if "10.251.0.2/24" not in bob_interfaces:
            raise RuntimeError("Bob did not receive its CORE-managed address")

        ping_output = alice.cmd(f"ping -c {PING_COUNT} -W 2 10.251.0.2")
        print(ping_output)

        packet_summary = re.search(
            r"(\d+) packets transmitted, (\d+) received, (\d+)% packet loss",
            ping_output,
        )
        if not packet_summary:
            raise RuntimeError("Could not parse the Docker-node ping result")
        transmitted, received, loss = map(int, packet_summary.groups())
        if transmitted != PING_COUNT or received != PING_COUNT or loss != 0:
            raise RuntimeError("Docker-node link did not achieve lossless connectivity")

        rtt_summary = re.search(
            r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms",
            ping_output,
        )
        if not rtt_summary:
            raise RuntimeError("Could not parse the Docker-node RTT")
        minimum_rtt_ms = float(rtt_summary.group(1))
        if minimum_rtt_ms < 8.0:
            raise RuntimeError(
                f"configured delay is not visible in RTT: {minimum_rtt_ms} ms"
            )

        print(
            "[CORE_DOCKER_NODE_SMOKE] OK "
            f"image={NODE_IMAGE} delayUs={DELAY_US} "
            f"bandwidthBps={BANDWIDTH_BPS}"
        )
    finally:
        session.shutdown()
        coreemu.shutdown()

    leaked_nodes = [name for name in node_names if container_exists(name)]
    if leaked_nodes:
        raise RuntimeError(f"CORE did not remove Docker nodes: {leaked_nodes}")
    print("[CORE_DOCKER_NODE_SMOKE] cleanup=OK")


if __name__ == "__main__":
    main()
