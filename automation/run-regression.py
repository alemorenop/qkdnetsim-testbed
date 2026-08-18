#!/usr/bin/env python3
"""Run the four supported QKD-backed VPN regression variants."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE_COMPOSE = ROOT / "docker" / "docker-compose.core.yml"
COMPOSE_FILES = (
    ROOT / "docker" / "docker-compose.vpn.yml",
    ROOT / "docker" / "docker-compose.key-relay-vpn.yml",
)
RESULT_MARKER = "[CORE_RESULT] "
CASE_NAMES = (
    "vpn-point-to-point-etsi004",
    "vpn-point-to-point-etsi014",
    "vpn-key-relay-etsi004",
    "vpn-key-relay-etsi014",
    "negative-vpn-rejects-mismatched-key",
)


@dataclass(frozen=True)
class Case:
    name: str
    compose_file: Path
    runner: str
    runner_args: tuple[str, ...]
    expected_failure: bool = False


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
        description=(
            "Build/start the testbed, run all supported VPN variants, "
            "collect structured evidence and clean every Compose project"
        )
    )
    parser.add_argument("--repetitions", type=positive_int, default=3)
    parser.add_argument(
        "--case",
        action="append",
        choices=CASE_NAMES,
        dest="selected_cases",
        help="Run only this case; repeat the option to select several cases",
    )
    parser.add_argument("--routers", type=int, choices=range(0, 9), default=0)
    parser.add_argument("--delay-ms", type=positive_int, default=5)
    parser.add_argument("--bandwidth-mbps", type=positive_int, default=100)
    parser.add_argument("--loss-percent", type=percentage, default=0.0)
    parser.add_argument("--startup-timeout", type=positive_int, default=300)
    parser.add_argument("--command-timeout", type=positive_int, default=900)
    parser.add_argument("--traffic-duration", type=positive_int, default=8)
    parser.add_argument("--min-generations", type=positive_int, default=2)
    parser.add_argument("--rekey-interval", type=positive_int, default=10)
    parser.add_argument(
        "--max-rekey-loss-percent", type=percentage, default=25.0
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Destination directory; defaults to results/regression-<UTC timestamp>",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Rebuild QKD, VPN and CORE images before the campaign",
    )
    parser.add_argument(
        "--skip-negative-tests",
        action="store_true",
        help="Skip the expected mismatched-key rejection case",
    )
    parser.add_argument(
        "--keep-core",
        action="store_true",
        help="Leave the shared CORE runtime running after the campaign",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the matrix without accessing Docker",
    )
    return parser.parse_args()


def compose(file: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(file), *args]


def run(
    command: list[str],
    *,
    check: bool = True,
    timeout: int | None = None,
    stream: bool = False,
    echo_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"[REGRESSION] command={shlex.join(command)}", flush=True)
    if not stream:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if echo_output and result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    else:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        lines: list[str] = []
        started = time.monotonic()
        assert process.stdout is not None
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for output_line in process.stdout:
                output_queue.put(output_line)
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while True:
                try:
                    line = output_queue.get(timeout=0.2)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line:
                    print(line, end="", flush=True)
                    lines.append(line)
                if timeout is not None and time.monotonic() - started > timeout:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, timeout)
            returncode = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
        result = subprocess.CompletedProcess(
            command, returncode, "".join(lines), None
        )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed with status {result.returncode}: {shlex.join(command)}"
        )
    return result


def stop_infrastructure() -> None:
    for file in COMPOSE_FILES:
        run(
            compose(file, "down", "--remove-orphans"),
            check=False,
            echo_output=False,
        )


def remove_stale_endpoints() -> None:
    identifiers: list[str] = []
    for prefix in ("qkd-core-vpn-",):
        result = run(
            ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
            check=False,
            echo_output=False,
        )
        identifiers.extend((result.stdout or "").split())
    if identifiers:
        unique = list(dict.fromkeys(identifiers))
        print(
            "[REGRESSION] removing stale CORE endpoints=" + ",".join(unique),
            flush=True,
        )
        run(["docker", "rm", "-f", *unique], echo_output=False)


def build_images() -> None:
    run(
        [
            "docker", "build", "-t", "qkdnetsim-testbed:latest",
            "-f", "docker/Dockerfile", ".",
        ]
    )
    run(
        [
            "docker", "build", "-t", "qkdnetsim-vpn-endpoint:latest",
            "-f", "docker/vpn/Dockerfile.vpn", ".",
        ]
    )
    run(compose(CORE_COMPOSE, "up", "-d", "--build", "core"))


def start_core(build: bool) -> None:
    if build:
        build_images()
    else:
        run(compose(CORE_COMPOSE, "up", "-d", "core"))


def matrix(args: argparse.Namespace) -> list[Case]:
    common = (
        "--routers", str(args.routers),
        "--delay-ms", str(args.delay_ms),
        "--bandwidth-mbps", str(args.bandwidth_mbps),
        "--loss-percent", str(args.loss_percent),
        "--startup-timeout", str(args.startup_timeout),
    )
    vpn = (
        *common,
        "--traffic-duration", str(args.traffic_duration),
        "--min-generations", str(args.min_generations),
        "--rekey-interval", str(args.rekey_interval),
        "--max-rekey-loss-percent", str(args.max_rekey_loss_percent),
    )
    return [
        Case(
            "vpn-point-to-point-etsi004", COMPOSE_FILES[0], "vpn-topology.py",
            ("--qkd-topology", "point-to-point", "--qkd-interface", "004", *vpn),
        ),
        Case(
            "vpn-point-to-point-etsi014", COMPOSE_FILES[0], "vpn-topology.py",
            ("--qkd-topology", "point-to-point", "--qkd-interface", "014", *vpn),
        ),
        Case(
            "vpn-key-relay-etsi004", COMPOSE_FILES[1], "vpn-topology.py",
            ("--qkd-topology", "key-relay", "--qkd-interface", "004", *vpn),
        ),
        Case(
            "vpn-key-relay-etsi014", COMPOSE_FILES[1], "vpn-topology.py",
            ("--qkd-topology", "key-relay", "--qkd-interface", "014", *vpn),
        ),
    ]


def negative_case(args: argparse.Namespace) -> Case:
    return Case(
        "negative-vpn-rejects-mismatched-key",
        COMPOSE_FILES[0],
        "vpn-topology.py",
        (
            "--qkd-topology", "point-to-point",
            "--qkd-interface", "014",
            "--routers", str(args.routers),
            "--delay-ms", str(args.delay_ms),
            "--bandwidth-mbps", str(args.bandwidth_mbps),
            "--loss-percent", str(args.loss_percent),
            "--startup-timeout", str(args.startup_timeout),
            "--rekey-interval", "10",
            "--fault-mode", "bob-key-mismatch",
        ),
        expected_failure=True,
    )


def parse_runner_result(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


def run_case(
    case: Case,
    repetition: int,
    output_dir: Path,
    command_timeout: int,
) -> dict[str, Any]:
    stop_infrastructure()
    remove_stale_endpoints()
    run(compose(case.compose_file, "up", "-d", "--force-recreate"))
    run_dir = output_dir / f"{case.name}-run-{repetition}"
    run_dir.mkdir(parents=True, exist_ok=True)
    command = compose(
        CORE_COMPOSE,
        "exec", "-T", "core",
        "/opt/core/venv/bin/python",
        f"/workspace/core/{case.runner}",
        *case.runner_args,
    )
    started = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    timed_out = False
    try:
        completed = run(
            command, check=False, timeout=command_timeout, stream=True
        )
    except subprocess.TimeoutExpired as error:
        timed_out = True
        completed = subprocess.CompletedProcess(command, 124, str(error), None)
    elapsed = round(time.monotonic() - monotonic_started, 3)
    (run_dir / "runner.log").write_text(completed.stdout or "", encoding="utf-8")
    infrastructure = run(
        compose(case.compose_file, "logs", "--no-color"),
        check=False,
        echo_output=False,
    ).stdout
    (run_dir / "infrastructure.log").write_text(
        infrastructure or "", encoding="utf-8"
    )
    runner_result = parse_runner_result(completed.stdout or "")
    expected_rejection = (
        case.expected_failure
        and completed.returncode != 0
        and "KMS streams diverged" in (completed.stdout or "")
    )
    passed = (
        expected_rejection
        if case.expected_failure
        else completed.returncode == 0
        and runner_result is not None
        and runner_result.get("status") == "passed"
    )
    record: dict[str, Any] = {
        "case": case.name,
        "repetition": repetition,
        "expected_failure": case.expected_failure,
        "passed": passed,
        "returncode": completed.returncode,
        "timed_out": timed_out,
        "started_at": started.isoformat(),
        "elapsed_seconds": elapsed,
        "command": command,
        "runner_result": runner_result,
        "qkd_link_budget": [
            line.strip()
            for line in (infrastructure or "").splitlines()
            if "[QKD_LINK_BUDGET]" in line
        ],
        "artifacts": {
            "runner_log": str((run_dir / "runner.log").relative_to(output_dir)),
            "infrastructure_log": str(
                (run_dir / "infrastructure.log").relative_to(output_dir)
            ),
        },
    }
    (run_dir / "result.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        f"[REGRESSION] case={case.name} repetition={repetition} "
        f"status={'PASSED' if passed else 'FAILED'} elapsedSeconds={elapsed}",
        flush=True,
    )
    stop_infrastructure()
    remove_stale_endpoints()
    return record


def write_summary(
    output_dir: Path, args: argparse.Namespace, records: list[dict[str, Any]]
) -> dict[str, Any]:
    passed = sum(1 for record in records if record["passed"])
    summary = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "configuration": {
            key: value
            for key, value in vars(args).items()
            if key not in {"output_dir", "list"}
        },
        "total": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "status": "passed" if passed == len(records) else "failed",
        "runs": records,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (output_dir / "summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "case", "repetition", "expected_failure", "passed",
                "returncode", "timed_out", "elapsed_seconds",
            ),
        )
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in writer.fieldnames})
    return summary


def main() -> int:
    args = parse_args()
    cases = matrix(args)
    if not args.skip_negative_tests:
        cases.append(negative_case(args))
    if args.selected_cases:
        selected = set(args.selected_cases)
        cases = [case for case in cases if case.name in selected]
    if args.list:
        for case in cases:
            print(
                f"{case.name}: compose={case.compose_file.name} "
                f"runner={case.runner} expectedFailure={case.expected_failure}"
            )
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (args.output_dir or ROOT / "results" / f"regression-{stamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    try:
        stop_infrastructure()
        remove_stale_endpoints()
        start_core(args.build)
        for case in cases:
            repetitions = 1 if case.expected_failure else args.repetitions
            for repetition in range(1, repetitions + 1):
                try:
                    record = run_case(
                        case, repetition, output_dir, args.command_timeout
                    )
                except Exception as error:
                    run_dir = output_dir / f"{case.name}-run-{repetition}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    record = {
                        "case": case.name,
                        "repetition": repetition,
                        "expected_failure": case.expected_failure,
                        "passed": False,
                        "returncode": None,
                        "timed_out": False,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": 0.0,
                        "command": None,
                        "runner_result": None,
                        "orchestration_error": str(error),
                        "qkd_link_budget": [],
                        "artifacts": {
                            "result_json": str(
                                (run_dir / "result.json").relative_to(output_dir)
                            )
                        },
                    }
                    (run_dir / "result.json").write_text(
                        json.dumps(record, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                    print(
                        f"[REGRESSION] case={case.name} repetition={repetition} "
                        f"status=FAILED orchestrationError={error}",
                        flush=True,
                    )
                    stop_infrastructure()
                    remove_stale_endpoints()
                records.append(record)
    finally:
        stop_infrastructure()
        remove_stale_endpoints()
        if not args.keep_core:
            run(compose(CORE_COMPOSE, "down", "--remove-orphans"), check=False)

    summary = write_summary(output_dir, args, records)
    print(
        "[REGRESSION] "
        f"status={summary['status'].upper()} passed={summary['passed']} "
        f"failed={summary['failed']} results={output_dir}"
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
