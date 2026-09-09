#!/usr/bin/env python3
"""Compare pristine monolithic QKDNetSim with the Docker/CORE adaptation.

The QKD key rate is a controlled input, not a performance result.  Primary
measurements are application delivery, missed sends, wall-clock stability and
aggregate Docker CPU/memory cost under a fixed offered load.

Point-to-point and SECOQC are paired architecture comparisons.  The smaller
Alice--trusted--Bob relay remains a functional reference only because it is
not topologically equivalent to upstream SECOQC.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"
CORE_COMPOSE = DOCKER_DIR / "docker-compose.core.yml"
OLD_TESTBED_COMMIT = "99783ee7f10314b52af71ce0528db7305f461f44"
RESULT_MARKER = "[CORE_RESULT] "
APP_RATE_BPS = 6400
APP_PACKET_SIZE = 800
DEFAULT_KEYS_PER_REQUEST = 3
KEY_BUFFER_LOW_WATERMARK = 1
MEASUREMENT_WARMUP_SECONDS = 5
DELIVERY_DRAIN_SECONDS = 2.0
DOCKER_DESKTOP = (
    Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
)
# Windows may expose an App Execution Alias named ``docker`` that exists but
# raises WinError 5. Prefer Docker Desktop's real executable when available;
# Linux still resolves the normal executable from PATH.
DOCKER = str(DOCKER_DESKTOP) if os.name == "nt" else (
    shutil.which("docker") or "docker"
)

VERSIONS = {
    "old": {
        "baseline_image": "qkdnetsim:base-old",
        "testbed_image": "qkdnetsim-testbed:old",
    },
    "new": {
        "baseline_image": "qkdnetsim:base-new",
        "testbed_image": "qkdnetsim-testbed:new",
    },
}

TOPOLOGIES = {
    "p2p": {
        "baseline_target": "examples_qkdnetsim_etsi_014",
        "compose": "docker-compose.yml",
        "qkd_topology": "point-to-point",
        "container_prefixes": ("qkd-p2p-", "qkd-core-traffic-"),
        "comparison_validity": "paired",
        "kms_control_hops": 1,
        "kms_control_delay_per_hop_ms": 0,
        "note": (
            "Same direct QKD/KMS roles, ETSI 014 consumer settings and offered "
            "load; the deployment boundary is monolithic versus Docker/CORE."
        ),
        "rate_env": {
            "QKD_ZERO_LOSS_KEY_RATE_BPS": "10000",
            "QKD_FIBER_LENGTH_KM": "0",
        },
    },
    "relay": {
        "baseline_target": "examples_qkdnetsim_secoqc",
        "compose": "docker-compose.key-relay.yml",
        "qkd_topology": "key-relay",
        "container_prefixes": ("qkd-relay-", "qkd-core-traffic-"),
        "comparison_validity": "reference_only",
        "kms_control_hops": 2,
        "kms_control_delay_per_hop_ms": 0,
        "note": (
            "Upstream SECOQC uses six KMS sites and a longer trusted path; the "
            "testbed uses Alice-trusted-Bob. Compare correctness and normalized "
            "delivery only, not an architecture-overhead percentage."
        ),
        "rate_env": {
            "QKD_ZERO_LOSS_KEY_RATE_BPS": "10000",
            "QKD_ALICE_RELAY_FIBER_LENGTH_KM": "0",
            "QKD_RELAY_BOB_FIBER_LENGTH_KM": "0",
        },
    },
    "secoqc": {
        "baseline_target": "examples_qkdnetsim_secoqc",
        "compose": "docker-compose.secoqc.yml",
        "qkd_topology": "secoqc",
        "container_prefixes": ("qkd-secoqc-", "qkd-core-traffic-"),
        "comparison_validity": "paired",
        "kms_control_hops": 4,
        "kms_control_delay_per_hop_ms": 2,
        "kms_delay_processes": 6,
        "note": (
            "Same six KMS sites, six QKD links, 10 kb/s link rates and A-to-F "
            "ETSI 014 workload; only the monolithic/distributed deployment "
            "boundary changes."
        ),
        "rate_env": {"QKD_ZERO_LOSS_KEY_RATE_BPS": "10000"},
    },
}


def run(command: list[str], *, env: dict[str, str] | None = None,
        timeout: int | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    complete_env = dict(os.environ)
    if env:
        complete_env.update(env)
    result = subprocess.run(
        command, cwd=ROOT, env=complete_env, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}"
        )
    return result


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run([DOCKER, *args], **kwargs)


def compose(path: Path, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run([DOCKER, "compose", "-f", str(path), *args], **kwargs)


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kmgt]?i?b)\s*", value.lower())
    if not match:
        return 0
    factors = {
        "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3,
        "kib": 1024, "mib": 1024**2, "gib": 1024**3,
    }
    return int(float(match.group(1)) * factors[match.group(2)])


class ResourceSampler:
    """Sample aggregate Docker resources for containers selected by prefix."""

    def __init__(self, prefixes: tuple[str, ...], interval: float = 1.0):
        self.prefixes = prefixes
        self.interval = interval
        self.samples: list[dict[str, float | int]] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            names = [
                name for name in docker("ps", "--format", "{{.Names}}").stdout.splitlines()
                if any(name.startswith(prefix) for prefix in self.prefixes)
            ]
            if names:
                snapshot = docker("stats", "--no-stream", "--format", "{{json .}}", *names)
                cpu_sum = 0.0
                memory_sum = 0
                count = 0
                for line in snapshot.stdout.splitlines():
                    try:
                        item = json.loads(line)
                        cpu_sum += float(item.get("CPUPerc", "0").rstrip("%"))
                        used = str(item.get("MemUsage", "0B / 0B")).split("/")[0]
                        memory_sum += parse_size(used)
                        count += 1
                    except (ValueError, json.JSONDecodeError):
                        continue
                if count:
                    self.samples.append({
                        "cpu_percent_sum": cpu_sum,
                        "memory_bytes_sum": memory_sum,
                        "containers": count,
                    })
            self.stop_event.wait(self.interval)

    def stop(self) -> dict[str, float | int | None]:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if not self.samples:
            return {"sample_count": 0, "mean_cpu_percent_sum": None,
                    "peak_memory_bytes_sum": None, "max_container_count": None}
        return {
            "sample_count": len(self.samples),
            "mean_cpu_percent_sum": statistics.fmean(
                float(sample["cpu_percent_sum"]) for sample in self.samples
            ),
            "peak_memory_bytes_sum": max(
                int(sample["memory_bytes_sum"]) for sample in self.samples
            ),
            "max_container_count": max(
                int(sample["containers"]) for sample in self.samples
            ),
        }


def require_images(versions: tuple[str, ...]) -> None:
    missing = []
    stale = []
    for version in versions:
        config = VERSIONS[version]
        for role, image in config.items():
            inspection = docker(
                "image", "inspect", image, "--format",
                "{{index .Config.Labels \"org.qkdnetsim.comparison.instrumentation\"}}",
            )
            if inspection.returncode:
                missing.append(image)
            else:
                expected = {
                    ("old", "baseline_image"): "matched-workload-v5",
                    ("new", "baseline_image"): "matched-workload-v7",
                    ("old", "testbed_image"): "matched-app-traces-v7",
                    ("new", "testbed_image"): "matched-app-traces-v8",
                }[(version, role)]
                if inspection.stdout.strip() != expected:
                    stale.append(image)
    if missing:
        raise RuntimeError(
            "missing comparison images: " + ", ".join(sorted(set(missing)))
            + ". Build them with docker/comparison/build-all.sh"
        )
    if stale:
        raise RuntimeError(
            "comparison images predate the benchmark instrumentation: "
            + ", ".join(sorted(set(stale)))
            + ". Rebuild them with docker/comparison/build-all.sh"
        )


def section(text: str, heading: str, next_heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    end = text.find(next_heading, start + len(heading))
    return text[start:end if end >= 0 else None]


def packet_totals(block: str) -> tuple[int, int]:
    entries = re.findall(r"Number:\s*(\d+)\s+Bytes:\s*(\d+)", block)
    return sum(int(number) for number, _ in entries), sum(int(size) for _, size in entries)


def parse_monolithic_paper_validation(output: str, generation_window: int) -> dict[str, Any]:
    generated_block = section(output, "QKD LINK STATS:", "SERVICE STATS:")
    generated: dict[str, list[int]] = {}
    for link_id, bits in re.findall(
        r"QKDSystem link:\s*(\S+).*?Size:\s*(\d+)\s*\(bits\)",
        generated_block,
    ):
        generated.setdefault(link_id, []).append(int(bits))
    for compound_id, bits in re.findall(
        r"Link \(([^)]+)\)\s*Generated \(bits\):\s*(\d+)", generated_block
    ):
        # This older formatter labels every row ``moduleId-keyId`` and the
        # callback observed the same physical key at both QKD endpoints.  The
        # module UUID is the first field; divide the accumulated size by two,
        # matching Ratio() in the newer example and Table 3 of the paper.
        uuid_match = re.match(r"([0-9a-fA-F-]{36})-", compound_id)
        link_id = uuid_match.group(1) if uuid_match else compound_id
        generated.setdefault(link_id, []).append(int(bits) // 2)
    link_rows = []
    for link_id, sizes in sorted(generated.items()):
        total = sum(sizes)
        link_rows.append({
            "link_id": link_id,
            "generated_keys": len(sizes),
            "generated_bits": total,
            "average_generated_key_size_bits": total / len(sizes),
            "observed_generation_rate_bps": total / generation_window,
        })

    service_start = output.find("SERVICE STATS:")
    service_block = output[service_start:] if service_start >= 0 else ""
    served_keys = {
        key_id: int(bits)
        for key_id, bits in re.findall(
            r"Key ID:\s*(\S+)\s+Size:\s*(\d+)\s*\(bits\)", service_block
        )
    }
    # Newer format, when present, prints one already-deduplicated row.
    for key_id, bits in re.findall(
        r"Key\s+(\S+)\s+Served \(bits\):\s*(\d+)", service_block
    ):
        served_keys[key_id] = int(bits)
    material_by_key = {key_id: "qkd" for key_id in served_keys}
    service_by_kms: dict[tuple[str, str], list[int]] = {}
    for match in re.finditer(
        r"\[COMPARE_KMS\] served\s+context=(\S+)\s+ksid=(\S*)[^\n]*"
        r"keyId=(\S+)\s+bits=(\d+)\s+type=(\S+)",
        output,
    ):
        # Empty KSID identifies internal KMS-to-KMS material movement rather
        # than a key delivered to the cryptographic application.
        if not match.group(2):
            continue
        context, key_id, bits, material_type = (
            match.group(1), match.group(3), match.group(4), match.group(5)
        )
        served_keys[key_id] = int(bits)
        material_by_key[key_id] = material_type
        node_match = re.search(r"/NodeList/(\d+)/", context)
        kms = f"node-{node_match.group(1)}" if node_match else context
        service_by_kms.setdefault((kms, material_type), []).append(int(bits))
    for match in re.finditer(
        r"\[COMPARE_KMS\] served\s+context=(\S+)\s+appId=(\S+)\s+"
        r"keyId=(\S+)\s+bits=(\d+)\s+type=(\S+)",
        output,
    ):
        context, key_id, bits, material_type = (
            match.group(1), match.group(3), match.group(4), match.group(5)
        )
        served_keys[key_id] = int(bits)
        material_by_key[key_id] = material_type
        node_match = re.search(r"/NodeList/(\d+)/", context)
        kms = f"node-{node_match.group(1)}" if node_match else context
        service_by_kms.setdefault((kms, material_type), []).append(int(bits))
    service_available = bool(served_keys)
    relay_block = section(output, "KEY RELAY STATS:", "Wasted key material")
    relay_bits = [
        int(value) for value in re.findall(r"Amount \(bits\):\s*(\d+)", relay_block)
    ]
    machine_relay_events = [
        (int(node), int(bits)) for node, bits in re.findall(
            r"\[COMPARE_KMS\] relay\s+node=(\d+)[^\n]*bits=(\d+)", output
        )
    ]
    machine_relay = [bits for _, bits in machine_relay_events]
    if machine_relay:
        relay_bits = machine_relay
    waste_block = section(output, "Wasted key material", "QKD LINK STATS:")
    waste_bits = [
        int(value) for value in re.findall(r"Wasted amount:\s*(\d+)", waste_block)
    ]
    machine_waste = [
        int(value) for value in re.findall(
            r"\[COMPARE_KMS\] waste[^\n]*bits=(\d+)", output
        )
    ]
    if machine_waste:
        waste_bits = machine_waste
    consumption_by_material = []
    for material_type in sorted(set(material_by_key.values())):
        values = [
            bits for key_id, bits in served_keys.items()
            if material_by_key[key_id] == material_type
        ]
        consumption_by_material.append({
            "material_type": material_type,
            "keys_consumed": len(values),
            "keys_consumed_bits": sum(values),
        })
    key_uses = re.findall(
        r"\[COMPARE_APP_KEY\]\s+appId=(\S+)\s+encKeyId=(\S+)\s+"
        r"authKeyId=(\S+)\s+payloadBits=(\d+)",
        output,
    )
    is_real_key = lambda value: bool(value.strip("0"))
    encryption_ids = [enc for _, enc, _, _ in key_uses if is_real_key(enc)]
    authentication_ids = [auth for _, _, auth, _ in key_uses if is_real_key(auth)]
    buffer_samples = [
        {"time_seconds": float(timestamp), "context": context, "bits": int(bits)}
        for timestamp, context, bits in re.findall(
            r"\[COMPARE_BUFFER\]\s+time=([0-9.]+)\s+context=(\S+)\s+bits=(\d+)",
            output,
        )
    ]
    relay_by_node: dict[str, list[int]] = {}
    for node, bits in machine_relay_events:
        relay_by_node.setdefault(f"node-{node}", []).append(bits)
    return {
        "measurement_window_seconds": generation_window,
        "qkd_links": link_rows,
        "service": {
            # Some pinned upstream examples print the SERVICE STATS heading
            # without connecting the service trace.  Missing observations are
            # not equivalent to a measured zero.
            "supply_events": len(served_keys) if service_available else None,
            "supplied_bits": sum(served_keys.values()) if service_available else None,
        },
        "application_consumption": {
            "keys_consumed": len(served_keys) if service_available else None,
            "keys_consumed_bits": (
                sum(served_keys.values()) if service_available else None
            ),
        },
        "application_consumption_by_material": consumption_by_material,
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
        "service_by_kms_and_material": [
            {"kms": kms, "material_type": material_type,
             "supply_events": len(values), "supplied_bits": sum(values)}
            for (kms, material_type), values in sorted(service_by_kms.items())
        ],
        "relay_by_kms": [
            {"kms": kms, "relay_events": len(values), "relayed_bits": sum(values)}
            for kms, values in sorted(relay_by_node.items())
        ],
        "relay": {"relay_events": len(relay_bits), "relayed_bits": sum(relay_bits)},
        "relay_waste": {"waste_events": len(waste_bits), "wasted_bits": sum(waste_bits)},
        "buffer_time_series_available": bool(buffer_samples),
        "buffer_time_series": buffer_samples,
    }


def parse_monolithic_output(output: str, duration: int, simulation_time: int) -> dict[str, Any]:
    sent = section(output, "APP-APP Data Packets Sent:", "APP-APP Data Packets Received:")
    received = section(output, "APP-APP Data Packets Received:", "Missed Send Packet Calls:")
    missed = section(output, "Missed Send Packet Calls:", "APP-APP Signaling Packets Sent:")
    sent_packets, sent_bytes = packet_totals(sent)
    received_packets, received_bytes = packet_totals(received)
    missed_calls = sum(int(value) for value in re.findall(r"Number:\s*(\d+)", missed))
    # One scheduling opportunity exists per packet interval.  With key use
    # enabled an opportunity may become Mx instead of Tx; it must still remain
    # in the denominator when measuring realization of the offered load.
    expected_packets = max(
        1, math.ceil(duration * APP_RATE_BPS / (APP_PACKET_SIZE * 8))
    )
    received_payload_bytes = received_packets * APP_PACKET_SIZE
    return {
        "sent_packets": sent_packets,
        "sent_bytes": sent_bytes,
        "received_packets": received_packets,
        "received_bytes": received_bytes,
        "delivery_ratio": received_packets / sent_packets if sent_packets else None,
        "received_payload_bytes": received_payload_bytes,
        "application_goodput_bps": received_payload_bytes * 8 / duration,
        "expected_application_packets": expected_packets,
        "offered_load_realization": sent_packets / expected_packets,
        "missed_send_calls": missed_calls,
        "offered_load_shortfall_packets": max(expected_packets - sent_packets, 0),
        "paper_validation": parse_monolithic_paper_validation(
            output, duration
        ),
    }


def run_monolithic(version: str, topology: str, duration: int, repetition: int,
                   keys_per_request: int, encryption_type: int,
                   authentication_type: int) -> dict[str, Any]:
    config = VERSIONS[version]
    topo = TOPOLOGIES[topology]
    name = f"qkd-compare-base-{version}-{topology}-{os.getpid()}-{repetition}"
    warmup = 20
    measurement_start = warmup + MEASUREMENT_WARMUP_SECONDS
    sim_time = measurement_start + duration + 1
    invocation = (
        f"{topo['baseline_target']} --simTime={sim_time} --appStartTime={warmup} "
        f"--appStopTime={measurement_start + duration} --qkdStartTime=0 "
        f"--qkdStopTime={sim_time} --useCrypto=0 --seed={repetition} "
        f"--comparisonMeasurementStart={measurement_start} "
        f"--appRate={APP_RATE_BPS} --appPacketSize={APP_PACKET_SIZE} "
        f"--numberOfKeyToFetchFromKMS={keys_per_request} "
        f"--encryptionType={encryption_type} "
        f"--authenticationType={authentication_type}"
    )
    sampler = ResourceSampler((name,))
    sampler.start()
    started = time.monotonic()
    try:
        # NOT --SimulatorImplementationType=... on the ns-3 CommandLine: that
        # GlobalValue is silently ignored there (confirmed with --PrintGlobals
        # -- it still reports [ns3::DefaultSimulatorImpl] afterwards, and the
        # process runs 10x+ faster than simulated time). NS_GLOBAL_VALUE is
        # the one mechanism that actually switches the simulator backend
        # before Simulator::Run(), verified with `time` against wall clock.
        result = docker(
            "run", "--name", name,
            "-e", "NS_GLOBAL_VALUE=SimulatorImplementationType=ns3::RealtimeSimulatorImpl",
            config["baseline_image"], "./ns3", "run", invocation, timeout=sim_time + 120,
        )
    finally:
        resources = sampler.stop()
        docker("rm", "-f", name)
    wall = time.monotonic() - started
    metrics = parse_monolithic_output(result.stdout, duration, sim_time)
    return {
        "deployment": "monolithic", "image": config["baseline_image"],
        "returncode": result.returncode,
        "passed": (
            result.returncode == 0
            and metrics["received_packets"] > 0
            and metrics["delivery_ratio"] == 1.0
        ),
        "wall_seconds": round(wall, 3), "simulated_seconds": sim_time,
        "realtime_factor": sim_time / wall if wall else None,
        "metrics": metrics, "resources": resources, "log": result.stdout,
    }


def materialize_old_compose(directory: Path, filename: str) -> Path:
    output = directory / filename
    result = run(["git", "show", f"{OLD_TESTBED_COMMIT}:docker/{filename}"], check=True)
    output.write_text(result.stdout, encoding="utf-8")
    return output


def parse_core_result(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


def stop_testbed(compose_file: Path) -> None:
    compose(compose_file, "down", "--remove-orphans")
    stale = docker("ps", "-aq", "--filter", "name=qkd-core-traffic-").stdout.split()
    if stale:
        docker("rm", "-f", *stale)


def run_distributed(version: str, topology: str, duration: int, repetition: int,
                    temp_directory: Path, keys_per_request: int,
                    encryption_type: int, authentication_type: int) -> dict[str, Any]:
    config = VERSIONS[version]
    topo = TOPOLOGIES[topology]
    docker("tag", config["testbed_image"], "qkdnetsim-testbed:latest", check=True)
    compose_file = (
        materialize_old_compose(temp_directory, str(topo["compose"]))
        if version == "old" and topology != "secoqc"
        else DOCKER_DIR / str(topo["compose"])
    )
    try:
        compose(CORE_COMPOSE, "up", "-d", "core", check=True)
        stop_testbed(compose_file)
        started = time.monotonic()
        compose_env = dict(topo["rate_env"])
        compose_env["QKD_NS3_VERSION"] = "3.46" if version == "old" else "3.48"
        compose(compose_file, "up", "-d", "--force-recreate",
                env=compose_env, check=True)
        infrastructure_seconds = time.monotonic() - started

        local_runner = ROOT / "old-examples" / "core" / "traffic-topology.py"
        remote_runner = "/workspace/old-examples/core/traffic-topology.py"
        if version == "old":
            compatibility_runner = temp_directory / "traffic-topology-ns346.py"
            compatibility_runner.write_text(
                local_runner.read_text(encoding="utf-8").replace("ns3.48-", "ns3.46-"),
                encoding="utf-8",
            )
            docker("cp", str(compatibility_runner),
                   "qkdnetsim-core:/tmp/traffic-topology-ns346.py", check=True)
            remote_runner = "/tmp/traffic-topology-ns346.py"

        command = [
            DOCKER, "compose", "-f", str(CORE_COMPOSE), "exec", "-T",
            "-e", f"CORE_QKD_IMAGE={config['testbed_image']}", "core",
            "/opt/core/venv/bin/python", remote_runner,
            "--qkd-topology", str(topo["qkd_topology"]),
            "--routers", "1", "--delay-ms", "2", "--bandwidth-mbps", "50",
            "--loss-percent", "0", "--traffic-duration", str(duration),
            "--app-rate-bps", str(APP_RATE_BPS),
            "--app-packet-size", str(APP_PACKET_SIZE),
            "--keys-per-request", str(keys_per_request),
            "--encryption-type", str(encryption_type),
            "--authentication-type", str(authentication_type),
            "--delivery-drain-seconds", str(DELIVERY_DRAIN_SECONDS),
            "--measurement-warmup-seconds", str(MEASUREMENT_WARMUP_SECONDS),
            "--min-traffic-packets", "3", "--startup-timeout", "300",
        ]
        sampler = ResourceSampler(tuple(topo["container_prefixes"]))
        sampler.start()
        workload_started = time.monotonic()
        try:
            result = run(command, timeout=duration + 420)
        finally:
            resources = sampler.stop()
        wall = time.monotonic() - workload_started
        parsed = parse_core_result(result.stdout)
        infrastructure_log = compose(compose_file, "logs", "--no-color").stdout
        combined_log = (
            result.stdout
            + "\n[COMPARE_INFRASTRUCTURE_LOG]\n"
            + infrastructure_log
        )
        passed = result.returncode == 0 and parsed is not None and parsed.get("status") == "passed"
        delay_processes = infrastructure_log.count("KMS egress delay=2ms")
        if topology == "secoqc":
            expected_delay_processes = int(topo["kms_delay_processes"])
            passed = passed and delay_processes == expected_delay_processes
            if parsed is not None:
                parsed.setdefault("metrics", {})[
                    "kms_delay_configured_processes"
                ] = delay_processes
        return {
            "deployment": "distributed", "image": config["testbed_image"],
            "returncode": result.returncode, "passed": passed,
            "infrastructure_startup_seconds": round(infrastructure_seconds, 3),
            "wall_seconds": round(wall, 3),
            "metrics": (parsed or {}).get("metrics", {}), "runner_result": parsed,
            "resources": resources, "log": combined_log,
        }
    finally:
        stop_testbed(compose_file)


def failed_deployment(deployment: str, error: Exception) -> dict[str, Any]:
    return {
        "deployment": deployment, "returncode": None, "passed": False,
        "wall_seconds": None, "metrics": {}, "resources": {},
        "error": str(error), "log": f"{type(error).__name__}: {error}\n",
    }


def ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def summarize_pair(topology: str, baseline: dict[str, Any],
                   testbed: dict[str, Any]) -> dict[str, Any]:
    base_delivery = baseline["metrics"].get("delivery_ratio")
    distributed_delivery = testbed["metrics"].get("delivery_ratio")
    base_goodput = baseline["metrics"].get("application_goodput_bps")
    distributed_goodput = testbed["metrics"].get("application_goodput_bps")
    validity = TOPOLOGIES[topology]["comparison_validity"]
    return {
        "comparison_validity": validity,
        "interpretation": TOPOLOGIES[topology]["note"],
        "baseline_delivery_ratio": base_delivery,
        "distributed_delivery_ratio": distributed_delivery,
        "baseline_offered_load_realization": baseline["metrics"].get("offered_load_realization"),
        "distributed_offered_load_realization": testbed["metrics"].get("offered_load_realization"),
        "baseline_goodput_bps": base_goodput,
        "distributed_goodput_bps": distributed_goodput,
        "goodput_retention": (
            ratio(distributed_goodput, base_goodput) if validity == "paired" else None
        ),
        "delivery_retention": (
            ratio(distributed_delivery, base_delivery) if validity == "paired" else None
        ),
        "both_passed": baseline["passed"] and testbed["passed"],
    }


def write_paper_style_tables(records: list[dict[str, Any]], output_dir: Path) -> None:
    """Export Tables 2--4 analogues plus distributed-only accounting."""
    settings_fields = (
        "version", "topology", "repetition", "deployment", "application",
        "interface", "data_rate_kbps", "packet_size_bytes", "encryption",
        "authentication", "key_lifetime_bytes", "configured_start_time_seconds",
        "configured_stop_time_seconds", "keys_per_request", "crypto_operations_enabled",
    )
    with (output_dir / "application-settings.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=settings_fields)
        writer.writeheader()
        for record in records:
            duration = record["controls"]["measurement_duration_seconds"]
            for deployment_key, deployment_name in (
                ("monolithic", "monolithic"), ("distributed", "distributed")
            ):
                writer.writerow({
                    "version": record["version"], "topology": record["topology"],
                    "repetition": record["repetition"], "deployment": deployment_name,
                    "application": "Alice->Bob", "interface": "ETSI GS QKD 014",
                    "data_rate_kbps": APP_RATE_BPS / 1000,
                    "packet_size_bytes": APP_PACKET_SIZE,
                    # The architecture campaign deliberately disables crypto
                    # operations on both sides to isolate deployment overhead.
                    # VPN/OTP correctness and rotation are exercised by the
                    # functional regression runner, not inferred here.
                    "encryption": (
                        {0: "disabled", 1: "OTP", 2: "AES"}
                        [record["controls"]["encryption_type"]]
                    ),
                    "authentication": (
                        {0: "disabled", 1: "VMAC", 2: "MD5", 3: "SHA1"}
                        [record["controls"]["authentication_type"]]
                    ),
                    "key_lifetime_bytes": None,
                    "configured_start_time_seconds": 20 if deployment_key == "monolithic" else 2,
                    "configured_stop_time_seconds": (
                        20 + MEASUREMENT_WARMUP_SECONDS + duration
                        if deployment_key == "monolithic"
                        else 5000
                    ),
                    "keys_per_request": record["controls"]["keys_per_request"],
                    "crypto_operations_enabled": False,
                })

    application_fields = (
        "version", "topology", "repetition", "deployment", "sent_bytes",
        "received_bytes", "sent_packets", "received_packets",
        "missed_send_calls", "offered_load_shortfall_packets", "delivery_ratio",
        "application_goodput_bps", "application_key_requests",
        "application_keys_requested", "keys_consumed", "keys_consumed_bits",
        "qkd_keys_consumed", "qkd_bits_consumed", "pqc_keys_consumed",
        "pqc_bits_consumed", "kms_material_trace_events",
        "encryption_key_use_operations", "authentication_key_use_operations",
        "unique_encryption_keys_used", "unique_authentication_keys_used",
        "otp_payload_bits_protected",
    )
    with (output_dir / "application-statistics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=application_fields)
        writer.writeheader()
        for record in records:
            for deployment_key, deployment_name in (
                ("monolithic", "monolithic"), ("distributed", "distributed")
            ):
                metrics = record[deployment_key].get("metrics", {})
                validation = metrics.get("paper_validation", {})
                consumption = validation.get("application_consumption", {})
                by_material = {
                    row["material_type"]: row
                    for row in validation.get("application_consumption_by_material", [])
                }
                key_use = validation.get("application_key_use", {})
                writer.writerow({
                    "version": record["version"],
                    "topology": record["topology"],
                    "repetition": record["repetition"],
                    "deployment": deployment_name,
                    "sent_bytes": metrics.get("sent_bytes"),
                    "received_bytes": metrics.get("received_bytes"),
                    "sent_packets": metrics.get("sent_packets"),
                    "received_packets": metrics.get("received_packets"),
                    "missed_send_calls": metrics.get("missed_send_calls"),
                    "offered_load_shortfall_packets": metrics.get(
                        "offered_load_shortfall_packets"
                    ),
                    "delivery_ratio": metrics.get("delivery_ratio"),
                    "application_goodput_bps": metrics.get("application_goodput_bps"),
                    "application_key_requests": metrics.get("application_key_requests"),
                    "application_keys_requested": metrics.get("application_keys_requested"),
                    "keys_consumed": consumption.get("keys_consumed"),
                    "keys_consumed_bits": consumption.get("keys_consumed_bits"),
                    "qkd_keys_consumed": by_material.get("qkd", {}).get("keys_consumed"),
                    "qkd_bits_consumed": by_material.get("qkd", {}).get("keys_consumed_bits"),
                    "pqc_keys_consumed": by_material.get("pqc", {}).get("keys_consumed"),
                    "pqc_bits_consumed": by_material.get("pqc", {}).get("keys_consumed_bits"),
                    "kms_material_trace_events": metrics.get("kms_material_trace_events"),
                    "encryption_key_use_operations": key_use.get("encryption_operations"),
                    "authentication_key_use_operations": key_use.get("authentication_operations"),
                    "unique_encryption_keys_used": key_use.get("unique_encryption_keys_used"),
                    "unique_authentication_keys_used": key_use.get("unique_authentication_keys_used"),
                    "otp_payload_bits_protected": key_use.get("otp_payload_bits_protected"),
                })

    link_fields = (
        "version", "topology", "repetition", "deployment", "row_scope", "link_id",
        "measurement_window_seconds", "generated_keys", "generated_bits",
        "average_generated_key_size_bits", "configured_key_rate_bps",
        "observed_generation_rate_bps", "relay_events", "relayed_bits",
        "served_keys", "served_key_bits",
    )
    with (output_dir / "qkd-link-statistics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=link_fields)
        writer.writeheader()
        for record in records:
            for deployment_key, deployment_name in (
                ("monolithic", "monolithic"), ("distributed", "distributed")
            ):
                validation = record[deployment_key].get("metrics", {}).get(
                    "paper_validation", {}
                )
                relay = validation.get("relay", {})
                consumption = validation.get("application_consumption", {})
                service = {
                    "supply_events": consumption.get("keys_consumed"),
                    "supplied_bits": consumption.get("keys_consumed_bits"),
                }
                if deployment_name == "distributed":
                    relay_rows = validation.get("relay_by_kms", [])
                    relay = {
                        "relay_events": sum(row.get("relay_events", 0) for row in relay_rows),
                        "relayed_bits": sum(row.get("relayed_bits", 0) for row in relay_rows),
                    }
                for link in validation.get("qkd_links", []):
                    writer.writerow({
                        "version": record["version"],
                        "topology": record["topology"],
                        "repetition": record["repetition"],
                        "deployment": deployment_name,
                        "row_scope": "physical_qkd_link",
                        "measurement_window_seconds": validation.get(
                            "measurement_window_seconds"
                        ),
                        "configured_key_rate_bps": record["controls"]["qkd_key_rate_bps"],
                        **link,
                    })
                # The archived examples expose relay and service accounting as
                # aggregates, not as UUID-addressable physical-link events.
                # Keep it in the same table without falsely assigning the
                # aggregate to every generated link.
                if any(value is not None for value in (
                    relay.get("relay_events"), relay.get("relayed_bits"),
                    service.get("supply_events"), service.get("supplied_bits"),
                )):
                    writer.writerow({
                        "version": record["version"], "topology": record["topology"],
                        "repetition": record["repetition"],
                        "deployment": deployment_name,
                        "row_scope": "aggregate_accounting",
                        "link_id": "ALL_LINKS_ACCOUNTING",
                        "measurement_window_seconds": validation.get(
                            "measurement_window_seconds"
                        ),
                        "configured_key_rate_bps": record["controls"]["qkd_key_rate_bps"],
                        "relay_events": relay.get("relay_events"),
                        "relayed_bits": relay.get("relayed_bits"),
                        "served_keys": service.get("supply_events"),
                        "served_key_bits": service.get("supplied_bits"),
                    })

    accounting_fields = (
        "version", "topology", "repetition", "deployment", "kms",
        "material_type", "supply_events", "supplied_bits", "relay_events",
        "relayed_bits", "waste_events", "wasted_bits",
        "relay_attempts", "relay_attempt_bits", "relay_successes", "relay_success_bits",
    )
    with (output_dir / "key-accounting.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=accounting_fields)
        writer.writeheader()
        for record in records:
            for deployment_key, deployment_name in (
                ("monolithic", "monolithic"), ("distributed", "distributed")
            ):
                validation = record[deployment_key].get("metrics", {}).get(
                    "paper_validation", {}
                )
                service_rows = validation.get("service_by_kms_and_material", [])
                relay_rows = validation.get("relay_by_kms", [])
                waste_rows = validation.get("relay_waste_by_kms", [])
                if deployment_name == "monolithic" and not (
                    service_rows or relay_rows or waste_rows
                ):
                    writer.writerow({
                        "version": record["version"], "topology": record["topology"],
                        "repetition": record["repetition"], "deployment": deployment_name,
                        "kms": "aggregate", "material_type": "qkd",
                        **validation.get("service", {}),
                        **validation.get("relay", {}),
                        **validation.get("relay_waste", {}),
                    })
                    continue
                relay_by_kms = {row["kms"]: row for row in relay_rows}
                waste_by_kms = {row["kms"]: row for row in waste_rows}
                kms_names = set(relay_by_kms) | set(waste_by_kms) | {
                    row["kms"] for row in service_rows
                }
                for kms in sorted(kms_names):
                    matching = [row for row in service_rows if row["kms"] == kms] or [
                        {"kms": kms, "material_type": "none", "supply_events": 0,
                         "supplied_bits": 0}
                    ]
                    for service in matching:
                        writer.writerow({
                            "version": record["version"], "topology": record["topology"],
                            "repetition": record["repetition"], "deployment": deployment_name,
                            **service, **relay_by_kms.get(kms, {}),
                            **waste_by_kms.get(kms, {}),
                        })

    buffer_fields = (
        "version", "topology", "repetition", "deployment", "kms",
        "time_seconds", "context", "bits",
    )
    with (output_dir / "buffer-timeseries.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=buffer_fields)
        writer.writeheader()
        for record in records:
            for deployment_key, deployment_name in (
                ("monolithic", "monolithic"), ("distributed", "distributed")
            ):
                validation = record[deployment_key].get("metrics", {}).get(
                    "paper_validation", {}
                )
                for sample in validation.get("buffer_time_series", []):
                    writer.writerow({
                        "version": record["version"],
                        "topology": record["topology"],
                        "repetition": record["repetition"],
                        "deployment": deployment_name,
                        "kms": sample.get("kms", "aggregate"),
                        "time_seconds": sample.get("time_seconds"),
                        "context": sample.get("context"),
                        "bits": sample.get("bits"),
                    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", choices=("old", "new", "both"), default="both")
    parser.add_argument(
        "--topology", choices=("p2p", "relay", "secoqc", "both", "all"),
        default="both",
    )
    parser.add_argument("--duration", type=positive, default=30)
    parser.add_argument("--repetitions", type=positive, default=3)
    parser.add_argument(
        "--keys-per-request", type=positive, default=DEFAULT_KEYS_PER_REQUEST,
        help="ETSI 014 prefetch batch size (default: 3, matching upstream examples)",
    )
    parser.add_argument(
        "--workload-profile", choices=("transport", "qkd"), default="transport",
        help="transport isolates process/network overhead; qkd also consumes OTP/VMAC keys",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    encryption_type = 0 if args.workload_profile == "transport" else 1
    authentication_type = 0 if args.workload_profile == "transport" else 1
    versions = ("old", "new") if args.version == "both" else (args.version,)
    if args.topology == "both":
        topologies = ("p2p", "secoqc")
    elif args.topology == "all":
        topologies = ("p2p", "secoqc", "relay")
    else:
        topologies = (args.topology,)
    cases = [(version, topology, repetition)
             for version in versions for topology in topologies
             for repetition in range(1, args.repetitions + 1)]
    if args.list:
        for version, topology, repetition in cases:
            print(f"{version}/{topology}/run-{repetition}: "
                  f"validity={TOPOLOGIES[topology]['comparison_validity']}")
        return 0

    require_images(versions)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (args.output_dir or ROOT / "results" / f"architecture-{stamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="qkd-architecture-") as temporary:
        temp_directory = Path(temporary)
        try:
            for version, topology, repetition in cases:
                print(f"[COMPARE] {version}/{topology}/run-{repetition}", flush=True)
                try:
                    baseline = run_monolithic(
                        version, topology, args.duration, repetition,
                        args.keys_per_request, encryption_type, authentication_type,
                    )
                except Exception as error:
                    baseline = failed_deployment("monolithic", error)
                try:
                    testbed = run_distributed(
                        version, topology, args.duration, repetition, temp_directory,
                        args.keys_per_request, encryption_type, authentication_type,
                    )
                except Exception as error:
                    testbed = failed_deployment("distributed", error)
                run_dir = output_dir / f"{version}-{topology}-run-{repetition}"
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "monolithic.log").write_text(
                    baseline.pop("log"), encoding="utf-8")
                (run_dir / "distributed.log").write_text(
                    testbed.pop("log"), encoding="utf-8")
                record = {
                    "version": version, "topology": topology,
                    "repetition": repetition,
                    "controls": {
                        "offered_application_rate_bps": APP_RATE_BPS,
                        "application_packet_size_bytes": APP_PACKET_SIZE,
                        "qkd_key_rate_bps": 10000,
                        "postprocessing_key_size_bytes": 256,
                        "keys_per_request": args.keys_per_request,
                        "key_buffer_low_watermark": KEY_BUFFER_LOW_WATERMARK,
                        "measurement_warmup_seconds": MEASUREMENT_WARMUP_SECONDS,
                        "workload_profile": args.workload_profile,
                        "encryption_type": encryption_type,
                        "authentication_type": authentication_type,
                        "delivery_drain_seconds": DELIVERY_DRAIN_SECONDS,
                        "measurement_duration_seconds": args.duration,
                        "application_classical_links": 2,
                        "application_classical_delay_per_link_ms": 2,
                        "application_classical_bandwidth_mbps": 50,
                        "kms_control_hops": TOPOLOGIES[topology]["kms_control_hops"],
                        "kms_control_delay_per_hop_ms": TOPOLOGIES[topology][
                            "kms_control_delay_per_hop_ms"
                        ],
                        "use_crypto": False,
                    },
                    "monolithic": baseline, "distributed": testbed,
                    "comparison": summarize_pair(topology, baseline, testbed),
                }
                (run_dir / "result.json").write_text(
                    json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
                records.append(record)
        finally:
            docker("tag", VERSIONS["new"]["testbed_image"],
                   "qkdnetsim-testbed:latest")
            compose(CORE_COMPOSE, "down", "--remove-orphans")

    summary = {
        "schema_version": 5,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "primary_metric": (
                "application goodput and offered-load realization under "
                "matched sustainable load"
            ),
            "qkd_key_rate_role": "controlled input, never an architecture result",
            "p2p": TOPOLOGIES["p2p"]["note"],
            "relay": TOPOLOGIES["relay"]["note"],
            "secoqc": TOPOLOGIES["secoqc"]["note"],
            "vpn": "validated separately by automation/run-regression.py",
            "paper_style_accounting": (
                "application settings, windowed per-link generation, per-KMS "
                "key-material and per-application delivery/consumption tables "
                "corresponding to Tables 2, 3 and 4 "
                "of Dervisevic et al.; buffer time series are explicitly "
                "unavailable in the archived distributed fixtures"
            ),
        },
        "runs": records,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_paper_style_tables(records, output_dir)
    with (output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ("version", "topology", "repetition", "comparison_validity",
                  "both_passed", "baseline_delivery_ratio",
                  "distributed_delivery_ratio", "delivery_retention",
                  "baseline_offered_load_realization",
                  "distributed_offered_load_realization", "goodput_retention",
                  "baseline_goodput_bps", "distributed_goodput_bps",
                  "baseline_mean_cpu_percent", "distributed_mean_cpu_percent",
                  "baseline_peak_memory_bytes", "distributed_peak_memory_bytes")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({"version": record["version"],
                             "topology": record["topology"],
                             "repetition": record["repetition"],
                             **record["comparison"],
                             "baseline_goodput_bps": record["monolithic"]["metrics"].get("application_goodput_bps"),
                             "distributed_goodput_bps": record["distributed"]["metrics"].get("application_goodput_bps"),
                             "baseline_mean_cpu_percent": record["monolithic"]["resources"].get("mean_cpu_percent_sum"),
                             "distributed_mean_cpu_percent": record["distributed"]["resources"].get("mean_cpu_percent_sum"),
                             "baseline_peak_memory_bytes": record["monolithic"]["resources"].get("peak_memory_bytes_sum"),
                             "distributed_peak_memory_bytes": record["distributed"]["resources"].get("peak_memory_bytes_sum")})
    try:
        from plot_architecture_comparison import render, render_paper_validation
        render(summary, output_dir / "architecture-comparison.svg")
        render_paper_validation(summary, output_dir / "qkd-validation.svg")
    except Exception as error:  # Results remain usable if presentation fails.
        print(f"[COMPARE] warning: could not render SVG charts: {error}")
    failures = sum(1 for record in records if not record["comparison"]["both_passed"])
    print(f"[COMPARE] results={output_dir} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
