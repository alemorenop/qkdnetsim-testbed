#!/usr/bin/env python3
"""Run all three Padua application flows concurrently over QKD-backed VPNs."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SINGLE_RUNNER = Path(__file__).with_name("run-padua-vpn.py")
SPEC = importlib.util.spec_from_file_location("padua_vpn_single", SINGLE_RUNNER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import common Padua VPN runner: {SINGLE_RUNNER}")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)


FLOWS = (
    {
        "id": "1-to-6",
        "topology": "padua-1-to-6",
        "rate_mbps": 10.0,
        "block_size": 800,
        "traffic_direction": "forward",
        "start_offset": 0,
        "active_duration": 120,
    },
    {
        "id": "1-to-5",
        "topology": "padua-1-to-5",
        "rate_mbps": 0.007,
        "block_size": 300,
        "traffic_direction": "forward",
        "start_offset": 5,
        "active_duration": 60,
    },
    {
        "id": "5-to-1",
        "topology": "padua-5-to-1",
        "rate_mbps": 0.014,
        "block_size": 300,
        "traffic_direction": "reverse",
        "start_offset": 15,
        "active_duration": 60,
    },
)


def scale(value: int, factor: float, minimum: int = 1) -> int:
    return max(minimum, int(round(value * factor)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=BASE.positive_int, default=5)
    parser.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        help="scale active durations and the 30 s rekey interval for a pilot",
    )
    parser.add_argument("--startup-timeout", type=BASE.positive_int, default=300)
    parser.add_argument("--command-timeout", type=BASE.positive_int, default=900)
    parser.add_argument("--qkd-interface", choices=("004", "014"), default="014")
    parser.add_argument("--keying-mode", choices=("psk", "ppk"), default="ppk")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--require-pqc", action="store_true")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if not 0 < args.time_scale <= 1:
        parser.error("--time-scale must be in (0, 1]")
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
    return args


def flow_schedule(flow: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    duration = scale(int(flow["active_duration"]), args.time_scale, minimum=10)
    rekey_interval = scale(30, args.time_scale, minimum=5)
    generations = (duration - 1) // rekey_interval + 1
    traffic_tail = duration - (generations - 1) * rekey_interval
    start_offset = max(0, int(round(int(flow["start_offset"]) * args.time_scale)))
    return {
        **flow,
        "duration": duration,
        "rekey_interval": rekey_interval,
        "generations": generations,
        "traffic_tail": traffic_tail,
        "start_offset": start_offset,
    }


def flow_command(flow: dict[str, Any], args: argparse.Namespace) -> list[str]:
    command = [
        BASE.DOCKER,
        "compose", "-f", str(BASE.CORE_COMPOSE), "exec", "-T", "core",
        "/opt/core/venv/bin/python", "/workspace/core/vpn-topology.py",
        "--qkd-topology", str(flow["topology"]),
        "--qkd-interface", args.qkd_interface,
        "--keying-mode", args.keying_mode,
        "--routers", "0",
        "--delay-ms", "2",
        "--bandwidth-mbps", "100",
        "--loss-percent", "0",
        "--startup-timeout", str(args.startup_timeout),
        "--traffic-duration", str(flow["traffic_tail"]),
        "--min-generations", str(flow["generations"]),
        "--rekey-interval", str(flow["rekey_interval"]),
        "--traffic-start-delay", str(flow["start_offset"]),
        "--traffic-rate-mbps", str(flow["rate_mbps"]),
        "--traffic-block-size", str(flow["block_size"]),
        "--traffic-direction", str(flow["traffic_direction"]),
        "--min-throughput-mbps", str(max(0.001, flow["rate_mbps"] * 0.25)),
    ]
    if args.require_pqc:
        command.append("--require-pqc")
    return command


def tail(path: Path, lines: int = 24) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])
    except OSError:
        return ""


def run_parallel_flows(
    run_dir: Path, args: argparse.Namespace
) -> dict[str, dict[str, Any]]:
    schedules = [flow_schedule(flow, args) for flow in FLOWS]
    processes: dict[str, subprocess.Popen[str]] = {}
    streams: dict[str, Any] = {}
    logs: dict[str, Path] = {}
    commands: dict[str, list[str]] = {}
    started: dict[str, float] = {}
    try:
        for flow in schedules:
            flow_id = str(flow["id"])
            log_path = run_dir / f"{flow_id}.log"
            stream = log_path.open("w", encoding="utf-8")
            command = flow_command(flow, args)
            print(
                f"[PADUA_VPN_FULL] flow={flow_id} status=STARTED "
                f"rateMbps={flow['rate_mbps']} durationSeconds={flow['duration']}",
                flush=True,
            )
            processes[flow_id] = subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            streams[flow_id] = stream
            logs[flow_id] = log_path
            commands[flow_id] = command
            started[flow_id] = time.monotonic()

        pending = set(processes)
        last_report = 0.0
        while pending:
            now = time.monotonic()
            for flow_id in list(pending):
                process = processes[flow_id]
                if process.poll() is not None:
                    pending.remove(flow_id)
                    print(
                        f"[PADUA_VPN_FULL] flow={flow_id} "
                        f"exitStatus={process.returncode}",
                        flush=True,
                    )
                elif now - started[flow_id] > args.command_timeout:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    pending.remove(flow_id)
                    print(
                        f"[PADUA_VPN_FULL] flow={flow_id} status=TIMEOUT",
                        flush=True,
                    )
            if pending and now - last_report >= 15:
                print(
                    "[PADUA_VPN_FULL] waiting flows=" + ",".join(sorted(pending)),
                    flush=True,
                )
                last_report = now
            if pending:
                time.sleep(2)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in streams.values():
            stream.close()

    results: dict[str, dict[str, Any]] = {}
    for flow in schedules:
        flow_id = str(flow["id"])
        output = logs[flow_id].read_text(encoding="utf-8")
        runner_result = BASE.parse_result(output)
        returncode = processes[flow_id].returncode
        passed = (
            returncode == 0
            and runner_result is not None
            and runner_result.get("status") == "passed"
        )
        results[flow_id] = {
            "flow": flow_id,
            "passed": passed,
            "returncode": returncode,
            "schedule": flow,
            "command": commands[flow_id],
            "runner_log": logs[flow_id].name,
            "runner_result": runner_result,
        }
        if not passed:
            print(
                f"[PADUA_VPN_FULL] flow={flow_id} status=FAILED\n{tail(logs[flow_id])}",
                flush=True,
            )
    return results


def run_once(
    repetition: int, args: argparse.Namespace, output: Path
) -> dict[str, Any]:
    BASE.cleanup()
    BASE.compose(BASE.PADUA_COMPOSE, "up", "-d", "--force-recreate")
    run_dir = output / f"vpn-full-distributed-{repetition}"
    run_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    try:
        flows = run_parallel_flows(run_dir, args)
        kms_dir = run_dir / "kms-logs"
        kms_dir.mkdir(exist_ok=True)
        for container in BASE.PADUA_CONTAINERS:
            log = BASE.docker("logs", container, check=False).stdout
            (kms_dir / f"{container}.log").write_text(log, encoding="utf-8")
        passed = all(flow["passed"] for flow in flows.values())
        record = {
            "repetition": repetition,
            "passed": passed,
            "started_at": started_at.isoformat(),
            "elapsed_seconds": round(time.monotonic() - monotonic_started, 3),
            "flows": flows,
            "artifacts": {"kms_logs": str(kms_dir.relative_to(output))},
        }
        (run_dir / "result.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(
            f"[PADUA_VPN_FULL] run={repetition} "
            f"status={'PASSED' if passed else 'FAILED'}",
            flush=True,
        )
        return record
    finally:
        BASE.cleanup()


def load_completed_run(output: Path, repetition: int) -> dict[str, Any] | None:
    path = output / f"vpn-full-distributed-{repetition}" / "result.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("repetition") == repetition and record.get("passed") is True:
        return record
    return None


def write_summary(
    output: Path, args: argparse.Namespace, records: list[dict[str, Any]]
) -> dict[str, Any]:
    passed = sum(record.get("passed") is True for record in records)
    summary = {
        "schema_version": 1,
        "profile": "padua-vpn-full",
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
        "repetition", "flow", "passed", "offered_rate_mbps",
        "throughput_mbps", "offered_rate_achievement", "iperf_retransmits",
        "iperf_min_interval_mbps", "iperf_zero_throughput_intervals",
        "esp_packets", "generations_verified",
    )
    with (output / "vpn-full-statistics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            for flow_id, flow in record.get("flows", {}).items():
                metrics = (flow.get("runner_result") or {}).get("metrics") or {}
                writer.writerow({
                    "repetition": record["repetition"],
                    "flow": flow_id,
                    "passed": flow.get("passed"),
                    **{field: metrics.get(field) for field in fields[3:]},
                })
    return summary


def main() -> int:
    args = parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (
        args.output_dir or ROOT / "results" / f"padua-vpn-full-{stamp}"
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.build:
        BASE.build_images()

    records: list[dict[str, Any]] = []
    pending: list[int] = []
    for repetition in range(1, args.repetitions + 1):
        cached = load_completed_run(output, repetition) if args.resume else None
        if cached is not None:
            records.append(cached)
            print(
                f"[PADUA_VPN_FULL] run={repetition} "
                f"({repetition}/{args.repetitions}) status=REUSED",
                flush=True,
            )
        else:
            pending.append(repetition)

    if pending:
        BASE.compose(BASE.CORE_COMPOSE, "up", "-d", "core")
    try:
        for repetition in pending:
            print(
                f"[PADUA_VPN_FULL] run={repetition} "
                f"({repetition}/{args.repetitions})",
                flush=True,
            )
            try:
                records.append(run_once(repetition, args, output))
            except Exception as error:
                records.append({
                    "repetition": repetition,
                    "passed": False,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": None,
                    "error": str(error),
                    "flows": {},
                })
                print(
                    f"[PADUA_VPN_FULL] run={repetition} error={error}",
                    flush=True,
                )
    finally:
        if pending:
            BASE.cleanup()
            BASE.compose(BASE.CORE_COMPOSE, "down", "--remove-orphans", check=False)

    records.sort(key=lambda record: int(record["repetition"]))
    summary = write_summary(output, args, records)
    print(
        f"[PADUA_VPN_FULL] results={output} failures={summary['failed']}",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
