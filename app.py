"""Question Generator — standalone Flask app.

Curriculum -> Grade -> Subject -> AP/GP. Picks the EXACT prompt for that
combination from the MongoDB Compass exports (see prompt_library.py), fills in
the lesson details, overrides the prompt's built-in counts with the user's
requested distribution, and renders the generated questions (LaTeX -> math).

AP = Assessment Practice (subjective: VSA / SA / LA).
GP = Guided Practice (objective: SCQ + RA).
"""

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import prompt_library as pl

# Load .env (API keys live here, never in the browser). Optional dependency:
# if python-dotenv isn't installed we fall back to a tiny manual parser.
def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return
    except Exception:
        pass
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


_RETRYABLE = (
    urllib.error.URLError,
    socket.timeout,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    ConnectionResetError,
    TimeoutError,
)


def _open_json(req, timeout=540, attempts=3):
    """urlopen + JSON parse, retrying on transient network/timeout errors.

    HTTPError (4xx/5xx) is re-raised immediately so callers can surface the
    API error message.  Anything that looks like a transient read/connect
    failure is retried up to `attempts` times with a short back-off.
    """
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except _RETRYABLE as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(2)
    raise last


BASE_DIR = Path(__file__).parent.resolve()

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MODELS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
DEFAULT_GEMMA_MODEL = "google/gemma-4-31b-it"
GEMMA_MODELS = ["google/gemma-4-31b-it"]

# Shown in the dropdown. AP (Andhra Pradesh) and TG (Telangana) have no prompt
# files yet, so the UI disables them via the availability map.
CURRICULA = ["CBSE", "ICSE", "AP", "TG"]
GRADES = [str(grade) for grade in range(6, 13)]
SUBJECTS = pl.SUBJECTS

# AP = Assessment Practice (subjective); GP = Guided Practice (objective).
PRACTICE_TYPES = {
    "ap": {"code": "ap", "label": "Assessment Practice", "questionStyle": "subjective"},
    "gp": {"code": "gp", "label": "Guided Practice", "questionStyle": "objective"},
}

# The only valid (cognitiveLevel, difficultyLevel) pairs the model may use.
VALID_COMBOS = [
    ("Factual",       "Easy"),
    ("Understanding", "Easy"),
    ("Understanding", "Medium"),
    ("Understanding", "Hard"),
    ("Application",   "Medium"),
    ("Application",   "Hard"),
]
_VALID_COMBO_SET = {(c, d) for c, d in VALID_COMBOS}
_COG_NEAREST_DIFF = {
    "Factual":       {"Easy": "Easy", "Medium": "Easy",   "Hard": "Easy"},
    "Understanding": {"Easy": "Easy", "Medium": "Medium", "Hard": "Hard"},
    "Application":   {"Easy": "Medium", "Medium": "Medium", "Hard": "Hard"},
}
_COG_ALIAS = {
    "knowledge": "Factual", "recall": "Factual", "remembering": "Factual",
    "comprehension": "Understanding", "analysis": "Application",
    "evaluate": "Application", "create": "Application", "synthesis": "Application",
}
_DIFF_ALIAS = {"low": "Easy", "moderate": "Medium", "high": "Hard"}


def _normalize_combo(cognitive, difficulty):
    """Map any (cognitiveLevel, difficultyLevel) to the nearest allowed combination."""
    cog = str(cognitive or "").strip()
    diff = str(difficulty or "").strip()
    cog = _COG_ALIAS.get(cog.lower(), cog)
    diff = _DIFF_ALIAS.get(diff.lower(), diff)
    cog = cog[0].upper() + cog[1:] if cog else "Understanding"
    diff = diff[0].upper() + diff[1:] if diff else "Medium"
    if (cog, diff) in _VALID_COMBO_SET:
        return cog, diff
    near = _COG_NEAREST_DIFF.get(cog)
    if near:
        return cog, near.get(diff, next(iter(near.values())))
    return "Understanding", "Medium"


def _normalize_questions(questions):
    out = []
    for q in (questions or []):
        q = dict(q)
        q["cognitiveLevel"], q["difficultyLevel"] = _normalize_combo(
            q.get("cognitiveLevel"), q.get("difficultyLevel")
        )
        out.append(q)
    return out


# Relative weights for splitting the requested total into category counts.
# GP -> SCQ:RA = 2:1 (e.g. 30 -> 20/10).  AP -> VSA:SA:LA = 50:30:20.
PRACTICE_DISTRIBUTIONS = {
    "gp": [
        {"code": "SCQ", "label": "Single Correct Questions", "weight": 20},
        {"code": "RA", "label": "Reason-Assertion", "weight": 10},
    ],
    "ap": [
        {"code": "VSA", "label": "Very Short Answer", "weight": 50},
        {"code": "SA", "label": "Short Answer", "weight": 30},
        {"code": "LA", "label": "Long Answer", "weight": 20},
    ],
}


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB uploads


# --- Global JSON error handlers (prevent Flask from returning HTML error pages) --
@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File too large. Maximum upload size is 64 MB."}), 413


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": f"Bad request: {e.description}"}), 400


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"Internal server error: {e}"}), 500


@app.errorhandler(Exception)
def unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": f"HTTP {e.code}: {e.description}"}), e.code
    return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Claude-Key, X-Gemma-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# --- Model calls -------------------------------------------------------------
def call_claude(prompt, api_key, model=None, max_output_tokens=16000):
    body = json.dumps(
        {
            "model": model or DEFAULT_CLAUDE_MODEL,
            "max_tokens": max_output_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        data = _open_json(req)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Claude API request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Claude API request failed: {exc.reason}. Check your internet connection / DNS.") from exc

    text_parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
    output = "\n".join(text_parts).strip()
    if not output:
        raise ValueError("Claude returned an empty response.")
    return output


def call_gemma(prompt, api_key, model=None, max_output_tokens=16000):
    selected_model = str(model or DEFAULT_GEMMA_MODEL).strip()
    if "/" in selected_model and not selected_model.startswith("models/"):
        body = json.dumps(
            {
                "model": selected_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_output_tokens,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "content-type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-Title": "Question Generator",
            },
            method="POST",
        )
        try:
            data = _open_json(req)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"Gemma API request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"Gemma API request failed: {exc.reason}. Check your internet connection / DNS.") from exc

        text_parts = []
        for choice in data.get("choices", []):
            message = choice.get("message") or {}
            if message.get("content"):
                text_parts.append(str(message.get("content") or ""))
        output = "\n".join(text_parts).strip()
        if not output:
            raise ValueError("Gemma returned an empty response.")
        return output

    selected_model = selected_model.removeprefix("models/")
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={api_key}",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        data = _open_json(req)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Gemma API request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Gemma API request failed: {exc.reason}. Check your internet connection / DNS.") from exc

    text_parts = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            if "text" in part:
                text_parts.append(part.get("text", ""))
    output = "\n".join(text_parts).strip()
    if not output:
        raise ValueError("Gemma returned an empty response.")
    return output


def model_provider_from_payload(payload):
    provider = str((payload or {}).get("modelProvider") or "claude").strip().lower()
    return "gemma" if provider == "gemma" else "claude"


def model_name_from_payload(payload):
    if model_provider_from_payload(payload) == "gemma":
        return str((payload or {}).get("gemmaModel") or DEFAULT_GEMMA_MODEL).strip() or DEFAULT_GEMMA_MODEL
    return str((payload or {}).get("claudeModel") or DEFAULT_CLAUDE_MODEL).strip() or DEFAULT_CLAUDE_MODEL


def api_key_from_request(payload, provider):
    """API keys come from server-side environment (.env) — never the browser."""
    if provider == "gemma":
        env_keys = ("GEMMA_API_KEY", "OPENROUTER_API_KEY")
    else:
        env_keys = ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    for name in env_keys:
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    raise ValueError(
        f"Missing {provider.capitalize()} API key on the server. "
        f"Set {env_keys[0]} in the .env file (or Render environment) and restart."
    )


def call_model(prompt, payload, max_output_tokens=16000):
    provider = model_provider_from_payload(payload)
    model = model_name_from_payload(payload)
    api_key = api_key_from_request(payload, provider)
    if provider == "gemma":
        return call_gemma(prompt, api_key, model, max_output_tokens=max_output_tokens)
    return call_claude(prompt, api_key, model, max_output_tokens=max_output_tokens)


# --- Source / script file parsing -------------------------------------------
def parse_source_file(file_storage):
    filename = str(getattr(file_storage, "filename", "") or "").strip()
    suffix = Path(filename).suffix.lower()
    data = file_storage.read()
    if not data:
        raise ValueError("Uploaded file is empty.")

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise ValueError("PDF support is not installed. Run: pip install pypdf") from exc
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(part.strip() for part in pages if part.strip()).strip()
        if not text:
            raise ValueError("No readable text was found in the PDF.")
        return text

    if suffix not in {".txt", ".text", ".md", ".markdown", ""}:
        raise ValueError("Upload the script/source as .pdf, .txt, or .md.")

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode the text file.")


# --- Distribution ------------------------------------------------------------
def split_counts(total, weights):
    """Split `total` into integer counts proportional to `weights`, summing to total."""
    total = max(0, int(total))
    weight_sum = sum(weights) or 1
    raw = [total * weight / weight_sum for weight in weights]
    counts = [int(value) for value in raw]
    remainder = total - sum(counts)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    return counts


def practice_distribution(practice_code, total):
    spec = PRACTICE_DISTRIBUTIONS.get(practice_code, [])
    counts = split_counts(total, [item["weight"] for item in spec])
    return [
        {"code": item["code"], "label": item["label"], "count": count}
        for item, count in zip(spec, counts)
    ]


def cognitive_distribution(total):
    """50% Understanding, 30% Application, 20% Factual — exact counts per combo."""
    und = round(total * 0.50)
    app_ = round(total * 0.30)
    fac = total - und - app_
    und3 = split_counts(und, [1, 1, 1])
    app2 = split_counts(app_, [1, 1])
    return [
        {"cognitiveLevel": "Factual",       "difficultyLevel": "Easy",   "count": fac},
        {"cognitiveLevel": "Understanding", "difficultyLevel": "Easy",   "count": und3[0]},
        {"cognitiveLevel": "Understanding", "difficultyLevel": "Medium", "count": und3[1]},
        {"cognitiveLevel": "Understanding", "difficultyLevel": "Hard",   "count": und3[2]},
        {"cognitiveLevel": "Application",   "difficultyLevel": "Medium", "count": app2[0]},
        {"cognitiveLevel": "Application",   "difficultyLevel": "Hard",   "count": app2[1]},
    ]


def resolve_num_questions(payload):
    try:
        num_questions = int(payload.get("numQuestions") or 10)
    except (TypeError, ValueError):
        num_questions = 10
    return max(1, min(num_questions, 100))


# --- Question generation -----------------------------------------------------
def build_prompt(payload):
    curriculum = str(payload.get("curriculum") or "").strip()
    grade = str(payload.get("grade") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    practice_code = str(payload.get("practiceType") or "").strip().lower()
    num_questions = resolve_num_questions(payload)

    if curriculum not in CURRICULA:
        raise ValueError("Select a valid curriculum.")
    if grade not in GRADES:
        raise ValueError("Select a valid grade.")
    if subject not in SUBJECTS:
        raise ValueError("Select a valid subject.")
    if practice_code not in PRACTICE_TYPES:
        raise ValueError("Select Assessment Practice (AP) or Guided Practice (GP).")

    template = pl.get_prompt(curriculum, practice_code, subject)
    if not template:
        raise ValueError(
            f"No {practice_code.upper()} prompt exists for {curriculum} {subject}. "
            "Pick a different curriculum/subject combination."
        )

    script = str(payload.get("script") or "").strip()
    chapter = str(payload.get("chapter") or "").strip()

    # Questions cover the WHOLE chapter (no subtopic / topic-list / LO inputs).
    whole_chapter = (
        f'the entire chapter "{chapter}" — covering all of its subtopics, subconcepts and themes'
        if chapter else "the entire chapter — covering all of its subtopics and subconcepts"
    )
    subtopic = whole_chapter
    topics = whole_chapter
    learning_outcomes = "Cover the key learning outcomes spanning the whole chapter."

    textbook = {"CBSE": "NCERT", "ICSE": "Selina (Concise series)"}.get(curriculum, "NCERT")

    distribution = practice_distribution(practice_code, num_questions)
    text = pl.override_counts(template, distribution)
    # The prompts mix two placeholder conventions (snake_case and CamelCase).
    # NOTE: only exact keys here are replaced, so LaTeX like \text{O}, \frac{a}{b}
    # and \begin{array} are left untouched.
    text = pl.fill(
        text,
        {
            "curriculum": curriculum,
            "grade": grade,
            "GradeNumber": grade,
            "subject": subject,
            "Subject": subject,
            "chapter_name": chapter,
            "chapter": chapter,
            "ChapterName": chapter,
            "topic": subtopic,
            "content_cell": subtopic,
            "subtopic": subtopic,
            "SubtopicName": subtopic,
            "topics": topics,
            "subtopics": topics,
            "learning_outcomes": learning_outcomes,
            "learningoutcomes": learning_outcomes,
            "LearningOutcomes": learning_outcomes,
            "Textbook": textbook,
            "transcipt": script,  # note: misspelled in the source prompts
            "transcript": script,
            "script": script,
            "Script": script,
            "Q_no": num_questions,
        },
    )
    if script:
        text = text.replace("[File Attached]", script)
    scope = (
        f"[SCOPE — OVERRIDE]: Generate questions covering {whole_chapter}. "
        "Spread the questions across the whole chapter; do NOT restrict them to a single subtopic.\n\n"
    )
    text = scope + text
    # Cognitive distribution: 50% Understanding / 30% Application / 20% Factual
    cog_dist = cognitive_distribution(num_questions)
    cog_lines = "\n".join(
        f"  {item['cognitiveLevel']}+{item['difficultyLevel']}: {item['count']} questions"
        for item in cog_dist
    )
    cog_rule = (
        "[COGNITIVE DISTRIBUTION — OVERRIDE, apply across ALL question types]\n"
        f"Distribute the {num_questions} questions by cognitiveLevel+difficultyLevel EXACTLY as:\n"
        f"{cog_lines}\n"
        "Every question must be assigned one of these exact pairs. "
        "Do not use any other cognitiveLevel or difficultyLevel value.\n"
    )

    valid_combo_str = ", ".join(f"{c}+{d}" for c, d in VALID_COMBOS)
    combo_rule = (
        f"Only use these cognitiveLevel+difficultyLevel combinations: {valid_combo_str}. "
        "No other combinations are allowed.\n"
    )

    if practice_code == "gp":
        text += (
            "\n\n========================================================\n"
            "[FINAL OUTPUT RULE — HIGHEST PRIORITY, OVERRIDES ANY EARLIER FORMAT]\n"
            "Output a raw JSON array — do NOT wrap it in markdown code fences (no ```json), "
            "do NOT add any text before [ or after ]. The response must start with [ and end with ].\n"
            "EVERY object — SCQ and especially every RA (Reason-Assertion) — MUST contain the keys "
            "\"questionType\", \"question\", \"options\", \"answers\", and \"solution\". "
            "An RA object without options, answers and solution is INVALID.\n"
            + cog_rule
            + combo_rule +
            "Every RA question object MUST follow this exact shape:\n"
            "{\n"
            '  "cognitiveLevel": "Understanding",\n'
            '  "difficultyLevel": "Medium",\n'
            '  "questionType": "RA",\n'
            '  "question": "<b>Assertion (A)</b>: <statement> <br><b>Reason (R)</b>: <statement>",\n'
            '  "options": [\n'
            '    "Both A and R are true and R is the correct explanation of A",\n'
            '    "Both A and R are true but R is not the correct explanation of A",\n'
            '    "A is true but R is false",\n'
            '    "A is false but R is true"\n'
            "  ],\n"
            '  "answers": ["A is true but R is false"],\n'
            '  "solution": "<one or two line justification of the correct option>"\n'
            "}\n"
            "Use those four option strings verbatim for every RA; set \"answers\" to the one "
            "correct option string; always include \"solution\".\n"
        )
    else:  # ap
        text += (
            "\n\n========================================================\n"
            "[FINAL OUTPUT RULE — HIGHEST PRIORITY, OVERRIDES ANY EARLIER FORMAT]\n"
            "Output a raw JSON array — do NOT wrap it in markdown code fences (no ```json), "
            "do NOT add any text before [ or after ]. The response must start with [ and end with ].\n"
            "Every object MUST have keys: \"questionType\", \"question\", \"cognitiveLevel\", "
            "\"difficultyLevel\", \"solution\".\n"
            + cog_rule
            + combo_rule +
            "Example shape: "
            "{\"questionType\":\"VSA\",\"cognitiveLevel\":\"Factual\",\"difficultyLevel\":\"Easy\","
            "\"question\":\"...\",\"solution\":\"...\"}\n"
        )
    return text, distribution, num_questions


def generate_questions_result(payload):
    practice_code = str(payload.get("practiceType") or "").strip().lower()
    prompt, distribution, num_questions = build_prompt(payload)
    raw = call_model(prompt, payload, max_output_tokens=int(payload.get("maxTokens") or 16000))
    practice = PRACTICE_TYPES.get(practice_code, {})
    parsed = try_parse_questions(raw)
    if parsed:
        parsed = _normalize_questions(parsed)
    return {
        "questions": raw,
        "parsed": parsed,
        "practiceType": practice_code,
        "practiceLabel": practice.get("label", ""),
        "questionStyle": practice.get("questionStyle", ""),
        "numQuestions": num_questions,
        "distribution": distribution,
        "curriculum": payload.get("curriculum", ""),
        "grade": payload.get("grade", ""),
        "subject": payload.get("subject", ""),
        "provider": model_provider_from_payload(payload),
        "model": model_name_from_payload(payload),
    }


def _extract_json_array(text):
    """Find and parse the outermost [...] JSON array in text."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


def try_parse_questions(raw):
    """The prompts emit a JSON array of question objects; return it if parseable."""
    text = str(raw or "").strip()
    # 1. Try fenced code block first (model often wraps despite instructions)
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        result = _extract_json_array(fenced.group(1).strip())
        if result is not None:
            return result
    # 2. Fall back to scanning the full text for [...] array
    return _extract_json_array(text)


# --- Routes ------------------------------------------------------------------
@app.get("/")
def index():
    return render_template(
        "question_generator.html",
        curricula=CURRICULA,
        grades=GRADES,
        subjects=SUBJECTS,
        practice_types=list(PRACTICE_TYPES.values()),
        availability=pl.availability(),
        default_claude_model=DEFAULT_CLAUDE_MODEL,
        claude_models=CLAUDE_MODELS,
        default_gemma_model=DEFAULT_GEMMA_MODEL,
        gemma_models=GEMMA_MODELS,
    )


@app.get("/api/options")
def options_route():
    return jsonify(
        {
            "curricula": CURRICULA,
            "grades": GRADES,
            "subjects": SUBJECTS,
            "practiceTypes": list(PRACTICE_TYPES.values()),
            "availability": pl.availability(),
        }
    )


@app.post("/api/parse-source")
def parse_source_route():
    if "file" not in request.files:
        return jsonify({"error": "Upload a file first."}), 400
    try:
        return jsonify({"text": parse_source_file(request.files["file"])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# --- Async job queue (so long model calls don't hit the Cloudflare 100s limit) --
# Cloudflare's free Quick Tunnel returns HTTP 524 if a single request takes
# longer than ~100s. Generation can take minutes, so we run it in a background
# thread: the client gets a job_id immediately, then polls /api/job/<id>.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _run_job(job_id, payload):
    try:
        result = generate_questions_result(payload)
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "done", "result": result, "ts": time.time()}
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "error", "error": str(exc), "ts": time.time()}


def _prune_jobs(max_age=3600):
    """Drop finished jobs older than max_age so the dict doesn't grow forever."""
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if j.get("status") in ("done", "error") and now - j.get("ts", now) > max_age]
        for jid in stale:
            JOBS.pop(jid, None)


@app.post("/api/generate-questions")
def generate_questions_route():
    """Start generation in the background and return a job_id immediately."""
    try:
        payload = request.get_json(force=True)
    except Exception as exc:
        return jsonify({"error": f"Invalid request body: {exc}"}), 400
    _prune_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "pending", "ts": time.time()}
    threading.Thread(target=_run_job, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.get("/api/job/<job_id>")
def job_status_route(job_id):
    """Poll a generation job. Returns status pending/done/error."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Job not found or expired. Generate again."}), 404
    if job["status"] == "done":
        return jsonify({"status": "done", **job["result"]})
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]}), 400
    return jsonify({"status": "pending"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    from waitress import serve
    # Render (and most hosts) inject the port to bind via $PORT; default to 5001 locally.
    port = int(os.environ.get("PORT", "5001"))
    print(f"Question Generator running on http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port, threads=8)
