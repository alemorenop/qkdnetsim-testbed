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
    print(
        f"[PADUA_VPN] results={output} failures={summary['failed']}",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
