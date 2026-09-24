import math
from datetime import date, timedelta

from skill import analysis


def _daily_series(start: date, end: date, monthly_views: dict[tuple[int, int], int]) -> dict[str, int]:
    """Build a synthetic daily datapoints dict where every day in a given
    (year, month) gets monthly_views[(year, month)] // days_in_month views.
    """
    out = {}
    d = start
    while d <= end:
        key = (d.year, d.month)
        if key in monthly_views:
            days_in_month = 28 if d.month == 2 else 30
            out[d.strftime("%Y%m%d")] = monthly_views[key] // days_in_month
        d += timedelta(days=1)
    return out


def _full_coverage(start: date, end: date) -> list[list[str]]:
    return [[start.strftime("%Y%m%d"), end.strftime("%Y%m%d")]]


def test_years_to_range_is_month_aligned():
    today = date(2026, 9, 24)
    start, end = analysis.years_to_range(2, today=today)
    assert end == date(2026, 9, 22)  # today - 2 (publication lag)
    assert start == date(2024, 9, 1)  # first day of the month, 2 years before


def test_frozen_clock_determinism_within_same_month():
    # A month is only "complete" once its last day <= today - 2. Freezing
    # `today` at two points that don't cross a month-completion boundary
    # must yield a bit-identical complete-months list.
    start = date(2024, 1, 1)
    end_a = date(2024, 10, 1)  # today=2024-10-03 -> cutoff=2024-10-01
    datapoints = _daily_series(start, date(2024, 10, 31), {(2024, m): 1000 for m in range(1, 11)})
    fetched = _full_coverage(start, date(2024, 10, 31))

    months_a = analysis.complete_months(datapoints, fetched, start, end_a, today=date(2024, 10, 3))
    months_b = analysis.complete_months(datapoints, fetched, start, end_a, today=date(2024, 10, 4))
    assert [(m.month_start, m.views) for m in months_a] == [(m.month_start, m.views) for m in months_b]
    assert months_a[-1].month_start == date(2024, 9, 1)  # September complete, October excluded (in progress)


def test_completeness_ignores_cache_history_outside_requested_range():
    # The bug the planning review's round 3 caught: an earlier draft let a
    # WIDER prior fetch make a month look "complete" even when the CURRENT
    # request is narrower. Completeness must be scoped to [start, end] as
    # actually requested this call, not to fetch history.
    wide_start, wide_end = date(2023, 1, 1), date(2025, 12, 31)
    narrow_start, narrow_end = date(2024, 1, 1), date(2024, 12, 31)
    datapoints = _daily_series(wide_start, wide_end, {(y, m): 1000 for y in (2023, 2024, 2025) for m in range(1, 13)})
    fetched = _full_coverage(wide_start, wide_end)

    months_cold = analysis.complete_months(datapoints, _full_coverage(narrow_start, narrow_end), narrow_start, narrow_end, today=date(2026, 1, 5))
    months_warm = analysis.complete_months(datapoints, fetched, narrow_start, narrow_end, today=date(2026, 1, 5))
    assert [(m.month_start, m.views) for m in months_cold] == [(m.month_start, m.views) for m in months_warm]
    assert len(months_warm) == 12  # exactly the 12 months of 2024, nothing from 2023/2025


def test_growth_null_below_two_periods():
    g = analysis.compute_growth([analysis.MonthPoint(date(2024, 1, 1), 100)])
    assert g.pct is None
    assert g.reason_if_null == "not enough complete periods to compute a trend"

    g0 = analysis.compute_growth([])
    assert g0.pct is None


def test_growth_null_on_zero_baseline():
    months = [analysis.MonthPoint(date(2024, 1, 1), 0), analysis.MonthPoint(date(2024, 2, 1), 50)]
    g = analysis.compute_growth(months)
    assert g.pct is None
    assert g.reason_if_null == "cannot compute percentage growth from a zero baseline"
    assert g.absolute == 50


def test_seasonality_does_not_fire_on_a_pure_non_seasonal_decline():
    # Regression fixture for the round-3 finding: a raw (non-detrended)
    # month-of-year ratio test false-fired on a pure decline. Construct a
    # 24-month, zero-seasonality exponential decline (~80% total decline,
    # well past where the old bug fired at ~65%) and assert it stays quiet.
    months = []
    views = 10000.0
    start = date(2024, 1, 1)
    for i in range(24):
        y, m = divmod(start.month - 1 + i, 12)
        months.append(analysis.MonthPoint(date(start.year + y, m + 1, 1), int(views)))
        views *= 0.935  # ~80% total decline over 24 months, no seasonal signal
    seasonal, ratio = analysis.detect_seasonality(months)
    assert seasonal is False, f"false seasonality fire, ratio={ratio}"


def test_seasonality_fires_on_a_real_seasonal_pattern():
    # January spike every year, flat otherwise — genuine seasonality.
    months = []
    for year in (2024, 2025):
        for m in range(1, 13):
            views = 5000 if m == 1 else 1000
            months.append(analysis.MonthPoint(date(year, m, 1), views))
    seasonal, ratio = analysis.detect_seasonality(months)
    assert seasonal is True
    assert ratio > analysis.SEASONALITY_RATIO


def test_confidence_labels_reach_low_medium_high():
    # Low: short span, real shape observed live (uk astronomy, 2026-09-24 review).
    short_months = [analysis.MonthPoint(date(2026, 1, 1) + timedelta(days=31 * i), 300) for i in range(3)]
    low = analysis.compute_confidence(short_months, coverage=1.0, span_days=90)
    assert low.label == "Low"
    assert low.reasons

    # High: 24 clean, high-volume, low-variance months (observed live: the
    # "English language" comparison query, all five languages landed High).
    high_months = [analysis.MonthPoint(date(2024, 1, 1) + timedelta(days=31 * i), 20000 + (i % 3) * 50) for i in range(24)]
    high = analysis.compute_confidence(high_months, coverage=1.0, span_days=730)
    assert high.label == "High"

    # Medium is explicitly a SYNTHETIC fixture: empirically, the real demo
    # series pulled during planning review are bimodal (Low or High) under
    # these thresholds — no real series naturally lands Medium, and random
    # noise strong enough to raise CV past CV_HIGH_CEILING also tends to
    # trip the (small-sample-noisy, at n<24 groups) seasonality test or the
    # outlier test simultaneously, landing Low instead. This fixed sequence
    # (16 months, <24 so the seasonality check doesn't apply at all) was
    # found to trip exactly the outlier downgrade and nothing else.
    medium_values = [7248, 6329, 3936, 4516, 3418, 6745, 5429, 6784, 4313, 3726, 6963, 7917, 7189, 6933, 7000, 6569]
    medium_months = [
        analysis.MonthPoint(date(2024, 1, 1) + timedelta(days=31 * i), v) for i, v in enumerate(medium_values)
    ]
    medium = analysis.compute_confidence(medium_months, coverage=1.0, span_days=16 * 30)
    assert medium.label == "Medium", f"expected Medium, got {medium.label} ({medium.reasons})"


def test_regression_stats_present_for_explain_json():
    months = [analysis.MonthPoint(date(2024, 1, 1) + timedelta(days=31 * i), 1000 + i * 10) for i in range(12)]
    r = analysis.compute_regression(months)
    assert r.slope is not None
    assert r.r_squared is not None
    assert r.ci_low is not None and r.ci_high is not None
    assert r.ci_low <= r.slope <= r.ci_high
