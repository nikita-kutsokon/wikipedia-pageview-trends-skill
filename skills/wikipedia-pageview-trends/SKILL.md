---
name: wikipedia-pageview-trends
description: Analyze Wikimedia pageview trends across languages and topics to help B2C product teams decide which topics or language markets to invest in. Fetches real Wikipedia pageview data, computes growth with an explicit trust/confidence label, and produces a one-page PDF report with a chart. Use when the user asks about interest/growth/trends in a topic on Wikipedia, wants to compare a topic across languages or language editions, or wants to know how much to trust an observed growth trend.
---

# Wikipedia Pageview Trends

Analyzes real Wikimedia pageview data to answer questions like "is interest
in X growing on Y-language Wikipedia, and how much can we trust that?" or
"compare interest in X between language A and language B." Produces a chart
and a one-page PDF report.

## Setup (once per environment)

```
pip install -e <path-to-this-directory>
```

This registers a `wikitrends` command. If you get `ModuleNotFoundError` or
`command not found` when running it, run the `pip install -e` command above
first — the tool prints the exact command to run if a dependency is missing.

## The one command you need

```
wikitrends --topic "<free text topic>" --langs <lang1,lang2,...> --years <N>
```

That's it — one invocation, no subcommands. It resolves the topic to the
correct article in each language (via Wikipedia/Wikidata, not guesswork),
fetches real pageview history, computes growth and a confidence label, and
writes a chart (PNG) and a one-page report (PDF). It prints the absolute
paths of both files, plus a human-readable summary, to stdout.

### The three reference query shapes

1. **Compare growth across languages for one topic:**
   ```
   wikitrends --topic "intermittent fasting" --langs pl,cs --years 2
   ```
2. **Trend + trustworthiness for one language** (add `--explain` for the
   underlying regression stats):
   ```
   wikitrends --topic astronomy --langs uk --years 2 --explain
   ```
3. **Compare one topic across several candidate language editions**, then
   rank which to investigate next:
   ```
   wikitrends --topic "English language" --langs de,fr,es,pl,ja --years 2
   ```
   Read each language's growth % and confidence label from the output (or
   `--json`). Rank candidates by (growth direction × confidence), and call
   out any language reported as "no dedicated article in this language
   edition" as a content gap, not a failure — it is itself a valid finding.

## Reading the output

- **Always read numbers from the tool's own output** (plain text or
  `--json`), never restate a number you didn't get from there — the tool is
  the single source of truth for every figure in your answer.
- Every growth figure comes with a **confidence label** (Low / Medium /
  High) and **stated reasons** (data span, volume, variance, anomalies,
  seasonality). Always mention both the label and at least one reason when
  you report a growth number — "growing 12%" alone is an incomplete answer;
  "growing 12% (Medium confidence — some month-to-month variance)" is not.
- If `growth` is `null`, say so explicitly and give the stated reason
  (e.g. "not enough history yet" or "starts from zero views") — do not
  invent a percentage.
- If a language shows "no dedicated article in this language edition," or a
  resolution warning about suspiciously few language editions, report that
  plainly — these are real, useful findings about content gaps, not errors
  to work around.

## Follow-up / refinement queries

Follow-ups are cheap by design — pageview data is cached locally per
(project, article). Just re-invoke `wikitrends` with adjusted flags; you do
not need to do anything special to "reuse" prior data:

- Add a language: `wikitrends --topic ... --langs pl,cs,uk --years 2`
- Narrow or widen the date range: `--years 1` or `--years 5`, or explicit
  `--start YYYYMMDD --end YYYYMMDD`
- Force fresh data: add `--refresh-cache`
- If the auto-resolved article seems wrong for one language, correct it
  directly: `--article pl:Some_Specific_Title`
- If the topic is not naturally an English concept, anchor resolution to a
  different language: `--from-lang de`

## Limits (state these in your answer when relevant)

- Pageviews measure attention, not purchase intent.
- Growth figures come from Wikimedia's public Pageviews API only — no other
  data source is consulted.
- A topic with no article in a requested language is reported as a gap, not
  silently skipped.
- Confidence labels use a fixed, documented rule (see the repository
  README) — they are not a statistical guarantee.
