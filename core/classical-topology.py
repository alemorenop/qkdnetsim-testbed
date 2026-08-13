#!/usr/bin/env python3
"""Build and verify a configurable classical path between Docker endpoints."""

import argparse
import ipaddress
import os
import re
import subprocess

from core.emulator.coreemu import CoreEmu
from core.emulator.data import InterfaceData, LinkOptions
from core.emulator.enumerations import EventTypes
from core.nodes.base import CoreNode
from core.nodes.docker import DockerNode, DockerOptions


NODE_IMAGE = os.environ.get("CORE_NODE_IMAGE", "qkdnetsim-core-node:22.04")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def percentage(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 100.0:
        raise argparse.ArgumentTypeError("loss must be between 0 and 100")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emulate a direct or routed classical Alice-Bob path"
    )
    parser.add_argument(
        "--routers",
        type=int,
        default=0,
        choices=range(0, 9),
        metavar="0..8",
        help="number of CORE routers between the Docker endpoints",
    )
    parser.add_argument(
        "--delay-ms",
        type=positive_int,
        default=5,
        help="one-way NetEm delay applied independently to every link",
    )
    parser.add_argument(
        "--bandwidth-mbps",
        type=positive_int,
        default=100,
        help="bandwidth applied independently to every link",
    )
    parser.add_argument(
        "--loss-percent",
        type=percentage,
        default=0.0,
        help="packet loss applied independently to every link",
    )
    parser.add_argument(
        "--ping-count",
        type=positive_int,
        default=3,
        help="ICMP probes used for verification",
    )
    return parser.parse_args()


def iface(address: ipaddress.IPv4Address, name: str | None = None) -> InterfaceData:
    return InterfaceData(name=name, ip4=str(address), ip4_mask=30)


def container_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def main() -> None:
    args = parse_args()
    suffix = str(os.getpid())
    alice_name = f"qkd-core-classical-alice-{suffix}"
    bob_name = f"qkd-core-classical-bob-{suffix}"
    endpoint_names = (alice_name, bob_name)

    link_count = args.routers + 1
    link_networks = [
        ipaddress.ip_network(f"10.252.{index}.0/30")
        for index in range(link_count)
    ]
    link_options = LinkOptions(
        delay=args.delay_ms * 1_000,
        bandwidth=args.bandwidth_mbps * 1_000_000,
        loss=args.loss_percent,
    )

    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)

    try:
        alice = session.add_node(
            DockerNode,
            name=alice_name,
            options=DockerOptions(image=NODE_IMAGE),
        )
        bob = session.add_node(
            DockerNode,
            name=bob_name,
            options=DockerOptions(image=NODE_IMAGE),
        )
        routers = [
            session.add_node(CoreNode, name=f"classical-router-{index + 1}")
            for index in range(args.routers)
        ]
        path = [alice, *routers, bob]

        for index, (left, right) in enumerate(zip(path, path[1:])):
            network = link_networks[index]
            session.add_link(
                left.id,
                right.id,
                iface(network.network_address + 1),
                iface(network.network_address + 2),
                link_options,
            )

        session.instantiate()

        if not alice.alive() or not bob.alive():
            raise RuntimeError("CORE did not keep both Docker endpoints running")

        if routers:
            alice.cmd(
                f"ip route replace default via {link_networks[0].network_address + 2}"
            )
            bob.cmd(
                f"ip route replace default via {link_networks[-1].network_address + 1}"
            )

            for index, router in enumerate(routers):
                router.cmd("sysctl -w net.ipv4.ip_forward=1")
                if index > 0:
                    router.cmd(
                        f"ip route replace {link_networks[0]} "
                        f"via {link_networks[index].network_address + 1}"
                    )
                if index < len(routers) - 1:
                    router.cmd(
                        f"ip route replace {link_networks[-1]} "
                        f"via {link_networks[index + 1].network_address + 2}"
                    )

        bob_address = link_networks[-1].network_address + 2
        print(f"[CORE_CLASSICAL] path={' -> '.join(node.name for node in path)}")
        for node in path:
            print(f"[{node.name}]\n{node.cmd('ip -brief address show')}")
            print(node.cmd("ip route show"))

        ping_output = alice.cmd(
            f"ping -c {args.ping_count} -W 3 {bob_address}"
        )
        print(ping_output)
        packet_summary = re.search(
            r"(\d+) packets transmitted, (\d+) received, (\d+)% packet loss",
            ping_output,
        )
        if not packet_summary:
            raise RuntimeError("could not parse the classical-path ping result")
        transmitted, received, measured_loss = map(int, packet_summary.groups())
        if args.loss_percent == 0.0 and (
            transmitted != args.ping_count
            or received != args.ping_count
            or measured_loss != 0
        ):
            raise RuntimeError("lossless classical path dropped packets")

        rtt_summary = re.search(
            r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms",
            ping_output,
        )
        if not rtt_summary:
            raise RuntimeError("could not parse the classical-path RTT")
        minimum_rtt_ms = float(rtt_summary.group(1))
        expected_rtt_ms = 2 * link_count * args.delay_ms
        if minimum_rtt_ms < expected_rtt_ms * 0.8:
            raise RuntimeError(
                "configured per-link delay is not visible in RTT: "
                f"minimum={minimum_rtt_ms} expected~={expected_rtt_ms} ms"
            )

        print(
            "[CORE_CLASSICAL] OK "
            f"routers={args.routers} links={link_count} "
            f"delayPerLinkMs={args.delay_ms} "
            f"bandwidthPerLinkMbps={args.bandwidth_mbps} "
            f"lossPerLinkPercent={args.loss_percent:g}"
        )
    finally:
        session.shutdown()
        coreemu.shutdown()

    leaked_endpoints = [name for name in endpoint_names if container_exists(name)]
    if leaked_endpoints:
        raise RuntimeError(f"CORE did not remove endpoints: {leaked_endpoints}")
    print("[CORE_CLASSICAL] cleanup=OK")


if __name__ == "__main__":
    main()
