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

## Run
```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5001 and paste a **Claude** (Anthropic) or **Gemma**
(OpenRouter) API key in the Model section. The key is sent per request, never stored.

## Endpoints
- `GET /` — the UI
- `GET /api/options` — curricula/grades/subjects/practice types + availability map
- `POST /api/parse-source` — extract text from an uploaded PDF/TXT/MD script
- `POST /api/generate-questions` — select prompt, override counts, fill, call model
