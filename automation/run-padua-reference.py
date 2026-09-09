#!/usr/bin/env python3
"""Compare monolithic and distributed executions of the Padua workload."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"
MANIFEST = ROOT / "examples" / "comparison" / "padua-reference.json"
DOCKER_DESKTOP = Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
DOCKER = str(DOCKER_DESKTOP) if os.name == "nt" else (shutil.which("docker") or "docker")
VERSIONS = {
    "old": ("qkdnetsim:base-old", "qkdnetsim-testbed:old", "3.46"),
    "new": ("qkdnetsim:base-new", "qkdnetsim-testbed:new", "3.48"),
    # Development-only target: compare the current working-tree image with
    # the normalized current upstream baseline before freezing a revision.
    "working": ("qkdnetsim:base-new-reference", "qkdnetsim-testbed:latest", "3.48"),
}


def run(command: list[str], timeout: int | None = None) -> Any:
    return __import__("subprocess").run(
        command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").STDOUT,
        timeout=timeout, check=False,
    )


def docker(*args: str, timeout: int | None = None) -> Any:
    return run([DOCKER, *args], timeout=timeout)


def compose(path: Path, *args: str, timeout: int | None = None) -> Any:
    return run([DOCKER, "compose", "-f", str(path), *args], timeout=timeout)


def scaled_manifest(scale: float) -> dict[str, Any]:
    source = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Keep the published 10/15/25-second starts. A pilot shortens only each
    # active duration; scaling absolute instants creates an artificial burst
    # of three SAE session establishments.
    latest_stop = 0
    for app in source["applications"]:
        start = int(app["startTimeSeconds"])
        duration = int(app["stopTimeSeconds"]) - start
        app["stopTimeSeconds"] = start + max(1, round(duration * scale))
        latest_stop = max(latest_stop, app["stopTimeSeconds"])
    if scale < 1:
        source["simulationTimeSeconds"] = latest_stop + 3
        source["qkdGenerationWindowSeconds"] = source["simulationTimeSeconds"]
    for link in source["qkdLinks"]:
        link["stopTimeSeconds"] = source["qkdGenerationWindowSeconds"]
    return source


def qkdnetsim_input(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "qkd_links": [
            {
                "startTime": 0,
                "stopTime": profile["qkdGenerationWindowSeconds"],
                "keyRate": link["keyRateBps"],
                "keySize": link["keySizeBits"] // 8,
                "ppPacketSize": 100,
                "ppRate": 1000,
                "srcDstDistance": 0,
                "srcNodeId": link["source"],
                "dstNodeId": link["destination"],
            }
            for link in profile["qkdLinks"]
        ],
        "etsi_004": [],
        "etsi_014": [
            {
                "startTime": app["startTimeSeconds"],
                "stopTime": app["stopTimeSeconds"],
                "encryptionType": 1 if app["encryption"] == "OTP" else 2,
                # QKDNetSim implements VMAC/MD5/SHA1, not the paper's SHA2.
                "authenticationType": 0,
                "numberOfKeyToFetchFromKMSOptions": app["keysPerRequest"],
                "appHoldTimeValue": 1,
                "appRate": app["rateBps"],
                "appPacketSize": app["packetSizeBytes"],
                "aesLifetime": app.get("aesLifetimeBytes", 300000),
                "srcDstDistance": 0,
                "srcNodeId": app["source"],
                "dstNodeId": app["destination"],
            }
            for app in profile["applications"]
        ],
    }


def run_monolithic(version: str, repetition: int, profile: dict[str, Any],
                   directory: Path) -> dict[str, Any]:
    image, _, ns_version = VERSIONS[version]
    case_dir = directory / f"{version}-monolithic-{repetition}"
    case_dir.mkdir(parents=True)
    input_file = case_dir / "input.json"
    input_file.write_text(json.dumps(qkdnetsim_input(profile), indent=2), encoding="utf-8")
    mount = f"{case_dir.resolve()}:/results"
    binary = (
        f"/opt/ns-3-dev/build/contrib/qkdnetsim/examples/"
        f"ns{ns_version}-examples_qkdnetsim_etsi_combined_input-default"
    )
    started = time.monotonic()
    result = docker(
        "run", "--rm", "-v", mount,
        image, binary,
        "--inputFile=/results/input.json", "--statsFile=/results/stats.json",
        "--outputFile=/results/events.json", "--outputType=json",
        f"--simTime={profile['simulationTimeSeconds']}", "--useCrypto=1",
        f"--seed={repetition}",
        # The current monolithic model performs the complete 10 Mbit/s crypto
        # workload in one process and can need several minutes in an
        # unoptimised ns-3 build. This is a guard against hangs, not a pacing
        # mechanism; simulated time and the statistics window remain 130 s.
        timeout=max(900, int(profile["simulationTimeSeconds"]) * 8),
    )
    (case_dir / "run.log").write_text(result.stdout, encoding="utf-8")
    stats_file = case_dir / "stats.json"
    stats = json.loads(stats_file.read_text(encoding="utf-8")) if stats_file.exists() else {}
    return {
        "deployment": "monolithic", "returncode": result.returncode,
        "passed": result.returncode == 0 and bool(stats),
        "wall_seconds": round(time.monotonic() - started, 3),
        "statistics": stats,
        "actual_key_use_events": result.stdout.count("[COMPARE_APP_KEY]"),
    }


def run_distributed(version: str, repetition: int, scale: float,
                    profile: dict[str, Any], directory: Path) -> dict[str, Any]:
    _, image, ns_version = VERSIONS[version]
    compose_file = DOCKER_DIR / "docker-compose.padua-reference.yml"
    core_compose = DOCKER_DIR / "docker-compose.core.yml"
    case_dir = directory / f"{version}-distributed-{repetition}"
    case_dir.mkdir(parents=True, exist_ok=True)
    container_result = "/workspace/" + str(
        (case_dir / "result.json").resolve().relative_to(ROOT.resolve())
    ).replace("\\", "/")
    docker("tag", image, "qkdnetsim-testbed:latest")
    compose(core_compose, "up", "-d", "core")
    compose(compose_file, "down", "--remove-orphans")
    env = dict(os.environ)
    env["QKD_NS3_VERSION"] = ns_version
    up = __import__("subprocess").run(
        [DOCKER, "compose", "-f", str(compose_file), "up", "-d", "--force-recreate"],
        cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
        stdout=__import__("subprocess").PIPE, stderr=__import__("subprocess").STDOUT,
        check=False,
    )
    if up.returncode:
        raise RuntimeError(up.stdout)
    command = [
        DOCKER, "compose", "-f", str(core_compose), "exec", "-T",
        "-e", f"CORE_QKD_IMAGE={image}",
        "-e", f"QKD_NS3_VERSION={ns_version}",
        "core", "/opt/core/venv/bin/python",
        "/workspace/old-examples/core/padua-reference.py",
        "--time-scale", str(scale), "--startup-timeout", "300",
        "--output", container_result,
    ]
    if os.environ.get("QKD_APP_NS_LOG"):
        command[command.index("core"):command.index("core")] = [
            "-e", f"QKD_APP_NS_LOG={os.environ['QKD_APP_NS_LOG']}"
        ]
    started = time.monotonic()
    try:
        result = run(command, timeout=int(profile["simulationTimeSeconds"]) + 420)
        (case_dir / "run.log").write_text(result.stdout, encoding="utf-8")
        result_file = case_dir / "result.json"
        if result_file.exists():
            parsed = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            raise RuntimeError(
                "distributed runner did not produce result.json:\n"
                + result.stdout[-4000:])
        return {
            "deployment": "distributed", "returncode": result.returncode,
            "passed": result.returncode == 0 and parsed.get("passed", False),
            "wall_seconds": round(time.monotonic() - started, 3),
            "statistics": parsed,
            "actual_key_use_events": sum(
                app.get("encryption_key_use_operations", 0)
                for app in parsed.get("applications", [])
            ),
        }
    finally:
        compose(compose_file, "down", "--remove-orphans")


def flatten_results(records: list[dict[str, Any]], output: Path) -> None:
    app_fields = (
        "version", "repetition", "deployment", "application", "sent_bytes",
        "received_bytes", "sent_packets", "received_packets", "missed_send_calls",
        "delivery_ratio", "application_goodput_bps", "keys_consumed",
        "offered_rate_bps", "offered_rate_achievement",
        "keys_consumed_bits", "actual_key_use_events",
        "encryption_key_use_operations", "unique_encryption_keys_used",
        "otp_payload_bits_protected",
    )
    with (output / "application-statistics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=app_fields)
        writer.writeheader()
        for record in records:
            run_result = record["result"]
            if record["deployment"] == "distributed":
                for app in run_result.get("applications", []):
                    writer.writerow({
                        "version": record["version"], "repetition": record["repetition"],
                        "deployment": "distributed", **app,
                        "actual_key_use_events": app.get("encryption_key_use_operations"),
                    })
            else:
                for app_id, values in run_result.get("etsi_014", {}).items():
                    app = values.get("QKDApps Statistics", {})
                    keys = values.get("Key Consumption Statistics", {})
                    writer.writerow({
                        "version": record["version"], "repetition": record["repetition"],
                        "deployment": "monolithic", "application": app_id,
                        "sent_bytes": app.get("Bytes Sent"),
                        "received_bytes": app.get("Bytes Received"),
                        "sent_packets": app.get("Packets Sent"),
                        "received_packets": app.get("Packets Received"),
                        "missed_send_calls": app.get("Missed send packet calls"),
                        "delivery_ratio": (
                            app.get("Packets Received") / app.get("Packets Sent")
                            if app.get("Packets Sent") else None
                        ),
                        "offered_rate_bps": None,
                        "offered_rate_achievement": None,
                        "keys_consumed": keys.get("Key-pairs consumed"),
                        "keys_consumed_bits": keys.get("Key-pairs consumed (bits)"),
                        "actual_key_use_events": record["actual_key_use_events"],
                        "encryption_key_use_operations": None,
                        "unique_encryption_keys_used": None,
                        "otp_payload_bits_protected": None,
                    })
    link_fields = (
        "version", "repetition", "deployment", "link_id", "generated_keys",
        "generated_bits", "configured_key_rate_bps", "relayed_keys", "relayed_bits",
        "served_keys", "served_bits",
    )
    with (output / "qkd-link-statistics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=link_fields)
        writer.writeheader()
        for record in records:
            if record["deployment"] == "monolithic":
                for link_id, link in record["result"].get("qkd_links", {}).items():
                    writer.writerow({
                        "version": record["version"], "repetition": record["repetition"],
                        "deployment": "monolithic", "link_id": link_id,
                        "generated_keys": link.get("Key-pairs generated"),
                        "generated_bits": link.get("Key-pairs generated (bits)"),
                        "configured_key_rate_bps": link.get("Key rate (bit/sec)"),
                        "relayed_keys": link.get("Key-pairs relayed"),
                        "relayed_bits": link.get("Key-pairs relayed (bits)"),
                        "served_keys": link.get("Key-pairs consumed"),
                        "served_bits": link.get("Key-pairs consumed (bits)"),
                    })
                continue

            # Distributed PP module UUIDs encode edge number 1..6. A physical
            # key is observed at both ends, so deduplicate by edge and key ID.
            kms = record["result"].get("kms", {})
            generated: dict[int, dict[str, int]] = {index: {} for index in range(1, 7)}
            relay: dict[tuple[int, int], list[int]] = {}
            served: dict[int, list[int]] = {index: [] for index in range(1, 7)}
            edge_nodes = ((1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (3, 6))
            edge_index = {tuple(sorted(nodes)): index + 1 for index, nodes in enumerate(edge_nodes)}
            next_hop = (
                (0, 2, 2, 2, 2, 2), (1, 0, 3, 3, 3, 3),
                (2, 2, 0, 4, 4, 6), (3, 3, 3, 0, 5, 3),
                (4, 4, 4, 4, 0, 4), (3, 3, 3, 3, 3, 0),
            )
            for container, values in kms.items():
                local = "abcdef".index(container[-1]) + 1
                for event in values.get("generated", []):
                    match = __import__("re").search(r"-000([1-6])-", event["link_id"])
                    if match:
                        generated[int(match.group(1))][event["key_id"]] = event["bits"]
                for event in values.get("relay_succeeded", []):
                    # RelayConsumption already reports the physical hop that
                    # consumed the wrapping key: src is the local KMS and dst
                    # its immediate neighbour.  Treating dst as the final SAE
                    # and routing it through next_hop counted events against
                    # the wrong link, particularly around nodes 3, 4 and 6.
                    source = event.get("source") or event.get("node") or local
                    destination = event["destination"]
                    edge = edge_index.get(tuple(sorted((source, destination))))
                    if edge:
                        relay.setdefault((edge, source), []).append(event["bits"])
                app_events = values.get("application_supplied", [])
                if app_events and local in (1, 5, 6):
                    remote = 5 if local == 1 else 1
                    # Site 1 serves both destinations but both routes begin 1-2.
                    if local == 6:
                        remote = 1
                    hop = next_hop[local - 1][remote - 1]
                    edge = edge_index[tuple(sorted((local, hop)))]
                    served[edge].extend(event["bits"] for event in app_events)
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
            for index, link in enumerate(manifest["qkdLinks"], 1):
                sizes = list(generated[index].values())
                relay_values = [bits for (edge, _), values in relay.items()
                                if edge == index for bits in values]
                writer.writerow({
                    "version": record["version"], "repetition": record["repetition"],
                    "deployment": "distributed", "link_id": f"{link['source']}-{link['destination']}",
                    "generated_keys": len(sizes), "generated_bits": sum(sizes),
                    "configured_key_rate_bps": link["keyRateBps"],
                    "relayed_keys": len(relay_values), "relayed_bits": sum(relay_values),
                    "served_keys": len(served[index]), "served_bits": sum(served[index]),
                })

    with (output / "key-accounting.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ("version", "repetition", "deployment", "kms", "supplied_keys",
                  "supplied_bits", "relay_attempts", "relay_attempt_bits",
                  "relay_successes", "relay_success_bits", "waste_events",
                  "wasted_bits")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            if record["deployment"] != "distributed":
                continue
            for kms_name, values in record["result"].get("kms", {}).items():
                supplied = values.get("application_supplied", [])
                relayed = values.get("relayed", [])
                succeeded = values.get("relay_succeeded", [])
                wasted = values.get("wasted", [])
                writer.writerow({
                    "version": record["version"], "repetition": record["repetition"],
                    "deployment": "distributed", "kms": kms_name,
                    "supplied_keys": len(supplied),
                    "supplied_bits": sum(row["bits"] for row in supplied),
                    "relay_attempts": len(relayed),
                    "relay_attempt_bits": sum(row["bits"] for row in relayed),
                    "relay_successes": len(succeeded),
                    "relay_success_bits": sum(row["bits"] for row in succeeded),
                    "waste_events": len(wasted),
                    "wasted_bits": sum(row["bits"] for row in wasted),
                })

    with (output / "buffer-timeseries.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ("version", "repetition", "deployment", "kms", "time_seconds",
                  "context", "bits")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            if record["deployment"] != "distributed":
                continue
            for kms_name, values in record["result"].get("kms", {}).items():
                for sample in values.get("buffer_samples", []):
                    writer.writerow({
                        "version": record["version"], "repetition": record["repetition"],
                        "deployment": "distributed", "kms": kms_name, **sample,
                    })


def render_comparison(records: list[dict[str, Any]], output: Path) -> None:
    """Render a dependency-free overview; CSV/JSON remain authoritative."""
    groups: list[tuple[str, float, float]] = []
    ordered_versions = tuple(dict.fromkeys(record["version"] for record in records))
    for version in ordered_versions:
        for deployment in ("monolithic", "distributed"):
            values = []
            deliveries = []
            for record in records:
                if record["version"] != version or record["deployment"] != deployment:
                    continue
                if deployment == "distributed":
                    apps = record["result"].get("applications", [])
                    values.append(sum(float(row.get("application_goodput_bps", 0)) for row in apps))
                    deliveries.extend(float(row.get("delivery_ratio") or 0) for row in apps)
                else:
                    apps = record["result"].get("etsi_014", {}).values()
                    total = 0.0
                    for values_map in apps:
                        app = values_map.get("QKDApps Statistics", {})
                        duration = max(float(app.get("Stop Time (sec)", 0)) -
                                       float(app.get("Start Time (sec)", 0)), 1)
                        total += float(app.get("Bytes Received", 0)) * 8 / duration
                        sent = float(app.get("Packets Sent", 0))
                        deliveries.append(float(app.get("Packets Received", 0)) / sent if sent else 0)
                    values.append(total)
            if values:
                groups.append((f"{version} {deployment}", sum(values) / len(values),
                               sum(deliveries) / len(deliveries) if deliveries else 0))
    maximum = max(1.0, max((value for _, value, _ in groups), default=0.0))
    width, height, left, top, plot_height = 900, 520, 105, 70, 330
    bar_width = 115
    gap = 70
    colors = ("#315C78", "#D4773B", "#56876D", "#8A5A83")
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F5F0"/>',
        '<text x="40" y="38" font-family="sans-serif" font-size="24" font-weight="700" fill="#1D2730">Padua reference: useful application goodput</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#52606B"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="850" y2="{top + plot_height}" stroke="#52606B"/>',
    ]
    for index, (label, value, delivery) in enumerate(groups):
        x = left + 55 + index * (bar_width + gap)
        bar_height = value / maximum * (plot_height - 35)
        y = top + plot_height - bar_height
        body.extend((
            f'<rect x="{x}" y="{y:.1f}" width="{bar_width}" height="{bar_height:.1f}" rx="5" fill="{colors[index % len(colors)]}"/>',
            f'<text x="{x + bar_width / 2}" y="{y - 9:.1f}" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#1D2730">{value / 1000:.1f} kb/s</text>',
            f'<text x="{x + bar_width / 2}" y="{top + plot_height + 25}" text-anchor="middle" font-family="sans-serif" font-size="13" fill="#1D2730">{label}</text>',
            f'<text x="{x + bar_width / 2}" y="{top + plot_height + 45}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#52606B">delivery {delivery * 100:.1f}%</text>',
        ))
    body.append('<text x="24" y="245" transform="rotate(-90 24 245)" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#52606B">aggregate goodput (bit/s)</text>')
    body.append('</svg>')
    (output / "padua-reference-comparison.svg").write_text("\n".join(body), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", choices=("old", "new", "working", "both"), default="both"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument(
        "--deployment", choices=("monolithic", "distributed", "both"), default="both"
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    versions = ("old", "new") if args.version == "both" else (args.version,)
    deployments = (
        ("monolithic", "distributed")
        if args.deployment == "both" else (args.deployment,)
    )
    profile = scaled_manifest(args.time_scale)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output_dir or ROOT / "results" / f"padua-reference-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        for version in versions:
            for repetition in range(1, args.repetitions + 1):
                for deployment in deployments:
                    print(
                        f"[PADUA] {version}/{deployment}/run-{repetition} "
                        f"({len(records) + 1}/"
                        f"{len(versions) * args.repetitions * len(deployments)})",
                        flush=True,
                    )
                    if deployment == "monolithic":
                        run_result = run_monolithic(version, repetition, profile, output)
                    else:
                        run_result = run_distributed(
                            version, repetition, args.time_scale, profile, output
                        )
                    records.append({
                        "version": version, "repetition": repetition,
                        "deployment": deployment,
                        "passed": run_result["passed"],
                        "wall_seconds": run_result["wall_seconds"],
                        "actual_key_use_events": run_result["actual_key_use_events"],
                        "result": run_result["statistics"],
                    })
    finally:
        compose(DOCKER_DIR / "docker-compose.padua-reference.yml", "down", "--remove-orphans")
    summary = {
        "schema_version": 1, "profile": profile,
        "authentication_limitation": (
            "The paper reports SHA2, whereas QKDNetSim supports VMAC, MD5 and SHA1. "
            "Authentication is disabled so real OTP/AES execution remains comparable."
        ),
        "runs": records,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    flatten_results(records, output)
    render_comparison(records, output)
    failures = sum(not row["passed"] for row in records)
    print(f"[PADUA] results={output} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
