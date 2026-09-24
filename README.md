# Wikipedia Pageview Trends — an Agent Skill

A self-contained [Agent Skill](https://agentskills.io/specification) that lets a tool-using LLM agent — including a cheap one (Claude Haiku 4.5, Gemini Flash) — analyze [Wikimedia pageview data](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html) to help B2C product teams decide which topics or language markets to invest in.

The skill itself lives in [`skills/wikipedia-pageview-trends/`](skills/wikipedia-pageview-trends/) — that directory is the actual deliverable (`SKILL.md` + Python code). This README is the case-study writeup: what it does, why it's built this way, how it was verified, and how AI tools were used to build it.

## Quick start

```bash
python3.12 -m venv .venv   # tested on 3.12; any Python >=3.11 (incl. 3.14) works
source .venv/bin/activate
pip install -e "skills/wikipedia-pageview-trends[dev]"

wikitrends --topic "intermittent fasting" --langs pl,cs --years 2
wikitrends --topic astronomy --langs uk --years 2 --explain
wikitrends --topic "English language" --langs de,fr,es,pl,ja --years 2
```

Each call prints resolved article titles, a growth figure with a Low/Medium/High confidence label and reasons, and writes a chart (PNG) + a one-page PDF report. Run `pytest --disable-socket skills/wikipedia-pageview-trends/tests/` to run the unit suite (no live network calls).

## What it does

Given a free-text topic and a set of language editions, the skill:

1. **Resolves** the topic to the correct Wikipedia article in each language — via Wikidata sitelinks, not naive per-language search (a naive search for an English phrase against e.g. Polish Wikipedia's search index returns unrelated articles; confirmed empirically during development).
2. **Fetches** real daily pageview history from the Wikimedia Pageviews API, cached locally so follow-up queries (add a language, narrow/widen the range) never re-fetch what's already known.
3. **Analyzes** growth and computes an explicit, reasoned confidence label (not just a number) — because the task's own example question is literally "how much can we trust this growth?"
4. **Reports**: a chart (PNG) and a one-page PDF combining the chart, key stats, the confidence label with its inputs, and stated limitations.

## Why it's built this way

Full RALPLAN-DR planning artifacts (principles, decision drivers, alternatives considered and rejected, and three rounds of adversarial Architect+Critic review) are in [`.omc/`](.omc/) locally during development — **not published** in this repo (see [AI-assisted development](#ai-assisted-development-and-how-it-was-verified) below for why and what that process caught). The decisions that matter for a reader of this code:

- **One flat CLI command, no subcommands.** A cheap tool-calling model has to make zero "which verb do I call" decisions and zero "how do I thread a file path between two calls" decisions — see the multi-option architecture comparison this decision came from in the design notes below.
- **Always fetch daily, never trust Wikimedia's own "monthly" granularity.** Verified live during development: Wikimedia's monthly bucket for an in-progress month returns a near-meaningless partial value with no flag distinguishing it from a real month. The skill always fetches daily data and rolls up complete calendar months itself, with an explicit three-part completeness rule (a month counts only if every one of its days is cached, its last day has fully elapsed past the publication lag, and it lies within the currently-requested range — not just "whatever was ever fetched"). This was the single most consequential correctness bug found during development: an earlier version of the read logic checked the wrong window and could silently sum a partial month as if it were complete, fabricating a headline growth number that swung by double digits of percentage points depending on cache history alone. Fixed by scoping the completeness check to the exact range being requested.
- **Cache key is `(project, article)` only** — no granularity component — so narrowing, widening, or changing the display granularity between calls never invalidates the cache. A separate "fetched ranges" ledger (distinct from "which days have data") means a day Wikimedia's API silently omits (this happens; confirmed live) is remembered as "checked, no data" instead of being re-requested forever.
- **Confidence is a fixed, named-threshold rule, not a black box.** Span, data volume, coverage, variance (CV), anomaly count (median/MAD-based, robust to small samples), and a seasonality check (month-of-year effect, computed on a **detrended** series so a plain decline can't be mistaken for seasonality — an earlier version of this check did make that mistake) each contribute a documented downgrade. The report shows the label's *inputs*, not just the label, so the reasoning is auditable even where the exact cut points are debatable.
- **Growth is one pinned formula** — mean of the last few complete months vs. mean of the first few, with an explicit `null` (not a crash or a fabricated 0%) when there isn't enough history or the baseline is zero.

### Confidence thresholds (see `skill/analysis.py`)

| Constant | Value | Why |
|---|---|---|
| `MIN_SPAN_DAYS` | 180 | Below ~6 months, "trend" isn't a meaningful concept |
| `MIN_MEAN_VIEWS` | 100/month | Below this, Poisson-style sampling noise dominates any percentage |
| `MIN_COVERAGE` | 0.90 | Gappy data undermines any figure computed from it |
| `CV_HIGH_CEILING` / `CV_MEDIUM_CEILING` | 0.40 / 0.80 | Coefficient-of-variation bands, calibrated by running the rule against real pulled series so the label scale isn't degenerate (see below) |
| `OUTLIER_MODZ` / `OUTLIER_MEDIUM_MAX` | 3.5 / 1 | Modified z-score (median/MAD, Iglewicz & Hoaglin) — robust at the small sample sizes (12–24 points) this tool typically sees, unlike a plain standard-deviation z-score |
| `SEASONALITY_RATIO` | 1.5 | Detrended month-of-year group-mean ratio above this flags a seasonal confound |

Real series pulled during development: the two lower-volume, higher-variance demo articles (Czech "intermittent fasting", Ukrainian "astronomy") land **Low**; the five higher-volume "English language" comparison series land **High**. No real series pulled during development happened to land **Medium** under these thresholds — the test suite's Medium fixture is explicitly synthetic and documented as such rather than silently presented as observed behavior.

## Analysis is always monthly

Growth, confidence, and seasonality are computed on self-rolled-up **complete calendar months**, regardless of what `--granularity` the chart displays. This is a deliberate simplification: it gives exactly one definition of "coefficient of variation" and "a period" instead of two (daily vs. monthly) that could silently disagree. `--granularity daily` only changes what the *chart* plots (raw daily points instead of monthly bars); it never changes what is measured.

## Assumptions & limitations

- Pageviews measure **attention**, not purchase intent or willingness to pay — the skill never claims otherwise.
- Data comes only from the Wikimedia Pageviews API (`access=all-access`, `agent=user` — bot/spider traffic filtered at the source), with a documented floor (2015-07-01) and a 2-day publication lag.
- A language with no dedicated article for a topic is reported as an explicit finding (a real content gap), not silently skipped or guessed at via a fuzzy search match.
- A topic that resolves to a Wikipedia **disambiguation page** is a hard error with candidate suggestions, not a false "no data in any language" result.
- Confidence labels are a documented heuristic, not a statistical guarantee — every label ships with the reasons and inputs behind it.
- Wikimedia pageview data is CC BY-SA 4.0.

## Live verification against a cheap model

`verification/verify_with_gemini.py` drives the skill through a real Gemini (Google AI Studio free tier) call: the model receives `SKILL.md`'s body as its system prompt and exactly one generic tool — `run_shell(command)`, restricted server-side to invocations starting with `wikitrends` — so the model has to read the instructions and compose the actual command itself, the same way a host agent (e.g. Claude Code) would use this skill in practice. Run it with `GOOGLE_API_KEY=... python verification/verify_with_gemini.py`; it writes transcripts and the resulting PDFs/PNGs under `verification/output_*/`. `verification/check_numeric_consistency.py` then checks that every number in the model's final prose answer matches a number the tool itself produced somewhere in that call's tool-result output.

**Status: done.** Run live against `gemini-2.5-flash` (Google AI Studio free tier) for all 3 reference queries — transcripts and generated PDFs/PNGs are committed under `verification/transcript_*.md` and `verification/output_*/`. All three passed `check_numeric_consistency.py`. Notably, the model:
- composed the correct `wikitrends` invocation itself from `SKILL.md` alone, on the first try, for every query (no retries needed) — including the 5-language fan-out in query 3;
- correctly reported "no dedicated article in this language edition" for Polish (query 1) as a finding rather than glossing over it;
- consistently surfaced the confidence label *and its stated reasons* rather than quoting a bare growth percentage;
- in query 2, correctly cited the `--explain` regression output's YoY figure when the SKILL.md example suggested it.
No hardcoded model or query knowledge was used — see `_run_one_query`/`run_shell` in `verify_with_gemini.py`. Two SDK potholes hit along the way, fixed in the harness rather than worked around by hand: (a) `gemini-2.0-flash` is retired server-side — the harness defaults to `gemini-2.5-flash`, confirmed live to support function calling on the free tier; (b) the harness previously used `from __future__ import annotations`, which turns `command: str` into the literal string `"str"` at runtime and broke the SDK's automatic function-calling schema introspection — removed.

## AI-assisted development and how it was verified

This skill was built with AI assistance (Claude, via Claude Code) at every stage — requirements gathering (a Socratic "deep interview" that turned the open-ended case brief into a concrete, testable spec), planning (a three-round adversarial Architect/Critic consensus review of the implementation plan, non-interactive, before any code was written), and implementation.

The planning review process specifically was not a rubber stamp: across three rounds, independent review agents empirically pulled real data from the live Wikimedia/Wikidata APIs and found (and the plan was revised to fix) several defects that would otherwise have shipped, including: a partial-month bucket bug that fabricated a ~-99% headline growth number on the flagship demo query, a cache redesign that silently re-broke the "narrow the date range" requirement on a different axis than the first fix addressed, a resolution bug where an ambiguous topic (a real Wikidata disambiguation page) would have silently produced a false "no article in any language" result instead of a clear error, and a seasonality detector that would have false-flagged a plain decline as "seasonal." All of these were caught by **running the design against real data during planning**, not by code review alone — and the same discipline carried into implementation: the unit test suite's real-data fixtures (Low/High confidence labels, the seasonality regression guard, the cache-narrowing and completeness tests) are built from series actually pulled from the live API while building this, specifically so a future regression is caught by `pytest`, not by another manual data-pulling session.

Verification of the AI-produced output at each stage: (1) every non-trivial technical claim used during planning was checked against a real `curl`/API call rather than assumed — the planning documents distinguish "verified live" from "stated" explicitly; (2) the full unit test suite (17 tests) runs with `pytest-socket --disable-socket`, so "no live network calls in tests" is enforced, not just claimed; (3) the CLI was smoke-tested against the real Wikimedia/Wikidata APIs for all three reference query shapes (including the flagship "intermittent fasting" comparison, which — confirmed live — has no Polish Wikipedia article at all, a real content-gap finding rather than a bug) before this README was written, producing real 1-page PDFs checked with `pypdf`; (4) the live Gemini verification run (above, `verification/transcript_*.md`) is independent evidence the finished skill, not just the plan, works through an actual free-tier cheap tool-calling model — including catching and fixing two real SDK/model-name issues along the way rather than papering over them.

## Roadmap — scaling to deeper research and larger data

- **Batch/portfolio mode**: rank N candidate topics or languages by growth × confidence in one call, instead of one topic per invocation.
- **Smarter incremental caching**: the current cache already only fetches missing date ranges; the next step is background pre-warming for a standing set of topics a team tracks over time.
- **Deeper statistical rigor**: the month-of-year seasonality check is a first-order heuristic; a real seasonal decomposition (e.g. STL) or changepoint detection would handle shorter spans and more complex patterns than the current ≥24-month gate covers.
- **Multi-source triangulation**: pair Wikipedia interest with other public signals (e.g. search-trend data) once a single-source Wikipedia-only view of a topic proves useful enough to be worth extending.
- **MCP packaging**: expose the same analysis as an MCP tool with a typed schema, for agents that aren't using the Agent Skills / Claude Code convention this was built for.
