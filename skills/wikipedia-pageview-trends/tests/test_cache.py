from datetime import date, timedelta
from unittest.mock import patch

from skill import cache


def _fake_fetch_factory(response_by_range, calls):
    def _fake_fetch(project, article, start, end):
        calls.append((start, end))
        from skill.wiki_api import FetchResult

        data = response_by_range.get((start, end), {})
        return FetchResult(datapoints=data, effective_start=start, effective_end=end, clamped_to_floor=False)

    return _fake_fetch


def test_narrowing_via_granularity_auto_issues_zero_http_calls(tmp_path):
    calls = []
    wide_start, wide_end = date(2024, 1, 1), date(2024, 12, 31)
    fake = _fake_fetch_factory({(wide_start, wide_end): {"20240601": 10}}, calls)

    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        cache.get_daily_series("en.wikipedia", "X", wide_start, wide_end, cache_dir=tmp_path)
    assert len(calls) == 1

    # Narrower range, fully inside the already-fetched window — must be a
    # pure cache hit regardless of what --granularity the CLI resolved to
    # (fetch/cache never depend on granularity; see cache.py module docstring).
    narrow_start, narrow_end = date(2024, 3, 1), date(2024, 9, 30)
    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        result = cache.get_daily_series("en.wikipedia", "X", narrow_start, narrow_end, cache_dir=tmp_path)
    assert result.http_requests_made == 0
    assert len(calls) == 1  # unchanged


def test_missing_day_inside_fetched_range_is_not_retried(tmp_path):
    calls = []
    start, end = date(2024, 1, 1), date(2024, 1, 31)
    # API silently omits 2024-01-15, as confirmed happens live during planning review.
    data = {f"202401{d:02d}": 1 for d in range(1, 32) if d != 15}
    fake = _fake_fetch_factory({(start, end): data}, calls)

    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        r1 = cache.get_daily_series("en.wikipedia", "X", start, end, cache_dir=tmp_path)
    assert "20240115" not in r1.datapoints
    assert len(calls) == 1

    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        r2 = cache.get_daily_series("en.wikipedia", "X", start, end, cache_dir=tmp_path)
    assert r2.http_requests_made == 0  # the missing day is not re-requested forever
    assert len(calls) == 1


def test_widening_only_fetches_the_new_delta(tmp_path):
    calls = []
    a_start, a_end = date(2024, 3, 1), date(2024, 6, 30)
    b_range_start, b_range_end = date(2024, 1, 1), date(2024, 12, 31)

    def fake(project, article, start, end):
        calls.append((start, end))
        from skill.wiki_api import FetchResult

        return FetchResult(datapoints={}, effective_start=start, effective_end=end, clamped_to_floor=False)

    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        cache.get_daily_series("en.wikipedia", "X", a_start, a_end, cache_dir=tmp_path)
    assert calls == [(a_start, a_end)]

    with patch("skill.cache.wiki_api.fetch_daily", side_effect=fake):
        cache.get_daily_series("en.wikipedia", "X", b_range_start, b_range_end, cache_dir=tmp_path)
    # Only the two uncovered gaps (before and after the first fetch) are requested.
    assert len(calls) == 3
    assert calls[1] == (b_range_start, a_start - timedelta(days=1))
    assert calls[2] == (a_end + timedelta(days=1), b_range_end)
