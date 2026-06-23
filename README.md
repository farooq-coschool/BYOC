# Question Generator (AP / GP)

A standalone Flask app that generates exam questions from your prompt library.

**Flow:** pick **Curriculum → Grade → Subject → AP/GP**, fill in the lesson content
(chapter, subtopic, topics, learning outcomes, script), and generate. The app picks
the **exact** prompt for that Curriculum + practice type + subject, overrides the
prompt's built-in counts with your requested distribution, and renders the output
with all LaTeX shown as real math (MathJax).

- **GP — Guided Practice** → objective questions: **SCQ + RA**
- **AP — Assessment Practice** → subjective questions: **VSA / SA / LA**

## Distribution (overrides the prompt's built-in counts)
- **GP:** SCQ : RA = 2 : 1  (e.g. 30 → SCQ 20, RA 10)
- **AP:** VSA 50% · SA 30% · LA 20%  (e.g. 30 → VSA 15, SA 9, LA 6)

Counts use a largest-remainder split so they always sum to the requested total.
Edit the ratios in `PRACTICE_DISTRIBUTIONS` in `app.py`.

## Prompts
The prompts live in `prompts_db/` (copied from the MongoDB Compass exports):

| File | Practice | Title in data | Subjects |
|---|---|---|---|
| `CBSE_GP.json` | GP / objective | `auto_objective_questions` | Biology, Chemistry, Physics, Mathematics |
| `CBSE_AP.json` | AP / subjective | `auto_subjective_questions` | Biology, Chemistry, Physics, Mathematics |
| `ICSE_GP.json` | GP / objective | `auto_objective_questions` | + Geography, Civics, History |
| `ICSE_AP.json` | AP / subjective | `auto_subjective_questions` | + Geography, Civics, History |

- Subject is detected from each prompt's role line (`prompt_library.py`).
- **English** has no prompt yet, and **AP / TG** curricula have no files — the UI
  disables any combination without a prompt.
- To add/replace prompts, drop a new export into `prompts_db/` with the same name
  and restart.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your real key into .env
python app.py
```
Open http://localhost:5001 and pick a **Provider** + **Model**. The API key is read
from the server-side `.env` file (`CLAUDE_API_KEY` / `GEMMA_API_KEY`) — it is never
typed in the browser or sent from the client.

## Deploy on Render (runs across computers, permanent URL)
1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com) → **New → Blueprint**, point it at the repo
   (`render.yaml` is already included).
3. In the service's **Environment** tab, add `CLAUDE_API_KEY` (and optionally
   `GEMMA_API_KEY`). Deploy.

Generation runs as a background job (`POST /api/generate-questions` returns a
`job_id`, the UI polls `GET /api/job/<id>`), so long model calls don't hit any
request-timeout limit.

## Endpoints
- `GET /` — the UI
- `GET /api/options` — curricula/grades/subjects/practice types + availability map
- `POST /api/parse-source` — extract text from an uploaded PDF/TXT/MD script
- `POST /api/generate-questions` — start a generation job, returns `{job_id}`
- `GET /api/job/<job_id>` — poll job status (pending / done / error)
- `GET /health` — health check
