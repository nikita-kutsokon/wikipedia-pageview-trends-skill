"""Live verification: drive the wikitrends skill through a real Gemini model.

The model receives SKILL.md's body as its system prompt and exactly ONE
generic tool: run_shell(command). It has to read the instructions and
compose the actual `wikitrends ...` invocation itself — nothing about
argument choice is hardcoded here, so this exercises the same path a real
host agent (e.g. Claude Code) would take when using this skill.

Requires GOOGLE_API_KEY in the environment (never read from a file, never
written to one). Requires `pip install -r verification/requirements.txt`.

Usage:
    GOOGLE_API_KEY=... python verification/verify_with_gemini.py
"""

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "wikipedia-pageview-trends"
SKILL_MD = SKILL_DIR / "SKILL.md"
OUTPUT_ROOT = Path(__file__).resolve().parent

QUERIES = [
    (
        "1_intermittent_fasting",
        "Compare growth of interest in intermittent fasting between Polish and Czech Wikipedia "
        "over the last two years.",
    ),
    (
        "2_astronomy_uk",
        "We're considering adding an astronomy course to our educational app. Is interest in "
        "this topic growing on Ukrainian Wikipedia, and how much can we trust that growth?",
    ),
    (
        "3_english_language",
        "We're building a language-learning app. Compare interest in learning English across "
        "German, French, Spanish, Polish, and Japanese Wikipedia, and tell me which audiences "
        "are worth investigating next and why.",
    ),
]

ALLOWED_METACHAR_FREE = True  # shell=False makes a metacharacter blocklist unnecessary; see plan.


def _skill_md_body() -> str:
    text = SKILL_MD.read_text(encoding="utf-8")
    # Strip the YAML frontmatter (between the first pair of `---` lines) —
    # the model gets the instructions, not the machine-readable metadata block.
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else text


_TOOL_CALL_LOG: list[tuple[str, str]] = []


def run_shell(command: str) -> str:
    """Execute a wikitrends invocation. The ONLY tool the model gets.

    Restricted to commands whose first token is exactly "wikitrends" —
    argv is parsed with shlex and passed to subprocess with shell=False, so
    shell metacharacters inside an argument (e.g. a topic like "AT&T") are
    inert literal characters, never shell syntax; no additional blocklist
    is needed or applied.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        result = f"error: could not parse command: {exc}"
        _TOOL_CALL_LOG.append((command, result))
        return result
    if not argv or argv[0] != "wikitrends":
        result = "error: only 'wikitrends ...' commands are permitted through this tool"
        _TOOL_CALL_LOG.append((command, result))
        return result
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=120)
        result = f"exit_code={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    except subprocess.TimeoutExpired:
        result = "error: command timed out after 120s"
    _TOOL_CALL_LOG.append((command, result))
    return result


def _send_with_retry(chat, query: str, max_attempts: int = 5):
    """Retry on transient 503/429 from the API itself (observed live: the
    free tier's 5-req/min cap and transient model-unavailable both happen in
    practice), honoring the API's own suggested retry delay when present.
    """
    import re
    import time

    from google.genai import errors

    last_exc = None
    for attempt in range(max_attempts):
        try:
            return chat.send_message(query)
        except errors.ServerError as exc:
            last_exc = exc
            delay = 5 * (attempt + 1)
        except errors.ClientError as exc:
            last_exc = exc
            match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", str(exc))
            delay = int(match.group(1)) + 2 if match else 20 * (attempt + 1)
        print(f"  (retrying after {delay}s: {last_exc})", file=sys.stderr)
        import time as _t

        _t.sleep(delay)
    raise last_exc


def _run_one_query(client, model_name: str, system_prompt: str, query: str):
    from google.genai import types

    _TOOL_CALL_LOG.clear()
    chat = client.chats.create(
        model=model_name,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[run_shell],
        ),
    )
    response = _send_with_retry(chat, query)

    transcript_lines = [f"**User:** {query}\n"]
    for command, result in _TOOL_CALL_LOG:
        transcript_lines.append(f"**model (tool call):** `run_shell({command!r})`")
        transcript_lines.append(f"**tool result:**\n```\n{result}\n```")
    transcript_lines.append(f"\n**Final answer:**\n{response.text}")
    return response.text, "\n\n".join(transcript_lines)


def main() -> int:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("error: GOOGLE_API_KEY not set in environment", file=sys.stderr)
        return 1

    from google import genai

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    system_prompt = _skill_md_body()

    for slug, query in QUERIES:
        print(f"=== running query: {slug} ===")
        out_dir = OUTPUT_ROOT / f"output_{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            final_text, transcript = _run_one_query(client, model_name, system_prompt, query)
        except Exception as exc:  # live-API run — report and continue to the next query
            print(f"query {slug} failed: {exc}", file=sys.stderr)
            continue

        transcript_path = OUTPUT_ROOT / f"transcript_{slug}.md"
        transcript_path.write_text(
            f"# Verification transcript: {slug}\n\n"
            f"Model: {model_name}\nRun at: {datetime.now(timezone.utc).isoformat()}\n\n{transcript}\n",
            encoding="utf-8",
        )

        # Collect any artifacts the model's tool calls produced (absolute
        # paths the CLI printed to stdout) into this query's output dir.
        for match in re.finditer(r"^(chart|report): (.+\.(?:png|pdf))$", transcript, re.MULTILINE):
            src = Path(match.group(2))
            if src.exists():
                dest = out_dir / src.name
                dest.write_bytes(src.read_bytes())

        print(f"  transcript: {transcript_path}")
        print(f"  artifacts:  {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
