#!/usr/bin/env python3
"""
Radio playout quota analyser -- command line entry point.

Usage
-----
Process one day (prints a report and updates results.csv):
    python quotas.py run 2026-07-08.log

Process several days at once (e.g. a back-fill):
    python quotas.py run logs/2026-07-*.log

Rebuild the dashboard from results.csv:
    python quotas.py dashboard

Diagnose a file (encoding + how each line is classified), writing nothing:
    python quotas.py inspect 2026-07-08.log

Do everything for a day and refresh the dashboard:
    python quotas.py run 2026-07-08.log --dashboard

Each run also writes a byte-faithful duplicate with per-quota 1/0 flag columns
into an 'annotated/' sub-folder (use --annotated-dir to redirect, --no-annotate
to skip).

Options
    --config PATH     quota definition file (default: quota_config.json)
    --results PATH    accumulated results CSV (default: results.csv)
    --out PATH        dashboard HTML output (default: dashboard.html)
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

from quota_analyzer import Config, DayResult, analyse_file, annotate_file

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# results.csv persistence (one row per day, upsert by date)
# --------------------------------------------------------------------------- #
def load_results(results_path: Path) -> dict[str, dict]:
    if not results_path.exists():
        return {}
    with results_path.open(newline="", encoding="utf-8") as fh:
        return {row["date"]: row for row in csv.DictReader(fh)}


def save_results(results_path: Path, rows_by_date: dict[str, dict], quota_ids: list[str]) -> None:
    fieldnames = ["date", "total"]
    for qid in quota_ids:
        fieldnames += [f"{qid}_count", f"{qid}_pct"]
    ordered = [rows_by_date[d] for d in sorted(rows_by_date)]
    with results_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ordered)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_report(result: DayResult, config: Config) -> None:
    print(f"\n  Playout quota report -- {result.date}")
    print(f"  {'-' * 46}")
    print(f"  Total songs played : {result.total}")
    for q in config.quotas:
        c = result.quota_counts.get(q.id, 0)
        p = result.quota_pct.get(q.id, 0.0)
        d = result.quota_denom.get(q.id, result.total)
        tgt = f"   target {q.target:g}%" if q.target is not None else ""
        base = "daytime songs" if q.denominator == "window" else "songs of the day"
        print(f"  {q.name:<8} ({q.description})")
        print(f"        count : {c:>4}    share : {p:5.1f}% of {d} {base}{tgt}")
    print()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_run(args) -> None:
    config = Config.load(args.config)
    quota_ids = [q.id for q in config.quotas]
    results_path = Path(args.results)

    # Expand globs (Windows cmd does not expand them for us).
    paths: list[str] = []
    for pattern in args.files:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched if matched else [pattern])

    if not paths:
        sys.exit("No input files matched.")

    rows_by_date = load_results(results_path)
    for p in paths:
        if not Path(p).exists():
            print(f"  ! skipping (not found): {p}", file=sys.stderr)
            continue
        result = analyse_file(p, config)
        print_report(result, config)
        rows_by_date[result.date] = result.as_row(quota_ids)

        if not args.no_annotate:
            src = Path(p)
            ann_dir = Path(args.annotated_dir) if args.annotated_dir else src.parent / "annotated"
            out = annotate_file(src, ann_dir / src.name, config)
            print(f"  annotated copy -> {out}")

    save_results(results_path, rows_by_date, quota_ids)
    print(f"  results.csv updated -> {results_path}  ({len(rows_by_date)} day(s) total)")

    if args.dashboard:
        build_dashboard(results_path, Path(args.out), config)


def cmd_dashboard(args) -> None:
    config = Config.load(args.config)
    build_dashboard(Path(args.results), Path(args.out), config)


def cmd_inspect(args) -> None:
    """Diagnose one or more log files without touching results.csv.

    Reports the detected encoding and how each line was classified, so an
    all-zero result is easy to explain (wrong encoding -> 0 songs; config
    mismatch -> songs > 0 but quota counts 0, with sample unmatched paths).
    """
    from quota_analyzer import (Config as _C, classify, detect_codec,
                                iter_lines, parse_path, quota_flag)
    config = _C.load(args.config)
    excluded = set(config.exclude_path_segments)

    paths: list[str] = []
    for pattern in args.files:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched if matched else [pattern])

    for p in paths:
        if not Path(p).exists():
            print(f"  ! not found: {p}", file=sys.stderr)
            continue
        raw = Path(p).read_bytes()
        codec = detect_codec(raw)
        text = raw.decode(codec)

        non_empty = fillers = excluded_lines = songs = 0
        qcounts = {q.id: 0 for q in config.quotas}
        matched_samples, unmatched_samples = [], []
        for body, _term in iter_lines(text):
            if not body.strip():
                continue
            non_empty += 1
            path = parse_path(body)
            if not path:
                fillers += 1
                continue
            ev = classify(body, config, excluded)
            if not ev.is_song:
                excluded_lines += 1
                continue
            songs += 1
            hit = False
            for q in config.quotas:
                if quota_flag(q, ev):
                    qcounts[q.id] += 1
                    hit = True
            if hit and len(matched_samples) < 3:
                matched_samples.append(path)
            elif not hit and len(unmatched_samples) < 3:
                unmatched_samples.append(path)

        print(f"\n  {Path(p).name}")
        print(f"  {'-' * 46}")
        print(f"  detected encoding     : {codec}")
        print(f"  non-empty lines       : {non_empty}")
        print(f"  filler (no path)      : {fillers}")
        print(f"  excluded (folder)     : {excluded_lines}")
        print(f"  counted songs (total) : {songs}")
        for q in config.quotas:
            print(f"      {q.name:<8} matches : {qcounts[q.id]}")
        if songs == 0 and non_empty > 0:
            print("  >> 0 songs from non-empty lines: likely an encoding/format mismatch.")
        elif songs > 0 and all(v == 0 for v in qcounts.values()):
            print("  >> songs found but no quota matched: check path_contains vs these paths:")
            for s in unmatched_samples:
                print(f"       {s}")
        else:
            if matched_samples:
                print("  sample matched paths:")
                for s in matched_samples:
                    print(f"       {s}")
    print()


def build_dashboard(results_path: Path, out_path: Path, config: Config) -> None:
    try:
        import plotly.graph_objects as go
    except ImportError:
        sys.exit(
            "Plotly is required for the dashboard.\n"
            "Install it with:  pip install plotly"
        )

    rows_by_date = load_results(results_path)
    if not rows_by_date:
        sys.exit("results.csv is empty -- run the analyser on some log files first.")

    dates = sorted(rows_by_date)
    fig = go.Figure()

    palette = ["#2563eb", "#e11d48", "#059669", "#d97706", "#7c3aed"]
    for i, q in enumerate(config.quotas):
        color = palette[i % len(palette)]
        y = [float(rows_by_date[d].get(f"{q.id}_pct", 0) or 0) for d in dates]
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=y,
                mode="lines+markers",
                name=f"{q.name} — {q.description}",
                line=dict(width=3, color=color),
                marker=dict(size=8),
                hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>",
            )
        )

    # Target lines: dotted, in the same colour as their quota.
    for i, q in enumerate(config.quotas):
        if q.target is None:
            continue
        color = palette[i % len(palette)]
        fig.add_hline(
            y=q.target,
            line=dict(color=color, width=2, dash="dot"),
            annotation_text=f"{q.name} target {q.target:g}%",
            annotation_position="top left",
            annotation_font=dict(color=color, size=11),
        )

    # With many days of history the plot gets wide; default to showing the
    # most recent window and let a range slider handle horizontal scrolling
    # (drag it, or drag inside the chart) through the full history.
    visible_days = 30
    default_range = [dates[-visible_days], dates[-1]] if len(dates) > visible_days else [dates[0], dates[-1]]

    fig.update_layout(
        title="Daily quota share (% of songs played)",
        xaxis_title="Date",
        xaxis=dict(
            type="date",
            range=default_range,
            rangeslider=dict(visible=True, thickness=0.08),
        ),
        yaxis_title="Share of songs played (%)",
        yaxis=dict(ticksuffix="%", rangemode="tozero"),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(t=90, r=30, l=60, b=60),
    )

    out_path.write_text(fig.to_html(include_plotlyjs="cdn", full_html=True), encoding="utf-8")
    print(f"  dashboard written -> {out_path}")


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Radio playout quota analyser")
    parser.add_argument("--config", default=str(HERE / "quota_config.json"))
    parser.add_argument("--results", default=str(HERE / "results.csv"))
    parser.add_argument("--out", default=str(HERE / "dashboard.html"))

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="analyse one or more daily log files")
    p_run.add_argument("files", nargs="+", help="log file(s) or glob pattern(s)")
    p_run.add_argument("--dashboard", action="store_true", help="also rebuild the dashboard")
    p_run.add_argument("--annotated-dir", default=None,
                       help="folder for annotated duplicates (default: an 'annotated' subfolder beside each log)")
    p_run.add_argument("--no-annotate", action="store_true",
                       help="do not write annotated duplicate files")
    p_run.set_defaults(func=cmd_run)

    p_dash = sub.add_parser("dashboard", help="rebuild dashboard.html from results.csv")
    p_dash.set_defaults(func=cmd_dashboard)

    p_ins = sub.add_parser("inspect", help="diagnose encoding/parsing of log file(s) without writing anything")
    p_ins.add_argument("files", nargs="+", help="log file(s) or glob pattern(s)")
    p_ins.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
