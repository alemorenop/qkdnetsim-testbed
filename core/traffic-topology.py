#!/usr/bin/env python3
"""Run the native QKDNetSim traffic consumer over the common CORE path."""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from core.emulator.coreemu import CoreEmu
from core.emulator.data import InterfaceData, LinkOptions
from core.emulator.enumerations import EventTypes
from core.nodes.base import CoreNode
from core.nodes.docker import DockerNode, DockerOptions


QKD_IMAGE = os.environ.get("CORE_QKD_IMAGE", "qkdnetsim-testbed:latest")


@dataclass(frozen=True)
class Scenario:
    kms_networks: tuple[str, str]
    kms_ips: tuple[str, str]
    app_kms_ips: tuple[str, str]
    app_ids: tuple[str, str]
    binaries: tuple[str, str]
    peer_flags: tuple[str, str]
    log_markers: tuple[str, str]
    macs: tuple[str, str]
    readiness_containers: tuple[str, str]


SCENARIOS = {
    "point-to-point": Scenario(
        kms_networks=(
            "qkdnetsim-point-to-point_net_alice_kms_app",
            "qkdnetsim-point-to-point_net_bob_kms_app",
        ),
        kms_ips=("192.168.35.3", "192.168.46.4"),
        app_kms_ips=("192.168.35.5", "192.168.46.6"),
        app_ids=(
            "bbbbbbbb-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ),
        binaries=(
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.46-p2p_etsi014_alice-default",
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.46-p2p_etsi014_bob-default",
        ),
        peer_flags=("peerBobIp", "peerAliceIp"),
        log_markers=("P2P_ETSI014_ALICE", "P2P_ETSI014_BOB"),
        macs=("02:00:00:00:35:05", "02:00:00:00:46:06"),
        readiness_containers=("qkd-p2p-kms-alice", "qkd-p2p-kms-bob"),
    ),
    "key-relay": Scenario(
        kms_networks=(
            "qkdnetsim-key-relay_net_alice_kms_app",
            "qkdnetsim-key-relay_net_bob_kms_app",
        ),
        kms_ips=("192.168.119.5", "192.168.120.7"),
        app_kms_ips=("192.168.119.8", "192.168.120.9"),
        app_ids=(
            "eeeeeeee-0000-0000-0000-000000000001",
            "eeeeeeee-0000-0000-0000-000000000002",
        ),
        binaries=(
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.46-relay_etsi014_alice-default",
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.46-relay_etsi014_bob-default",
        ),
        peer_flags=("peerBobIp", "peerAliceIp"),
        log_markers=("RELAY_ETSI014_ALICE", "RELAY_ETSI014_BOB"),
        macs=("02:00:00:00:77:08", "02:00:00:00:78:09"),
        readiness_containers=("qkd-relay-kms-alice", "qkd-relay-kms-bob"),
    ),
}


@dataclass
class EndpointOptions(DockerOptions):
    kms_network: str = ""
    kms_ip: str = ""
    kms_mac: str = ""


class EndpointNode(DockerNode):
    """DockerNode created on its external KMS network before CORE wiring."""

    def __init__(self, *args: Any, options: EndpointOptions, **kwargs: Any) -> None:
        super().__init__(*args, options=options, **kwargs)
        self.kms_network = options.kms_network
        self.kms_ip = options.kms_ip
        self.kms_mac = options.kms_mac

    def startup(self) -> None:
        with self.lock:
            if self.up:
                raise RuntimeError(f"endpoint already running: {self.name}")
            self.makenodedir()
            self.host_cmd(
                "docker run -td --init "
                f"--network {self.kms_network} --ip {self.kms_ip} "
                f"--mac-address {self.kms_mac} --name {self.name} "
                f"--hostname {self.name} --privileged {self.image} "
                "tail -f /dev/null"
            )
            self.pid = self.host_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' {self.name}"
            )
            output = self.host_cmd(f"cat /proc/{self.pid}/environ")
            for line in output.split("\x00"):
                if line:
                    key, value = line.split("=", 1)
                    self.env[key] = value
            self.up = True


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def percentage(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 100.0:
        raise argparse.ArgumentTypeError("value must be between 0 and 100")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run encrypted QKDNetSim application traffic over CORE"
    )
    parser.add_argument("--qkd-topology", choices=SCENARIOS, required=True)
    parser.add_argument("--routers", type=int, choices=range(0, 9), default=0)
    parser.add_argument("--delay-ms", type=positive_int, default=5)
    parser.add_argument("--bandwidth-mbps", type=positive_int, default=100)
    parser.add_argument("--loss-percent", type=percentage, default=0.0)
    parser.add_argument("--startup-timeout", type=positive_int, default=240)
    parser.add_argument("--traffic-duration", type=positive_int, default=8)
    parser.add_argument("--min-traffic-packets", type=positive_int, default=3)
    return parser.parse_args()


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stdout}")
    return result


def wait_for_infrastructure(containers: tuple[str, ...], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        statuses = {}
        for container in containers:
            result = docker(
                "inspect", "-f",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container, check=False,
            )
            statuses[container] = result.stdout.strip() if result.returncode == 0 else "missing"
        if all(status == "healthy" for status in statuses.values()):
            print(f"[CORE_TRAFFIC] infrastructure=healthy containers={','.join(containers)}")
            return
        time.sleep(2)
    raise RuntimeError(f"QKD/KMS infrastructure did not become healthy: {statuses}")


def iface(address: ipaddress.IPv4Address, name: str | None = None) -> InterfaceData:
    return InterfaceData(name=name, ip4=str(address), ip4_mask=30)


def configure_routers(routers: list[CoreNode], networks: list[ipaddress.IPv4Network]) -> None:
    for index, router in enumerate(routers):
        router.cmd("sysctl -w net.ipv4.ip_forward=1")
        if index > 0:
            router.cmd(
                f"ip route replace {networks[0]} via {networks[index].network_address + 1}"
            )
        if index < len(routers) - 1:
            router.cmd(
                f"ip route replace {networks[-1]} via {networks[index + 1].network_address + 2}"
            )


def start_endpoint(
    node: EndpointNode,
    binary: str,
    own_data: str,
    peer_data: str,
    own_kms: str,
    kms: str,
    peer_flag: str,
    gateway: str,
) -> None:
    command = [
        "exec", "-d", node.name, "/opt/entrypoint.sh", binary,
        "--devData=eth1", "--devKms=eth0",
        f"--myIpData={own_data}", f"--myIpKms={own_kms}",
        f"--{peer_flag}={peer_data}",
        f"--{'kmsAliceIp' if 'alice' in node.name else 'kmsBobIp'}={kms}",
        "--appStartTime=2",
    ]
    if gateway:
        command.append(f"--dataGateway={gateway}")
    docker(*command)


def wait_for_traffic(
    alice: EndpointNode,
    bob: EndpointNode,
    scenario: Scenario,
    timeout: int,
    traffic_duration: int,
    min_packets: int,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alice_log = docker("exec", alice.name, "sh", "-c", "cat /tmp/qkdnetsim.log 2>/dev/null", check=False).stdout
        bob_log = docker("exec", bob.name, "sh", "-c", "cat /tmp/qkdnetsim.log 2>/dev/null", check=False).stdout
        if "GET_KEY request" in alice_log and "GET_KEY request" in bob_log:
            capture = docker(
                "exec", alice.name, "timeout", str(traffic_duration),
                "tcpdump", "-nn", "-i", "eth1", "-c", str(min_packets),
                "tcp port 8081", check=False
            )
            if capture.returncode == 0:
                match = re.search(r"(\d+) packets captured", capture.stdout)
                packets = int(match.group(1)) if match else min_packets
                print(
                    "[CORE_TRAFFIC] encryptedApplicationTraffic=OK "
                    f"tcp8081Packets={packets} durationSeconds={traffic_duration}"
                )
                return {"tcp8081_packets": packets}
        time.sleep(3)
    raise RuntimeError("QKDNetSim application traffic did not become ready")


def verify_relay_evidence() -> dict[str, int]:
    containers = (
        "qkd-relay-kms-alice",
        "qkd-relay-kms-trusted",
        "qkd-relay-kms-bob",
    )
    logs = {name: docker("logs", name, check=False).stdout for name in containers}
    relay_consumed = sum(text.count("Relay consumed") for text in logs.values())
    alice_served = logs[containers[0]].count("KMS Alice serves key")
    bob_served = logs[containers[2]].count("KMS Bob serves key")
    if relay_consumed < 1 or alice_served < 1 or bob_served < 1:
        raise RuntimeError(
            "key-relay evidence incomplete: "
            f"relayConsumed={relay_consumed} aliceServed={alice_served} "
            f"bobServed={bob_served}"
        )
    print(
        "[CORE_TRAFFIC] relayEvidence=OK "
        f"relayConsumed={relay_consumed} aliceServed={alice_served} "
        f"bobServed={bob_served}"
    )
    return {
        "relay_consumed": relay_consumed,
        "alice_keys_served": alice_served,
        "bob_keys_served": bob_served,
    }


def main() -> None:
    args = parse_args()
    started = time.monotonic()
    scenario = SCENARIOS[args.qkd_topology]
    suffix = str(os.getpid())
    names = (f"qkd-core-traffic-alice-{suffix}", f"qkd-core-traffic-bob-{suffix}")
    networks = [
        ipaddress.ip_network(f"10.254.{index}.0/30")
        for index in range(args.routers + 1)
    ]
    own_data = (str(networks[0].network_address + 1), str(networks[-1].network_address + 2))

    coreemu = CoreEmu()
    session_id = (time.time_ns() ^ os.getpid()) % 2_000_000_000 + 1
    session = coreemu.create_session(session_id)
    session.set_state(EventTypes.CONFIGURATION_STATE)
    try:
        for network in scenario.kms_networks:
            if docker("network", "inspect", network, check=False).returncode:
                raise RuntimeError(f"required KMS network does not exist: {network}")
        wait_for_infrastructure(scenario.readiness_containers, args.startup_timeout)
        endpoints = [
            session.add_node(
                EndpointNode, name=names[index],
                options=EndpointOptions(
                    image=QKD_IMAGE, kms_network=scenario.kms_networks[index],
                    kms_ip=scenario.app_kms_ips[index], kms_mac=scenario.macs[index]
                ),
            )
            for index in range(2)
        ]
        routers = [session.add_node(CoreNode, name=f"traffic-router-{i + 1}") for i in range(args.routers)]
        path = [endpoints[0], *routers, endpoints[1]]
        options = LinkOptions(
            delay=args.delay_ms * 1_000,
            bandwidth=args.bandwidth_mbps * 1_000_000,
            loss=args.loss_percent,
        )
        for index, (left, right) in enumerate(zip(path, path[1:])):
            network = networks[index]
            session.add_link(
                left.id, right.id,
                iface(network.network_address + 1, "eth1" if left is endpoints[0] else None),
                iface(network.network_address + 2, "eth1" if right is endpoints[1] else None),
                options,
            )
        session.instantiate()
        configure_routers(routers, networks)
        alice_gateway = str(networks[0].network_address + 2) if routers else ""
        bob_gateway = str(networks[-1].network_address + 1) if routers else ""
        start_endpoint(
            endpoints[1], scenario.binaries[1], own_data[1], own_data[0],
            scenario.app_kms_ips[1], scenario.kms_ips[1], scenario.peer_flags[1], bob_gateway
        )
        start_endpoint(
            endpoints[0], scenario.binaries[0], own_data[0], own_data[1],
            scenario.app_kms_ips[0], scenario.kms_ips[0], scenario.peer_flags[0], alice_gateway
        )
        metrics = wait_for_traffic(
            endpoints[0], endpoints[1], scenario, args.startup_timeout,
            args.traffic_duration, args.min_traffic_packets,
        )
        if args.qkd_topology == "key-relay":
            metrics.update(verify_relay_evidence())
        print(
            f"[CORE_TRAFFIC] OK topology={args.qkd_topology} routers={args.routers} "
            f"delayPerLinkMs={args.delay_ms} bandwidthPerLinkMbps={args.bandwidth_mbps} "
            f"lossPerLinkPercent={args.loss_percent}"
        )
    finally:
        session.shutdown()
        coreemu.shutdown()

    leaked = [name for name in names if docker("inspect", name, check=False).returncode == 0]
    if leaked:
        raise RuntimeError(f"CORE did not remove endpoints: {leaked}")
    print("[CORE_TRAFFIC] cleanup=OK")
    result = {
        "schema_version": 1,
        "runner": "traffic",
        "status": "passed",
        "topology": args.qkd_topology,
        "routers": args.routers,
        "links": args.routers + 1,
        "delay_per_link_ms": args.delay_ms,
        "bandwidth_per_link_mbps": args.bandwidth_mbps,
        "loss_per_link_percent": args.loss_percent,
        "traffic_duration_seconds": args.traffic_duration,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "metrics": metrics,
    }
    print(f"[CORE_RESULT] {json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "[CORE_RESULT] "
            + json.dumps(
                {
                    "schema_version": 1,
                    "runner": "traffic",
                    "status": "failed",
                    "error": str(error),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        raise
