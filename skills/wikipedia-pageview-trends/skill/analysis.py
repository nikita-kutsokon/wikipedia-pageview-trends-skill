"""Growth metric, confidence heuristic, seasonality check, regression stats.

Design decision made explicit here (this resolves an ambiguity the planning
review flagged — "CV" and "growth_pct" must share one definition of what a
"period" is, or two implementers get different labels for the same data):
growth, confidence, and seasonality are ALWAYS computed on self-rolled-up
COMPLETE CALENDAR MONTHS, regardless of what --granularity the report chart
displays. --granularity only changes what report.py plots; it never changes
what analysis.py measures. This is what makes the completeness rule, CV, and
the confidence label well-defined with exactly one meaning each.

A calendar month counts as "complete" only if (per the planning review's
three-conjunct rule):
  (a) both its first and last day lie within the *currently requested*
      [start, end] — not merely within whatever was ever fetched,
  (b) every day in it is covered by `fetched_ranges`, and
  (c) its last day is on or before the fetch cutoff (today - 2 days).
An in-progress or never-fully-fetched month is excluded from the series
entirely rather than included with a partial sum.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from .wiki_api import fetch_cutoff

MIN_SPAN_DAYS = 180
MIN_COVERAGE = 0.90
MIN_MEAN_VIEWS = 100  # views/month; analysis always operates on monthly rollups (see module docstring)
CV_HIGH_CEILING = 0.40
CV_MEDIUM_CEILING = 0.80
OUTLIER_MODZ = 3.5
OUTLIER_MEDIUM_MAX = 1
SEASONALITY_RATIO = 1.5

LABELS = ["Low", "Medium", "High"]


def years_to_range(years: float, today: date | None = None) -> tuple[date, date]:
    """--years N -> (start, end), month-aligned so the leading edge of the
    series is stable regardless of what day of the month `today` is.
    start = first day of the month N years before the month containing
    (today - 2 days, the fetch cutoff). end = the fetch cutoff itself.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=2)
    anchor_month = date(cutoff.year, cutoff.month, 1)
    start = date(anchor_month.year - int(years), anchor_month.month, 1)
    return start, cutoff


@dataclass
class MonthPoint:
    month_start: date
    views: int


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _day_covered(fetched_ranges: list[list[str]], day: date) -> bool:
    ds = day.strftime("%Y%m%d")
    for a, b in fetched_ranges:
        if a <= ds <= b:
            return True
    return False


def complete_months(
    datapoints: dict[str, int],
    fetched_ranges: list[list[str]],
    start: date,
    end: date,
    today: date | None = None,
) -> list[MonthPoint]:
    cutoff = (today or date.today()) - timedelta(days=2)
    months: list[MonthPoint] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = _next_month(cur)
        month_last = nxt - timedelta(days=1)

        # (a) both first and last day within the currently requested range
        if cur < start or month_last > end:
            cur = nxt
            continue
        # (c) month must have fully elapsed under the publication-lag cutoff
        if month_last > cutoff:
            cur = nxt
            continue
        # (b) every day in the month must be covered by fetched_ranges
        d = cur
        covered_all = True
        total = 0
        while d <= month_last:
            if not _day_covered(fetched_ranges, d):
                covered_all = False
                break
            total += datapoints.get(d.strftime("%Y%m%d"), 0)
            d += timedelta(days=1)
        if covered_all:
            months.append(MonthPoint(month_start=cur, views=total))
        cur = nxt
    return months


def coverage_ratio(datapoints: dict[str, int], start: date, end: date) -> float:
    total_days = (end - start).days + 1
    if total_days <= 0:
        return 0.0
    present = 0
    d = start
    while d <= end:
        if d.strftime("%Y%m%d") in datapoints:
            present += 1
        d += timedelta(days=1)
    return present / total_days


@dataclass
class GrowthResult:
    pct: float | None
    absolute: float | None
    reason_if_null: str | None


def compute_growth(months: list[MonthPoint]) -> GrowthResult:
    n = len(months)
    if n < 2:
        return GrowthResult(None, None, "not enough complete periods to compute a trend")
    k = min(3, n // 2)
    first_mean = statistics.mean(m.views for m in months[:k])
    last_mean = statistics.mean(m.views for m in months[-k:])
    if first_mean == 0:
        return GrowthResult(None, last_mean - first_mean, "cannot compute percentage growth from a zero baseline")
    pct = (last_mean - first_mean) / first_mean * 100
    return GrowthResult(pct, last_mean - first_mean, None)


def compute_cagr(months: list[MonthPoint]) -> float | None:
    n = len(months)
    if n < 12:
        return None
    k = min(3, n // 2)
    first_mean = statistics.mean(m.views for m in months[:k])
    last_mean = statistics.mean(m.views for m in months[-k:])
    if first_mean < 1:
        return None
    years_span = n / 12
    return ((last_mean / first_mean) ** (1 / years_span) - 1) * 100


def compute_yoy_growth(months: list[MonthPoint]) -> float | None:
    if len(months) < 24:
        return None
    last12 = statistics.mean(m.views for m in months[-12:])
    prior12 = statistics.mean(m.views for m in months[-24:-12])
    if prior12 == 0:
        return None
    return (last12 - prior12) / prior12 * 100


def _modified_z_outliers(values: list[float]) -> list[int]:
    if len(values) < 2:
        return []
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad == 0:
        return []
    outliers = []
    for i, v in enumerate(values):
        mz = 0.6745 * (v - med) / mad
        if abs(mz) > OUTLIER_MODZ:
            outliers.append(i)
    return outliers


def detect_seasonality(months: list[MonthPoint]) -> tuple[bool, float | None]:
    """Detrend (log1p-linear fit, always-positive denominator) before
    computing the calendar-month-group ratio — an earlier draft's raw
    (non-detrended) ratio test false-fired on a pure decline with zero
    seasonality; this fixes that by dividing out the trend first.
    """
    if len(months) < 24:
        return False, None
    values = [m.views for m in months]
    idx = np.arange(len(values), dtype=float)
    log_v = np.log1p(np.array(values, dtype=float))
    slope, intercept = np.polyfit(idx, log_v, 1)
    fitted = np.exp(slope * idx + intercept)  # always > 0
    residuals = (np.array(values, dtype=float) + 1) / fitted

    groups: dict[int, list[float]] = {}
    for m, r in zip(months, residuals):
        groups.setdefault(m.month_start.month, []).append(r)
    group_means = {k: statistics.mean(v) for k, v in groups.items()}
    if len(group_means) < 2:
        return False, None
    ratio = float(max(group_means.values()) / min(group_means.values()))
    return bool(ratio > SEASONALITY_RATIO), ratio


@dataclass
class RegressionResult:
    slope: float | None
    r_squared: float | None
    ci_low: float | None
    ci_high: float | None


def compute_regression(months: list[MonthPoint]) -> RegressionResult:
    n = len(months)
    if n < 3:
        return RegressionResult(None, None, None, None)
    x = np.arange(n, dtype=float)
    y = np.array([m.views for m in months], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    dof = n - 2
    if dof > 0 and ss_tot > 0:
        mse = ss_res / dof
        x_mean = x.mean()
        sxx = float(np.sum((x - x_mean) ** 2))
        se_slope = math.sqrt(mse / sxx) if sxx > 0 else 0.0
        t_approx = 2.0  # rough 95% CI per plan (spec: secondary/--explain-only detail)
        ci_low = slope - t_approx * se_slope
        ci_high = slope + t_approx * se_slope
    else:
        ci_low = ci_high = slope

    return RegressionResult(float(slope), float(r_squared), float(ci_low), float(ci_high))


@dataclass
class ConfidenceResult:
    label: str
    reasons: list[str] = field(default_factory=list)
    seasonal: bool = False
    seasonality_ratio: float | None = None


def compute_confidence(months: list[MonthPoint], coverage: float, span_days: int) -> ConfidenceResult:
    level = 2  # High
    reasons: list[str] = []

    if span_days < MIN_SPAN_DAYS:
        level = 0
        reasons.append(f"only {span_days} days of history — too short to call a trend")

    mean_views = statistics.mean(m.views for m in months) if months else 0.0
    if mean_views < MIN_MEAN_VIEWS:
        level = 0
        reasons.append(f"only {mean_views:.0f} views/month on average — too few to distinguish signal from noise")

    if coverage < MIN_COVERAGE:
        level = max(0, level - 1)
        reasons.append(f"{coverage * 100:.0f}% of expected days present — gaps reduce confidence")

    values = [float(m.views) for m in months]
    cv = 0.0
    if len(values) >= 2 and statistics.mean(values) > 0:
        cv = statistics.pstdev(values) / statistics.mean(values)
    if cv > CV_MEDIUM_CEILING:
        level = max(0, level - 2)
        reasons.append(f"high month-to-month variance (CV={cv:.2f})")
    elif cv > CV_HIGH_CEILING:
        level = max(0, level - 1)
        reasons.append(f"moderate month-to-month variance (CV={cv:.2f})")

    outliers = _modified_z_outliers(values)
    if len(outliers) > OUTLIER_MEDIUM_MAX:
        level = max(0, level - 1)
        reasons.append(f"{len(outliers)} anomalous period(s) detected")

    seasonal, ratio = detect_seasonality(months)
    if seasonal:
        level = max(0, level - 1)
        reasons.append(
            f"strong seasonal pattern detected (detrended month-of-year ratio {ratio:.2f}) — "
            "raw growth may partly reflect calendar timing rather than a real trend; "
            "see the year-over-year figure"
        )

    if not reasons:
        reasons.append("sufficient history, low variance, no anomalies or seasonal confound detected")

    return ConfidenceResult(label=LABELS[level], reasons=reasons, seasonal=seasonal, seasonality_ratio=ratio)


@dataclass
class SeriesAnalysis:
    span_days: int
    n_complete_periods: int
    coverage: float
    mean_views: float
    growth: GrowthResult
    cagr_pct: float | None
    yoy_growth_pct: float | None
    regression: RegressionResult
    confidence: ConfidenceResult
    volume_note: str | None


def analyze_series(
    datapoints: dict[str, int],
    fetched_ranges: list[list[str]],
    start: date,
    end: date,
) -> SeriesAnalysis:
    months = complete_months(datapoints, fetched_ranges, start, end)
    coverage = coverage_ratio(datapoints, start, end)
    span_days = (end - start).days + 1
    mean_views = statistics.mean(m.views for m in months) if months else 0.0

    growth = compute_growth(months)
    cagr = compute_cagr(months)
    yoy = compute_yoy_growth(months)
    regression = compute_regression(months)
    confidence = compute_confidence(months, coverage, span_days)

    volume_note = None
    if months and mean_views < MIN_MEAN_VIEWS:
        volume_note = f"average {mean_views:.0f} views/month is below the {MIN_MEAN_VIEWS}/month reliability floor"

    return SeriesAnalysis(
        span_days=span_days,
        n_complete_periods=len(months),
        coverage=coverage,
        mean_views=mean_views,
        growth=growth,
        cagr_pct=cagr,
        yoy_growth_pct=yoy,
        regression=regression,
        confidence=confidence,
        volume_note=volume_note,
    )
