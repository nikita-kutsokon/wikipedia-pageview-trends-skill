"""wikitrends — flat single-command CLI (Option D: no subcommands in the
agent-facing surface, so a cheap tool-calling model has exactly one
invocation shape to learn). See SKILL.md for the invocation contract this
implements.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path


def _remediation_and_exit(exc: ImportError) -> None:
    skill_dir = Path(__file__).resolve().parent.parent
    print(
        f"error: missing dependency ({exc}).\n"
        f"fix: pip install -e {skill_dir}",
        file=sys.stderr,
    )
    sys.exit(1)


try:
    from . import analysis, cache, report
    from .wiki_api import DisambiguationError, WikiApiError, resolve_topic
except ImportError as exc:  # pragma: no cover — exercised manually, not under pytest
    _remediation_and_exit(exc)
    raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="wikitrends",
        description="Analyze Wikimedia pageview trends across languages for a topic.",
    )
    p.add_argument("--topic", required=True, help='free-text topic, e.g. "intermittent fasting"')
    p.add_argument("--langs", required=True, help="comma-separated language codes, e.g. pl,cs")
    p.add_argument("--from-lang", default="en", help="anchor project for topic resolution (default: en)")
    p.add_argument(
        "--article",
        action="append",
        default=[],
        metavar="LANG:TITLE",
        help="manual override for one language's resolved title, repeatable",
    )
    p.add_argument("--years", type=float, default=None, help="lookback window in years")
    p.add_argument("--start", default=None, help="YYYYMMDD (overrides --years)")
    p.add_argument("--end", default=None, help="YYYYMMDD (overrides --years)")
    p.add_argument("--granularity", choices=["auto", "daily", "monthly"], default="auto")
    p.add_argument("--json", action="store_true", dest="json_out")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--refresh-cache", action="store_true")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--out", default=None, help="output directory (default: ./wikitrends-out)")
    return p.parse_args(argv)


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        lang, _, title = pair.partition(":")
        if not title:
            raise SystemExit(f"error: --article expects LANG:TITLE, got {pair!r}")
        out[lang] = title
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    overrides = _parse_overrides(args.article)

    try:
        resolved = resolve_topic(args.topic, langs, from_lang=args.from_lang, overrides=overrides)
    except DisambiguationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except WikiApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y%m%d").date()
        end = datetime.strptime(args.end, "%Y%m%d").date()
    else:
        years = args.years if args.years is not None else 2.0
        start, end = analysis.years_to_range(years)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    out_dir = Path(args.out) if args.out else Path.cwd() / "wikitrends-out"
    out_dir.mkdir(parents=True, exist_ok=True)

    analyses: dict[str, analysis.SeriesAnalysis] = {}
    monthly_series: dict[str, list[tuple[date, int]]] = {}
    daily_series: dict[str, list[tuple[date, int]]] = {}

    print(f"resolved via {resolved.anchor_lang}.wikipedia: {resolved.anchor_title!r}")
    if resolved.warning:
        print(f"warning: {resolved.warning}", file=sys.stderr)

    for lang in langs:
        title = resolved.titles.get(lang)
        if title is None:
            print(f"{lang}: no dedicated article in this language edition")
            continue
        project = f"{lang}.wikipedia"
        print(f"{lang}: {title!r}")
        series = cache.get_daily_series(project, title, start, end, cache_dir=cache_dir, refresh=args.refresh_cache)
        a = analysis.analyze_series(series.datapoints, series.fetched_ranges, start, end)
        analyses[lang] = a

        months = analysis.complete_months(series.datapoints, series.fetched_ranges, start, end)
        monthly_series[lang] = [(m.month_start, m.views) for m in months]
        daily_series[lang] = sorted(
            (datetime.strptime(d, "%Y%m%d").date(), v) for d, v in series.datapoints.items()
        )

    use_daily = args.granularity == "daily" or (
        args.granularity == "auto" and (end - start).days <= 18 * 30
    )
    chart_series = daily_series if use_daily else monthly_series

    chart_path, truncated = report.make_chart(
        chart_series, f'"{args.topic}" — Wikipedia pageviews', out_dir / "chart.png"
    )
    pdf_inputs = report.ReportInputs(
        topic=args.topic,
        resolved_titles=resolved.titles,
        analyses=analyses,
        chart_path=chart_path,
        chart_truncated=truncated,
        resolution_warning=resolved.warning,
    )
    pdf_path = report.make_pdf(pdf_inputs, out_dir / "report.pdf")

    print(f"chart: {chart_path.resolve()}")
    print(f"report: {pdf_path.resolve()}")

    for lang, a in analyses.items():
        growth_str = f"{a.growth.pct:+.1f}%" if a.growth.pct is not None else (a.growth.reason_if_null or "n/a")
        print(f"{lang}: growth {growth_str}, confidence {a.confidence.label} ({'; '.join(a.confidence.reasons)})")
        if args.explain:
            r = a.regression
            print(
                f"  regression: slope={r.slope}, R²={r.r_squared}, "
                f"95% CI=({r.ci_low}, {r.ci_high}); YoY={a.yoy_growth_pct}"
            )

    if args.json_out:
        payload = {
            "resolved": resolved.titles,
            "resolution_warning": resolved.warning,
            "series": {},
        }
        for lang, a in analyses.items():
            payload["series"][lang] = {
                "series_summary": {
                    "span_days": a.span_days,
                    "n_complete_periods": a.n_complete_periods,
                    "coverage": a.coverage,
                    "mean_views": a.mean_views,
                },
                "growth": {
                    "pct": a.growth.pct,
                    "absolute": a.growth.absolute,
                    "reason_if_null": a.growth.reason_if_null,
                },
                "yoy_growth_pct": a.yoy_growth_pct,
                "cagr_pct": a.cagr_pct,
                "regression": {
                    "slope": a.regression.slope,
                    "r_squared": a.regression.r_squared,
                    "ci_low": a.regression.ci_low,
                    "ci_high": a.regression.ci_high,
                },
                "confidence": {"label": a.confidence.label, "reasons": a.confidence.reasons},
                "seasonal": a.confidence.seasonal,
                "volume_note": a.volume_note,
            }
        payload["artifacts"] = {"pdf": str(pdf_path.resolve()), "png": [str(chart_path.resolve())]}
        print(json.dumps(payload, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
