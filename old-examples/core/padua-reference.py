#!/usr/bin/env python3
"""Run the three Padua reference applications over CORE.

The QKD/KMS plane is supplied by docker-compose.padua-reference.yml.  This
runner creates six application processes (one sender and receiver per flow),
connects every pair with a 100 Mb/s, 2 ms CORE link, enables real OTP/AES
execution, and emits machine-readable application and KMS accounting.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from core.emulator.coreemu import CoreEmu
from core.emulator.data import LinkOptions
from core.emulator.enumerations import EventTypes


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("traffic_topology", HERE / "traffic-topology.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load the common CORE traffic runner")
COMMON = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = COMMON
SPEC.loader.exec_module(COMMON)

MANIFEST = Path("/workspace/examples/comparison/padua-reference.json")
QKD_IMAGE = os.environ.get("CORE_QKD_IMAGE", "qkdnetsim-testbed:latest")
NS_VERSION = os.environ.get("QKD_NS3_VERSION", "3.48")
ALICE_BINARY = (
    f"/opt/ns-3-dev/build/contrib/qkdnetsim/examples/"
    f"ns{NS_VERSION}-relay_etsi014_alice-default"
)
BOB_BINARY = (
    f"/opt/ns-3-dev/build/contrib/qkdnetsim/examples/"
    f"ns{NS_VERSION}-relay_etsi014_bob-default"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument(
        "--time-scale", type=float, default=1.0,
        help="scale application start/stop instants (0.2 is suitable for a pilot)",
    )
    parser.add_argument("--startup-timeout", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.time_scale <= 1:
        parser.error("--time-scale must be in (0, 1]")
    return args


def scaled_time(value: int | float, scale: float, *, minimum: int = 1) -> int:
    return max(minimum, int(round(float(value) * scale)))


def flow_times(flow: dict[str, Any], scale: float) -> tuple[int, int]:
    """Preserve the published start and shorten only the active duration."""
    start = int(flow["startTimeSeconds"])
    duration = int(flow["stopTimeSeconds"]) - start
    return start, start + scaled_time(duration, scale)


def endpoint_command(node: Any, flow: dict[str, Any], own_data: str,
                     peer_data: str, own_kms_ip: str, kms_ip: str,
                     role: str, start: int, stop: int, sim_time: int) -> None:
    pair_index = 1 if flow["id"] in ("1-to-5", "5-to-1") else 2
    site = int(flow["source"] if role == "alice" else flow["destination"])
    peer_site = int(flow["destination"] if role == "alice" else flow["source"])
    left, right = sorted((site, peer_site))
    left_id = f"eeeeeeee-0000-0000-000{pair_index}-00000000000{left}"
    right_id = f"eeeeeeee-0000-0000-000{pair_index}-00000000000{right}"
    alice_id = left_id if int(flow["source"]) == left else right_id
    bob_id = right_id if int(flow["destination"]) == right else left_id
    binary = ALICE_BINARY if role == "alice" else BOB_BINARY
    command = ["exec", "-d"]
    app_ns_log = os.environ.get("QKD_APP_NS_LOG")
    if app_ns_log:
        command.extend(("-e", f"NS_LOG={app_ns_log}"))
    command.extend((node.name, "/opt/entrypoint.sh"))
    command.extend((
        binary,
        "--devData=eth1", "--devKms=eth0",
        f"--myIpData={own_data}", f"--myIpKms={own_kms_ip}",
        f"--{'peerBobIp' if role == 'alice' else 'peerAliceIp'}={peer_data}",
        f"--{'kmsAliceIp' if role == 'alice' else 'kmsBobIp'}={kms_ip}",
        f"--etsiAliceId={alice_id}", f"--etsiBobId={bob_id}",
        f"--appStartTime={start}", f"--appStopTime={stop}",
        f"--simTime={sim_time}", "--numberOfKeyToFetchFromKMS=3",
        "--useCrypto=1", "--authenticationType=0",
        f"--encryptionType={1 if flow['encryption'] == 'OTP' else 2}",
        f"--aesLifetime={int(flow.get('aesLifetimeBytes', 300000))}",
    ))
    if role == "alice":
        command.extend((
            f"--appRateBps={int(flow['rateBps'])}",
            f"--appPacketSize={int(flow['packetSizeBytes'])}",
        ))
    COMMON.docker(*command, check=True)


def read_log(name: str) -> str:
    return COMMON.docker(
        "exec", name, "sh", "-c", "cat /tmp/qkdnetsim.log 2>/dev/null",
        check=False,
    ).stdout


def wait_for_receivers(rows: list[dict[str, Any]], timeout: int) -> None:
    """Do not start any sender until every peer data socket is in Listen()."""
    receivers = [row for row in rows if row["role"] == "bob"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = 0
        for row in receivers:
            name = row["node"].name
            log = read_log(name)
            if "NS_FATAL" in log or "terminate called" in log:
                raise RuntimeError(f"Padua receiver failed before readiness: {name}")
            if "Listening for application traffic" in log:
                ready += 1
        if ready == len(receivers):
            print(f"[PADUA] receiversReady={ready}/{len(receivers)}", flush=True)
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Padua receivers did not become ready: ready={ready}/{len(receivers)}"
    )


def wait_for_senders(rows: list[dict[str, Any]], timeout: int) -> None:
    """Wait for every sender's simulation clock, not a wall-clock estimate."""
    senders = [row for row in rows if row["role"] == "alice"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = {row["node"].name: read_log(row["node"].name) for row in senders}
        for name, log in logs.items():
            fatal = re.search(
                r"NS_FATAL[^\n]*|assert failed[^\n]*|terminate called[^\n]*",
                log, re.IGNORECASE)
            if fatal:
                raise RuntimeError(f"Padua sender {name} failed: {fatal.group(0)}")
        completed = sum("[COMPARE_APP_DONE]" in log for log in logs.values())
        if completed == len(senders):
            return
        time.sleep(0.5)
    incomplete = []
    for name, log in logs.items():
        if "[COMPARE_APP_DONE]" in log:
            continue
        summaries = re.findall(
            r"\[COMPARE_APP_SUMMARY\][^\n]*time=([0-9.]+)", log)
        incomplete.append(
            f"{name}@t={summaries[-1]}s" if summaries else f"{name}@t=unknown")
    raise RuntimeError(
        f"Padua senders did not finish: completed={completed}/{len(senders)}; "
        f"incomplete={','.join(incomplete)}"
    )


def preserve_failure_logs(output: Path | None,
                          endpoint_rows: list[dict[str, Any]],
                          containers: tuple[str, ...]) -> None:
    """Keep the evidence from a failed run instead of deleting it with CORE."""
    if output is None:
        return
    run_dir = output.parent
    endpoint_dir = run_dir / "endpoint-logs"
    kms_dir = run_dir / "kms-logs"
    endpoint_dir.mkdir(parents=True, exist_ok=True)
    kms_dir.mkdir(parents=True, exist_ok=True)
    for row in endpoint_rows:
        name = row["node"].name
        (endpoint_dir / f"{name}.log").write_text(
            read_log(name), encoding="utf-8")
    for name in containers:
        (kms_dir / f"{name}.log").write_text(
            COMMON.docker("logs", name, check=False).stdout,
            encoding="utf-8")


def app_metrics(flow: dict[str, Any], alice_log: str, bob_log: str,
                scale: float) -> dict[str, Any]:
    alice_summaries = re.findall(
        r"\[COMPARE_APP_SUMMARY\][^\n]*txPackets=(\d+)\s+txBytes=(\d+)"
        r"\s+missed=(\d+)\s+keyUses=(\d+)\s+uniqueEncKeys=(\d+)"
        r"\s+payloadBits=(\d+)", alice_log)
    bob_summaries = re.findall(
        r"\[COMPARE_APP_SUMMARY\][^\n]*rxPackets=(\d+)\s+rxBytes=(\d+)",
        bob_log)

    if alice_summaries and bob_summaries:
        sent_packets, sent_bytes, missed, key_uses, unique_keys, payload_bits = (
            int(value) for value in alice_summaries[-1])
        received_packets, received_bytes = (
            int(value) for value in bob_summaries[-1])
    else:
        # Compatibility with immutable old/new comparison images that still
        # expose one marker per packet.
        sent_sizes = [int(x) for x in re.findall(r"\[COMPARE_APP\] Tx[^\n]*bytes=(\d+)", alice_log)]
        received_sizes = [int(x) for x in re.findall(r"\[COMPARE_APP\] Rx[^\n]*bytes=(\d+)", bob_log)]
        keys = re.findall(
            r"\[COMPARE_APP_KEY\][^\n]*encKeyId=(\S+)\s+authKeyId=(\S+)\s+payloadBits=(\d+)",
            alice_log)
        used_ids = [key for key, _, _ in keys if key.strip("0")]
        sent_packets, sent_bytes = len(sent_sizes), sum(sent_sizes)
        received_packets, received_bytes = len(received_sizes), sum(received_sizes)
        missed = len(re.findall(r"\[COMPARE_APP\] Mx\b", alice_log))
        key_uses, unique_keys = len(used_ids), len(set(used_ids))
        payload_bits = sum(int(bits) for key, _, bits in keys if key.strip("0"))
    duration = scaled_time(
        int(flow["stopTimeSeconds"]) - int(flow["startTimeSeconds"]), scale
    )
    received_payload = received_packets * int(flow["packetSizeBytes"])
    achieved_goodput = received_payload * 8 / duration
    return {
        "application": flow["id"],
        "sent_packets": sent_packets,
        "received_packets": received_packets,
        "sent_bytes": sent_bytes,
        "received_bytes": received_bytes,
        "missed_send_calls": missed,
        "delivery_ratio": received_packets / sent_packets if sent_packets else None,
        "application_goodput_bps": achieved_goodput,
        "offered_rate_bps": int(flow["rateBps"]),
        "offered_rate_achievement": achieved_goodput / int(flow["rateBps"]),
        "encryption_key_use_operations": key_uses,
        "unique_encryption_keys_used": unique_keys,
        "otp_payload_bits_protected": (
            payload_bits
            if flow["encryption"] == "OTP" else 0
        ),
    }


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    flows = manifest["applications"]
    containers = tuple(f"qkd-padua-site-{letter}" for letter in "abcdef")
    COMMON.wait_for_infrastructure(containers, args.startup_timeout)

    site_network = {
        1: ("qkdnetsim-padua-reference_kms_app_a", "192.168.231.11", "192.168.231"),
        5: ("qkdnetsim-padua-reference_kms_app_e", "192.168.232.15", "192.168.232"),
        6: ("qkdnetsim-padua-reference_kms_app_f", "192.168.233.16", "192.168.233"),
    }
    coreemu = CoreEmu()
    session = coreemu.create_session((time.time_ns() ^ os.getpid()) % 2_000_000_000 + 1)
    session.set_state(EventTypes.CONFIGURATION_STATE)
    endpoint_rows: list[dict[str, Any]] = []
    try:
        for network, _, _ in site_network.values():
            if COMMON.docker("network", "inspect", network, check=False).returncode:
                raise RuntimeError(f"required KMS network does not exist: {network}")
        for index, flow in enumerate(flows):
            network = ipaddress.ip_network(f"10.253.{index}.0/30")
            pair_nodes = []
            for role_index, role in enumerate(("alice", "bob")):
                site = int(flow["source"] if role == "alice" else flow["destination"])
                kms_network, kms_ip, prefix = site_network[site]
                own_kms_ip = f"{prefix}.{30 + index * 2 + role_index}"
                name = f"qkd-padua-{flow['id']}-{role}-{os.getpid()}"
                node = session.add_node(
                    COMMON.EndpointNode,
                    name=name,
                    options=COMMON.EndpointOptions(
                        image=QKD_IMAGE,
                        kms_network=kms_network,
                        kms_ip=own_kms_ip,
                        kms_mac=f"02:00:00:fa:{index + 1:02x}:{role_index + 1:02x}",
                    ),
                )
                pair_nodes.append(node)
                endpoint_rows.append({
                    "node": node, "flow": flow, "role": role,
                    "kms_ip": kms_ip, "own_kms_ip": own_kms_ip,
                    "own_data": str(network.network_address + role_index + 1),
                    "peer_data": str(network.network_address + (2 if role_index == 0 else 1)),
                })
            session.add_link(
                pair_nodes[0].id, pair_nodes[1].id,
                COMMON.iface(network.network_address + 1, "eth1"),
                COMMON.iface(network.network_address + 2, "eth1"),
                LinkOptions(delay=2_000, bandwidth=100_000_000, loss=0),
            )
        session.instantiate()

        flow_schedule = {
            flow["id"]: flow_times(flow, args.time_scale) for flow in flows
        }
        sim_time = max(stop for _, stop in flow_schedule.values()) + 3
        # Receivers have no offered load, so starting their listeners at t=1
        # does not alter the application workload. Waiting for the readiness
        # trace removes a cross-process TCP race that cannot exist in the
        # monolithic reference.
        for row in endpoint_rows:
            if row["role"] != "bob":
                continue
            flow = row["flow"]
            endpoint_command(
                row["node"], flow, row["own_data"], row["peer_data"],
                row["own_kms_ip"], row["kms_ip"], "bob", 1,
                sim_time + 60, sim_time + 60,
            )
        wait_for_receivers(endpoint_rows, args.startup_timeout)
        for row in endpoint_rows:
            if row["role"] != "alice":
                continue
            flow = row["flow"]
            start, stop = flow_schedule[flow["id"]]
            endpoint_command(
                row["node"], flow, row["own_data"], row["peer_data"],
                row["own_kms_ip"], row["kms_ip"], "alice",
                start, stop, sim_time,
            )

        try:
            wait_for_senders(endpoint_rows, max(120, sim_time * 5))
        except Exception:
            preserve_failure_logs(args.output, endpoint_rows, containers)
            raise
        # A bounded drain avoids reading the receiver immediately before its
        # final TCP frame is delivered.
        time.sleep(2)

        applications = []
        endpoint_logs: dict[str, str] = {}
        for row in endpoint_rows:
            endpoint_logs[row["node"].name] = read_log(row["node"].name)
        for flow in flows:
            alice = next(row for row in endpoint_rows
                         if row["flow"]["id"] == flow["id"] and row["role"] == "alice")
            bob = next(row for row in endpoint_rows
                       if row["flow"]["id"] == flow["id"] and row["role"] == "bob")
            applications.append(app_metrics(
                flow,
                endpoint_logs[alice["node"].name],
                endpoint_logs[bob["node"].name],
                args.time_scale,
            ))
        kms_logs = {name: COMMON.docker("logs", name, check=False).stdout for name in containers}
        fatal_pattern = re.compile(
            r"NS_FATAL|assert failed|terminate called|Segmentation fault|JSON parse error",
            re.IGNORECASE)
        fatal_log_markers = sum(
            len(fatal_pattern.findall(log))
            for log in (*endpoint_logs.values(), *kms_logs.values()))
        result = {
            "schema_version": 1,
            "profile": "padua-reference",
            "time_scale": args.time_scale,
            "real_crypto": True,
            "authentication_limitation": (
                "The paper specifies SHA2, which QKDNetSim does not implement; "
                "the executable validates real OTP/AES with authentication disabled."
            ),
            "applications": applications,
            "kms": {
                name: COMMON.parse_kms_statistics(log)
                for name, log in kms_logs.items()
            },
            "fatal_log_markers": fatal_log_markers,
            "passed": fatal_log_markers == 0 and all(
                row["sent_packets"] > 0
                and row["received_packets"] > 0
                # This is a functional verdict, not a throughput verdict.  A
                # short, time-scaled run can be key-limited and therefore must
                # not fail merely because it did not attain the configured
                # application rate.  Throughput remains an explicit metric for
                # the full monolithic/distributed comparison.  The published
                # workload itself reports a few packets missing at its stop
                # boundary, hence the 98% delivery floor.
                and row["delivery_ratio"] >= 0.98
                and row["encryption_key_use_operations"] > 0
                for row in applications
            ),
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log_dir = args.output.parent / "endpoint-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            for name, log in endpoint_logs.items():
                (log_dir / f"{name}.log").write_text(log, encoding="utf-8")
            kms_log_dir = args.output.parent / "kms-logs"
            kms_log_dir.mkdir(parents=True, exist_ok=True)
            for name, log in kms_logs.items():
                (kms_log_dir / f"{name}.log").write_text(log, encoding="utf-8")
            print(
                f"[PADUA_RESULT] output={args.output} passed={result['passed']}",
                flush=True,
            )
        else:
            print("[PADUA_RESULT] " + json.dumps(result, sort_keys=True), flush=True)
        if not result["passed"]:
            raise RuntimeError("Padua reference validation did not satisfy its criteria")
    finally:
        session.shutdown()
        coreemu.shutdown()


if __name__ == "__main__":
    main()
