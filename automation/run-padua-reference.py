#!/usr/bin/env python3
"""Compare monolithic and distributed executions of the Padua workload."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shutil
import statistics
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
    case_dir.mkdir(parents=True, exist_ok=True)
    for stale_output in ("stats.json", "events.json"):
        (case_dir / stale_output).unlink(missing_ok=True)
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
    compose(core_compose, "up", "-d", "core")
    compose(compose_file, "down", "--remove-orphans")
    env = dict(os.environ)
    env["QKD_NS3_VERSION"] = ns_version
    env["QKD_TESTBED_IMAGE"] = image
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


def load_completed_run(version: str, repetition: int, deployment: str,
                       directory: Path) -> dict[str, Any] | None:
    """Load a successful run when explicitly resuming an output directory."""
    case_dir = directory / f"{version}-{deployment}-{repetition}"
    run_log = case_dir / "run.log"
    log_text = run_log.read_text(encoding="utf-8") if run_log.exists() else ""
    if deployment == "monolithic":
        stats_file = case_dir / "stats.json"
        if not stats_file.exists():
            return None
        statistics = json.loads(stats_file.read_text(encoding="utf-8"))
        if not statistics:
            return None
        actual_key_use_events = log_text.count("[COMPARE_APP_KEY]")
    else:
        result_file = case_dir / "result.json"
        if not result_file.exists():
            return None
        statistics = json.loads(result_file.read_text(encoding="utf-8"))
        if not statistics.get("passed", False):
            return None
        actual_key_use_events = sum(
            app.get("encryption_key_use_operations", 0)
            for app in statistics.get("applications", [])
        )
    return {
        "deployment": deployment,
        "returncode": 0,
        "passed": True,
        "wall_seconds": None,
        "statistics": statistics,
        "actual_key_use_events": actual_key_use_events,
    }


def flatten_results(records: list[dict[str, Any]], output: Path) -> None:
    app_fields = (
        "version", "repetition", "deployment", "application", "sent_bytes",
        "received_bytes", "sent_packets", "received_packets", "missed_send_calls",
        "missed_send_socket", "missed_send_key_wait",
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
                monolithic_apps = run_result.get(
                    "ETSI_014", run_result.get("etsi_014", {})
                )
                for app_id, values in monolithic_apps.items():
                    app = values.get("QKDApps Statistics", {})
                    keys = values.get("Key Consumption Statistics", {})
                    duration = max(
                        float(app.get("Stop Time (sec)", 0)) -
                        float(app.get("Start Time (sec)", 0)),
                        1.0,
                    )
                    received_bytes = app.get("Bytes Received")
                    goodput = (
                        float(received_bytes) * 8 / duration
                        if received_bytes is not None else None
                    )
                    offered_rate = app.get("Traffic Rate (bit/sec)")
                    key_uses = keys.get(
                        "Key-pairs Consumed by App for Encryption/Decryption",
                        keys.get("Key-pairs consumed"),
                    )
                    key_use_bits = keys.get(
                        "Key-pairs Consumed by App for Encryption/Decryption (bits)",
                        keys.get("Key-pairs consumed (bits)"),
                    )
                    writer.writerow({
                        "version": record["version"], "repetition": record["repetition"],
                        "deployment": "monolithic",
                        "application": (
                            app_id if "-to-" in app_id
                            else app_id.replace("-", "-to-", 1)
                        ),
                        "sent_bytes": app.get("Bytes Sent"),
                        "received_bytes": received_bytes,
                        "sent_packets": app.get("Packets Sent"),
                        "received_packets": app.get("Packets Received"),
                        "missed_send_calls": app.get("Missed send packet calls"),
                        "missed_send_socket": None,
                        "missed_send_key_wait": None,
                        "delivery_ratio": (
                            app.get("Packets Received") / app.get("Packets Sent")
                            if app.get("Packets Sent") else None
                        ),
                        "application_goodput_bps": goodput,
                        "offered_rate_bps": offered_rate,
                        "offered_rate_achievement": (
                            goodput / float(offered_rate)
                            if goodput is not None and offered_rate else None
                        ),
                        "keys_consumed": key_uses,
                        "keys_consumed_bits": key_use_bits,
                        "actual_key_use_events": key_uses,
                        "encryption_key_use_operations": key_uses,
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
                        "generated_keys": link.get(
                            "Key-pairs generated QKD", link.get("Key-pairs generated")
                        ),
                        "generated_bits": link.get(
                            "Key-pairs generated QKD (bits)",
                            link.get("Key-pairs generated (bits)"),
                        ),
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
        fields = ("version", "repetition", "deployment", "kms", "prepared_keys",
                  "prepared_bits", "supplied_keys", "supplied_bits",
                  "relay_attempts", "relay_attempt_bits",
                  "relay_successes", "relay_success_bits", "waste_events",
                  "wasted_bits")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            if record["deployment"] != "distributed":
                continue
            for kms_name, values in record["result"].get("kms", {}).items():
                prepared = values.get("prepared", [])
                supplied = values.get("application_supplied", [])
                relayed = values.get("relayed", [])
                succeeded = values.get("relay_succeeded", [])
                wasted = values.get("wasted", [])
                writer.writerow({
                    "version": record["version"], "repetition": record["repetition"],
                    "deployment": "distributed", "kms": kms_name,
                    "prepared_keys": len(prepared) if "prepared" in values and prepared else None,
                    "prepared_bits": sum(row["bits"] for row in prepared) if prepared else None,
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


def render_comparison(output: Path) -> None:
    """Compare matched monolithic/distributed loads without aggregating flows."""
    source = output / "application-statistics.csv"
    samples: dict[tuple[str, str, str], list[dict[str, float]]] = {}
    with source.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            achievement = row.get("offered_rate_achievement")
            if not achievement:
                continue
            key = (row["version"], row["application"], row["deployment"])
            samples.setdefault(key, []).append({
                "achievement": float(achievement),
                "delivery": float(row.get("delivery_ratio") or 0),
                "goodput": float(row.get("application_goodput_bps") or 0),
                "offered": float(row.get("offered_rate_bps") or 0),
            })

    versions = tuple(dict.fromkeys(key[0] for key in samples))
    flow_order = ("1-to-5", "5-to-1", "1-to-6")
    deployments = ("monolithic", "distributed")
    rows: list[dict[str, Any]] = []
    for version in versions:
        for flow in flow_order:
            for deployment in deployments:
                values = samples.get((version, flow, deployment), [])
                if not values:
                    continue
                achievements = [value["achievement"] for value in values]
                rows.append({
                    "version": version,
                    "flow": flow,
                    "deployment": deployment,
                    "samples": len(values),
                    "mean_offered_rate_achievement": statistics.fmean(achievements),
                    "stddev_offered_rate_achievement": (
                        statistics.stdev(achievements) if len(achievements) > 1 else 0.0
                    ),
                    "mean_delivery_ratio": statistics.fmean(
                        value["delivery"] for value in values
                    ),
                    "mean_goodput_bps": statistics.fmean(
                        value["goodput"] for value in values
                    ),
                    "offered_rate_bps": statistics.fmean(
                        value["offered"] for value in values
                    ),
                })

    fields = (
        "version", "flow", "deployment", "samples",
        "mean_offered_rate_achievement", "stddev_offered_rate_achievement",
        "mean_delivery_ratio", "mean_goodput_bps", "offered_rate_bps",
    )
    with (output / "padua-reference-comparison.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    groups = [(version, flow) for version in versions for flow in flow_order]
    width = max(980, 150 + len(groups) * 150)
    height, left, right, top, bottom = 590, 90, 35, 85, 125
    plot_width = width - left - right
    plot_height = height - top - bottom
    group_width = plot_width / max(len(groups), 1)
    bar_width = min(42.0, group_width * 0.30)
    colors = {"monolithic": "#315C78", "distributed": "#D4773B"}
    row_index = {
        (row["version"], row["flow"], row["deployment"]): row for row in rows
    }
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F5F0"/>',
        '<text x="44" y="39" font-family="sans-serif" font-size="24" font-weight="700" fill="#17324D">Padua: cumplimiento de carga por flujo</text>',
        '<text x="44" y="63" font-family="sans-serif" font-size="13" fill="#52606B">Media de las repeticiones; barras de error: ±1 desviación típica</text>',
    ]
    for tick in range(0, 121, 20):
        y = top + plot_height - tick / 120 * plot_height
        body.extend((
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#D6D2C9"/>',
            f'<text x="{left-10}" y="{y+5:.1f}" text-anchor="end" font-family="sans-serif" font-size="12" fill="#52606B">{tick}%</text>',
        ))
    target_y = top + plot_height - 100 / 120 * plot_height
    body.append(
        f'<line x1="{left}" y1="{target_y:.1f}" x2="{width-right}" y2="{target_y:.1f}" stroke="#56876D" stroke-width="2" stroke-dasharray="7 6"/>'
    )
    for group_index, (version, flow) in enumerate(groups):
        center = left + group_width * (group_index + 0.5)
        body.extend((
            f'<text x="{center:.1f}" y="{top+plot_height+30}" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#1D2730">{flow.replace("-to-", "→")}</text>',
            f'<text x="{center:.1f}" y="{top+plot_height+49}" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#52606B">{version.upper()}</text>',
        ))
        for deployment_index, deployment in enumerate(deployments):
            row = row_index.get((version, flow, deployment))
            if row is None:
                continue
            mean = float(row["mean_offered_rate_achievement"]) * 100
            deviation = float(row["stddev_offered_rate_achievement"]) * 100
            x = center + (deployment_index - 0.5) * (bar_width + 7) - bar_width / 2
            bar_height = min(mean, 120) / 120 * plot_height
            y = top + plot_height - bar_height
            error_top = top + plot_height - min(mean + deviation, 120) / 120 * plot_height
            error_bottom = top + plot_height - max(mean - deviation, 0) / 120 * plot_height
            body.extend((
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="3" fill="{colors[deployment]}"/>',
                f'<line x1="{x+bar_width/2:.1f}" y1="{error_top:.1f}" x2="{x+bar_width/2:.1f}" y2="{error_bottom:.1f}" stroke="#1D2730" stroke-width="1.5"/>',
                f'<line x1="{x+bar_width/2-5:.1f}" y1="{error_top:.1f}" x2="{x+bar_width/2+5:.1f}" y2="{error_top:.1f}" stroke="#1D2730" stroke-width="1.5"/>',
                f'<line x1="{x+bar_width/2-5:.1f}" y1="{error_bottom:.1f}" x2="{x+bar_width/2+5:.1f}" y2="{error_bottom:.1f}" stroke="#1D2730" stroke-width="1.5"/>',
                f'<text x="{x+bar_width/2:.1f}" y="{max(y-8, top+12):.1f}" text-anchor="middle" font-family="sans-serif" font-size="11" font-weight="700" fill="#1D2730">{mean:.1f}%</text>',
            ))
    legend_y = height - 56
    for index, (deployment, label) in enumerate((
        ("monolithic", "QKDNetSim monolítico"),
        ("distributed", "QKDNetSim distribuido"),
    )):
        x = left + index * 240
        body.extend((
            f'<rect x="{x}" y="{legend_y-13}" width="16" height="16" rx="2" fill="{colors[deployment]}"/>',
            f'<text x="{x+25}" y="{legend_y}" font-family="sans-serif" font-size="13" fill="#1D2730">{label}</text>',
        ))
    body.extend((
        f'<text x="27" y="{top+plot_height/2:.1f}" transform="rotate(-90 27 {top+plot_height/2:.1f})" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#52606B">goodput / carga ofrecida</text>',
        '</svg>',
    ))
    (output / "padua-reference-comparison.svg").write_text(
        "\n".join(body), encoding="utf-8"
    )


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
    parser.add_argument(
        "--resume", action="store_true",
        help="reuse successful runs already present in --output-dir",
    )
    parser.add_argument(
        "--rerun-deployment",
        choices=("none", "monolithic", "distributed", "both"),
        default="none",
        help="with --resume, rerun this deployment instead of reusing it",
    )
    args = parser.parse_args()
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
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
    distributed_cleanup_required = False
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
                    force_rerun = args.rerun_deployment in (deployment, "both")
                    run_result = (
                        load_completed_run(version, repetition, deployment, output)
                        if args.resume and not force_rerun else None
                    )
                    if run_result is not None:
                        print("[PADUA] reusing completed run", flush=True)
                    elif deployment == "monolithic":
                        run_result = run_monolithic(version, repetition, profile, output)
                    else:
                        distributed_cleanup_required = True
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
        if distributed_cleanup_required:
            compose(
                DOCKER_DIR / "docker-compose.padua-reference.yml",
                "down", "--remove-orphans",
            )
    summary_runs = []
    for record in records:
        compact = {key: value for key, value in record.items() if key != "result"}
        if record["deployment"] == "distributed":
            # Per-KMS event and buffer histories remain available in each
            # run's result.json and in the generated CSV tables. Duplicating
            # them here made summary.json hundreds of megabytes larger.
            compact["result"] = {
                key: value for key, value in record["result"].items()
                if key != "kms"
            }
            compact["detailed_result"] = (
                f"{record['version']}-distributed-{record['repetition']}/result.json"
            )
        else:
            compact["result"] = record["result"]
            compact["detailed_result"] = (
                f"{record['version']}-monolithic-{record['repetition']}/stats.json"
            )
        summary_runs.append(compact)
    summary = {
        "schema_version": 1, "profile": profile,
        "authentication_limitation": (
            "The paper reports SHA2, whereas QKDNetSim supports VMAC, MD5 and SHA1. "
            "Authentication is disabled so real OTP/AES execution remains comparable."
        ),
        "runs": summary_runs,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    flatten_results(records, output)
    render_comparison(output)
    failures = sum(not row["passed"] for row in records)
    print(f"[PADUA] results={output} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
