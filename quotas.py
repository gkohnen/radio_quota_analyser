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

Do everything for a day and refresh the dashboard:
    python quotas.py run 2026-07-08.log --dashboard

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

from quota_analyzer import Config, DayResult, analyse_file

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

    save_results(results_path, rows_by_date, quota_ids)
    print(f"  results.csv updated -> {results_path}  ({len(rows_by_date)} day(s) total)")

    if args.dashboard:
        build_dashboard(results_path, Path(args.out), config)


def cmd_dashboard(args) -> None:
    config = Config.load(args.config)
    build_dashboard(Path(args.results), Path(args.out), config)


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

    fig.update_layout(
        title="Daily quota share (% of songs played)",
        xaxis_title="Date",
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
    p_run.set_defaults(func=cmd_run)

    p_dash = sub.add_parser("dashboard", help="rebuild dashboard.html from results.csv")
    p_dash.set_defaults(func=cmd_dashboard)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
