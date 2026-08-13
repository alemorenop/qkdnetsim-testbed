#!/usr/bin/env python3
"""Run the point-to-point QKD VPN over a configurable CORE classical path."""

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


VPN_IMAGE = os.environ.get("CORE_VPN_IMAGE", "qkdnetsim-vpn-endpoint:latest")


@dataclass(frozen=True)
class QkdTopology:
    kms_networks: tuple[str, str]
    kms_ips: tuple[str, str]
    app_kms_ips: tuple[str, str]
    app_ids: tuple[str, str]
    macs: tuple[str, str]
    readiness_containers: tuple[str, str]


QKD_TOPOLOGIES = {
    "point-to-point": QkdTopology(
        kms_networks=(
            "qkdnetsim-point-to-point-vpn_net_alice_kms_app",
            "qkdnetsim-point-to-point-vpn_net_bob_kms_app",
        ),
        kms_ips=("192.168.35.3", "192.168.46.4"),
        app_kms_ips=("192.168.35.5", "192.168.46.6"),
        app_ids=(
            "bbbbbbbb-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ),
        macs=("02:00:00:00:35:05", "02:00:00:00:46:06"),
        readiness_containers=("qkd-p2p-vpn-kms-alice", "qkd-p2p-vpn-kms-bob"),
    ),
    "key-relay": QkdTopology(
        kms_networks=(
            "qkdnetsim-key-relay-vpn_net_alice_kms_app",
            "qkdnetsim-key-relay-vpn_net_bob_kms_app",
        ),
        kms_ips=("192.168.119.5", "192.168.120.7"),
        app_kms_ips=("192.168.119.8", "192.168.120.9"),
        app_ids=(
            "eeeeeeee-0000-0000-0000-000000000001",
            "eeeeeeee-0000-0000-0000-000000000002",
        ),
        macs=("02:00:00:00:77:08", "02:00:00:00:78:09"),
        readiness_containers=("qkd-relay-vpn-kms-alice", "qkd-relay-vpn-kms-bob"),
    ),
}


@dataclass(frozen=True)
class VpnEndpoint:
    node: DockerNode
    role: str
    own_ip: str
    peer_ip: str
    kms_ip: str
    kms_app_ip: str
    kms_network: str
    own_app_id: str
    peer_app_id: str


@dataclass
class VpnDockerOptions(DockerOptions):
    kms_network: str = ""
    kms_app_ip: str = ""
    kms_app_mac: str = ""


class VpnDockerNode(DockerNode):
    """Docker node born on its KMS network before CORE adds the data path."""

    def __init__(self, *args: Any, options: VpnDockerOptions, **kwargs: Any) -> None:
        super().__init__(*args, options=options, **kwargs)
        self.kms_network = options.kms_network
        self.kms_app_ip = options.kms_app_ip
        self.kms_app_mac = options.kms_app_mac

    def startup(self) -> None:
        with self.lock:
            if self.up:
                raise RuntimeError(f"VPN Docker node is already running: {self.name}")
            self.makenodedir()
            hostname = self.name.replace("_", "-")
            self.host_cmd(
                "docker run -td --init "
                f"--network {self.kms_network} --ip {self.kms_app_ip} "
                f"--mac-address {self.kms_app_mac} "
                f"--hostname {hostname} --name {self.name} "
                "--sysctl net.ipv6.conf.all.disable_ipv6=0 "
                f"--privileged {self.image} tail -f /dev/null"
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
        description="Run strongSwan over a direct or routed CORE path"
    )
    parser.add_argument("--routers", type=int, choices=range(0, 9), default=0)
    parser.add_argument("--delay-ms", type=positive_int, default=5)
    parser.add_argument("--bandwidth-mbps", type=positive_int, default=100)
    parser.add_argument("--loss-percent", type=percentage, default=0.0)
    parser.add_argument("--qkd-interface", choices=("004", "014"), default="004")
    parser.add_argument(
        "--qkd-topology", choices=QKD_TOPOLOGIES, default="point-to-point"
    )
    parser.add_argument("--startup-timeout", type=positive_int, default=240)
    parser.add_argument("--rekey-interval", type=positive_int, default=60)
    return parser.parse_args()


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): {result.stdout}"
        )
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
            print(f"[CORE_VPN] infrastructure=healthy containers={','.join(containers)}")
            return
        time.sleep(2)
    raise RuntimeError(f"QKD/KMS infrastructure did not become healthy: {statuses}")


def iface(
    address: ipaddress.IPv4Address, name: str | None = None
) -> InterfaceData:
    return InterfaceData(name=name, ip4=str(address), ip4_mask=30)


def configure_routes(
    alice: DockerNode,
    bob: DockerNode,
    routers: list[CoreNode],
    networks: list[ipaddress.IPv4Network],
) -> None:
    if not routers:
        return
    alice.cmd(
        f"ip route replace {networks[-1].network_address + 2}/32 "
        f"via {networks[0].network_address + 2}"
    )
    bob.cmd(
        f"ip route replace {networks[0].network_address + 1}/32 "
        f"via {networks[-1].network_address + 1}"
    )
    for index, router in enumerate(routers):
        router.cmd("sysctl -w net.ipv4.ip_forward=1")
        if index > 0:
            router.cmd(
                f"ip route replace {networks[0]} "
                f"via {networks[index].network_address + 1}"
            )
        if index < len(routers) - 1:
            router.cmd(
                f"ip route replace {networks[-1]} "
                f"via {networks[index + 1].network_address + 2}"
            )


def start_vpn(endpoint: VpnEndpoint, args: argparse.Namespace) -> None:
    variables = {
        "VPN_ROLE": endpoint.role,
        "QKD_INTERFACE": args.qkd_interface,
        "OWN_IP": endpoint.own_ip,
        "PEER_IP": endpoint.peer_ip,
        "KMS_IP": endpoint.kms_ip,
        "OWN_APP_ID": endpoint.own_app_id,
        "PEER_APP_ID": endpoint.peer_app_id,
        "CONTROL_PORT": "9090",
        "KEY_CHUNK_SIZE_BITS": "256",
        "REKEY_INTERVAL_S": str(args.rekey_interval),
    }
    command = ["exec", "-d"]
    for key, value in variables.items():
        command.extend(("-e", f"{key}={value}"))
    command.extend((endpoint.node.name, "/opt/entrypoint-vpn.sh"))
    docker(*command)


def state(endpoint: VpnEndpoint) -> dict[str, object] | None:
    result = docker(
        "exec",
        endpoint.node.name,
        "cat",
        "/run/qkd-vpn/state.json",
        check=False,
    )
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def wait_for_tunnel(
    alice: VpnEndpoint,
    bob: VpnEndpoint,
    qkd_interface: str,
    timeout: int,
) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + timeout
    last_report = 0.0
    alice_state = None
    bob_state = None
    while time.monotonic() < deadline:
        alice_state = state(alice)
        bob_state = state(bob)
        if alice_state and bob_state:
            same_generation = (
                alice_state.get("generation") == bob_state.get("generation")
                and int(alice_state.get("generation", 0)) >= 1
            )
            if (
                alice_state.get("tunnel_up")
                and bob_state.get("tunnel_up")
                and same_generation
            ):
                if alice_state.get("qkd_interface") != qkd_interface:
                    raise RuntimeError("Alice reports the wrong ETSI interface")
                if bob_state.get("qkd_interface") != qkd_interface:
                    raise RuntimeError("Bob reports the wrong ETSI interface")
                if alice_state.get("key_fingerprint") != bob_state.get(
                    "key_fingerprint"
                ):
                    raise RuntimeError("VPN peers committed different QKD keys")
                return alice_state, bob_state
        now = time.monotonic()
        if now - last_report >= 10:
            print(
                "[CORE_VPN] waiting "
                f"alice={alice_state} bob={bob_state}",
                flush=True,
            )
            last_report = now
        time.sleep(2)
    raise RuntimeError(
        f"VPN did not become ready within {timeout}s; "
        f"alice={alice_state} bob={bob_state}"
    )


def verify_encrypted_ping(alice: VpnEndpoint, bob: VpnEndpoint) -> None:
    route = alice.node.cmd(f"ip route get {bob.own_ip}")
    match = re.search(r"\bdev\s+(\S+)", route)
    if not match:
        raise RuntimeError(f"could not resolve classical interface: {route}")
    interface = match.group(1)
    capture = "/tmp/core-vpn-verify.pcap"
    docker(
        "exec",
        "-d",
        alice.node.name,
        "timeout",
        "6",
        "tcpdump",
        "-U",
        "-i",
        interface,
        "-s",
        "128",
        "-w",
        capture,
        f"host {bob.own_ip} and (ip proto 50 or icmp)",
    )
    time.sleep(1)
    ping = alice.node.cmd(f"ping -c 3 -W 3 {bob.own_ip}")
    print(ping)
    if "0% packet loss" not in ping:
        raise RuntimeError("traffic through the QKD-backed VPN failed")
    time.sleep(6)

    esp = docker(
        "exec",
        alice.node.name,
        "sh",
        "-c",
        f"tcpdump -nn -r {capture} 'ip proto 50' 2>/dev/null | wc -l",
    ).stdout.strip()
    plaintext = docker(
        "exec",
        alice.node.name,
        "sh",
        "-c",
        f"tcpdump -nn -r {capture} icmp 2>/dev/null | wc -l",
    ).stdout.strip()
    docker("exec", alice.node.name, "rm", "-f", capture)
    if int(esp) <= 0 or int(plaintext) != 0:
        raise RuntimeError(
            f"outer path verification failed: ESP={esp} plaintextICMP={plaintext}"
        )
    print(f"[CORE_VPN] encryptedTraffic=OK espPackets={esp} plaintextICMP=0")


def stop_vpn(endpoint: VpnEndpoint | None) -> None:
    if endpoint is None:
        return
    docker(
        "exec",
        endpoint.node.name,
        "sh",
        "-c",
        "pkill -TERM -f '[q]kd-vpn.py' 2>/dev/null || true; "
        "ipsec stop >/dev/null 2>&1 || true",
        check=False,
    )


def main() -> None:
    args = parse_args()
    topology = QKD_TOPOLOGIES[args.qkd_topology]
    suffix = str(os.getpid())
    alice_name = f"qkd-core-vpn-alice-{suffix}"
    bob_name = f"qkd-core-vpn-bob-{suffix}"
    endpoint_names = (alice_name, bob_name)
    networks = [
        ipaddress.ip_network(f"10.253.{index}.0/30")
        for index in range(args.routers + 1)
    ]
    alice_classical = str(networks[0].network_address + 1)
    bob_classical = str(networks[-1].network_address + 2)

    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)
    alice: VpnEndpoint | None = None
    bob: VpnEndpoint | None = None
    try:
        for kms_network in topology.kms_networks:
            if docker("network", "inspect", kms_network, check=False).returncode:
                raise RuntimeError(
                    f"required KMS network does not exist: {kms_network}; "
                    "start the point-to-point PP/KMS infrastructure first"
                )
        wait_for_infrastructure(topology.readiness_containers, args.startup_timeout)
        alice_node = session.add_node(
            VpnDockerNode,
            name=alice_name,
            options=VpnDockerOptions(
                image=VPN_IMAGE,
                kms_network=topology.kms_networks[0],
                kms_app_ip=topology.app_kms_ips[0],
                kms_app_mac=topology.macs[0],
            ),
        )
        bob_node = session.add_node(
            VpnDockerNode,
            name=bob_name,
            options=VpnDockerOptions(
                image=VPN_IMAGE,
                kms_network=topology.kms_networks[1],
                kms_app_ip=topology.app_kms_ips[1],
                kms_app_mac=topology.macs[1],
            ),
        )
        routers = [
            session.add_node(CoreNode, name=f"vpn-classical-router-{index + 1}")
            for index in range(args.routers)
        ]
        path = [alice_node, *routers, bob_node]
        options = LinkOptions(
            delay=args.delay_ms * 1_000,
            bandwidth=args.bandwidth_mbps * 1_000_000,
            loss=args.loss_percent,
        )
        for index, (left, right) in enumerate(zip(path, path[1:])):
            network = networks[index]
            session.add_link(
                left.id,
                right.id,
                iface(
                    network.network_address + 1,
                    "eth1" if left is alice_node else None,
                ),
                iface(
                    network.network_address + 2,
                    "eth1" if right is bob_node else None,
                ),
                options,
            )
        session.instantiate()
        configure_routes(alice_node, bob_node, routers, networks)

        alice = VpnEndpoint(
            alice_node,
            "alice",
            alice_classical,
            bob_classical,
            topology.kms_ips[0],
            topology.app_kms_ips[0],
            topology.kms_networks[0],
            topology.app_ids[0],
            topology.app_ids[1],
        )
        bob = VpnEndpoint(
            bob_node,
            "bob",
            bob_classical,
            alice_classical,
            topology.kms_ips[1],
            topology.app_kms_ips[1],
            topology.kms_networks[1],
            topology.app_ids[1],
            topology.app_ids[0],
        )
        start_vpn(bob, args)
        start_vpn(alice, args)

        alice_state, _ = wait_for_tunnel(
            alice, bob, args.qkd_interface, args.startup_timeout
        )
        generation = int(alice_state["generation"])
        print(
            "[CORE_VPN] tunnel=OK "
            f"topology={args.qkd_topology} interface=ETSI{args.qkd_interface} generation={generation} "
            f"routers={args.routers} links={args.routers + 1}"
        )
        verify_encrypted_ping(alice, bob)
        print(
            "[CORE_VPN] OK "
            f"routers={args.routers} delayPerLinkMs={args.delay_ms} "
            f"bandwidthPerLinkMbps={args.bandwidth_mbps} "
            f"lossPerLinkPercent={args.loss_percent}"
        )
    finally:
        # Close persistent KMS sockets before removing the network namespace.
        stop_vpn(alice)
        stop_vpn(bob)
        time.sleep(0.5)
        session.shutdown()
        coreemu.shutdown()

    leaked = [name for name in endpoint_names if docker("inspect", name, check=False).returncode == 0]
    if leaked:
        raise RuntimeError(f"CORE did not remove VPN endpoints: {leaked}")
    print("[CORE_VPN] cleanup=OK")


if __name__ == "__main__":
    main()
