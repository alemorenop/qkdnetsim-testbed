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


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
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
    parser.add_argument("--min-generations", type=positive_int, default=1)
    parser.add_argument("--traffic-duration", type=positive_int, default=5)
    parser.add_argument(
        "--min-throughput-mbps", type=positive_float, default=0.01
    )
    parser.add_argument(
        "--max-rekey-loss-percent", type=percentage, default=25.0
    )
    parser.add_argument(
        "--fault-mode", choices=("none", "bob-key-mismatch"), default="none"
    )
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
        "QKD_TEST_TAMPER_KEY": (
            "1"
            if args.fault_mode == "bob-key-mismatch" and endpoint.role == "bob"
            else "0"
        ),
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
    target_generation: int = 1,
    generation_fingerprints: dict[int, str] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[int, str]]:
    deadline = time.monotonic() + timeout
    last_report = 0.0
    alice_state = None
    bob_state = None
    history = generation_fingerprints if generation_fingerprints is not None else {}
    while time.monotonic() < deadline:
        alice_state = state(alice)
        bob_state = state(bob)
        if alice_state and bob_state:
            for endpoint, endpoint_state in (
                ("Alice", alice_state),
                ("Bob", bob_state),
            ):
                last_error = str(endpoint_state.get("last_error") or "")
                if "KMS streams diverged" in last_error:
                    raise RuntimeError(f"{endpoint}: {last_error}")
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
                generation = int(alice_state["generation"])
                fingerprint = str(alice_state.get("key_fingerprint", ""))
                if not fingerprint:
                    raise RuntimeError("VPN generation has no key fingerprint")
                previous = history.get(generation)
                if previous is not None and previous != fingerprint:
                    raise RuntimeError(
                        f"generation {generation} changed fingerprint"
                    )
                if generation not in history:
                    history[generation] = fingerprint
                    print(
                        "[CORE_VPN] generation=COMMITTED "
                        f"number={generation} fingerprint={fingerprint}",
                        flush=True,
                    )
                required = set(range(1, target_generation + 1))
                if generation >= target_generation and required.issubset(history):
                    if len(set(history[g] for g in required)) != len(required):
                        raise RuntimeError("VPN rekey reused a previous QKD fingerprint")
                    return alice_state, bob_state, history
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


def start_rekey_probe(alice: VpnEndpoint, bob: VpnEndpoint) -> None:
    docker(
        "exec", alice.node.name, "sh", "-c",
        f"rm -f /tmp/qkd-rekey-ping.log /tmp/qkd-rekey-ping.pid; "
        f"ping -i 0.2 {bob.own_ip} >/tmp/qkd-rekey-ping.log 2>&1 & "
        "echo $! >/tmp/qkd-rekey-ping.pid",
    )


def stop_rekey_probe(
    alice: VpnEndpoint, max_loss_percent: float
) -> dict[str, float | int]:
    output = docker(
        "exec", alice.node.name, "sh", "-c",
        "test -f /tmp/qkd-rekey-ping.pid && "
        "kill -INT $(cat /tmp/qkd-rekey-ping.pid) 2>/dev/null || true; "
        "sleep 1; cat /tmp/qkd-rekey-ping.log 2>/dev/null || true",
        check=False,
    ).stdout
    match = re.search(
        r"(\d+) packets transmitted, (\d+) received.*?([0-9.]+)% packet loss",
        output,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"continuous rekey ping produced no summary: {output}")
    transmitted = int(match.group(1))
    received = int(match.group(2))
    loss = float(match.group(3))
    if transmitted < 1 or received < 1 or loss > max_loss_percent:
        raise RuntimeError(
            "continuous traffic failed during rekey: "
            f"transmitted={transmitted} received={received} lossPercent={loss}"
        )
    print(
        "[CORE_VPN] rekeyTraffic=OK "
        f"transmitted={transmitted} received={received} lossPercent={loss}"
    )
    return {
        "rekey_ping_transmitted": transmitted,
        "rekey_ping_received": received,
        "rekey_ping_loss_percent": loss,
    }


def verify_old_generations_retired(
    alice: VpnEndpoint, current_generation: int
) -> None:
    if current_generation <= 1:
        return
    status = docker(
        "exec", alice.node.name, "ipsec", "statusall", check=False
    ).stdout
    for generation in range(1, current_generation):
        if re.search(rf"^\s*qkd-{generation}\[\d+\]: ESTABLISHED", status, re.MULTILINE):
            raise RuntimeError(f"old IKE SA qkd-{generation} is still established")
    print(
        "[CORE_VPN] oldGenerationsRetired=OK "
        f"currentGeneration={current_generation}"
    )


def verify_encrypted_traffic(
    alice: VpnEndpoint,
    bob: VpnEndpoint,
    duration: int,
    min_throughput_mbps: float,
) -> dict[str, float | int]:
    route = alice.node.cmd(f"ip route get {bob.own_ip}")
    match = re.search(r"\bdev\s+(\S+)", route)
    if not match:
        raise RuntimeError(f"could not resolve classical interface: {route}")
    interface = match.group(1)
    capture = "/tmp/core-vpn-verify.pcap"
    docker(
        "exec", "-d", alice.node.name, "tcpdump",
        "-U",
        "-i",
        interface,
        "-Q",
        "in",
        "-s",
        "128",
        "-w",
        capture,
        f"host {bob.own_ip} and (ip proto 50 or icmp or tcp port 5201)",
    )
    time.sleep(1)
    ping = alice.node.cmd(f"ping -c 3 -W 3 {bob.own_ip}")
    print(ping)
    if "0% packet loss" not in ping:
        raise RuntimeError("traffic through the QKD-backed VPN failed")

    docker(
        "exec", "-d", bob.node.name, "iperf3", "-s", "-1", "-B", bob.own_ip
    )
    time.sleep(1)
    iperf_output = alice.node.cmd(
        f"iperf3 -c {bob.own_ip} -t {duration} -J"
    )
    json_start = iperf_output.find("{")
    if json_start < 0:
        raise RuntimeError(f"iperf3 returned no JSON: {iperf_output}")
    try:
        iperf_result = json.loads(iperf_output[json_start:])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid iperf3 JSON: {iperf_output}") from error
    end = iperf_result.get("end", {})
    received = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    throughput_mbps = float(received.get("bits_per_second", 0.0)) / 1_000_000
    retransmits = int(sent.get("retransmits", 0) or 0)
    if throughput_mbps < min_throughput_mbps:
        raise RuntimeError(
            f"iperf3 throughput too low: {throughput_mbps:.6f} Mbit/s"
        )

    docker("exec", alice.node.name, "pkill", "-INT", "tcpdump", check=False)
    time.sleep(1)

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
    plaintext_iperf = docker(
        "exec", alice.node.name, "sh", "-c",
        f"tcpdump -nn -r {capture} 'tcp port 5201' 2>/dev/null | wc -l",
    ).stdout.strip()
    plaintext_iperf_payload = docker(
        "exec", alice.node.name, "sh", "-c",
        f"tcpdump -nn -r {capture} 'tcp port 5201' 2>/dev/null "
        "| grep -Ev 'length 0$' | wc -l",
    ).stdout.strip()
    docker("exec", alice.node.name, "rm", "-f", capture)
    if (
        int(esp) <= 0
        or int(plaintext) != 0
        or int(plaintext_iperf_payload) != 0
    ):
        raise RuntimeError(
            "outer path verification failed: "
            f"ESP={esp} plaintextICMP={plaintext} "
            f"plaintextIperf={plaintext_iperf} "
            f"plaintextIperfPayload={plaintext_iperf_payload}"
        )
    print(
        "[CORE_VPN] encryptedTraffic=OK "
        f"espPackets={esp} plaintextICMP=0 "
        f"plaintextIperfPayload=0 plaintextIperfControl={plaintext_iperf} "
        f"throughputMbps={throughput_mbps:.3f} retransmits={retransmits}"
    )
    return {
        "esp_packets": int(esp),
        "plaintext_icmp_packets": int(plaintext),
        "plaintext_iperf_packets": int(plaintext_iperf),
        "plaintext_iperf_payload_packets": int(plaintext_iperf_payload),
        "throughput_mbps": round(throughput_mbps, 6),
        "iperf_retransmits": retransmits,
    }


def verify_relay_evidence(qkd_interface: str) -> dict[str, object]:
    containers = (
        "qkd-relay-vpn-kms-alice",
        "qkd-relay-vpn-kms-trusted",
        "qkd-relay-vpn-kms-bob",
    )
    logs = {name: docker("logs", name, check=False).stdout for name in containers}
    relay_consumed = sum(text.count("Relay consumed") for text in logs.values())
    if relay_consumed < 1:
        raise RuntimeError("no trusted-node Relay consumed evidence was observed")

    evidence: dict[str, object] = {"relay_consumed": relay_consumed}
    if qkd_interface == "004":
        combined = "\n".join(logs.values())
        operations = {
            operation: len(
                re.findall(
                    rf"\[RELAY_ETSI004_CONTROL\].*operation={operation}\b",
                    combined,
                )
            )
            for operation in ("new_app", "register", "fill")
        }
        missing = [name for name, count in operations.items() if count < 1]
        if missing:
            raise RuntimeError(
                "ETSI 004 relay-control evidence missing: " + ",".join(missing)
            )
        evidence["etsi004_control_operations"] = operations
    else:
        alice_served = logs[containers[0]].count("KMS Alice serves key")
        bob_served = logs[containers[2]].count("KMS Bob serves key")
        if alice_served < 1 or bob_served < 1:
            raise RuntimeError(
                "ETSI 014 relay endpoints did not both serve key material: "
                f"alice={alice_served} bob={bob_served}"
            )
        evidence.update(
            {"alice_keys_served": alice_served, "bob_keys_served": bob_served}
        )

    print(
        "[CORE_VPN] relayEvidence=OK "
        + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    )
    return evidence


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
    started = time.monotonic()
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
    session_id = (time.time_ns() ^ os.getpid()) % 2_000_000_000 + 1
    session = coreemu.create_session(session_id)
    session.set_state(EventTypes.CONFIGURATION_STATE)
    alice: VpnEndpoint | None = None
    bob: VpnEndpoint | None = None
    metrics: dict[str, object] = {}
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

        alice_state, _, fingerprints = wait_for_tunnel(
            alice, bob, args.qkd_interface, args.startup_timeout,
            target_generation=1,
        )
        if args.min_generations > 1:
            start_rekey_probe(alice, bob)
            alice_state, _, fingerprints = wait_for_tunnel(
                alice, bob, args.qkd_interface, args.startup_timeout,
                target_generation=args.min_generations,
                generation_fingerprints=fingerprints,
            )
            metrics.update(
                stop_rekey_probe(alice, args.max_rekey_loss_percent)
            )
        generation = int(alice_state["generation"])
        verify_old_generations_retired(alice, generation)
        metrics["generations_verified"] = generation
        metrics["generation_fingerprints"] = {
            str(number): fingerprint
            for number, fingerprint in sorted(fingerprints.items())
        }
        print(
            "[CORE_VPN] tunnel=OK "
            f"topology={args.qkd_topology} interface=ETSI{args.qkd_interface} generation={generation} "
            f"routers={args.routers} links={args.routers + 1}"
        )
        metrics.update(
            verify_encrypted_traffic(
                alice, bob, args.traffic_duration, args.min_throughput_mbps
            )
        )
        if args.qkd_topology == "key-relay":
            metrics["relay_evidence"] = verify_relay_evidence(args.qkd_interface)
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
    result = {
        "schema_version": 1,
        "runner": "vpn",
        "status": "passed",
        "topology": args.qkd_topology,
        "qkd_interface": args.qkd_interface,
        "fault_mode": args.fault_mode,
        "routers": args.routers,
        "links": args.routers + 1,
        "delay_per_link_ms": args.delay_ms,
        "bandwidth_per_link_mbps": args.bandwidth_mbps,
        "loss_per_link_percent": args.loss_percent,
        "rekey_interval_seconds": args.rekey_interval,
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
                    "runner": "vpn",
                    "status": "failed",
                    "error": str(error),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        raise
