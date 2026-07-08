# Radio playout quota analyser

Reads one playout log per day, computes music quotas, and plots them over time.

## What it produces

For each daily log it reports:

- total number of songs played that day,
- **FR** (*Morceaux sur textes en français*): count + % of total,
- **FWB** (*Morceaux FWB*): count + % of total.

It appends one row per day to `results.csv` and (re)builds an interactive
`dashboard.html` that plots each quota's daily share over time.

## What counts as a song

A log line is counted as a song only when **both** are true:

1. it has a real file path (the last field is not an empty `()`), and
2. none of its folder segments is `StationIds` or `Jingles`.

Rule 2 excludes jingles wherever they live, including `3_Made in Belgium\Jingles`,
while keeping the real songs in `3_Made in Belgium\Music`. Rule 1 automatically drops the filler entries (`Silence`, `top horaire`, `Pub`)
because they all carry an empty path. This is deliberately *not* a text match on
the word "silence": the real song `CHARLES - silence BEFR.mp3` has a path and is
correctly counted.

## Quota rules (edit `quota_config.json`, no code change)

| Quota    | Matches if path contains      | ...or file name contains | Numerator window | Target |
|----------|-------------------------------|--------------------------|------------------|-------:|
| FR       | `2_French`, `3_F sur le Plat` | `BEFR`                   | whole day        | 20%    |
| FWB      | `2_Belge`                     | `BEFR`                   | whole day        | 15%    |
| FWB_Day  | `2_Belge`                     | `BEFR`                   | 06:00–22:00      | 11.25% |

`target` draws a dotted line of the quota's own colour on the dashboard — the
level to reach. `FWB_Day` applies the same match as `FWB` but counts only songs
started between 06:00 and 22:00 in the numerator, while the denominator stays the
**full 24h total** (`"denominator": "day"`). To measure it against daytime songs
only instead, change that one field to `"denominator": "window"` (day 1 then
reads 15.5% instead of 10.4%).

A song can match several quotas at once (a `BEFR` track is French-language *and*
Belgian) — that is expected. Add a `Quota 4` later by adding another entry to the
`quotas` list; the report and dashboard pick it up automatically, target line
included.

## Audit / warrant files

Every time you `run`, the analyser also writes a byte-faithful duplicate of each
log into an `annotated/` sub-folder, with three extra tab-separated columns
appended — one per quota, in config order (`FR`, `FWB`, `FWB_Day`):

- a **counted song** gets `1` (in the quota) or `0` (not) in each column;
- any **non-song line** (filler, StationIds, Jingles) gets **blank** cells.

This lets anyone re-derive the numbers by hand — e.g. open a file in Excel and,
for any quota column, `SUM` = that quota's count (numerator) and `COUNT` of the
numeric rows = the day's total (denominator). Because the flags are produced by
the exact same `classify()` logic that fills `results.csv`, the two can never
disagree (verified: all seven sample days reconcile to the row, and stripping the
three columns reproduces the original file byte-for-byte). The original columns,
encoding (Windows-1252) and CRLF line endings are preserved untouched.

Duplicates default to an `annotated/` folder beside each log; use
`--annotated-dir PATH` to redirect them, or `--no-annotate` to skip.



- Python 3.10+ (no compiler needed).
- `pip install -r requirements.txt` (only Plotly, for the dashboard).

## Usage

Analyse one day and refresh the dashboard:

```bash
python quotas.py run 2026-07-08.log --dashboard
```

Back-fill several days at once:

```bash
python quotas.py run "logs/2026-07-*.log" --dashboard
```

Rebuild the dashboard only:

```bash
python quotas.py dashboard
```

Open `dashboard.html` in any browser. `results.csv` is the running history — keep
it (e.g. in version control) so the chart grows one point per day.

## Scheduling

**Windows playout machine (Task Scheduler)** — create a Basic Task, "Daily",
action *Start a program*:

- Program: `python`
- Arguments: `C:\radio-quotas\quotas.py run "C:\logs\%date%.log" --dashboard`
  (adjust the path/date token to however the log filename is built)

No install/build step on the machine beyond Python + `pip install plotly`.

**Cloud (GitHub Actions)** — `.github/workflows/daily-quota.yml` runs daily,
processes everything in `logs/`, and commits the updated `results.csv` and
`dashboard.html`. Push the repo, drop each day's log into `logs/`, done. Enable
GitHub Pages to serve `dashboard.html` at a URL for the team.
