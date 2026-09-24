from datetime import date

import responses

from skill import wiki_api


@responses.activate
def test_fetch_daily_url_construction_and_clamping():
    responses.add(
        responses.GET,
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        "cs.wikipedia/all-access/user/Test_Article/daily/20150701/20150710",
        json={"items": [{"timestamp": "2015070100", "views": 5}]},
        status=200,
    )
    result = wiki_api.fetch_daily("cs.wikipedia", "Test Article", date(2015, 6, 1), date(2015, 7, 10))
    assert result.clamped_to_floor is True
    assert result.effective_start == date(2015, 7, 1)
    assert result.datapoints == {"20150701": 5}


@responses.activate
def test_fetch_daily_title_encoding_non_ascii():
    responses.add(
        responses.GET,
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        "uk.wikipedia/all-access/user/%D0%A2%D0%B5%D1%81%D1%82/daily/20230101/20230102",
        json={"items": []},
        status=200,
    )
    result = wiki_api.fetch_daily("uk.wikipedia", "Тест", date(2023, 1, 1), date(2023, 1, 2))
    assert result.datapoints == {}


@responses.activate
def test_fetch_daily_404_means_no_data_not_missing_article():
    responses.add(
        responses.GET,
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        "cs.wikipedia/all-access/user/X/daily/20230101/20230102",
        json={"detail": "no data"},
        status=404,
    )
    result = wiki_api.fetch_daily("cs.wikipedia", "X", date(2023, 1, 1), date(2023, 1, 2))
    assert result.datapoints == {}


@responses.activate
def test_resolve_topic_disambiguation_hard_fails():
    responses.add(
        responses.GET,
        "https://en.wikipedia.org/w/api.php",
        json={"query": {"search": [{"title": "Learning English"}]}},
        status=200,
        match=[responses.matchers.query_param_matcher({
            "action": "query", "list": "search", "srsearch": "learning English",
            "srlimit": "1", "format": "json",
        })],
    )
    responses.add(
        responses.GET,
        "https://en.wikipedia.org/w/api.php",
        json={
            "query": {
                "pages": {
                    "123": {
                        "title": "Learning English",
                        "pageprops": {"disambiguation": ""},
                    }
                }
            }
        },
        status=200,
        match=[responses.matchers.query_param_matcher({
            "action": "query", "titles": "Learning English", "prop": "pageprops",
            "redirects": "1", "format": "json",
        })],
    )
    responses.add(
        responses.GET,
        "https://en.wikipedia.org/w/api.php",
        json={"query": {"search": [{"title": "Learning English"}, {"title": "BBC Learning English"}]}},
        status=200,
    )

    try:
        wiki_api.resolve_topic("learning English", ["de", "fr"], from_lang="en")
        assert False, "expected DisambiguationError"
    except wiki_api.DisambiguationError as exc:
        assert "disambiguation" in str(exc)
        assert exc.candidates  # candidate list populated, not a dead end


@responses.activate
def test_resolve_topic_anchor_fallback_to_first_lang():
    # English search misses entirely.
    responses.add(
        responses.GET,
        "https://en.wikipedia.org/w/api.php",
        json={"query": {"search": []}},
        status=200,
    )
    # Falls back to the first requested language.
    responses.add(
        responses.GET,
        "https://pl.wikipedia.org/w/api.php",
        json={"query": {"search": [{"title": "Test PL"}]}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://pl.wikipedia.org/w/api.php",
        json={"query": {"pages": {"1": {"title": "Test PL", "pageprops": {}}}}},
        status=200,
    )
    resolved = wiki_api.resolve_topic("some very specific polish-only topic", ["pl"], from_lang="en")
    assert resolved.anchor_lang == "pl"
    assert resolved.anchor_title == "Test PL"
