"""Check that every number in a transcript's final prose answer traces back
to a number the tool actually returned in one of that transcript's tool-
result blocks (plain-text CLI output and/or --json — SKILL.md tells the
model it may read either), per Principle 2 ("every claim traces to fetched
data" — a number the model states but the tool never produced is a
fabrication risk this check exists to catch).

Usage:
    python verification/check_numeric_consistency.py verification/transcript_*.md
"""

from __future__ import annotations

import re
import sys

NUMBER_RE = re.compile(r"[-+]?\d[\d\s,.]*%?")
# 4-digit years, small integers (month/day/language counts), and the "1" in
# "1-page" are excluded — they're structural prose, not data claims.
ALLOWLIST_SMALL_INT_MAX = 12


def _extract_numbers(text: str) -> list[float]:
    out = []
    for raw in NUMBER_RE.findall(text):
        cleaned = raw.replace(" ", "").replace(",", "").rstrip("%")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if 1900 <= value <= 2100 and "." not in cleaned:
            continue  # looks like a year
        if abs(value) <= ALLOWLIST_SMALL_INT_MAX and "." not in cleaned:
            continue  # small integer — month/day/language count, not a data claim
        out.append(value)
    return out


def _extract_tool_result_numbers(transcript: str) -> list[float]:
    """Every number the tool itself produced, across all tool-result blocks
    in the transcript — whether the model asked for plain text or --json.
    """
    numbers = []
    for match in re.finditer(r"\*\*tool result:\*\*\n```\n(.*?)\n```", transcript, re.DOTALL):
        numbers.extend(_extract_numbers(match.group(1)))
    return numbers


def _rounds_to(candidate: float, target: float) -> bool:
    s = f"{candidate:g}"
    decimals = len(s.split(".")[1]) if "." in s else 0
    return round(target, decimals) == candidate


def check_transcript(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    final_answer_match = re.search(r"\*\*Final answer:\*\*\n(.*)", text, re.DOTALL)
    prose = final_answer_match.group(1) if final_answer_match else text

    prose_numbers = _extract_numbers(prose)
    tool_numbers = _extract_tool_result_numbers(text)

    failures = []
    for n in prose_numbers:
        if not any(_rounds_to(n, j) or _rounds_to(n, -j) for j in tool_numbers):
            failures.append(f"{path}: prose number {n} not found in any tool-result output")
    return failures


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_numeric_consistency.py <transcript.md> [...]", file=sys.stderr)
        return 1
    all_failures = []
    for path in argv:
        all_failures.extend(check_transcript(path))
    if all_failures:
        for f in all_failures:
            print(f"FAIL: {f}")
        return 1
    print(f"OK: {len(argv)} transcript(s) numerically consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
