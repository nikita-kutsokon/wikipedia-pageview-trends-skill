"""Wikimedia Pageviews client + Wikidata-based cross-language topic resolution.

All endpoint/parameter choices here were verified live (2026-09-24) against the
real Wikimedia and Wikidata APIs during planning review — see
.omc/plans/ralplan-wikipedia-trends-skill.md for the curl transcripts. In
particular: pageviews are always fetched at daily granularity (never
"monthly" — Wikimedia's own monthly bucket for an in-progress month returns a
near-meaningless low value with no flag), and topic resolution goes through
Wikidata sitelinks rather than naive cross-language search (searching an
English phrase directly against e.g. pl.wikipedia's search index returns
unrelated articles, confirmed live).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote

import requests

USER_AGENT = (
    "wikitrends/0.1 (Genesis case-study Agent Skill; "
    "https://github.com/nikita-kutsokon/wikipedia-pageview-trends-skill)"
)

PAGEVIEWS_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
DATA_FLOOR = date(2015, 7, 1)
PUBLICATION_LAG_DAYS = 2

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = USER_AGENT

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0


class WikiApiError(Exception):
    """Base error for any topic/article resolution or fetch failure."""


class DisambiguationError(WikiApiError):
    """Raised when a topic resolves to a Wikipedia disambiguation page."""

    def __init__(self, topic: str, anchor_title: str, candidates: list[str]):
        self.topic = topic
        self.anchor_title = anchor_title
        self.candidates = candidates
        msg = (
            f"topic {topic!r} resolves to a Wikipedia disambiguation page "
            f"({anchor_title!r}), not a single concept — retry with a more "
            f"specific --topic or supply --article lang:Title directly."
        )
        if candidates:
            msg += f" Candidates: {', '.join(candidates[:10])}"
        super().__init__(msg)


def _title_to_url_segment(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="_")


def _get_with_retry(url: str, params: dict) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = _SESSION.get(url, params=params, timeout=15)
        except requests.RequestException as exc:  # network-level failure
            last_exc = exc
            time.sleep(_RETRY_BASE_DELAY * (2**attempt))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else _RETRY_BASE_DELAY * (2**attempt)
            time.sleep(delay)
            last_exc = WikiApiError(f"HTTP {resp.status_code} from {url}")
            continue
        return resp
    raise WikiApiError(f"request to {url} failed after {_RETRY_ATTEMPTS} attempts") from last_exc


@dataclass
class ResolvedTopic:
    anchor_lang: str
    anchor_title: str
    qid: str | None
    titles: dict[str, str | None] = field(default_factory=dict)
    """lang -> resolved title, or None if that language has no sitelink."""
    warning: str | None = None


def _search_anchor(topic: str, lang: str) -> str | None:
    resp = _get_with_retry(
        f"https://{lang}.wikipedia.org/w/api.php",
        {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srlimit": 1,
            "format": "json",
        },
    )
    data = resp.json()
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def resolve_topic(
    topic: str,
    langs: list[str],
    from_lang: str = "en",
    overrides: dict[str, str] | None = None,
) -> ResolvedTopic:
    """Resolve free-text `topic` to per-language article titles.

    Anchor project defaults to `from_lang` (default "en"); if that search
    misses, falls back to the first entry of `langs`. The anchor title is
    then checked for disambiguation (hard-fails loudly if so) and its
    Wikidata sitelinks are read to get every requested language's title.
    `overrides` (from --article lang:Title) take precedence per-language.
    """
    overrides = overrides or {}

    anchor_lang = from_lang
    anchor_title = _search_anchor(topic, anchor_lang)
    if anchor_title is None and langs:
        anchor_lang = langs[0]
        anchor_title = _search_anchor(topic, anchor_lang)
    if anchor_title is None:
        raise WikiApiError(f"no article found for topic {topic!r} in any anchor project tried")

    resp = _get_with_retry(
        f"https://{anchor_lang}.wikipedia.org/w/api.php",
        {
            "action": "query",
            "titles": anchor_title,
            "prop": "pageprops",
            "redirects": 1,
            "format": "json",
        },
    )
    pages = resp.json().get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    if "missing" in page:
        raise WikiApiError(f"anchor article {anchor_title!r} not found on {anchor_lang}.wikipedia")

    pageprops = page.get("pageprops", {})
    if "disambiguation" in pageprops:
        candidates = _search_candidates(topic, anchor_lang)
        raise DisambiguationError(topic, anchor_title, candidates)

    qid = pageprops.get("wikibase_item")
    titles: dict[str, str | None] = {lang: None for lang in langs}
    warning = None

    if qid:
        resp = _get_with_retry(
            "https://www.wikidata.org/w/api.php",
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "sitelinks",
                "format": "json",
            },
        )
        entity = resp.json().get("entities", {}).get(qid, {})
        sitelinks = entity.get("sitelinks", {})
        for lang in langs:
            site_key = f"{lang}wiki"
            if site_key in sitelinks:
                titles[lang] = sitelinks[site_key]["title"]
        if len(langs) > 1 and len(sitelinks) <= 2:
            warning = (
                "suspiciously few language editions for this concept "
                f"({len(sitelinks)} total sitelinks) — verify --topic resolved "
                "to what you intended"
            )
    else:
        # No Wikidata item at all: only the anchor language is known.
        titles[anchor_lang] = anchor_title

    for lang, override_title in overrides.items():
        titles[lang] = override_title

    return ResolvedTopic(anchor_lang=anchor_lang, anchor_title=anchor_title, qid=qid, titles=titles, warning=warning)


def _search_candidates(topic: str, lang: str) -> list[str]:
    try:
        resp = _get_with_retry(
            f"https://{lang}.wikipedia.org/w/api.php",
            {
                "action": "query",
                "list": "search",
                "srsearch": topic,
                "srlimit": 10,
                "format": "json",
            },
        )
        hits = resp.json().get("query", {}).get("search", [])
        return [h["title"] for h in hits]
    except WikiApiError:
        return []


def fetch_cutoff() -> date:
    """Latest date the API is ever requested up to (publication lag)."""
    return date.today() - timedelta(days=PUBLICATION_LAG_DAYS)


@dataclass
class FetchResult:
    datapoints: dict[str, int]
    effective_start: date
    effective_end: date
    clamped_to_floor: bool


def fetch_daily(project: str, article: str, start: date, end: date) -> FetchResult:
    """Fetch daily pageviews for [start, end] (inclusive), always granularity=daily.

    Clamps `start` to the documented data floor (2015-07-01, confirmed live:
    requests entirely before this date return HTTP 404) and `end` to
    `fetch_cutoff()` (publication lag). Returns datapoints for every day the
    API actually returned a value — callers (cache.py) are responsible for
    tracking which *ranges* were requested, since the API silently omits
    some days even within a valid range (confirmed live).
    """
    cutoff = fetch_cutoff()
    effective_end = min(end, cutoff)
    clamped = start < DATA_FLOOR
    effective_start = max(start, DATA_FLOOR)
    if effective_start > effective_end:
        return FetchResult({}, effective_start, effective_end, clamped)

    segment = _title_to_url_segment(article)
    url = (
        f"{PAGEVIEWS_BASE}/{project}/all-access/user/{segment}/daily/"
        f"{effective_start.strftime('%Y%m%d')}/{effective_end.strftime('%Y%m%d')}"
    )
    resp = _get_with_retry(url, {})
    if resp.status_code == 404:
        # Either no data in this window, or (rarely) the article title is
        # wrong. Article existence is established by resolve_topic()/the
        # pageprops call, not here — a 404 at this layer always means "no
        # pageview data in this window", never "article not found".
        return FetchResult({}, effective_start, effective_end, clamped)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    datapoints: dict[str, int] = {}
    for item in items:
        # timestamp is YYYYMMDDHH with HH="00" at daily granularity.
        day = item["timestamp"][:8]
        datapoints[day] = item["views"]
    return FetchResult(datapoints, effective_start, effective_end, clamped)
