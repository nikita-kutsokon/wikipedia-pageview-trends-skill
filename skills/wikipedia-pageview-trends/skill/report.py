"""Charts (matplotlib PNG) + a 1-page PDF report (reportlab).

Unicode font handling: reportlab's built-in fonts are Latin-1-only and
cannot render Polish/Czech/Cyrillic article titles (confirmed during
planning review — both flagship demo languages need this). We register
DejaVuSans, located via matplotlib's own bundled data path so we don't ship
a second copy of the same font file; if that path is ever missing at
runtime we fall back to Helvetica with a printed warning rather than
crashing (non-Latin titles will render as boxes in that fallback case).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # must happen before importing pyplot — a subprocess-invoked
# run on macOS can otherwise try to open a GUI backend and hang the tool call.

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .analysis import SeriesAnalysis

MAX_SERIES = 5

FONT_NAME = "Helvetica"
_dejavu = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
_dejavu_bold = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf"
if _dejavu.exists():
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(_dejavu)))
    FONT_NAME = "DejaVuSans"
    if _dejavu_bold.exists():
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(_dejavu_bold)))
        FONT_NAME_BOLD = "DejaVuSans-Bold"
    else:
        FONT_NAME_BOLD = FONT_NAME
else:
    print(
        "warning: DejaVuSans.ttf not found via matplotlib's data path — "
        "non-Latin titles (Polish/Czech/Cyrillic/etc.) may not render correctly in the PDF",
        file=sys.stderr,
    )
    FONT_NAME_BOLD = "Helvetica-Bold"


LIMITATIONS = [
    "Pageviews measure attention, not purchase intent or willingness to pay.",
    "`agent=user` filters known automated/bot traffic at the source, but cannot catch all synthetic traffic.",
    "Wikipedia-edition readership is not 1:1 with a language's speaker population.",
    "All pageview data is fetched once at daily granularity and rolled up locally into complete "
    "calendar months (never taken from Wikimedia's own monthly bucket, which can include "
    "unreliable partial-month values) — see README for why.",
]


def make_chart(
    series_by_lang: dict[str, list[tuple[date, int]]], title: str, out_path: Path
) -> tuple[Path, bool]:
    """Line chart, one series per language, capped at MAX_SERIES for legibility.
    Each series is a list of (x_date, views) points — callers decide whether
    that's the monthly rollup or the raw daily series (--granularity).
    Returns (path, truncated: bool).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    truncated = len(series_by_lang) > MAX_SERIES
    items = list(series_by_lang.items())[:MAX_SERIES]

    fig, ax = plt.subplots(figsize=(7.5, 4))
    for lang, points in items:
        if not points:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, marker="o", markersize=3, label=lang)
    ax.set_title(title)
    ax.set_ylabel("Monthly views")
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path, truncated


@dataclass
class ReportInputs:
    topic: str
    resolved_titles: dict[str, str | None]
    analyses: dict[str, SeriesAnalysis]
    chart_path: Path
    chart_truncated: bool
    resolution_warning: str | None


def _fmt_pct(x: float | None) -> str:
    return f"{x:+.1f}%" if x is not None else "n/a"


def make_pdf(inputs: ReportInputs, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    styles = {
        "title": ParagraphStyle("title", fontName=FONT_NAME_BOLD, fontSize=16, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=FONT_NAME_BOLD, fontSize=11, spaceBefore=8, spaceAfter=4),
        "body": ParagraphStyle("body", fontName=FONT_NAME, fontSize=9, leading=12),
        "small": ParagraphStyle("small", fontName=FONT_NAME, fontSize=7.5, leading=10, textColor=colors.grey),
    }

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
    )
    story = []
    story.append(Paragraph(f"Wikipedia interest trend: {inputs.topic}", styles["title"]))

    if inputs.resolution_warning:
        story.append(Paragraph(f"⚠ {inputs.resolution_warning}", styles["small"]))

    story.append(Image(str(inputs.chart_path), width=6.8 * inch, height=3.6 * inch))
    if inputs.chart_truncated:
        story.append(Paragraph("(chart limited to 5 languages for legibility)", styles["small"]))

    story.append(Paragraph("Findings by language", styles["h2"]))
    rows = [["Lang", "Article", "Growth", "YoY", "Confidence", "Why"]]
    for lang, title in inputs.resolved_titles.items():
        if title is None:
            rows.append([lang, "(no dedicated article)", "—", "—", "—", "content gap"])
            continue
        a = inputs.analyses.get(lang)
        if a is None:
            rows.append([lang, title, "—", "—", "—", "no data"])
            continue
        growth_str = _fmt_pct(a.growth.pct) if a.growth.pct is not None else (a.growth.reason_if_null or "n/a")
        why = "; ".join(a.confidence.reasons[:2])
        rows.append([lang, title, growth_str, _fmt_pct(a.yoy_growth_pct), a.confidence.label, why])

    table = Table(rows, colWidths=[0.5 * inch, 1.6 * inch, 0.7 * inch, 0.6 * inch, 0.7 * inch, 2.7 * inch])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), FONT_NAME_BOLD),
                ("FONTNAME", (0, 1), (-1, -1), FONT_NAME),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
            ]
        )
    )
    story.append(table)

    story.append(Paragraph("Assumptions & limitations", styles["h2"]))
    for line in LIMITATIONS:
        story.append(Paragraph(f"• {line}", styles["small"]))

    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            "Confidence labels come from a fixed, documented rule (span, data volume, coverage, "
            "variance, outliers, seasonality) — see README for the exact thresholds and why.",
            styles["small"],
        )
    )

    doc.build(story)
    return out_path
