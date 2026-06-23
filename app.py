"""Question Generator — standalone Flask app.

Curriculum -> Grade -> Subject -> AP/GP. Picks the EXACT prompt for that
combination from the MongoDB Compass exports (see prompt_library.py), fills in
the lesson details, overrides the prompt's built-in counts with the user's
requested distribution, and renders the generated questions (LaTeX -> math).

AP = Assessment Practice (subjective: VSA / SA / LA).
GP = Guided Practice (objective: SCQ + RA).
"""

import base64
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


MAX_UPLOAD_MB = 30
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES + (1024 * 1024)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_REQUEST_BYTES


# --- Global JSON error handlers (prevent Flask from returning HTML error pages) --
@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": f"File too large. Maximum upload size is {MAX_UPLOAD_MB} MB."}), 413


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
ANTHROPIC_FILES_BETA = "files-api-2025-04-14"


def _gemma_key_or_none():
    for name in ("GEMMA_API_KEY", "OPENROUTER_API_KEY"):
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    return None


def _claude_key_or_none():
    for name in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"):
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    return None


def _anthropic_headers(api_key, content_type=None, beta=None):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    if content_type:
        headers["content-type"] = content_type
    if beta:
        headers["anthropic-beta"] = beta
    return headers


def _safe_upload_filename(filename):
    name = Path(str(filename or "document.pdf")).name or "document.pdf"
    name = re.sub(r'[<>:"|?*\\/]', "_", name)
    name = "".join("_" if ord(ch) < 32 else ch for ch in name).strip(" .")
    if not name:
        name = "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:255]


def _anthropic_upload_pdf(data, filename, api_key):
    boundary = f"----QuestionGenerator{uuid.uuid4().hex}"
    safe_name = _safe_upload_filename(filename)
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode("utf-8"),
            b"Content-Type: application/pdf\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/files",
        data=body,
        headers=_anthropic_headers(
            api_key,
            content_type=f"multipart/form-data; boundary={boundary}",
            beta=ANTHROPIC_FILES_BETA,
        ),
        method="POST",
    )
    try:
        uploaded = _open_json(req, timeout=540, attempts=2)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Claude PDF upload failed: {detail}") from exc
    file_id = str(uploaded.get("id") or "").strip()
    if not file_id:
        raise ValueError("Claude PDF upload failed: no file id returned.")
    return file_id


def _anthropic_delete_file(file_id, api_key):
    if not file_id:
        return
    req = urllib.request.Request(
        f"https://api.anthropic.com/v1/files/{file_id}",
        headers=_anthropic_headers(api_key, beta=ANTHROPIC_FILES_BETA),
        method="DELETE",
    )
    try:
        _open_json(req, timeout=60, attempts=1)
    except Exception:
        pass


def _extract_text_from_pdf_with_claude(data, filename):
    api_key = _claude_key_or_none()
    if not api_key:
        return ""

    file_id = None
    try:
        file_id = _anthropic_upload_pdf(data, filename, api_key)
        prompt = (
            "Read this PDF visually and extract the lesson/source text for a question generator. "
            "The PDF may be scanned or image-only, so use OCR-like reading when needed. "
            "Return only the extracted text, preserving headings, paragraph order, lists, formulas, "
            "tables, and important labels. Do not summarize and do not add commentary."
        )
        body = json.dumps(
            {
                "model": DEFAULT_CLAUDE_MODEL,
                "max_tokens": 16000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {"type": "file", "file_id": file_id},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers=_anthropic_headers(
                api_key,
                content_type="application/json",
                beta=ANTHROPIC_FILES_BETA,
            ),
            method="POST",
        )
        try:
            response = _open_json(req, timeout=540, attempts=2)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"Claude PDF reading failed: {detail}") from exc
        text_parts = [
            block.get("text", "")
            for block in response.get("content", [])
            if block.get("type") == "text"
        ]
        return "\n".join(text_parts).strip()
    finally:
        _anthropic_delete_file(file_id, api_key)


def _openrouter_message_text(message):
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return str(content or "").strip()


def _extract_text_from_pdf_with_openrouter(data, filename, model=None):
    api_key = _gemma_key_or_none()
    if not api_key:
        return ""

    selected_model = str(model or DEFAULT_GEMMA_MODEL).strip() or DEFAULT_GEMMA_MODEL
    if "/" not in selected_model or selected_model.startswith("models/"):
        raise ValueError(
            "Gemma PDF reading requires the OpenRouter Gemma model. "
            "Select Gemma (OpenRouter) and set GEMMA_API_KEY or OPENROUTER_API_KEY."
        )

    safe_name = _safe_upload_filename(filename)
    pdf_data = base64.b64encode(data).decode("ascii")
    pdf_engine = str(os.environ.get("OPENROUTER_PDF_ENGINE") or "mistral-ocr").strip() or "mistral-ocr"
    prompt = (
        "Read this PDF visually and extract the lesson/source text for a question generator. "
        "The PDF may be scanned or image-only, so use OCR-like reading when needed. "
        "Return only the extracted text, preserving headings, paragraph order, lists, formulas, "
        "tables, and important labels. Do not summarize and do not add commentary."
    )
    body = json.dumps(
        {
            "model": selected_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "file",
                            "file": {
                                "filename": safe_name,
                                "file_data": f"data:application/pdf;base64,{pdf_data}",
                            },
                        },
                    ],
                }
            ],
            "plugins": [
                {
                    "id": "file-parser",
                    "pdf": {"engine": pdf_engine},
                }
            ],
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
        response = _open_json(req, timeout=540, attempts=2)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Gemma PDF reading failed: {detail}") from exc

    text_parts = []
    for choice in response.get("choices", []):
        text = _openrouter_message_text(choice.get("message") or {})
        if text:
            text_parts.append(text)
    return "\n".join(text_parts).strip()


def _extract_text_from_pdf_with_selected_model(data, filename, payload):
    provider = model_provider_from_payload(payload)
    if provider == "gemma":
        return _extract_text_from_pdf_with_openrouter(data, filename, model_name_from_payload(payload))
    return _extract_text_from_pdf_with_claude(data, filename)


def _missing_pdf_reader_key_message(payload):
    if model_provider_from_payload(payload) == "gemma":
        return (
            "No selectable text was found in this PDF. Set GEMMA_API_KEY or OPENROUTER_API_KEY "
            "on the server to read scanned PDFs with Gemma/OpenRouter, or upload an OCR/text-based PDF."
        )
    return (
        "No selectable text was found in this PDF. Set CLAUDE_API_KEY on the server to read scanned PDFs "
        "with Claude, or upload an OCR/text-based PDF."
    )


def _extract_pdf_text_with_pypdf(data):
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise ValueError("PDF support is not installed. Run: pip install pypdf") from exc

    reader = PdfReader(BytesIO(data), strict=False)
    if reader.is_encrypted:
        decrypt_result = reader.decrypt("")
        if decrypt_result == 0:
            raise ValueError("This PDF is password-protected. Upload an unlocked PDF.")

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n\n".join(part.strip() for part in pages if part.strip()).strip()


def _extract_pdf_text_with_pymupdf(data):
    try:
        import fitz
    except Exception:
        return ""

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ValueError("This PDF is password-protected. Upload an unlocked PDF.")

        pages = []
        for page in doc:
            text = (page.get_text("text") or "").strip()
            if not text:
                blocks = page.get_text("blocks") or []
                text_blocks = [
                    block for block in blocks
                    if len(block) >= 5 and isinstance(block[4], str) and block[4].strip()
                ]
                text_blocks.sort(key=lambda block: (block[1], block[0]))
                text = "\n".join(block[4].strip() for block in text_blocks)
            if text.strip():
                pages.append(text.strip())
        return "\n\n".join(pages).strip()
    finally:
        doc.close()


def _read_source_upload(file_storage):
    filename = str(getattr(file_storage, "filename", "") or "").strip()
    data = file_storage.read()
    if not data:
        raise ValueError("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File too large. Maximum upload size is {MAX_UPLOAD_MB} MB.")
    return filename, data


def parse_source_data(data, filename, payload=None):
    payload = payload or {}
    filename = str(filename or "").strip()
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        try:
            text = _extract_pdf_text_with_pypdf(data)
        except ValueError:
            raise
        except Exception:
            text = ""

        if not text:
            try:
                text = _extract_pdf_text_with_pymupdf(data)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    "Could not read this PDF. Upload a valid, unlocked PDF with selectable text."
                ) from exc

        if not text:
            try:
                text = _extract_text_from_pdf_with_selected_model(data, filename, payload)
            except ValueError:
                raise
            except Exception as exc:
                provider_label = "Gemma/OpenRouter" if model_provider_from_payload(payload) == "gemma" else "Claude"
                raise ValueError(f"{provider_label} PDF reading failed: {exc}") from exc

        if not text and (
            (_gemma_key_or_none() if model_provider_from_payload(payload) == "gemma" else _claude_key_or_none())
            is None
        ):
            raise ValueError(_missing_pdf_reader_key_message(payload))

        if not text:
            provider_label = "Gemma/OpenRouter" if model_provider_from_payload(payload) == "gemma" else "Claude"
            raise ValueError(
                f"No readable text was found in this PDF, even after {provider_label} PDF reading. Try a clearer scan or an OCR/text-based PDF."
            )
        return text

    if suffix not in {".txt", ".text", ".md", ".markdown", ""}:
        raise ValueError("Upload the script/source as .pdf, .txt, or .md.")

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode the text file.")


def parse_source_file(file_storage, payload=None):
    filename, data = _read_source_upload(file_storage)
    return parse_source_data(data, filename, payload)


SOURCE_UPLOADS = {}
SOURCE_UPLOADS_LOCK = threading.Lock()


def _prune_source_uploads(max_age=3600):
    now = time.time()
    with SOURCE_UPLOADS_LOCK:
        stale = [fid for fid, item in SOURCE_UPLOADS.items() if now - item.get("ts", now) > max_age]
        for fid in stale:
            SOURCE_UPLOADS.pop(fid, None)


def store_source_upload(file_storage):
    filename, data = _read_source_upload(file_storage)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".txt", ".text", ".md", ".markdown", ""}:
        raise ValueError("Upload the script/source as .pdf, .txt, or .md.")

    _prune_source_uploads()
    file_id = uuid.uuid4().hex
    with SOURCE_UPLOADS_LOCK:
        SOURCE_UPLOADS[file_id] = {
            "filename": filename or "source",
            "suffix": suffix,
            "size": len(data),
            "data": data,
            "ts": time.time(),
        }
    return {
        "fileId": file_id,
        "filename": filename or "source",
        "suffix": suffix,
        "size": len(data),
    }


def get_source_upload(file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return None
    with SOURCE_UPLOADS_LOCK:
        source = SOURCE_UPLOADS.get(file_id)
        if source:
            source["ts"] = time.time()
        return source


def apply_uploaded_source(payload):
    source_file_id = str((payload or {}).get("sourceFileId") or "").strip()
    if not source_file_id:
        return payload

    source = get_source_upload(source_file_id)
    if not source:
        raise ValueError("Uploaded source file expired or was not found. Upload the PDF again.")

    text = parse_source_data(source["data"], source["filename"], payload)
    payload = dict(payload)
    existing_script = str(payload.get("script") or "").strip()
    payload["script"] = f"{existing_script}\n\n{text}".strip() if existing_script else text
    payload["sourceFileId"] = ""
    return payload


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
        if script not in text:
            text += f"\n\n[SOURCE CONTENT / UPLOADED SCRIPT]\n{script}\n[/SOURCE CONTENT]\n"
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
    payload = apply_uploaded_source(payload)
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
        stored = store_source_upload(request.files["file"])
        return jsonify({"uploaded": True, **stored})
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
