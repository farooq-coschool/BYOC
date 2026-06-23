"""Loads the MongoDB Compass prompt exports and selects the exact prompt for a
given Curriculum + practice type (AP/GP) + Subject.

GP (Guided Practice / objective)  -> title auto_objective_questions (SCQ + RA)
AP (Assessment Practice / subjective) -> title auto_subjective_questions (VSA/SA/LA)

Each prompt embeds its own per-cognitive-level count block; we strip that and
inject the user's requested counts instead (see override_counts()).
"""

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
DB_DIR = BASE_DIR / "prompts_db"

# (curriculum, practice) -> filename in prompts_db/
FILES = {
    ("CBSE", "gp"): "CBSE_GP.json",
    ("CBSE", "ap"): "CBSE_AP.json",
    ("ICSE", "gp"): "ICSE_GP.json",
    ("ICSE", "ap"): "ICSE_AP.json",
}

# Canonical subjects the UI exposes.
SUBJECTS = ["English", "Biology", "Physics", "Chemistry", "Geography", "Civics", "History", "Mathematics"]

_COUNT_LINE = re.compile(r"^[ \t]*\[Number of [^\]]*\][ \t]*:.*$", re.IGNORECASE | re.MULTILINE)


def detect_subjects(data):
    """Return the canonical subject(s) a prompt targets, read from its role line.

    Handles forms like "Science (Biology)" and "Social Science (Geography)" and
    combined "History and Civics" prompts (registered under both subjects).
    """
    head = str(data or "")[:600]
    found = []

    def add(subject):
        if subject not in found:
            found.append(subject)

    # Prefer the subject named inside parentheses, e.g. Science (Biology).
    for match in re.finditer(r"\(([^)]+)\)", head):
        inner = match.group(1)
        for subject in SUBJECTS:
            if re.search(r"\b" + re.escape(subject) + r"\b", inner, re.IGNORECASE):
                add(subject)
    if found:
        return found

    # Otherwise read subject names directly from the role line.
    for subject in SUBJECTS:
        if re.search(r"\b" + re.escape(subject) + r"\b", head, re.IGNORECASE):
            add(subject)
    return found


def build_index():
    """index[(curriculum, practice)] = {subject: prompt_data_string}."""
    index = {}
    for (curriculum, practice), filename in FILES.items():
        path = DB_DIR / filename
        if not path.exists():
            continue
        try:
            docs = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        by_subject = {}
        for doc in docs:
            data = str(doc.get("data") or "")
            if not data.strip():
                continue
            for subject in detect_subjects(data):
                by_subject.setdefault(subject, data)  # first match wins
        index[(curriculum, practice)] = by_subject
    return index


INDEX = build_index()


def curricula():
    return sorted({curriculum for (curriculum, _practice) in INDEX})


def availability():
    """{curriculum: {subject: {"ap": bool, "gp": bool}}} for enabling UI options."""
    out = {}
    for (curriculum, practice), by_subject in INDEX.items():
        curr = out.setdefault(curriculum, {})
        for subject in by_subject:
            curr.setdefault(subject, {"ap": False, "gp": False})[practice] = True
    return out


def get_prompt(curriculum, practice, subject):
    return INDEX.get((curriculum, practice), {}).get(subject)


def override_counts(text, distribution):
    """Remove the prompt's built-in [Number of ...] block and inject our counts."""
    total = sum(item["count"] for item in distribution)
    parts = "; ".join(f"{item['code']}: {item['count']}" for item in distribution)
    instruction = (
        "[GENERATION COUNTS — these OVERRIDE every other count mentioned anywhere in this prompt]\n"
        f"Generate EXACTLY {total} questions in total, distributed as: {parts}.\n"
        "Spread them across the available cognitive levels (Factual / Understanding / Application) and "
        "difficulty levels (Easy / Medium / Hard) at your discretion. Do not generate any other "
        "quantities, totals, or question types than those listed here.\n"
    )
    matches = list(_COUNT_LINE.finditer(text))
    if matches:
        first, last = matches[0].start(), matches[-1].end()
        text = text[:first] + instruction + text[last:]
        text = _COUNT_LINE.sub("", text)  # clean any stragglers outside the block
        return text
    return instruction + "\n" + text


def fill(text, mapping):
    """Replace single-brace {placeholders} and the '[File Attached]' script marker."""
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", str(value))
    return text
