"""Check that every number in a transcript's final prose answer traces back
to a field the tool actually returned (--json output captured in that same
transcript's tool-result blocks), per Principle 2 ("every claim traces to
fetched data" — a number that's merely present in --json but misquoted in
prose is still a fabrication risk this check exists to catch).

Usage:
    python verification/check_numeric_consistency.py verification/transcript_*.md
"""

from __future__ import annotations

import json
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


def _extract_json_numbers(transcript: str) -> list[float]:
    numbers = []
    for match in re.finditer(r"```\n(\{.*?\})\n```", transcript, re.DOTALL):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        numbers.extend(_walk_numbers(payload))
    return numbers


def _walk_numbers(obj) -> list[float]:
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_walk_numbers(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_walk_numbers(v))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    return out


def _rounds_to(candidate: float, target: float) -> bool:
    s = f"{candidate:g}"
    decimals = len(s.split(".")[1]) if "." in s else 0
    return round(target, decimals) == candidate


def check_transcript(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    final_answer_match = re.search(r"\*\*Final answer:\*\*\n(.*)", text, re.DOTALL)
    prose = final_answer_match.group(1) if final_answer_match else text

    prose_numbers = _extract_numbers(prose)
    json_numbers = _extract_json_numbers(text)

    failures = []
    for n in prose_numbers:
        if not any(_rounds_to(n, j) or _rounds_to(n, -j) for j in json_numbers):
            failures.append(f"{path}: prose number {n} not found in any --json output field")
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
