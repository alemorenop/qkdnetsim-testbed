#!/usr/bin/env python3
"""Run the native QKDNetSim traffic consumer over the common CORE path."""

import argparse
import ipaddress
import json
import math
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
    statistics_containers: tuple[str, ...]


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
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-p2p_etsi014_alice-default",
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-p2p_etsi014_bob-default",
        ),
        peer_flags=("peerBobIp", "peerAliceIp"),
        log_markers=("P2P_ETSI014_ALICE", "P2P_ETSI014_BOB"),
        macs=("02:00:00:00:35:05", "02:00:00:00:46:06"),
        readiness_containers=("qkd-p2p-kms-alice", "qkd-p2p-kms-bob"),
        statistics_containers=("qkd-p2p-kms-alice", "qkd-p2p-kms-bob"),
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
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-relay_etsi014_alice-default",
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-relay_etsi014_bob-default",
        ),
        peer_flags=("peerBobIp", "peerAliceIp"),
        log_markers=("RELAY_ETSI014_ALICE", "RELAY_ETSI014_BOB"),
        macs=("02:00:00:00:77:08", "02:00:00:00:78:09"),
        readiness_containers=("qkd-relay-kms-alice", "qkd-relay-kms-bob"),
        statistics_containers=(
            "qkd-relay-kms-alice",
            "qkd-relay-kms-trusted",
            "qkd-relay-kms-bob",
        ),
    ),
    "secoqc": Scenario(
        kms_networks=(
            "qkdnetsim-secoqc_kms_app_a",
            "qkdnetsim-secoqc_kms_app_f",
        ),
        kms_ips=("192.168.231.11", "192.168.232.16"),
        app_kms_ips=("192.168.231.18", "192.168.232.19"),
        app_ids=(
            "eeeeeeee-0000-0000-0000-000000000001",
            "eeeeeeee-0000-0000-0000-000000000002",
        ),
        binaries=(
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-relay_etsi014_alice-default",
            "/opt/ns-3-dev/build/contrib/qkdnetsim/examples/ns3.48-relay_etsi014_bob-default",
        ),
        peer_flags=("peerBobIp", "peerAliceIp"),
        log_markers=("RELAY_ETSI014_ALICE", "RELAY_ETSI014_BOB"),
        macs=("02:00:00:00:e7:18", "02:00:00:00:e8:19"),
        readiness_containers=("qkd-secoqc-site-a", "qkd-secoqc-site-f"),
        statistics_containers=(
            "qkd-secoqc-site-a", "qkd-secoqc-site-b", "qkd-secoqc-site-c",
            "qkd-secoqc-site-d", "qkd-secoqc-site-e", "qkd-secoqc-site-f",
        ),
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


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0.0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
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
    parser.add_argument("--app-rate-bps", type=positive_int, default=6400)
    parser.add_argument("--app-packet-size", type=positive_int, default=800)
    parser.add_argument("--keys-per-request", type=positive_int, default=3)
    parser.add_argument("--encryption-type", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--authentication-type", type=int, choices=(0, 1, 2, 3), default=0)
    parser.add_argument("--delivery-drain-seconds", type=nonnegative_float, default=2.0)
    parser.add_argument("--measurement-warmup-seconds", type=nonnegative_float, default=5.0)
    return parser.parse_args()


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stdout}")
    return result


def parse_kms_statistics(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse the cumulative, machine-readable KMS markers from one log."""
    generated = [
        {"link_id": app_id, "key_id": key_id, "bits": int(bits)}
        for app_id, key_id, bits in re.findall(
            r"stores key appId=(\S+)\s+keyId=(\S+)\s+bits=(\d+)", text
        )
    ]
    mixed_supplied = []
    for match in re.finditer(
        r"Mixed key contribution type=(\S+)\s+bits=(\d+)\s+keyId=(\S+)([^\n]*)",
        text,
    ):
        suffix = match.group(4)
        ksid_match = re.search(r"\bksid=(\S*)", suffix)
        src_node_match = re.search(r"\bsrcNodeId=(\d+)", suffix)
        dst_node_match = re.search(r"\bdstNodeId=(\d+)", suffix)
        mixed_supplied.append({
            "type": match.group(1), "bits": int(match.group(2)),
            "key_id": match.group(3),
            "ksid": ksid_match.group(1) if ksid_match else None,
            "source_node": int(src_node_match.group(1)) if src_node_match else None,
            "destination_node": int(dst_node_match.group(1)) if dst_node_match else None,
        })
    legacy_supplied = [
        {"type": "qkd", "bits": int(bits), "key_id": key_id, "ksid": app_id,
         "source_node": None, "destination_node": None}
        for app_id, key_id, bits in re.findall(
            r"serves key\s+appId=(\S+)\s+keyId=(\S+)\s+bits=(\d+)", text
        )
    ]
    # Current QKDNetSim emits both the material-aware trace and the older
    # textual marker.  Combining them double-counts the same delivery and,
    # worse, classifies internal KMS-to-KMS material as VPN consumption.  A
    # non-empty KSID is the authoritative application boundary.  Archived
    # versions without the mixed trace retain their legacy marker fallback.
    supplied = mixed_supplied if mixed_supplied else legacy_supplied
    application_supplied = (
        [event for event in mixed_supplied if event["ksid"]]
        if mixed_supplied else [
            event for event in legacy_supplied
            if str(event.get("ksid", "")).startswith(("b", "e"))
        ]
    )
    relayed = [
        {"node": int(node) if node else None,
         "source": int(src), "destination": int(dst),
         "bits": int(bits)}
        for node, src, dst, bits in re.findall(
            r"Relay consumed(?:\s+node=(\d+))?\s+src=(\d+)\s+dst=(\d+)\s+bits=(\d+)",
            text,
        )
    ]
    relay_succeeded = [
        {"node": int(node) if node else None,
         "source": int(src), "destination": int(dst),
         "bits": int(bits)}
        for node, src, dst, bits in re.findall(
            r"Relay succeeded(?:\s+node=(\d+))?\s+src=(\d+)\s+dst=(\d+)\s+bits=(\d+)",
            text,
        )
    ]
    wasted = [
        {"source": int(src), "destination": int(dst), "bits": int(bits)}
        for src, dst, bits in re.findall(
            r"Relay WASTED src=(\d+)\s+dst=(\d+)\s+bits=(\d+)", text
        )
    ]
    buffer_samples = [
        {"time_seconds": float(timestamp), "context": context, "bits": int(bits)}
        for timestamp, context, bits in re.findall(
            r"\[COMPARE_BUFFER\]\s+time=([0-9.]+)\s+context=(\S+)\s+bits=(\d+)",
            text,
        )
    ]
    return {
        "generated": generated,
        "supplied": supplied,
        "application_supplied": application_supplied,
        "relayed": relayed,
        "relay_succeeded": relay_succeeded,
        "wasted": wasted,
        "buffer_samples": buffer_samples,
    }


def summarize_kms_window(
    before: dict[str, dict[str, list[dict[str, Any]]]],
    after: dict[str, dict[str, list[dict[str, Any]]]],
    duration: int,
    application_log: str = "",
    application_key_use_offset: int = 0,
    application_key_use_limit: int | None = None,
) -> dict[str, Any]:
    """Create paper-style link/service accounting from append-only traces."""
    deltas: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for container, final in after.items():
        initial = before.get(container, {})
        deltas[container] = {
            kind: values[len(initial.get(kind, [])):]
            for kind, values in final.items()
        }

    # One physical key is logged by both endpoint KMS processes. Deduplicating
    # (link id, key id) is the distributed equivalent of the /2 correction in
    # the monolithic QKDNetSim example used by the paper.
    generated: dict[tuple[str, str], int] = {}
    for events in deltas.values():
        for event in events["generated"]:
            generated[(event["link_id"], event["key_id"])] = event["bits"]
    links: dict[str, list[int]] = {}
    for (link_id, _), bits in generated.items():
        links.setdefault(link_id, []).append(bits)

    link_rows = []
    for link_id, sizes in sorted(links.items()):
        total = sum(sizes)
        link_rows.append({
            "link_id": link_id,
            "generated_keys": len(sizes),
            "generated_bits": total,
            "average_generated_key_size_bits": total / len(sizes),
            "observed_generation_rate_bps": total / duration,
        })

    service_rows = []
    relay_rows = []
    waste_rows = []
    for container, events in sorted(deltas.items()):
        by_type: dict[str, list[dict[str, Any]]] = {}
        for event in events["application_supplied"]:
            by_type.setdefault(event["type"], []).append(event)
        for key_type, values in sorted(by_type.items()):
            service_rows.append({
                "kms": container,
                "material_type": key_type,
                "supply_events": len(values),
                "supplied_bits": sum(value["bits"] for value in values),
            })
        if events["relayed"]:
            row = {
                "kms": container,
                # Backwards-compatible attempt counters used by the original
                # architecture campaign.
                "relay_events": len(events["relayed"]),
                "relayed_bits": sum(value["bits"] for value in events["relayed"]),
                "relay_attempts": len(events["relayed"]),
                "relay_attempt_bits": sum(value["bits"] for value in events["relayed"]),
            }
            succeeded = events.get("relay_succeeded", [])
            row.update({
                "relay_successes": len(succeeded),
                "relay_success_bits": sum(value["bits"] for value in succeeded),
            })
            relay_rows.append(row)
        if events["wasted"]:
            waste_rows.append({
                "kms": container,
                "waste_events": len(events["wasted"]),
                "wasted_bits": sum(value["bits"] for value in events["wasted"]),
            })

    # KeyServedMixed is emitted at both ends for the same logical key.  The
    # paper's user table counts application key consumption, not trace copies,
    # so deduplicate by material type and key identifier across all KMSes.
    consumed: dict[tuple[str, str], int] = {}
    for events in deltas.values():
        for event in events["application_supplied"]:
            consumed[(event["type"], event["key_id"])] = event["bits"]
    consumption_by_type = []
    for key_type in sorted({key[0] for key in consumed}):
        values = [
            bits for (material_type, _), bits in consumed.items()
            if material_type == key_type
        ]
        consumption_by_type.append({
            "material_type": key_type,
            "keys_consumed": len(values),
            "keys_consumed_bits": sum(values),
        })
    key_uses = re.findall(
        r"\[COMPARE_APP_KEY\]\s+appId=(\S+)\s+encKeyId=(\S+)\s+"
        r"authKeyId=(\S+)\s+payloadBits=(\d+)",
        application_log,
    )[application_key_use_offset:]
    if application_key_use_limit is not None:
        key_uses = key_uses[:application_key_use_limit]
    is_real_key = lambda value: bool(value.strip("0"))
    encryption_ids = [enc for _, enc, _, _ in key_uses if is_real_key(enc)]
    authentication_ids = [auth for _, _, auth, _ in key_uses if is_real_key(auth)]
    return {
        "measurement_window_seconds": duration,
        "qkd_links": link_rows,
        "service_by_kms_and_material": service_rows,
        "relay_by_kms": relay_rows,
        "relay_waste_by_kms": waste_rows,
        "application_consumption_by_material": consumption_by_type,
        "application_consumption": {
            "keys_consumed": len(consumed),
            "keys_consumed_bits": sum(consumed.values()),
        },
        "application_key_use": {
            "encryption_operations": len(encryption_ids),
            "authentication_operations": len(authentication_ids),
            "unique_encryption_keys_used": len(set(encryption_ids)),
            "unique_authentication_keys_used": len(set(authentication_ids)),
            "otp_payload_bits_protected": sum(
                int(payload_bits) for _, enc, _, payload_bits in key_uses
                if is_real_key(enc)
            ),
        },
        "buffer_time_series_available": any(
            events["buffer_samples"] for events in deltas.values()
        ),
        "buffer_time_series": [
            {"kms": container, **sample}
            for container, events in sorted(deltas.items())
            for sample in events["buffer_samples"]
        ],
    }


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
    app_rate_bps: int,
    app_packet_size: int,
    keys_per_request: int,
    encryption_type: int,
    authentication_type: int,
    app_ids: tuple[str, str],
) -> None:
    command = [
        "exec", "-d", node.name, "/opt/entrypoint.sh", binary,
        "--devData=eth1", "--devKms=eth0",
        f"--myIpData={own_data}", f"--myIpKms={own_kms}",
        f"--{peer_flag}={peer_data}",
        f"--{'kmsAliceIp' if 'alice' in node.name else 'kmsBobIp'}={kms}",
        f"--etsiAliceId={app_ids[0]}",
        f"--etsiBobId={app_ids[1]}",
        "--appStartTime=2",
        f"--numberOfKeyToFetchFromKMS={keys_per_request}",
        "--useCrypto=0",
        f"--authenticationType={authentication_type}",
        f"--encryptionType={encryption_type}",
    ]
    if "alice" in node.name:
        command.extend(
            (
                f"--appRateBps={app_rate_bps}",
                f"--appPacketSize={app_packet_size}",
            )
        )
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
    app_rate_bps: int,
    app_packet_size: int,
    keys_per_request: int,
    delivery_drain_seconds: float,
    measurement_warmup_seconds: float,
    keys_required: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_logs = {alice.name: "", bob.name: ""}
    while time.monotonic() < deadline:
        alice_log = docker("exec", alice.name, "sh", "-c", "cat /tmp/qkdnetsim.log 2>/dev/null", check=False).stdout
        bob_log = docker("exec", bob.name, "sh", "-c", "cat /tmp/qkdnetsim.log 2>/dev/null", check=False).stdout
        last_logs = {alice.name: alice_log, bob.name: bob_log}
        for endpoint in (alice, bob):
            if "NS_FATAL" in last_logs[endpoint.name] or \
               "terminate called" in last_logs[endpoint.name]:
                print(f"[CORE_TRAFFIC_DIAGNOSTIC] endpoint={endpoint.name} fatalLogTail")
                print("\n".join(last_logs[endpoint.name].splitlines()[-80:]))
                raise RuntimeError(f"QKDNetSim endpoint failed: {endpoint.name}")
            state = docker(
                "inspect", "-f",
                "status={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
                endpoint.name, check=False,
            )
            if state.returncode or "status=running" not in state.stdout:
                endpoint_log = last_logs[endpoint.name]
                print(
                    f"[CORE_TRAFFIC_DIAGNOSTIC] endpoint={endpoint.name} "
                    f"{state.stdout.strip() or 'state=unavailable'}"
                )
                print("\n".join(endpoint_log.splitlines()[-80:]))
                raise RuntimeError(f"QKDNetSim endpoint stopped: {endpoint.name}")
        key_path_ready = (
            not keys_required
            or ("GET_KEY request" in alice_log and "GET_KEY request" in bob_log)
        )
        if (
            key_path_ready
            and "[COMPARE_APP] Tx" in alice_log
            and "[COMPARE_APP] Rx" in bob_log
        ):
            # Both deployments run their application before the measured
            # interval.  This removes initial socket/key-store filling from
            # steady-state goodput without hiding it from startup metrics.
            if measurement_warmup_seconds:
                time.sleep(measurement_warmup_seconds)
                alice_log = docker(
                    "exec", alice.name, "sh", "-c",
                    "cat /tmp/qkdnetsim.log 2>/dev/null", check=False,
                ).stdout
                bob_log = docker(
                    "exec", bob.name, "sh", "-c",
                    "cat /tmp/qkdnetsim.log 2>/dev/null", check=False,
                ).stdout
            # Observe the application trace itself.  TCP segment counts cannot
            # be used as packet counts because one QKDApp frame may be split or
            # coalesced by the kernel transport used between the containers.
            before_logs = {
                name: docker("logs", name, check=False).stdout
                for name in scenario.statistics_containers
            }
            before_statistics = {
                name: parse_kms_statistics(text)
                for name, text in before_logs.items()
            }
            tx_before = len(re.findall(r"\[COMPARE_APP\] Tx\b", alice_log))
            rx_before = len(re.findall(r"\[COMPARE_APP\] Rx\b", bob_log))
            mx_before = len(re.findall(r"\[COMPARE_APP\] Mx\b", alice_log))
            key_use_before = alice_log.count("[COMPARE_APP_KEY]")
            kms_requests_before = sum(
                text.count("GET_KEY request to KMS")
                for text in (alice_log, bob_log)
            )
            expected_packets = max(
                1,
                math.ceil(
                    traffic_duration * app_rate_bps / (app_packet_size * 8)
                ),
            )
            # Keep a real time window for QKD-link and resource accounting,
            # but treat the application interval as half-open by retaining at
            # most the configured number of opportunities. This excludes an
            # event exactly on the right boundary without shortening the QKD
            # observation interval by one packet period.
            time.sleep(traffic_duration)
            alice_after = docker(
                "exec", alice.name, "sh", "-c",
                "cat /tmp/qkdnetsim.log 2>/dev/null", check=False,
            ).stdout
            observed_tx = (
                len(re.findall(r"\[COMPARE_APP\] Tx\b", alice_after)) - tx_before
            )
            observed_mx = (
                len(re.findall(r"\[COMPARE_APP\] Mx\b", alice_after)) - mx_before
            )
            measured_opportunities = min(observed_tx + observed_mx, expected_packets)
            missed_send_calls = min(observed_mx, measured_opportunities)
            tx_packets = min(observed_tx, measured_opportunities - missed_send_calls)
            tx_sizes = [
                int(value) for value in re.findall(
                    r"\[COMPARE_APP\] Tx\b[^\n]*\bbytes=(\d+)", alice_after
                )
            ][tx_before:tx_before + tx_packets]

            # Freeze the transmitted interval first, then give the last TCP
            # frame a bounded opportunity to reach Bob.  Reading both logs
            # back-to-back at the window boundary occasionally counted the Tx
            # but not its immediately following Rx and reported artificial
            # packet loss.  Cap the receive slice to the frozen Tx count so
            # traffic generated after the interval cannot inflate the result.
            drain_deadline = time.monotonic() + delivery_drain_seconds
            while True:
                bob_after = docker(
                    "exec", bob.name, "sh", "-c",
                    "cat /tmp/qkdnetsim.log 2>/dev/null", check=False,
                ).stdout
                observed_rx = (
                    len(re.findall(r"\[COMPARE_APP\] Rx\b", bob_after)) - rx_before
                )
                if observed_rx >= tx_packets or time.monotonic() >= drain_deadline:
                    break
                time.sleep(0.1)
            rx_packets = min(observed_rx, tx_packets)
            rx_sizes = [
                int(value) for value in re.findall(
                    r"\[COMPARE_APP\] Rx\b[^\n]*\bbytes=(\d+)", bob_after
                )
            ][rx_before:rx_before + tx_packets]
            tx_bytes = sum(tx_sizes)
            rx_bytes = sum(rx_sizes)
            kms_requests_after = sum(
                text.count("GET_KEY request to KMS")
                for text in (alice_after, bob_after)
            )
            kms_requests = max(kms_requests_after - kms_requests_before, 0)
            after_logs = {
                name: docker("logs", name, check=False).stdout
                for name in scenario.statistics_containers
            }
            after_statistics = {
                name: parse_kms_statistics(text)
                for name, text in after_logs.items()
            }
            served_delta = sum(
                count_key_delivery_events(after_logs[name])
                - count_key_delivery_events(before_logs[name])
                for name in scenario.readiness_containers
            )
            zero_bit_delivery_events = sum(
                count_zero_bit_key_delivery_events(after_logs[name])
                for name in scenario.readiness_containers
            )
            if zero_bit_delivery_events:
                raise RuntimeError(
                    "KMS emitted empty key material: "
                    f"zeroBitDeliveryEvents={zero_bit_delivery_events}"
                )
            delivery_ratio = rx_packets / tx_packets if tx_packets else None
            offered_load_realization = tx_packets / expected_packets
            received_payload_bytes = rx_packets * app_packet_size
            if rx_packets >= min_packets and rx_packets == tx_packets:
                print(
                    "[CORE_TRAFFIC] applicationTraffic=OK "
                    f"applicationTxPackets={tx_packets} "
                    f"applicationRxPackets={rx_packets} "
                    f"applicationRxBytes={rx_bytes} "
                    f"expectedApplicationPackets={expected_packets} "
                    f"durationSeconds={traffic_duration}"
                )
                paper_validation = summarize_kms_window(
                    before_statistics, after_statistics, traffic_duration,
                    alice_after, key_use_before, tx_packets,
                )
                return {
                    "sent_packets": tx_packets,
                    "sent_bytes": tx_bytes,
                    "received_packets": rx_packets,
                    "received_bytes": rx_bytes,
                    "received_payload_bytes": received_payload_bytes,
                    "application_goodput_bps": (
                        received_payload_bytes * 8 / traffic_duration
                    ),
                    "expected_application_packets": expected_packets,
                    "delivery_ratio": delivery_ratio,
                    "offered_load_realization": offered_load_realization,
                    "kms_material_trace_events": served_delta,
                    "application_key_requests": kms_requests,
                    "application_keys_requested": kms_requests * keys_per_request,
                    "zero_bit_key_delivery_events": zero_bit_delivery_events,
                    "missed_send_calls": missed_send_calls,
                    "offered_load_shortfall_packets": max(expected_packets - tx_packets, 0),
                    "paper_validation": paper_validation,
                }
            raise RuntimeError(
                "incomplete application delivery after drain interval: "
                f"tx={tx_packets} rx={rx_packets} "
                f"drainSeconds={delivery_drain_seconds}"
            )
        time.sleep(3)
    for endpoint in (alice, bob):
        print(f"[CORE_TRAFFIC_DIAGNOSTIC] endpoint={endpoint.name} finalLogTail")
        print("\n".join(last_logs[endpoint.name].splitlines()[-80:]))
    raise RuntimeError("QKDNetSim application traffic did not become ready")


def count_key_delivery_events(text: str) -> int:
    """Count legacy or mixed KMS trace events carrying a positive bit count."""
    return sum(
        1
        for value in re.findall(
            r"(?:serves key|Mixed key contribution)[^\n]*\bbits=(\d+)", text
        )
        if int(value) > 0
    )


def count_zero_bit_key_delivery_events(text: str) -> int:
    """Count KMS delivery traces that expose empty key material."""
    return sum(
        1
        for value in re.findall(
            r"(?:serves key|Mixed key contribution)[^\n]*\bbits=(\d+)", text
        )
        if int(value) == 0
    )


def verify_relay_evidence(scenario: Scenario) -> dict[str, int]:
    containers = scenario.statistics_containers
    logs = {name: docker("logs", name, check=False).stdout for name in containers}
    relay_consumed = sum(text.count("Relay consumed") for text in logs.values())
    # Pre-PQC QKDNetSim reports KeyServed; the current release reports every
    # supplied QKD/PQC component through KeyServedMixed.  Both are valid KMS
    # delivery evidence and the application Rx trace above is the end-to-end
    # success criterion.
    alice_served = count_key_delivery_events(logs[scenario.readiness_containers[0]])
    bob_served = count_key_delivery_events(logs[scenario.readiness_containers[1]])
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
        "alice_kms_delivery_events": alice_served,
        "bob_kms_delivery_events": bob_served,
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
            scenario.app_kms_ips[1], scenario.kms_ips[1], scenario.peer_flags[1], bob_gateway,
            args.app_rate_bps, args.app_packet_size, args.keys_per_request,
            args.encryption_type, args.authentication_type, scenario.app_ids,
        )
        start_endpoint(
            endpoints[0], scenario.binaries[0], own_data[0], own_data[1],
            scenario.app_kms_ips[0], scenario.kms_ips[0], scenario.peer_flags[0], alice_gateway,
            args.app_rate_bps, args.app_packet_size, args.keys_per_request,
            args.encryption_type, args.authentication_type, scenario.app_ids,
        )
        metrics = wait_for_traffic(
            endpoints[0], endpoints[1], scenario, args.startup_timeout,
            args.traffic_duration, args.min_traffic_packets,
            args.app_rate_bps, args.app_packet_size,
            args.keys_per_request,
            args.delivery_drain_seconds,
            args.measurement_warmup_seconds,
            bool(args.encryption_type or args.authentication_type),
        )
        if (
            args.qkd_topology in ("key-relay", "secoqc")
            and (args.encryption_type or args.authentication_type)
        ):
            metrics.update(verify_relay_evidence(scenario))
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
        "offered_application_rate_bps": args.app_rate_bps,
        "application_packet_size_bytes": args.app_packet_size,
        "encryption_type": args.encryption_type,
        "authentication_type": args.authentication_type,
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
