#!/usr/bin/env python3
"""Run a QKD-backed IPsec tunnel over the distributed Padua 1-to-6 path."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"
PADUA_COMPOSE = DOCKER_DIR / "docker-compose.padua-reference.yml"
CORE_COMPOSE = DOCKER_DIR / "docker-compose.core.yml"
DOCKER_DESKTOP = (
    Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
)
DOCKER = str(DOCKER_DESKTOP) if os.name == "nt" else (shutil.which("docker") or "docker")
RESULT_MARKER = "[CORE_RESULT] "
PADUA_CONTAINERS = tuple(f"qkd-padua-site-{letter}" for letter in "abcdef")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=positive_int, default=5)
    parser.add_argument("--duration", type=positive_int, default=120)
    parser.add_argument("--rate-mbps", type=positive_float, default=10.0)
    parser.add_argument("--block-size", type=positive_int, default=800)
    parser.add_argument("--generations", type=positive_int, default=4)
    parser.add_argument("--rekey-interval", type=positive_int, default=30)
    parser.add_argument("--startup-timeout", type=positive_int, default=300)
    parser.add_argument("--command-timeout", type=positive_int, default=900)
    parser.add_argument("--qkd-interface", choices=("004", "014"), default="014")
    parser.add_argument("--keying-mode", choices=("psk", "ppk"), default="ppk")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "reuse successful repetitions already present in --output-dir "
            "and run only missing or failed repetitions"
        ),
    )
    parser.add_argument(
        "--reference-results",
        type=Path,
        help=(
            "completed padua-reference directory; when supplied, produce the "
            "monolithic/distributed/VPN three-stage CSV and SVG"
        ),
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="rebuild the working QKD, VPN and CORE images before running",
    )
    args = parser.parse_args()
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
    rekey_window = (args.generations - 1) * args.rekey_interval
    if rekey_window >= args.duration:
        parser.error(
            "--duration must exceed (generations - 1) * --rekey-interval"
        )
    return args


def load_completed_run(
    output: Path, repetition: int
) -> dict[str, Any] | None:
    result_path = output / f"vpn-distributed-{repetition}" / "result.json"
    if not result_path.is_file():
        return None
    try:
        record = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    runner_result = record.get("runner_result") or {}
    if (
        record.get("repetition") == repetition
        and record.get("passed") is True
        and runner_result.get("status") == "passed"
    ):
        return record
    return None


def run(
    command: list[str],
    *,
    timeout: int | None = None,
    check: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("[PADUA_VPN] command=" + subprocess.list2cmdline(command), flush=True)
    if not stream:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    else:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        lines: list[str] = []
        started = time.monotonic()
        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if line:
                print(line, end="", flush=True)
                lines.append(line)
            elif process.poll() is not None:
                break
            elif timeout is not None and time.monotonic() - started > timeout:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout, "".join(lines))
            else:
                time.sleep(0.1)
        result = subprocess.CompletedProcess(
            command, process.returncode, "".join(lines), None
        )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed with status {result.returncode}: "
            + subprocess.list2cmdline(command)
        )
    return result


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run([DOCKER, *args], **kwargs)


def compose(
    path: Path, *args: str, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return docker("compose", "-f", str(path), *args, **kwargs)


def build_images() -> None:
    docker("build", "-t", "qkdnetsim-testbed:latest", "-f", "docker/Dockerfile", ".")
    docker(
        "build", "-t", "qkdnetsim-vpn-endpoint:latest",
        "-f", "docker/vpn/Dockerfile.vpn", ".",
    )
    compose(CORE_COMPOSE, "build", "core")


def cleanup() -> None:
    compose(PADUA_COMPOSE, "down", "--remove-orphans", check=False)
    stale = docker(
        "ps", "-aq", "--filter", "name=qkd-core-vpn-", check=False
    ).stdout.split()
    if stale:
        docker("rm", "-f", *stale, check=False)


def parse_result(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


def run_once(
    repetition: int,
    args: argparse.Namespace,
    output: Path,
) -> dict[str, Any]:
    cleanup()
    compose(PADUA_COMPOSE, "up", "-d", "--force-recreate")
    run_dir = output / f"vpn-distributed-{repetition}"
    run_dir.mkdir(parents=True, exist_ok=True)
    traffic_tail = args.duration - (args.generations - 1) * args.rekey_interval
    command = [
        DOCKER, "compose", "-f", str(CORE_COMPOSE), "exec", "-T", "core",
        "/opt/core/venv/bin/python", "/workspace/core/vpn-topology.py",
        "--qkd-topology", "padua-1-to-6",
        "--qkd-interface", args.qkd_interface,
        "--keying-mode", args.keying_mode,
        "--routers", "0",
        "--delay-ms", "2",
        "--bandwidth-mbps", "100",
        "--loss-percent", "0",
        "--startup-timeout", str(args.startup_timeout),
        "--traffic-duration", str(traffic_tail),
        "--min-generations", str(args.generations),
        "--rekey-interval", str(args.rekey_interval),
        "--traffic-rate-mbps", str(args.rate_mbps),
        "--traffic-block-size", str(args.block_size),
        "--min-throughput-mbps", "0.01",
    ]
    started = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    timed_out = False
    try:
        try:
            completed = run(
                command, timeout=args.command_timeout, check=False, stream=True
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            completed = subprocess.CompletedProcess(
                command, 124, str(error.output or error), None
            )
        runner_output = completed.stdout or ""
        (run_dir / "runner.log").write_text(runner_output, encoding="utf-8")
        kms_dir = run_dir / "kms-logs"
        kms_dir.mkdir(exist_ok=True)
        for container in PADUA_CONTAINERS:
            log = docker("logs", container, check=False).stdout
            (kms_dir / f"{container}.log").write_text(log, encoding="utf-8")
        runner_result = parse_result(runner_output)
        passed = (
            completed.returncode == 0
            and runner_result is not None
            and runner_result.get("status") == "passed"
        )
        record = {
            "repetition": repetition,
            "passed": passed,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "started_at": started.isoformat(),
            "elapsed_seconds": round(time.monotonic() - monotonic_started, 3),
            "command": command,
            "runner_result": runner_result,
            "artifacts": {
                "runner_log": str((run_dir / "runner.log").relative_to(output)),
                "kms_logs": str(kms_dir.relative_to(output)),
            },
        }
        (run_dir / "result.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(
            f"[PADUA_VPN] run={repetition} "
            f"status={'PASSED' if passed else 'FAILED'}",
            flush=True,
        )
        return record
    finally:
        cleanup()


def write_summary(
    output: Path, args: argparse.Namespace, records: list[dict[str, Any]]
) -> dict[str, Any]:
    passed = sum(bool(record["passed"]) for record in records)
    summary = {
        "schema_version": 1,
        "profile": "padua-vpn-1-to-6",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "configuration": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
            if key != "output_dir"
        },
        "total": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "status": "passed" if passed == len(records) else "failed",
        "runs": records,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    fields = (
        "repetition", "passed", "elapsed_seconds", "throughput_mbps",
        "offered_rate_mbps", "offered_rate_achievement", "iperf_retransmits",
        "iperf_min_interval_mbps", "iperf_zero_throughput_intervals",
        "esp_packets", "generations_verified",
    )
    with (output / "vpn-statistics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            result = record.get("runner_result") or {}
            metrics = result.get("metrics") or {}
            writer.writerow({
                "repetition": record["repetition"],
                "passed": record["passed"],
                "elapsed_seconds": record["elapsed_seconds"],
                **{field: metrics.get(field) for field in fields[3:]},
            })
    return summary


def render_three_stage(
    reference_dir: Path, vpn_output: Path, records: list[dict[str, Any]]
) -> None:
    reference_csv = reference_dir.resolve() / "application-statistics.csv"
    if not reference_csv.is_file():
        raise RuntimeError(f"missing Padua reference table: {reference_csv}")
    samples: dict[str, list[float]] = {
        "QKDNetSim monolítico": [],
        "QKDNetSim distribuido": [],
        "VPN distribuida": [],
    }
    with reference_csv.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if (
                row.get("version") != "working"
                or row.get("application") != "1-to-6"
            ):
                continue
            stage = (
                "QKDNetSim monolítico"
                if row.get("deployment") == "monolithic"
                else "QKDNetSim distribuido"
            )
            value = row.get("application_goodput_bps")
            if value:
                samples[stage].append(float(value) / 1_000_000)
    for record in records:
        if not record["passed"]:
            continue
        metrics = (record.get("runner_result") or {}).get("metrics") or {}
        value = metrics.get("throughput_mbps")
        if value is not None:
            samples["VPN distribuida"].append(float(value))
    if any(not values for values in samples.values()):
        missing = [stage for stage, values in samples.items() if not values]
        raise RuntimeError("missing samples for: " + ", ".join(missing))

    rows = []
    for stage, values in samples.items():
        mean = statistics.fmean(values)
        deviation = statistics.stdev(values) if len(values) > 1 else 0.0
        rows.append((stage, len(values), mean, deviation, mean / 10.0))
    with (vpn_output / "padua-three-stage.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow((
            "stage", "samples", "mean_goodput_mbps", "stddev_goodput_mbps",
            "offered_rate_achievement",
        ))
        writer.writerows(rows)

    width, height = 980, 560
    left, right, top, bottom = 105, 55, 75, 115
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = max(10.0, max(row[2] + row[3] for row in rows)) * 1.12
    points = []
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F5F0"/>',
        '<text x="44" y="38" font-family="sans-serif" font-size="24" font-weight="700" fill="#17324D">Padua 1→6: integración progresiva</text>',
    ]
    for tick in range(6):
        value = maximum * tick / 5
        y = top + plot_height - value / maximum * plot_height
        body.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#D6D2C9"/>'
        )
        body.append(
            f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" font-family="sans-serif" font-size="13" fill="#52606B">{value:.1f}</text>'
        )
    offered_y = top + plot_height - 10.0 / maximum * plot_height
    body.extend((
        f'<line x1="{left}" y1="{offered_y:.1f}" x2="{width-right}" y2="{offered_y:.1f}" stroke="#D4773B" stroke-width="2" stroke-dasharray="8 7"/>',
        f'<text x="{width-right-4}" y="{offered_y-8:.1f}" text-anchor="end" font-family="sans-serif" font-size="13" fill="#A85525">carga ofrecida: 10 Mbit/s</text>',
    ))
    for index, (stage, count, mean, deviation, _) in enumerate(rows):
        x = left + plot_width * index / (len(rows) - 1)
        y = top + plot_height - mean / maximum * plot_height
        upper = top + plot_height - (mean + deviation) / maximum * plot_height
        lower = top + plot_height - max(0.0, mean - deviation) / maximum * plot_height
        points.append((x, y))
        body.extend((
            f'<line x1="{x:.1f}" y1="{upper:.1f}" x2="{x:.1f}" y2="{lower:.1f}" stroke="#315C78" stroke-width="2"/>',
            f'<line x1="{x-7:.1f}" y1="{upper:.1f}" x2="{x+7:.1f}" y2="{upper:.1f}" stroke="#315C78" stroke-width="2"/>',
            f'<line x1="{x-7:.1f}" y1="{lower:.1f}" x2="{x+7:.1f}" y2="{lower:.1f}" stroke="#315C78" stroke-width="2"/>',
            f'<text x="{x:.1f}" y="{top+plot_height+31}" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#1D2730">{stage}</text>',
            f'<text x="{x:.1f}" y="{top+plot_height+51}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#52606B">n={count}</text>',
        ))
    body.append(
        '<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        + '" fill="none" stroke="#315C78" stroke-width="4"/>'
    )
    for x, y in points:
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="#315C78"/>')
    for (stage, _, mean, deviation, _), (x, y) in zip(rows, points):
        body.append(
            f'<text x="{x:.1f}" y="{y-18:.1f}" text-anchor="middle" font-family="sans-serif" font-size="14" font-weight="700" fill="#17324D">{mean:.3f} ± {deviation:.3f}</text>'
        )
    body.extend((
        f'<text x="28" y="{top+plot_height/2:.1f}" transform="rotate(-90 28 {top+plot_height/2:.1f})" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#52606B">goodput útil (Mbit/s)</text>',
        f'<text x="{width/2:.1f}" y="{height-24}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#52606B">Las dos primeras etapas usan cifrado QKDNetSim; la tercera usa QKD como PPK de IKEv2 y ESP para el tráfico.</text>',
        '</svg>',
    ))
    (vpn_output / "padua-three-stage.svg").write_text(
        "\n".join(body), encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (
        args.output_dir or ROOT / "results" / f"padua-vpn-{stamp}"
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.build:
        build_images()
    records: list[dict[str, Any]] = []
    pending: list[int] = []
    for repetition in range(1, args.repetitions + 1):
        cached = load_completed_run(output, repetition) if args.resume else None
        if cached is not None:
            records.append(cached)
            print(
                f"[PADUA_VPN] padua-1-to-6/run-{repetition} "
                f"({repetition}/{args.repetitions}) status=REUSED",
                flush=True,
            )
        else:
            pending.append(repetition)

    if pending:
        compose(CORE_COMPOSE, "up", "-d", "core")
    try:
        for repetition in pending:
            print(
                f"[PADUA_VPN] padua-1-to-6/run-{repetition} "
                f"({repetition}/{args.repetitions})",
                flush=True,
            )
            try:
                records.append(run_once(repetition, args, output))
            except Exception as error:
                records.append({
                    "repetition": repetition,
                    "passed": False,
                    "returncode": 1,
                    "timed_out": isinstance(error, subprocess.TimeoutExpired),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": None,
                    "runner_result": {"status": "failed", "error": str(error)},
                })
                print(f"[PADUA_VPN] run={repetition} error={error}", flush=True)
    finally:
        if pending:
            cleanup()
            compose(CORE_COMPOSE, "down", "--remove-orphans", check=False)
    records.sort(key=lambda record: int(record["repetition"]))
    summary = write_summary(output, args, records)
    if args.reference_results is not None and summary["failed"] == 0:
        render_three_stage(args.reference_results, output, records)
    elif args.reference_results is not None:
        print(
            "[PADUA_VPN] three-stage graph skipped because the VPN campaign failed",
            flush=True,
        )
    print(
        f"[PADUA_VPN] results={output} failures={summary['failed']}",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
