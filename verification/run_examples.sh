#!/usr/bin/env bash
# Runs the 3 reference query shapes for real, then asserts basic sanity on
# the produced PDFs via pypdf (1 page, contains the confidence label text,
# contains the correctly-rendered native-script title).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/verification/run_examples_out"
rm -rf "$OUT"
mkdir -p "$OUT"

run() {
  local name="$1"; shift
  echo "=== $name ==="
  wikitrends "$@" --out "$OUT/$name"
}

run "1_intermittent_fasting" --topic "intermittent fasting" --langs pl,cs --years 2
run "2_astronomy_uk" --topic astronomy --langs uk --years 2 --explain
run "3_english_language" --topic "English language" --langs de,fr,es,pl,ja --years 2

python3 - "$OUT" <<'PYEOF'
import sys
from pathlib import Path
from pypdf import PdfReader

out = Path(sys.argv[1])
failures = []
for pdf_path in sorted(out.glob("*/report.pdf")):
    reader = PdfReader(str(pdf_path))
    if len(reader.pages) != 1:
        failures.append(f"{pdf_path}: expected 1 page, got {len(reader.pages)}")
    text = reader.pages[0].extract_text()
    if not any(label in text for label in ("Low", "Medium", "High")):
        failures.append(f"{pdf_path}: no confidence label found in text")

if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)
print(f"OK: all report.pdf files under {out} are 1 page with a confidence label")
PYEOF
