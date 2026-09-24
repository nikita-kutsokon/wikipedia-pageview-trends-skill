"""Daily pageview cache with gap-only fetching.

Cache key is (project, article) ONLY — no granularity component. Monthly/
yearly rollups are computed at read time in analysis.py from this one daily
store, so narrowing, widening, or changing --granularity between calls is
always cache-compatible (there is only one storage shape to hit).

Each cache file stores two things:
  - `datapoints`: {YYYYMMDD: views} for every day the API actually returned
    a value.
  - `fetched_ranges`: [[start, end], ...] (merged, non-overlapping) recording
    every date range that has been *requested*, independent of whether the
    API returned a value for every day in it. This is what lets a day the
    API silently omits (confirmed live during planning review) be treated as
    "fetched, no data" instead of being retried forever.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import wiki_api

DATE_FMT = "%Y%m%d"


def default_cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / ".cache"


def _cache_file(project: str, article: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{project}\x00{article}".encode("utf-8")).hexdigest()[:20]
    safe_project = "".join(c if c.isalnum() else "_" for c in project)[:40]
    return cache_dir / f"{safe_project}__{digest}.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {"datapoints": {}, "fetched_ranges": []}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("datapoints", {})
    data.setdefault("fetched_ranges", [])
    return data


def _save(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f)


def _parse(d: str) -> date:
    return date(int(d[:4]), int(d[4:6]), int(d[6:8]))


def _fmt(d: date) -> str:
    return d.strftime(DATE_FMT)


def _merge_ranges(ranges: list[list[str]]) -> list[list[str]]:
    """Merge/coalesce a list of [start, end] date-string pairs (inclusive)."""
    if not ranges:
        return []
    parsed = sorted((_parse(a), _parse(b)) for a, b in ranges)
    merged: list[list[date]] = [list(parsed[0])]
    for start, end in parsed[1:]:
        last = merged[-1]
        if start <= last[1] + timedelta(days=1):
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [[_fmt(s), _fmt(e)] for s, e in merged]


def _missing_gaps(target_start: date, target_end: date, fetched_ranges: list[list[str]]) -> list[tuple[date, date]]:
    """Sub-ranges of [target_start, target_end] not covered by fetched_ranges."""
    covered = [(_parse(a), _parse(b)) for a, b in _merge_ranges(fetched_ranges)]
    covered = [(max(s, target_start), min(e, target_end)) for s, e in covered if e >= target_start and s <= target_end]
    covered.sort()

    gaps: list[tuple[date, date]] = []
    cursor = target_start
    for start, end in covered:
        if start > cursor:
            gaps.append((cursor, start - timedelta(days=1)))
        cursor = max(cursor, end + timedelta(days=1))
    if cursor <= target_end:
        gaps.append((cursor, target_end))
    return gaps


@dataclass
class SeriesResult:
    datapoints: dict[str, int]
    """{YYYYMMDD: views}, restricted to [start, end]."""
    fetched_ranges: list[list[str]]
    """Merged [start, end] pairs this key has ever had requested from the API."""
    http_requests_made: int


def get_daily_series(
    project: str,
    article: str,
    start: date,
    end: date,
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> SeriesResult:
    cache_dir = cache_dir or default_cache_dir()
    path = _cache_file(project, article, cache_dir)
    data = _load(path)

    if refresh:
        data["fetched_ranges"] = []

    gaps = _missing_gaps(start, end, data["fetched_ranges"])
    http_requests_made = 0
    for gap_start, gap_end in gaps:
        result = wiki_api.fetch_daily(project, article, gap_start, gap_end)
        http_requests_made += 1
        data["datapoints"].update(result.datapoints)
        # gap_start is safe to record even where clamped-to-floor, since the
        # floor is a fixed constant and a retry would clamp identically; the
        # end is capped at result.effective_end (not gap_end) so dates past
        # the publication-lag cutoff are NOT marked fetched and get retried
        # once they become available.
        if result.effective_end >= gap_start:
            data["fetched_ranges"].append([_fmt(gap_start), _fmt(result.effective_end)])

    data["fetched_ranges"] = _merge_ranges(data["fetched_ranges"])
    _save(path, data)

    start_str, end_str = _fmt(start), _fmt(end)
    series = {d: v for d, v in data["datapoints"].items() if start_str <= d <= end_str}
    return SeriesResult(
        datapoints=series,
        fetched_ranges=data["fetched_ranges"],
        http_requests_made=http_requests_made,
    )
