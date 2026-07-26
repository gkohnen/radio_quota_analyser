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
  * "window" -> songs played inside that same window, or
  * "day"    -> all songs played that day.

The single function classify() decides, for one line, whether it is a counted
song and which quotas it belongs to. Both the counting (analyse_file) and the
per-line audit (annotate_file) are built on it, so the annotated files can never
disagree with results.csv.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _to_seconds(hhmm: str) -> int:
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
    m = DATE_RE.search(Path(filename).name)
    if not m:
        raise ValueError(f"No YYYY-MM-DD date found in file name: {filename!r}")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _split_segments(path: str) -> list:
    return [seg for seg in re.split(r"[\\/]+", path) if seg]


def _path_field_index(fields: list):
    """Index of the path field: the one wrapped in parentheses, e.g. "(N:\\...)"
    or the empty "()" of a filler line.

    Scanned from the right so any trailing flag columns (0/1/blank) written by a
    previous annotation run are skipped, and so a parenthesised *title* earlier on
    the line is never mistaken for the path.
    """
    for i in range(len(fields) - 1, -1, -1):
        f = fields[i].strip()
        if f.startswith("(") and f.endswith(")"):
            return i
    return None


def parse_path(line: str):
    """Return the file path for a log line (or None). Tolerant of a raw 6-column
    line or an already-annotated line that has extra flag columns appended."""
    fields = line.rstrip("\r\n").split("\t")
    idx = _path_field_index(fields)
    if idx is None:
        return None
    return fields[idx].strip()[1:-1].strip()


def parse_time(line: str):
    first = line.split("\t", 1)[0].strip()
    if not re.match(r"^\d{1,2}:\d{2}:\d{2}$", first):
        return None
    return _to_seconds(first)


@dataclass
class LineEval:
    is_song: bool
    sec: int | None
    matches: dict          # qid -> bool (criteria match only, window not applied)


def classify(line: str, config: Config, excluded: set) -> LineEval:
    """Decide whether a single line is a counted song and which quotas it matches."""
    path = parse_path(line)
    if not path:
        return LineEval(False, None, {})          # filler (empty path) or malformed
    segments = _split_segments(path)
    if {s.lower() for s in segments} & excluded:
        return LineEval(False, None, {})          # StationIds / Jingles
    filename = segments[-1] if segments else ""
    sec = parse_time(line)
    return LineEval(True, sec, {q.id: q.matches(path, filename) for q in config.quotas})


def quota_flag(q: Quota, ev: LineEval) -> int:
    """1 if this counted song contributes to quota q's numerator, else 0."""
    return 1 if (ev.matches.get(q.id, False) and q.in_window(ev.sec)) else 0


@dataclass
class DayResult:
    date: str
    total: int
    quota_counts: dict
    quota_pct: dict
    quota_denom: dict
    quota_names: dict

    def as_row(self, quota_ids: list) -> dict:
        row = {"date": self.date, "total": self.total}
        for qid in quota_ids:
            row[f"{qid}_count"] = self.quota_counts.get(qid, 0)
            row[f"{qid}_pct"] = round(self.quota_pct.get(qid, 0.0), 2)
        return row


def detect_codec(raw: bytes) -> str:
    """Pick a text codec for a raw log file.

    Handles the encodings a Windows playout system realistically emits:
    UTF-16 / UTF-32 (with or without BOM), UTF-8 (with or without BOM) and
    single-byte Windows-1252. The same codec name round-trips on both decode and
    encode, so annotated duplicates stay faithful. Windows-1252 falls back to
    latin-1, which maps every byte 1:1 (a lossless superset for our purposes --
    all quota tokens are ASCII, and byte-for-byte output re-opens as 1252).
    """
    if not raw:
        return "utf-8"
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return "utf-32"                      # encode adds matching BOM
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"                      # encode adds a (LE) BOM
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"                   # encode re-adds the BOM
    # No BOM: sniff for UTF-16 by its tell-tale NUL bytes.
    sample = raw[:8192]
    if sample.count(0) > len(sample) * 0.25:
        even_nul = sample[0::2].count(0)
        odd_nul = sample[1::2].count(0)
        return "utf-16-le" if odd_nul >= even_nul else "utf-16-be"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"                     # Windows-1252 / ISO-8859-1


def read_text(log_path) -> tuple:
    """Return (decoded_text, codec_name) for a log file, auto-detecting encoding."""
    raw = Path(log_path).read_bytes()
    codec = detect_codec(raw)
    return raw.decode(codec), codec


_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")


def iter_lines(text: str):
    """Yield (body, terminator) splitting ONLY on real line terminators.

    Not str.splitlines(): that also breaks on U+0085 (the CP1252 ellipsis '…'
    seen inside song titles) and other Unicode separators, which would corrupt
    both counting and the duplicated files.
    """
    for m in _LINE_RE.finditer(text):
        body, term = m.group(1), m.group(2)
        if body == "" and term == "":
            break
        yield body, term


def analyse_file(log_path, config: Config) -> DayResult:
    """Parse one daily log file and compute totals + per-quota stats."""
    log_path = Path(log_path)
    date = date_from_filename(log_path.name)
    text, _codec = read_text(log_path)
    excluded = set(config.exclude_path_segments)

    songs = []  # list of LineEval for counted songs
    for body, _term in iter_lines(text):
        if not body.strip():
            continue
        ev = classify(body, config, excluded)
        if ev.is_song:
            songs.append(ev)

    total = len(songs)
    counts, pct, denom = {}, {}, {}
    for q in config.quotas:
        eligible = [ev for ev in songs if q.in_window(ev.sec)]
        c = sum(quota_flag(q, ev) for ev in songs)          # numerator (window-aware)
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


def annotate_file(log_path, out_path, config: Config):
    """Write a faithful duplicate of the log with one flag column per quota.

    Counted song  -> 1/0 in each quota column.
    Any other line -> blank cells, so COUNT of numeric rows == total songs and
                      SUM of a column == that quota's numerator.
    The original text and line endings are preserved and the file is re-encoded
    in its own detected encoding; only ASCII flag columns are appended. Uses the
    same iteration and classify() as analyse_file, so the file can never disagree
    with results.csv.
    """
    log_path, out_path = Path(log_path), Path(out_path)
    excluded = set(config.exclude_path_segments)
    text, codec = read_text(log_path)

    out = []
    for body, term in iter_lines(text):
        if not body.strip():
            out.append(body + term)
            continue
        fields = body.split("\t")
        idx = _path_field_index(fields)
        if idx is None:
            base = body                        # no path field; leave line intact
            ev = LineEval(False, None, {})
        else:
            base = "\t".join(fields[:idx + 1])  # drop any pre-existing flag columns
            ev = classify(base, config, excluded)
        if ev.is_song:
            flags = "\t".join(str(quota_flag(q, ev)) for q in config.quotas)
        else:
            flags = "\t".join("" for _ in config.quotas)
        out.append(base + "\t" + flags + term)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes("".join(out).encode(codec))
    return out_path
