"""
Core parsing and quota logic for the radio playout log analyser.

Pure standard library -- no external dependencies, nothing to compile.

A log line is 6 tab-separated fields:
    time <TAB> event <TAB> artist <TAB> title <TAB> (unused) <TAB> (path)
The path is always the last field, wrapped in parentheses. Filler entries
(Silence, "top horaire", Pub, ...) carry an empty path "()", which is how we
distinguish them from real songs -- including the genuine song
"CHARLES - silence BEFR.mp3", which we must keep.

A quota may optionally restrict itself to a time-of-day window (e.g. 06:00-22:00).
When it does, its percentage is computed against a chosen denominator:
  * "window" (default) -> songs played inside that same window, or
  * "day"              -> all songs played that day.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Matches YYYY-MM-DD anywhere in a file name (European calendar date).
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _to_seconds(hhmm: str) -> int:
    """'06:00' or '22:00:00' -> seconds since midnight."""
    parts = [int(p) for p in hhmm.strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    h, m, s = parts[:3]
    return h * 3600 + m * 60 + s


@dataclass
class Quota:
    id: str
    name: str
    description: str
    path_contains: list
    filename_contains: list
    target: float | None = None
    window: tuple | None = None            # (start_sec, end_sec), end exclusive
    denominator: str = "day"               # "day" or "window"

    def matches(self, full_path: str, filename: str) -> bool:
        pl = full_path.lower()
        fl = filename.lower()
        if any(s.lower() in pl for s in self.path_contains):
            return True
        if any(s.lower() in fl for s in self.filename_contains):
            return True
        return False

    def in_window(self, sec) -> bool:
        if self.window is None:
            return True
        if sec is None:
            return False
        start, end = self.window
        return start <= sec < end


@dataclass
class Config:
    encoding: str
    exclude_path_segments: list
    quotas: list

    @classmethod
    def load(cls, path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        quotas = []
        for q in data["quotas"]:
            window = None
            denominator = "day"
            tw = q.get("time_window")
            if tw:
                window = (_to_seconds(tw["start"]), _to_seconds(tw["end"]))
                denominator = q.get("denominator", "window")
            quotas.append(
                Quota(
                    id=q["id"],
                    name=q["name"],
                    description=q.get("description", ""),
                    path_contains=q.get("path_contains", []),
                    filename_contains=q.get("filename_contains", []),
                    target=q.get("target"),
                    window=window,
                    denominator=denominator,
                )
            )
        return cls(
            encoding=data.get("encoding", "windows-1252"),
            exclude_path_segments=[s.lower() for s in data.get("exclude_path_segments", [])],
            quotas=quotas,
        )


def date_from_filename(filename: str) -> str:
    """Extract an ISO date (YYYY-MM-DD) from a file name. European format."""
    m = DATE_RE.search(Path(filename).name)
    if not m:
        raise ValueError(f"No YYYY-MM-DD date found in file name: {filename!r}")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _split_segments(path: str) -> list:
    return [seg for seg in re.split(r"[\\/]+", path) if seg]


def parse_path(line: str):
    """Return the file path for a log line, or None if malformed.
    Path is the last tab field, wrapped in parentheses; "()" -> "" (filler)."""
    fields = line.rstrip("\n").split("\t")
    if len(fields) < 6:
        return None
    last = fields[-1].strip()
    if not (last.startswith("(") and last.endswith(")")):
        return None
    return last[1:-1].strip()


def parse_time(line: str):
    """Seconds-since-midnight from the first field, or None if unparseable."""
    first = line.split("\t", 1)[0].strip()
    if not re.match(r"^\d{1,2}:\d{2}:\d{2}$", first):
        return None
    return _to_seconds(first)


@dataclass
class DayResult:
    date: str
    total: int                             # full-day counted songs
    quota_counts: dict
    quota_pct: dict
    quota_denom: dict                      # denominator actually used per quota
    quota_names: dict

    def as_row(self, quota_ids: list) -> dict:
        row = {"date": self.date, "total": self.total}
        for qid in quota_ids:
            row[f"{qid}_count"] = self.quota_counts.get(qid, 0)
            row[f"{qid}_pct"] = round(self.quota_pct.get(qid, 0.0), 2)
        return row


def analyse_file(log_path, config: Config) -> DayResult:
    """Parse one daily log file and compute totals + per-quota stats."""
    log_path = Path(log_path)
    date = date_from_filename(log_path.name)
    text = log_path.read_text(encoding=config.encoding, errors="replace")

    excluded = set(config.exclude_path_segments)
    songs = []  # each: (sec, full_path, filename)

    for line in text.splitlines():
        if not line.strip():
            continue
        path = parse_path(line)
        if not path:
            continue  # filler (empty path) or malformed
        segments = _split_segments(path)
        if {s.lower() for s in segments} & excluded:
            continue  # StationIds / Jingles (incl. 3_Made in Belgium\Jingles)
        filename = segments[-1] if segments else ""
        songs.append((parse_time(line), path, filename))

    total = len(songs)
    counts, pct, denom = {}, {}, {}
    for q in config.quotas:
        eligible = [s for s in songs if q.in_window(s[0])]
        c = sum(1 for (_sec, p, fn) in eligible if q.matches(p, fn))
        d = len(eligible) if q.denominator == "window" else total
        counts[q.id] = c
        denom[q.id] = d
        pct[q.id] = (c / d * 100.0) if d else 0.0

    return DayResult(
        date=date,
        total=total,
        quota_counts=counts,
        quota_pct=pct,
        quota_denom=denom,
        quota_names={q.id: q.name for q in config.quotas},
    )
