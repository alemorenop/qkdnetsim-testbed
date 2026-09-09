#!/usr/bin/env python3
"""Render dependency-free SVG charts from an architecture summary.json."""

from __future__ import annotations

import argparse
import html
import json
import statistics
from pathlib import Path
from typing import Any, Callable


COLORS = {"monolithic": "#4666A6", "distributed": "#D56A3A"}


def nested(record: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = record
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def aggregate(records: list[dict[str, Any]], version: str, topology: str,
              deployment: str, path: tuple[str, ...]) -> tuple[float, float, int] | None:
    values = [
        value for record in records
        if record.get("version") == version and record.get("topology") == topology
        and (value := nested(record, (deployment, *path))) is not None
    ]
    if not values:
        return None
    return statistics.fmean(values), (statistics.stdev(values) if len(values) > 1 else 0), len(values)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render(summary: dict[str, Any], destination: Path) -> None:
    records = summary.get("runs", [])
    groups = [(v, t) for v in ("old", "new") for t in ("p2p", "secoqc", "relay")
              if any(r.get("version") == v and r.get("topology") == t for r in records)]
    if not groups:
        raise ValueError("summary contains no runs")

    panels: list[tuple[str, tuple[str, ...], Callable[[float], float], str]] = [
        ("Entrega de tráfico", ("metrics", "delivery_ratio"), lambda x: x * 100, "%"),
        ("Goodput de aplicación", ("metrics", "application_goodput_bps"), lambda x: x / 1000, "kbit/s"),
        ("CPU agregada media", ("resources", "mean_cpu_percent_sum"), lambda x: x, "% CPU"),
        ("Memoria agregada máxima", ("resources", "peak_memory_bytes_sum"), lambda x: x / 1048576, "MiB"),
    ]
    width, height = 1280, 820
    margin_x, top = 72, 104
    gap_x, gap_y = 44, 78
    panel_w = (width - 2 * margin_x - gap_x) / 2
    panel_h = (height - top - 72 - gap_y) / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F6F2"/>',
        '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#243447}.title{font-size:25px;font-weight:700}.sub{font-size:13px;fill:#667482}.pt{font-size:16px;font-weight:650}.axis{font-size:11px;fill:#667482}.val{font-size:10px;font-weight:600}.note{font-size:11px;fill:#8A5961}</style>',
        '<text x="72" y="45" class="title">QKDNetSim monolítico frente al testbed distribuido</text>',
        '<text x="72" y="70" class="sub">Media por campaña; las barras de error representan ±1 desviación típica. * Relay de tres KMS: referencia, no pareja topológica.</text>',
        '<rect x="850" y="34" width="14" height="14" rx="3" fill="#4666A6"/><text x="872" y="46" class="sub">Monolítico</text>',
        '<rect x="990" y="34" width="14" height="14" rx="3" fill="#D56A3A"/><text x="1012" y="46" class="sub">Distribuido</text>',
    ]

    for panel_index, (title, mono_path, transform, unit) in enumerate(panels):
        col, row = panel_index % 2, panel_index // 2
        x0 = margin_x + col * (panel_w + gap_x)
        y0 = top + row * (panel_h + gap_y)
        chart_top, chart_bottom = y0 + 38, y0 + panel_h - 42
        values: dict[tuple[str, str, str], tuple[float, float, int] | None] = {}
        maxima: list[float] = []
        for version, topology in groups:
            for deployment in ("monolithic", "distributed"):
                raw = aggregate(records, version, topology, deployment, mono_path)
                cooked = None if raw is None else (transform(raw[0]), transform(raw[1]), raw[2])
                values[(version, topology, deployment)] = cooked
                if cooked:
                    maxima.append(cooked[0] + cooked[1])
        maximum = max(maxima, default=1)
        if title == "Entrega de tráfico":
            maximum = max(100.0, maximum)
        else:
            maximum *= 1.15
        parts += [
            f'<text x="{x0:.1f}" y="{y0 + 18:.1f}" class="pt">{esc(title)} ({esc(unit)})</text>',
            f'<line x1="{x0:.1f}" y1="{chart_bottom:.1f}" x2="{x0 + panel_w:.1f}" y2="{chart_bottom:.1f}" stroke="#AEB7BF"/>',
        ]
        for tick in range(5):
            value = maximum * tick / 4
            y = chart_bottom - (chart_bottom - chart_top) * tick / 4
            parts.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 + panel_w:.1f}" y2="{y:.1f}" stroke="#DDE1E3"/>')
            parts.append(f'<text x="{x0 - 8:.1f}" y="{y + 4:.1f}" text-anchor="end" class="axis">{value:.0f}</text>')
        group_w = panel_w / len(groups)
        bar_w = min(38.0, group_w * 0.28)
        for group_index, (version, topology) in enumerate(groups):
            center = x0 + group_w * (group_index + 0.5)
            for offset, deployment in ((-0.55, "monolithic"), (0.55, "distributed")):
                item = values[(version, topology, deployment)]
                if not item:
                    continue
                mean, deviation, count = item
                bar_h = (chart_bottom - chart_top) * mean / maximum
                x = center + offset * bar_w - bar_w / 2
                y = chart_bottom - bar_h
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="3" fill="{COLORS[deployment]}"/>')
                err = (chart_bottom - chart_top) * deviation / maximum
                mid = x + bar_w / 2
                parts.append(f'<line x1="{mid:.1f}" y1="{max(chart_top, y - err):.1f}" x2="{mid:.1f}" y2="{min(chart_bottom, y + err):.1f}" stroke="#243447"/>')
                parts.append(f'<text x="{mid:.1f}" y="{max(chart_top + 10, y - err - 5):.1f}" text-anchor="middle" class="val">{mean:.1f}</text>')
            suffix = "*" if topology == "relay" else ""
            parts.append(f'<text x="{center:.1f}" y="{chart_bottom + 20:.1f}" text-anchor="middle" class="axis">{version.upper()} · {topology}{suffix}</text>')

    parts.append('</svg>')
    destination.write_text("\n".join(parts), encoding="utf-8")


def render_paper_validation(summary: dict[str, Any], destination: Path) -> None:
    """Plot the two cross-deployment observables closest to Tables 3 and 4."""
    records = summary.get("runs", [])
    groups = [(v, t) for v in ("old", "new") for t in ("p2p", "secoqc", "relay")
              if any(r.get("version") == v and r.get("topology") == t for r in records)]
    if not groups:
        raise ValueError("summary contains no runs")

    def rate_values(version: str, topology: str, deployment: str) -> list[float]:
        values = []
        for record in records:
            if record.get("version") != version or record.get("topology") != topology:
                continue
            links = nested_object(record, deployment, "metrics", "paper_validation", "qkd_links")
            rates = [float(link["observed_generation_rate_bps"]) for link in (links or [])
                     if isinstance(link.get("observed_generation_rate_bps"), (int, float))]
            if rates:
                values.append(statistics.fmean(rates) / 1000)
        return values

    def goodput_values(version: str, topology: str, deployment: str) -> list[float]:
        values = []
        for record in records:
            if record.get("version") == version and record.get("topology") == topology:
                value = nested(record, (deployment, "metrics", "application_goodput_bps"))
                if value is not None:
                    values.append(value / 1000)
        return values

    width, height = 1160, 500
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F6F2"/>',
        '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#243447}.title{font-size:24px;font-weight:700}.sub{font-size:12px;fill:#667482}.pt{font-size:16px;font-weight:650}.axis{font-size:11px;fill:#667482}.val{font-size:10px;font-weight:600}</style>',
        '<text x="60" y="42" class="title">Validación inspirada en las estadísticas de QKDNetSim</text>',
        '<text x="60" y="66" class="sub">Tasa generada observada por enlace y tráfico útil entregado; media ± desviación típica. * Relay de tres KMS: referencia no pareada.</text>',
        '<rect x="790" y="31" width="14" height="14" rx="3" fill="#4666A6"/><text x="812" y="43" class="sub">Monolítico</text>',
        '<rect x="930" y="31" width="14" height="14" rx="3" fill="#D56A3A"/><text x="952" y="43" class="sub">Distribuido</text>',
    ]
    panels = (
        ("Tasa QKD observada", rate_values, "kbit/s"),
        ("Goodput de aplicación", goodput_values, "kbit/s"),
    )
    for panel_index, (title, getter, unit) in enumerate(panels):
        x0 = 60 + panel_index * 565
        y_top, y_bottom, panel_w = 120, 415, 500
        data: dict[tuple[str, str, str], tuple[float, float]] = {}
        maximum = 1.0
        for version, topology in groups:
            for deployment in ("monolithic", "distributed"):
                values = getter(version, topology, deployment)
                if values:
                    mean = statistics.fmean(values)
                    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
                    data[(version, topology, deployment)] = (mean, deviation)
                    maximum = max(maximum, mean + deviation)
        maximum *= 1.18
        parts.append(f'<text x="{x0}" y="100" class="pt">{esc(title)} ({unit})</text>')
        for tick in range(5):
            value = maximum * tick / 4
            y = y_bottom - (y_bottom - y_top) * tick / 4
            parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + panel_w}" y2="{y:.1f}" stroke="#DDE1E3"/>')
            parts.append(f'<text x="{x0 - 7}" y="{y + 4:.1f}" text-anchor="end" class="axis">{value:.1f}</text>')
        group_w = panel_w / len(groups)
        bar_w = min(36.0, group_w * 0.28)
        for index, (version, topology) in enumerate(groups):
            center = x0 + group_w * (index + 0.5)
            for offset, deployment in ((-0.55, "monolithic"), (0.55, "distributed")):
                item = data.get((version, topology, deployment))
                if not item:
                    continue
                mean, deviation = item
                bar_h = (y_bottom - y_top) * mean / maximum
                x, y = center + offset * bar_w - bar_w / 2, y_bottom - bar_h
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{bar_h:.1f}" rx="3" fill="{COLORS[deployment]}"/>')
                err = (y_bottom - y_top) * deviation / maximum
                mid = x + bar_w / 2
                parts.append(f'<line x1="{mid:.1f}" y1="{y - err:.1f}" x2="{mid:.1f}" y2="{y + err:.1f}" stroke="#243447"/>')
                parts.append(f'<text x="{mid:.1f}" y="{max(y_top + 10, y - err - 5):.1f}" text-anchor="middle" class="val">{mean:.2f}</text>')
            suffix = "*" if topology == "relay" else ""
            parts.append(f'<text x="{center:.1f}" y="438" text-anchor="middle" class="axis">{version.upper()} · {topology}{suffix}</text>')
    parts.append('</svg>')
    destination.write_text("\n".join(parts), encoding="utf-8")


def nested_object(record: dict[str, Any], *path: str) -> Any:
    value: Any = record
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    destination = args.output or args.summary.with_name("architecture-comparison.svg")
    render(summary, destination)
    render_paper_validation(summary, destination.with_name("qkd-validation.svg"))
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
