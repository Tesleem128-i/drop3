# DROP — The AI Teacher That Never Stops Teaching

A Flask-based AI education platform. Teachers create a classroom and the AI writes the whole
course: weekly lessons, assignments, weekly tests, a midterm and a final exam. Students learn from
the lessons, get instant grading with a "why was this wrong" explanation, chat with an AI tutor,
study alone from any topic or PDF, and level up with XP, streaks and achievements. Teachers get
analytics that show who is struggling before the exam, and tests run in a locked-down secure mode.

The app starts at `/login` or `/signup`.

## Tech stack

Python 3.11, Flask, SQLAlchemy (SQLite locally, PostgreSQL on Render), Flask-Login, bcrypt,
Groq (`openai/gpt-oss-120b`) with OpenRouter as automatic fallback, gunicorn.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                # then add GROQ_API_KEY (https://console.groq.com)
python app.py
```

Open http://localhost:5000.

No API key? Leave `GROQ_API_KEY` blank and keep `AI_MOCK_FALLBACK=1`. Every flow still works with
demo content, and analytics don't need AI at all.

## Deploy on Render

1. Push this folder to a GitHub repo (`.env` is git-ignored, so keys stay private).
2. In Render choose **New → Blueprint** and select the repo. It reads `render.yaml` and creates
   the web service and the PostgreSQL database `drop-db`.
3. When asked, enter your secrets: **GROQ_API_KEY** and, optionally, **OPENROUTER_API_KEY**.
   `SECRET_KEY` and `DATABASE_URL` are filled in automatically.
4. Wait for the deploy, then open the `.onrender.com` URL. Tables are created on first boot.

Things worth knowing:

- **One gunicorn worker on purpose.** The AI engine runs one request at a time with an in-process
  lock, so more workers would multiply AI calls and trip free-tier rate limits.
- **Slow first request.** Free web services sleep when idle, so the first visit can take around
  a minute to wake up. Open the site before a demo.
- **Free database limits.** Free Render PostgreSQL instances are temporary. Check Render's current
  terms and upgrade or back up if you want to keep the data.
- **Uploaded files are not permanent.** Render's free disk resets on each deploy. This is fine
  because extracted text is saved to the database, but the original uploaded files are lost.
- **Check logs after deploy.** On boot DROP prints one line per AI task showing which model and
  key it will use, and whether it is falling back to demo content.
- `/teacher/ai-check` (when logged in as a teacher) sends a tiny real request to each provider.

## Project structure

```
app.py            All Flask routes
config.py         Settings (env vars, DB URL fixes for Render/Windows, secure cookies)
extensions.py     db, login manager, bcrypt
models.py         Database models
ai_engine.py      Groq/OpenRouter routing, prompts, grading, tutor, mock fallback
analytics.py      Study-time, topic accuracy, risk scoring, charts data
scheduling.py     Exam windows, UTC helpers, reminder thresholds
exports.py        CSV / Excel reports
templates/        Jinja2 templates
static/           CSS + JS
render.yaml       Render blueprint
```

## Environment variables

| Name | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing (auto-generated on Render) |
| `DATABASE_URL` | PostgreSQL URL (auto-set on Render). Empty = local SQLite |
| `GROQ_API_KEY`, `GROQ_MODEL` | Primary AI provider |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | Fallback AI provider |
| `AI_MOCK_FALLBACK` | `1` = demo content if AI fails, `0` = show errors |
| `IS_PRODUCTION` | `1` = secure cookies (Render also sets `RENDER` automatically) |

## Known limitations

- Password reset is a placeholder and does not send email.
- Exam proctoring uses browser events (fullscreen, tab visibility), so it deters cheating but
  can't fully prevent it.
- AI-written lessons and questions, especially maths, should be reviewed by a teacher.