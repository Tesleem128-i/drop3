"""AI engine for DROP.

Every AI feature in the product flows through this module. Two providers:

    Groq        -> the primary model for every job: course breakdown, lesson
                   notes, classwork, assignments, tests, exams, grading, the AI
                   tutor and the analytics write-ups
    OpenRouter  -> fallback + backup: used automatically whenever Groq has no
                   key, is rate-limited, or fails

Which provider handles which job lives in ROUTING below, so re-assigning a task
(e.g. sending lesson notes to OpenRouter first) is a one-word change. Model
names, base URLs and keys come from config.py (i.e. your .env).

If every provider for a job is unavailable, functions degrade to deterministic
demo content so the product still runs end-to-end. The terminal says so loudly
whenever that happens.
"""
import json
import os
import re
import threading
import time
from flask import current_app

try:
    from openai import OpenAI, RateLimitError
except ImportError:  # pragma: no cover
    OpenAI = None
    RateLimitError = None


# ---------------------------------------------------------------------------
# Task -> provider routing. Change a value here to move a job elsewhere.
# ---------------------------------------------------------------------------
ROUTING = {
    "plan": "groq",        # course / solo-course breakdown into weeks + lesson titles
    "lessons": "groq",     # lesson notes, examples, definitions, per-lesson quiz
    "tests": "groq",       # classwork, assignments, weekly tests, midterm, finals
    "grading": "groq",     # auto-grading + misconception detection
    "tutor": "groq",       # AI tutor chat
    "insights": "groq",    # teacher/student analytics write-ups
}

# Strictly ONE AI request at a time, across the whole app. Lessons, their parts, tests and
# everything else are written one after another (free tiers punish parallel requests with
# rate-limit waits, so going one at a time is actually faster overall).
_AI_SLOTS = threading.BoundedSemaphore(1)

# If a task's provider has no key or errors out, retry once on this provider.
DEFAULT_FALLBACK = "openrouter"
FALLBACKS = {task: DEFAULT_FALLBACK for task in ROUTING}

# Reasoning effort for Groq's gpt-oss models. LOW is fast but it makes arithmetic and
# algebra mistakes, so the jobs that must be mathematically right (writing lessons and
# writing test/exam questions) think harder. Everything else stays LOW.
TASK_EFFORT = {"lessons": "medium", "tests": "medium"}
EFFORT_EXTRA_TOKENS = 500   # reasoning tokens count against the completion limit

_PROVIDERS = {
    "groq": {
        "key": "GROQ_API_KEY", "base": "GROQ_BASE_URL", "model": "GROQ_MODEL",
        "default_base": "https://api.groq.com/openai/v1", "default_model": "openai/gpt-oss-120b",
        "native_json": True, "system_role": True,
        # gpt-oss is a reasoning model: reasoning tokens count against the completion
        # limit, so we ask for LOW reasoning effort and leave a little headroom.
        "token_headroom": 800,
        # Groq's free tier caps tokens per request/minute, so never ask for more than this
        # in one call (an oversized request is rejected outright).
        "max_total_tokens": 6000,
    },
    "openrouter": {
        "key": "OPENROUTER_API_KEY", "base": "OPENROUTER_BASE_URL", "model": "OPENROUTER_MODEL",
        "default_base": "https://openrouter.ai/api/v1", "default_model": "openrouter/free",
        # "openrouter/free" picks whichever free model is available, which may not
        # support JSON mode, so we parse JSON out of the text ourselves.
        "native_json": False, "system_role": True, "token_headroom": 1000,
        "max_total_tokens": None,
    },
}


def _extra_kwargs(provider, model, effort="low"):
    """Provider-specific request options."""
    if provider == "groq" and "gpt-oss" in model:
        return {"extra_body": {"reasoning_effort": effort}}
    return {}


def _any_provider_configured():
    """True if at least one AI provider has an API key (so a failure is a REAL failure, not 'no AI set up')."""
    if OpenAI is None:
        return False
    return any(current_app.config.get(spec["key"]) for spec in _PROVIDERS.values())


def _client(provider):
    """Build an OpenAI-compatible client for a provider, or None if unconfigured."""
    spec = _PROVIDERS[provider]
    api_key = current_app.config.get(spec["key"])
    if not api_key or OpenAI is None:
        return None
    base_url = current_app.config.get(spec["base"]) or spec["default_base"]
    return OpenAI(api_key=api_key, base_url=base_url, timeout=120)


def _model_for(provider):
    spec = _PROVIDERS[provider]
    return current_app.config.get(spec["model"]) or spec["default_model"]


# Matches an already-doubled backslash pair FIRST (so it is left alone), otherwise a lone backslash that is
# not a valid JSON escape. Replies often mix correct "\\\\(" with a stray "\\(" in the same string.
_BAD_ESCAPE = re.compile(r'\\\\|\\(?!["\\/bfnrtu])')


def _double_bad_escapes(text):
    return _BAD_ESCAPE.sub(lambda mo: mo.group(0) if len(mo.group(0)) == 2 else "\\\\", text)
# If the model forgets to double a LaTeX backslash, "\frac" silently becomes form-feed + "rac" in JSON.
# These control characters never belong in lesson text, so we turn them back into the LaTeX command.
_LATEX_CTRL = [
    ("\x0c", "f", ("rac", "orall")),
    ("\x08", "b", ("eta", "egin", "inom", "oxed", "ar{", "igl", "igr")),
    ("\t", "t", ("imes", "heta", "ext{", "an(", "an ", "au", "o ", "frac", "ilde")),
    ("\r", "r", ("ight", "ightarrow", "ho")),
    ("\n", "n", ("eq ", "eq\\", "abla", "otin", "eg ")),
]


def _fix_latex_controls(obj):
    if isinstance(obj, str):
        s = obj
        for ch, letter, suffixes in _LATEX_CTRL:
            if ch in s:
                for suffix in suffixes:
                    s = s.replace(ch + suffix, "\\" + letter + suffix)
        return s
    if isinstance(obj, list):
        return [_fix_latex_controls(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_latex_controls(v) for k, v in obj.items()}
    return obj


def _extract_json(text):
    """Parse a JSON object out of a model reply.

    Tries the whole reply, then an unwrapped ``` fence, then the outermost {...}. Each candidate is
    tried as-is and again with un-doubled LaTeX backslashes repaired (a single backslash before a bracket is invalid JSON and
    used to throw away the whole lesson).
    """
    text = text.strip()
    candidates = [text]
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    last_err = None
    for cand in candidates:
        for variant in (cand, _double_bad_escapes(cand)):
            try:
                return _fix_latex_controls(json.loads(variant, strict=False))
            except json.JSONDecodeError as exc:
                last_err = exc
    raise last_err or json.JSONDecodeError("no JSON object found in reply", text, 0)


# --- tokens-per-minute pacing (Groq free tier) -------------------------------------------------------
# Instead of firing requests and waiting 35s after every 429, spread them out so they fit the limit.
# Set GROQ_TPM in .env (e.g. 8000) or leave it: the limit is learned from the first rate-limit error.
try:
    _tpm_limit = int(os.environ.get("GROQ_TPM", "0") or 0)
except ValueError:
    _tpm_limit = 0
_TPM_LOG = []                  # (timestamp, estimated tokens) for the last 60 seconds
_TPM_LOCK = threading.Lock()


def _estimate_tokens(messages, total_max):
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(chars / 3.5 + total_max * 0.7)


def _pace(provider, est_tokens):
    """Sleep (outside the request slot) until this request fits in the provider's per-minute budget."""
    if provider != "groq" or not _tpm_limit:
        return
    while True:
        with _TPM_LOCK:
            now = time.time()
            _TPM_LOG[:] = [(t, n) for t, n in _TPM_LOG if now - t < 60]
            if not _TPM_LOG or sum(n for _, n in _TPM_LOG) + est_tokens <= _tpm_limit:
                _TPM_LOG.append((now, est_tokens))
                return
            wait = 60 - (now - _TPM_LOG[0][0]) + 0.5
        time.sleep(max(wait, 1.0))


def _failed_generation(exc):
    """Groq's JSON mode answers 400 'json_validate_failed' and hands back the text it wrote in
    error.failed_generation. Return that text so we can repair it ourselves instead of losing the lesson."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        try:
            body = exc.response.json()
        except Exception:
            return None
    err = body.get("error", body) if isinstance(body, dict) else None
    text = err.get("failed_generation") if isinstance(err, dict) else None
    return str(text) if text else None


# When a provider's DAILY allowance is used up there is no point asking again for hours: remember it
# and send those requests straight to the fallback provider.
_BLOCKED_UNTIL = {}           # provider -> unix time it should work again


def _parse_wait_seconds(message):
    """'try again in 3m8.784s' / '1h2m' / '34.5s' / '850ms' -> seconds (None if not found)."""
    m = re.search(r"try again in\s+((?:\d+h)?(?:\d+m(?!s))?(?:[\d.]+s)?(?:[\d.]+ms)?)", message)
    if not m or not m.group(1):
        return None
    text, total = m.group(1), 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", text):
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total or None


def _create_with_retry(client, kwargs, max_wait=60, provider=None):
    """Call the API, sleeping and retrying when a provider says "rate limited, try again in Ns"."""
    global _tpm_limit
    for attempt in range(3):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            text = str(exc)
            is_rate_limit = (RateLimitError is not None and isinstance(exc, RateLimitError)) \
                or "rate_limit" in text.lower() or "429" in text[:40]
            if not is_rate_limit:
                raise
            m_lim = re.search(r"tokens per minute.*?Limit (\d+)", text, re.DOTALL | re.IGNORECASE)
            if m_lim and not _tpm_limit:
                _tpm_limit = int(int(m_lim.group(1)) * 0.9)
                current_app.logger.info("[AI] learned Groq limit: pacing requests to ~%d tokens/minute", _tpm_limit)
            parsed = _parse_wait_seconds(text)
            wait = parsed + 1.5 if parsed else 20 * (attempt + 1)
            if "tokens per day" in text.lower() or wait > max_wait:
                if provider and wait > max_wait:
                    _BLOCKED_UNTIL[provider] = time.time() + min(wait, 6 * 3600)
                    current_app.logger.error(
                        "[AI] %s is rate-limited for ~%.0f min (%s) -> using the fallback provider until then",
                        provider, wait / 60, "daily token allowance used up" if "tokens per day" in text.lower() else "limit")
                raise
            if attempt == 2:
                raise
            current_app.logger.warning("[AI] rate limited -> waiting %.0fs then retrying (attempt %d)", wait, attempt + 1)
            time.sleep(wait)


def _fold_system(messages):
    """For models without a system role: merge system text into the first user turn."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    if not system:
        return rest
    if rest and rest[0]["role"] == "user":
        rest[0] = {"role": "user", "content": f"{system}\n\n---\n\n{rest[0]['content']}"}
    else:
        rest.insert(0, {"role": "user", "content": system})
    return rest


def _run(task, messages, json_mode, max_tokens, temperature):
    """Send messages to the model routed for `task` (with optional fallback).

    Returns parsed JSON (json_mode) or text, or None if no provider is
    configured / every attempt failed. Callers treat None as "use mock".
    """
    providers = [ROUTING[task]]
    fallback = FALLBACKS.get(task)
    if fallback and fallback not in providers:
        providers.append(fallback)

    for provider in providers:
        client = _client(provider)
        if client is None:
            current_app.logger.warning(
                "[AI] task=%s provider=%s: %s is not set (check your .env and restart) -> skipping this provider",
                task, provider, _PROVIDERS[provider]["key"],
            )
            continue
        if _BLOCKED_UNTIL.get(provider, 0) > time.time():
            continue          # out of allowance (logged when it happened) -> next provider
        spec = _PROVIDERS[provider]
        try:
            msgs = messages if spec["system_role"] else _fold_system(messages)
            model = _model_for(provider)
            effort = TASK_EFFORT.get(task, "low")
            total = max_tokens + spec["token_headroom"] + (
                EFFORT_EXTRA_TOKENS if (effort == "medium" and provider == "groq") else 0)
            if spec["max_total_tokens"]:
                total = min(total, spec["max_total_tokens"])
            kwargs = dict(model=model, messages=msgs, max_tokens=total, temperature=temperature,
                          **_extra_kwargs(provider, model, effort))
            if json_mode and spec["native_json"]:
                kwargs["response_format"] = {"type": "json_object"}
            t0 = time.time()
            _pace(provider, _estimate_tokens(msgs, total))
            try:
                with _AI_SLOTS:
                    response = _create_with_retry(client, kwargs, provider=provider)
            except Exception as exc:
                salvage = _failed_generation(exc) if json_mode else None
                if not salvage:
                    raise
                try:
                    parsed = _extract_json(salvage)
                    current_app.logger.info("[AI] task=%s: repaired JSON that %s's validator rejected", task, provider)
                    return parsed
                except json.JSONDecodeError:
                    current_app.logger.warning("[AI] task=%s: rejected JSON could not be repaired -> retrying without JSON mode", task)
                    kwargs.pop("response_format", None)
                    with _AI_SLOTS:
                        response = _create_with_retry(client, kwargs, provider=provider)
            usage = getattr(response, "usage", None)
            current_app.logger.info("[AI] task=%s provider=%s took %.1fs, %s completion tokens", task, provider,
                                    time.time() - t0, getattr(usage, "completion_tokens", "?"))
            choice = response.choices[0]
            content = choice.message.content or ""
            finish = getattr(choice, "finish_reason", None)
            if not content.strip():
                raise ValueError(f"empty response (finish_reason={finish}, max_tokens={total}) - "
                                 "the model spent its whole token budget before answering")
            if not json_mode:
                return content
            try:
                return _extract_json(content)
            except json.JSONDecodeError as exc:
                hint = " - output was CUT OFF by the token limit" if finish == "length" else ""
                raise ValueError(f"invalid JSON (finish_reason={finish}, {len(content)} chars){hint}: {exc}") from exc
        except Exception as exc:  # network, rate limit, bad JSON, ...
            current_app.logger.error(
                "[AI] task=%s provider=%s model=%s FAILED -> %s: %s",
                task, provider, _model_for(provider), type(exc).__name__, str(exc)[:500],
            )
    current_app.logger.warning("[AI] task=%s produced no AI result -> using DEMO content", task)
    return None


def _chat(system_prompt, user_prompt, task, json_mode=True, max_tokens=4000, temperature=0.4):
    """One system+user call routed by task. Returns parsed JSON dict / text / None."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return _run(task, messages, json_mode, max_tokens, temperature)



# ---------------------------------------------------------------------------
# Course generation — EVERYTHING IS ON DEMAND
#
# Creating a classroom (or a solo course) now builds only the OUTLINE: weeks and lesson
# titles, one small call. Each week's lessons, each week test, the midterm, the final
# exam and every assignment is generated later, when the teacher / student presses that
# item's Generate button. That keeps every request small (no more waiting minutes for a
# whole course), and a failure only affects the one item that was being generated.
# ---------------------------------------------------------------------------

STAY_IN_NOTES = """

STAY INSIDE THE LECTURE NOTES (very important):
- The lecture notes you are given are the ONLY source of ideas. Use only the concepts, terms, formulas, methods and vocabulary that appear in them.
- Do NOT introduce any new topic, formula, rule, shortcut, term or harder idea that the notes do not contain, even if it is related or true. If a question or example would need something the notes did not teach, leave it out.
- You may use new numbers, names and everyday situations, but every one must be solved with exactly the method taught in the notes.
- Keep the same notation, symbols and wording the notes use. Every answer must be reachable using only what the notes taught.
"""

MATH_RULES = r"""

MATH RULES (very important - follow every one):
1. Write every formula, equation and expression that has a variable or an operator in LaTeX (a plain number like 12 stays plain text). Never write maths in plain text such as x^2, 1/2, sqrt(x) or with unicode symbols (², √, ×, ÷, π, ≤).
2. Delimiters: inline maths is \\( ... \\) and display maths is \\[ ... \\]. Never use $ or $$ or \begin{equation}, and never put a space between the backslash and the bracket.
3. Your reply is JSON, so every LaTeX backslash must be written as TWO backslashes in your reply (they become one when the JSON is read). Write them exactly like this: \\frac{a}{b}, \\sqrt{x}, \\times, \\div, \\cdot, \\pm, \\leq, \\geq, \\neq, \\pi, \\theta, \\text{ cm}, \\left( ... \\right). Never one backslash, never four.
4. Syntax: always use braces for exponents and subscripts longer than one character (x^{10}, a_{n+1}); use \\frac{top}{bottom} for fractions; use \\times or \\cdot for multiplication (never the letter x or *); put units inside \\text{ }, e.g. \\(12\\text{ cm}^2\\).
5. Keep each formula whole inside ONE pair of delimiters. Show each step of a worked solution on its own line, one equation per line.
6. Correct example of a JSON string: "Solve \\(2x + 3 = 11\\). Subtract 3 from both sides: \\[2x = 8\\] Divide both sides by 2: \\[x = \\frac{8}{2} = 4\\]"
7. Accuracy: work out every number before you write it, do the arithmetic a second time, and substitute your final answer back into the original problem to confirm it. Tables, worked examples, practice answers and quiz answers must all agree. Pick numbers that come out cleanly for the level.
8. Stay at the stated grade level; do not use ideas far above it without simple scaffolding.
9. Multiple-choice questions: exactly one correct option; the wrong options come from common mistakes (sign slip, forgetting to square, flipping a fraction); no two options may have the same value; options that contain maths use the same delimiters, e.g. "A. \\(\\frac{3}{4}\\)"; the "answer" is copied exactly from one of the options.
10. If the subject has no real formulas (history, English...), do not force LaTeX. Chemistry formulas are fine, e.g. \\(\\text{H}_2\\text{O}\\).
"""

PLAN_SYSTEM_PROMPT = """You are DROP's AI curriculum architect. Produce a high-level
plan for a course — NOT full lesson content. Always respond with a single valid
JSON object and nothing else, matching exactly this schema:

{
  "overview": "string, short course overview (2-3 sentences)",
  "weeks": [
    {"number": 1, "title": "string", "summary": "string, 1-2 sentences", "lesson_titles": ["string", ...]}
  ]
}

Include 2-3 lesson_titles per week. Keep it concise — this is only an outline.
If a syllabus or notes are provided, follow their order and topics."""

LESSON_STYLE = """You are DROP's AI teacher. You are writing ONE lesson for a student who is studying ALONE,
with no teacher to ask. Write as if you are explaining to a complete beginner, even a young child:
- Use very simple everyday words and short sentences. The first time you use any technical word, explain it in plain language right there.
- Teach with short real-life scenarios (a school canteen, football, a phone battery, a market, cooking, a bus ride, saving pocket money).
- Give each scenario real numbers and walk through it step by step.
- Explain the WHY behind each idea in a sentence or two.
- Be brief and clear. Every section must be short: get to the point, no padding, no repeating yourself.
- Respond with a single valid JSON object and nothing else."""

NOTES_SYSTEM_PROMPT = LESSON_STYLE + r"""

Write the OBJECTIVES and the main LECTURE NOTES for this lesson. Schema:
{
  "objectives": ["3 simple 'you will be able to...' goals"],
  "notes": "markdown lecture notes"
}

The notes MUST be SHORT: about 450-600 words in total, organised under 5 markdown '## ' headings, each only 1-2 short paragraphs, in this order:
1. '## Let's start with a story' - a 2-3 sentence relatable scenario.
2. '## What is it? (in plain words)' - the idea in a few simple sentences with one everyday comparison.
3. '## Seeing it with a real example' - one short scenario with real numbers, a few tiny steps.
4. '## Putting the idea together' - the formal/technical version, briefly.
5. '## The big takeaway' - the one thing to remember, in one or two sentences.
Teach only the topic of this lesson title (and the source material, if given). Do not drift into other lessons of the course.
- Do NOT write definitions, worked-example sets, practice, quiz or homework here - those are written separately.""" + MATH_RULES

DETAIL_SYSTEM_PROMPT = LESSON_STYLE + r"""

You are given the lesson title and its full lecture notes. Write the DEFINITIONS, WORKED EXAMPLES and REAL-LIFE APPLICATIONS using ONLY what the notes teach. Schema:
{
  "definitions": ["Term: simple definition followed by a tiny everyday example", ...],
  "examples": "markdown",
  "applications": "markdown"
}

- definitions: 4-5 key terms that appear in the notes, each ONE sentence in plain words (plus a tiny example only if needed).
- examples: 3 worked examples that use only the method(s) in the notes, easy to harder, each under a '### Example N - short title' heading: one line of situation, then 'Step 1', 'Step 2', ... (one short line each), then a one-line 'Result:'.
- applications: 3-4 real-life uses of exactly the idea in the notes (no new ideas), each under a '### ' heading with just 1-2 sentences.""" + STAY_IN_NOTES + MATH_RULES

PRACTICE_SYSTEM_PROMPT = LESSON_STYLE + r"""

You are given the lesson title and its full lecture notes. Write the PRACTICE MATERIAL using ONLY what the notes teach. Schema:
{
  "common_mistakes": ["a mistake students make and how to avoid it, in simple words", ...],
  "practice": [{"question": "string", "answer": "full worked solution, step by step, ending with the final answer"}, ...],
  "revision": "markdown revision notes with bullet points",
  "summary": "markdown, a friendly summary of 2-3 sentences",
  "homework": ["string", ...],
  "quiz": [{"question": "string", "type": "mcq", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], "answer": "A. ..."}]
}

- common_mistakes: 3, one short sentence each.  practice: 4 questions, easy to harder, each answer showing brief working.
- revision: a short bullet list (4-6 bullets) of the key ideas and formulas.  summary: 2-3 sentences.  homework: 2 short tasks.
- quiz: 4 questions, every one multiple-choice with exactly 4 options and one correct answer. Check each calculation twice; the answer must exactly match one option.
- common_mistakes, practice, revision, summary, homework and quiz must all cover only points that are in the notes.""" + STAY_IN_NOTES + MATH_RULES

WEEKLY_TEST_SYSTEM_PROMPT = """You are DROP's test writer, writing the end-of-week test for
ONE week of a course, strictly grounded in the lesson material given to you. Every question
MUST be multiple-choice (type "mcq") with exactly 4 options and one correct answer — no
short-answer or essay questions. Cover each lesson in the week and don't repeat a fact.
Always respond with a single valid JSON object and nothing else:
{"questions": [{"question": "string", "type": "mcq", "topic": "2-4 word concept this question tests", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], "answer": "A. ..."}]}
Generate exactly the requested number of questions.
Use ONLY concepts, terms, formulas and methods that appear in the material given. Do NOT ask about anything the material does not contain, even if it is related.""" + MATH_RULES

EXAM_SYSTEM_PROMPT = """You are DROP's exam writer, generating objective exam questions strictly
grounded in the course content given to you. Every question must be multiple-choice (type "mcq")
with exactly 4 options and one correct answer. Cover the material thoroughly and avoid repeating
the same fact twice. Always respond with a single valid JSON object and nothing else:
{"questions": [{"question": "string", "type": "mcq", "topic": "2-4 word concept this question tests", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], "answer": "A. ..."}]}
Generate exactly the requested number of questions.
Use ONLY concepts, terms, formulas and methods that appear in the material given. Do NOT ask about anything the material does not contain, even if it is related.""" + MATH_RULES

SOLO_PLAN_SYSTEM_PROMPT = """You are DROP's self-study course planner. A student gave you
a topic and/or a source document (notes, slides, a textbook excerpt). Produce a plan
sized HONESTLY to how much genuinely distinct material is actually there.

Rules:
- Use as many weeks as the material supports, from 1 up to a maximum of 4. Do NOT pad.
- If the source only contains enough distinct content for 1-2 weeks, plan only 1-2 weeks.
  A short, focused course is far better than a long one with repeated or generic filler.
- If there's no source document (topic only, open-ended), 4 weeks is fine.
- Every week must cover genuinely different material — never reuse the same lesson
  titles or restate an earlier week under a new heading.

This is ONLY an outline (no lesson content). Always respond with a single valid JSON object
and nothing else, matching exactly:
{
  "overview": "string, short course overview (2-3 sentences)",
  "weeks": [
    {"number": 1, "title": "string", "summary": "string, 1-2 sentences", "lesson_titles": ["string", ...]}
  ],
  "pacing": {"sessions_per_week": 3, "minutes_per_session": 45, "exam_duration_minutes": 45}
}
Include 2-3 lesson_titles per week.
"pacing" is your recommended study schedule for a typical learner: sessions_per_week (2-6),
minutes_per_session (20-120) and exam_duration_minutes (20-120) for the final exam. Make heavier
or harder material get more sessions and a longer exam."""


_SPACED_DELIMS = re.compile(r"\\\s+([()\[\]])")


_DOLLAR_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_DOLLAR_INLINE = re.compile(r"(?<![\\$])\$([^$\n]*[\\^_][^$\n]*)\$")   # needs a \, ^ or _ inside, so "$5 and $10" is left alone


def _fix_math_delims(text):
    """Make maths renderable: '\\ (x\\)' -> '\\(x\\)' (a stray space stops the renderer) and stray $..$ / $$..$$ -> \\( \\) / \\[ \\]."""
    if not isinstance(text, str):
        return text
    t = _SPACED_DELIMS.sub(lambda mo: "\\" + mo.group(1), text)
    t = _DOLLAR_DISPLAY.sub(lambda mo: "\\[" + mo.group(1).strip() + "\\]", t)
    return _DOLLAR_INLINE.sub(lambda mo: "\\(" + mo.group(1).strip() + "\\)", t)


def _deep_fix_math(obj):
    """Apply _fix_math_delims to every string inside a lesson / question structure."""
    if isinstance(obj, str):
        return _fix_math_delims(obj)
    if isinstance(obj, list):
        return [_deep_fix_math(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_fix_math(v) for k, v in obj.items()}
    return obj


def _normalize_mcq(questions):
    """Make multiple-choice questions safe to grade.

    Guarantees every kept question has options labelled A-D and an `answer` that is character-for-
    character one of those options (the model often answers just "B" or repeats the text without
    the letter, which made correct student answers get marked wrong). Questions whose answer can't
    be matched to an option are dropped rather than shown with a wrong key.
    """
    out = []
    for q in questions or []:
        if not isinstance(q, dict) or not q.get("question"):
            continue
        opts = [_fix_math_delims(str(o).strip()) for o in (q.get("options") or []) if str(o).strip()]
        ans = _fix_math_delims(str(q.get("answer", "")).strip())
        if len(opts) < 2 or not ans:
            continue
        clean = []
        for i, o in enumerate(opts[:4]):
            body = re.sub(r"^[A-Da-d][.)]\s*", "", o)
            clean.append("ABCD"[i] + ". " + body)
        match = None
        m_letter = re.match(r"^([A-Da-d])(?:[.)]|$)", ans)
        if m_letter:
            idx = ord(m_letter.group(1).upper()) - 65
            if idx < len(clean):
                match = clean[idx]
        if match is None:
            bare = re.sub(r"^[A-Da-d][.)]\s*", "", ans).strip().lower()
            for c in clean:
                if c[3:].strip().lower() == bare:
                    match = c
                    break
        if match is None:
            continue
        q = dict(q)
        q["question"] = _fix_math_delims(q["question"])
        q["type"], q["options"], q["answer"] = "mcq", clean, match
        out.append(q)
    return out


def _clean_plan(plan, max_weeks=None):
    """Validate / tidy an outline from the model. Returns None if it is unusable."""
    if not isinstance(plan, dict) or not isinstance(plan.get("weeks"), list):
        return None
    weeks = [w for w in plan["weeks"] if isinstance(w, dict)]
    if max_weeks:
        weeks = weeks[:max_weeks]
    if not weeks:
        return None
    for i, w in enumerate(weeks, 1):
        w["number"] = i
        w["title"] = str(w.get("title") or f"Week {i}")
        w["summary"] = str(w.get("summary") or "")
        titles = [str(x).strip() for x in (w.get("lesson_titles") or []) if str(x).strip()][:4]
        w["lesson_titles"] = titles or [w["title"]]
    return {"overview": str(plan.get("overview") or ""), "weeks": weeks}


def _lesson_material(lessons, limit=9000):
    """Text of a week's lessons (notes first), to ground test questions in what was actually taught.
    The budget is split evenly so no lesson gets cut off completely."""
    per = max(1500, limit // max(1, len(lessons)))
    parts = []
    for lesson in lessons:
        text = (
            f"### {lesson.get('title', '')}\n"
            f"{str(lesson.get('notes', ''))[:3200]}\n"
            f"{str(lesson.get('examples', ''))[:400]}\n"
            f"{str(lesson.get('summary', ''))[:300]}"
        )
        parts.append(text[:per])
    return "\n\n".join(parts)


def _lesson_part(system_prompt, prompt, max_tokens, required, temperature=0.4):
    """One piece of a lesson. Tries twice; returns the parsed dict only if every required field is present."""
    for _ in range(2):
        result = _chat(system_prompt, prompt, task="lessons", max_tokens=max_tokens, temperature=temperature)
        if isinstance(result, dict) and all(result.get(k) for k in required):
            return result
    return None


def _write_lessons(plan_context, week_plan, source_text="", titles=None):
    """Write each lesson in THREE requests so every part can be long and detailed:
       1) objectives + lecture notes, 2) definitions + worked examples + real-life uses, 3) practice + quiz.
    A lesson is only returned if all three parts succeeded, so a failed lesson is retried cleanly later.

    Everything runs strictly one request at a time: part 1, then 2, then 3, then the next lesson.
    """
    app = current_app._get_current_object()
    all_titles = (week_plan.get("lesson_titles") or [week_plan.get("title", "Lesson")])[:4]
    todo = list(titles) if titles is not None else all_titles

    def write_one(title):
        idx = all_titles.index(title) + 1 if title in all_titles else 1
        others = ", ".join(t for t in all_titles if t != title) or "none"
        base = (
            f"Course outline (for context):\n{plan_context[:1500]}\n\n"
            f"Week {week_plan.get('number')}: \"{week_plan.get('title', '')}\" - {week_plan.get('summary', '')}\n"
            f"Other lessons this week (do not repeat them): {others}\n\n"
            f"This lesson (number {idx} of {len(all_titles)}): \"{title}\"\n"
        )
        if source_text:
            base += f"\nSource material from the teacher/student - stay faithful to it:\n{source_text[:2500]}\n"

        part1 = _lesson_part(NOTES_SYSTEM_PROMPT, base, 1500, ("notes",))
        if not part1:
            return None
        notes = str(part1["notes"])
        followup = base + (f"\nHere are the FULL lecture notes. Everything you write must stay inside them - "
                           f"same characters, notation and style, and nothing the notes do not teach:\n{notes}\n")
        part2 = _lesson_part(DETAIL_SYSTEM_PROMPT, followup, 1500, ("examples", "applications"), 0.2)
        if not part2:
            return None
        part3 = _lesson_part(PRACTICE_SYSTEM_PROMPT, followup, 1700, ("practice", "quiz"), 0.2)
        if not part3:
            return None
        quiz = _normalize_mcq(part3.get("quiz"))
        if not quiz:
            return None
        return _deep_fix_math({
            "title": title,
            "objectives": part1.get("objectives") or [],
            "notes": notes,
            "definitions": part2.get("definitions") or [],
            "examples": part2.get("examples", ""),
            "applications": part2.get("applications", ""),
            "common_mistakes": part3.get("common_mistakes") or [],
            "practice": part3.get("practice") or [],
            "revision": part3.get("revision", ""),
            "summary": part3.get("summary", ""),
            "homework": part3.get("homework") or [],
            "quiz": quiz,
        })

    if not todo:
        return []
    t0 = time.time()
    lessons = []
    for title in todo:
        try:
            lesson = write_one(title)
        except Exception:
            app.logger.exception("[AI] writing a lesson crashed")
            lesson = None
        if lesson:
            lessons.append(lesson)
    app.logger.info("[AI] week of %d lesson(s): %d written in %.0fs", len(todo), len(lessons), time.time() - t0)
    return lessons


# ---- outlines -------------------------------------------------------------
def generate_course_plan(subject, duration_weeks, target_grade, syllabus_text=""):
    """Teacher classroom outline only: {"overview", "weeks": [{number,title,summary,lesson_titles}]}."""
    plan_prompt = (
        f"Design an outline for a complete {duration_weeks}-week course.\n"
        f"Subject: {subject}\n"
        f"Target grade/level: {target_grade or 'general'}\n"
        f"Include 2-3 lessons per week.\n"
    )
    if syllabus_text:
        plan_prompt += f"\nBase it on these teacher notes / syllabus:\n{syllabus_text[:6000]}"
    plan = _chat(PLAN_SYSTEM_PROMPT, plan_prompt, task="plan",
                 max_tokens=min(3500, 500 + 130 * duration_weeks))
    return _clean_plan(plan, duration_weeks) or _mock_plan(subject, duration_weeks)


def generate_solo_plan(topic, source_text=""):
    """Solo-study outline only (1-4 weeks sized to the material). Lessons are generated per week later."""
    plan_prompt = f"Topic: {topic}\n"
    if source_text:
        plan_prompt += (
            f"\nSource material the student uploaded — base the plan on what's "
            f"actually here, sizing the number of weeks to match:\n{source_text[:6000]}"
        )
    else:
        plan_prompt += "\nNo source document was given — this is an open topic, plan a full 4-week course.\n"
    plan = _chat(SOLO_PLAN_SYSTEM_PROMPT, plan_prompt, task="plan", max_tokens=1500)
    cleaned = _clean_plan(plan, 4)
    if cleaned:
        # keep the AI's suggested pacing (scheduling.clean_pacing validates it later)
        cleaned["pacing"] = plan.get("pacing") if isinstance(plan.get("pacing"), dict) else None
        return cleaned
    weeks = max(1, min(4, (len(source_text) // 1500) + 1)) if source_text else 4
    return _mock_plan(topic, weeks)


# ---- lessons (one week at a time) ------------------------------------------
def generate_week_lessons(subject, plan, week_plan, source_text="", titles=None, target_grade=""):
    """Write the lessons of ONE week. Returns a list of lesson dicts (may be shorter than asked if
    some failed — the caller can press Generate again to fill the gaps)."""
    plan = plan or {}
    context = json.dumps({
        "subject": subject, "level": target_grade or "general", "overview": plan.get("overview", ""),
        "weeks": [{"number": w.get("number"), "title": w.get("title"), "lessons": w.get("lesson_titles")}
                  for w in plan.get("weeks", [])],
    })
    lessons = _write_lessons(context, week_plan, source_text, titles)
    if not lessons and not _any_provider_configured():
        lessons = _mock_week_lessons(subject, week_plan, titles)
    return lessons


# ---- tests and exams (generated only when asked) -----------------------------
def generate_week_test(subject, week_title, week_number, lessons, count=8):
    """End-of-week test grounded in that week's generated lessons. Returns a list of MCQs ([] on failure)."""
    prompt = (
        f"Course: {subject}\n"
        f"Week {week_number}: {week_title}\n\n"
        f"Lesson material:\n{_lesson_material(lessons)}\n\n"
        f"Generate exactly {count} multiple-choice questions."
    )
    result = _chat(WEEKLY_TEST_SYSTEM_PROMPT, prompt, task="tests", max_tokens=300 + count * 170)
    qs = _normalize_mcq(result.get("questions") if isinstance(result, dict) else None)
    if not qs and not _any_provider_configured():
        qs = _mock_assignment_questions(f"Week {week_number} test", count)
    return qs


def generate_exam(topic, weeks, num_questions=30):
    """Midterm / final exam covering the given weeks (each week: {"number","title","lessons":[dict]}).

    Questions are requested a week at a time to stay inside provider limits, then merged.
    Returns {"questions": [...]} — empty if the AI failed (so the caller can show an error).
    """
    if not weeks:
        return {"questions": []}
    base, remainder = divmod(num_questions, len(weeks))
    questions, seen = [], set()
    for i, week in enumerate(weeks):
        n = base + (1 if i < remainder else 0)
        if n <= 0:
            continue
        prompt = (
            f"Course topic: {topic}\n"
            f"Week {week.get('number')}: {week.get('title', '')}\n\n"
            f"Content to draw questions from:\n{_lesson_material(week.get('lessons', []))}\n\n"
            f"Generate exactly {n} multiple-choice questions from this week's material."
        )
        result = _chat(EXAM_SYSTEM_PROMPT, prompt, task="tests", max_tokens=300 + n * 170)
        for q in _normalize_mcq(result.get("questions") if isinstance(result, dict) else None):
            key = q["question"].strip().lower()
            if key not in seen:
                seen.add(key)
                questions.append(q)
    if not questions and not _any_provider_configured():
        return _mock_exam(topic, num_questions)
    return {"questions": questions[:num_questions]}


# ---- offline demo content (only used when NO api key is configured) ------------
def _mock_plan(subject, weeks):
    return {
        "overview": f"A {weeks}-week course on {subject}. (Demo outline — add GROQ_API_KEY for real AI content.)",
        "weeks": [{
            "number": w, "title": f"{subject}: part {w}", "summary": f"Building blocks of {subject}, part {w}.",
            "lesson_titles": [f"{subject} — Week {w}, Lesson 1", f"{subject} — Week {w}, Lesson 2"],
        } for w in range(1, weeks + 1)],
    }


def _mock_week_lessons(subject, week_plan, titles=None):
    titles = titles or week_plan.get("lesson_titles") or [week_plan.get("title", "Lesson")]
    w = week_plan.get("number") or 1
    out = []
    for l, title in enumerate(titles, 1):
        out.append({
            "title": title,
            "objectives": [f"Understand core concept {l} of week {w}", "Apply it to a real example"],
            "notes": (f"This lesson introduces key ideas in {subject} for week {w}. We build understanding step by "
                      f"step. (Demo content — add an AI key for real lessons.)"),
            "definitions": [f"Key term {l}: a foundational idea in {subject}"],
            "examples": f"Example 1: a worked problem in {subject}.\n\nExample 2: a second worked problem.",
            "applications": f"This concept shows up in everyday uses of {subject}.",
            "common_mistakes": ["Confusing related terms", "Skipping steps under time pressure"],
            "practice": [{"question": f"Practice question {l} for week {w}", "answer": "See lecture notes"}],
            "revision": f"Review the definitions and worked examples from week {w}.",
            "summary": f"Week {w} lesson {l} covered a core building block of {subject}.",
            "homework": [f"Complete practice set {l}"],
            "quiz": _mock_assignment_questions(title, 3),
        })
    return out


def _mock_exam(topic, num_questions=30):
    return {"questions": _mock_assignment_questions(f"{topic} exam", num_questions)}


# ---------------------------------------------------------------------------
# Assignment / test question generation (grounded in the teacher's uploaded
# syllabus/notes for that classroom, not a whole separate generated course)
# ---------------------------------------------------------------------------
ASSIGNMENT_SYSTEM_PROMPT = """You are DROP's assignment writer. Generate a set of
questions for a single assignment/test, tightly matched to the given title,
description, and kind. If source material is provided, base the questions
directly on it — reuse its terminology, examples, and specific content rather
than generic textbook questions. Every question MUST be multiple-choice (type
"mcq") with exactly 4 options and one correct answer — no short-answer or essay
questions. Always respond with a single valid JSON object:
{"questions": [{"question": "string", "type": "mcq",
"topic": "2-4 word concept this question tests", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], "answer": "A. ..."}]}
Generate exactly the requested number of questions, difficulty appropriate to
the kind (quick check for classwork, more rigorous for a test/exam).
If source material is given, ask ONLY about what it contains: no outside facts, formulas or topics.""" + MATH_RULES

DEFAULT_QUESTION_COUNTS = {"classwork": 5, "assignment": 8, "weekly_test": 8, "monthly_test": 12,
                           "midterm": 15, "final_exam": 25}


def generate_assignment_questions(subject, title, description, kind, source_text="", count=None):
    """Questions for ONE assessment, generated when the teacher presses Generate.

    Big sets are written in batches of 8 so no single request gets huge. Returns [] if the AI
    failed (the caller shows an error); demo questions are used only when no AI key is set at all.
    """
    count = count or DEFAULT_QUESTION_COUNTS.get(kind, 5)
    questions = []
    for _ in range(6):
        n = min(8, count - len(questions))
        if n <= 0:
            break
        prompt = (
            f"Subject: {subject}\n"
            f"Assignment title: {title}\n"
            f"Kind: {kind}\n"
            f"Description/instructions from the teacher: {description or '(none given)'}\n"
            f"Number of questions: {n}\n"
        )
        if source_text:
            prompt += f"\nBase the questions closely on this classroom material:\n{source_text[:7000]}"
        if questions:
            prompt += "\n\nDo NOT repeat or rephrase these questions already written:\n" + "\n".join(
                f"- {q['question'][:80]}" for q in questions[-20:])
        result = _chat(ASSIGNMENT_SYSTEM_PROMPT, prompt, task="tests", max_tokens=300 + n * 170)
        got = _normalize_mcq(result.get("questions") if isinstance(result, dict) else None)
        if not got:
            break
        questions.extend(got)
    if not questions and not _any_provider_configured():
        return _mock_assignment_questions(title, count)
    return questions[:count]


def _mock_assignment_questions(title, count=5):
    return [
        {"question": f"Question {i + 1} about {title}", "type": "mcq",
         "topic": title, "options": ["A. Option A", "B. Option B", "C. Option C", "D. Option D"], "answer": "A. Option A"}
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# AI Tutor
# ---------------------------------------------------------------------------
TUTOR_SYSTEM_PROMPT = """You are the DROP AI Tutor: warm, encouraging, and extremely clear.
You teach one concept at a time, check understanding, and adapt your explanation style
(simple, visual, mathematical, or "explain like I'm 10") based on what the student asks for.
Keep answers focused and well-structured with short paragraphs, examples, and, when useful,
a short follow-up question to check understanding. Respond in plain text (not JSON).

FORMATTING RULES — the chat window only displays plain text, so:
- NEVER use LaTeX. No \\( \\), \\[ \\], $, $$, \\frac, \\boxed, or any other LaTeX commands or delimiters.
- Write all math in plain, calculator-style notation instead: x^x, (ln(x) + 1), sqrt(x), a/b, x^2 + 3x - 5.
- Do not use Markdown headers (#, ##), horizontal rules (---), or emoji numbering (1️⃣, 2️⃣). Use plain
  numbered lists (1., 2., 3.) or short paragraphs instead.
- NEVER use asterisks (*) or underscores for bold or italics, and never use backticks. Write plain words only.
  For bullet points start the line with "- ". For emphasis, just use a clear sentence.
- Keep it readable as plain chat text — no tables, no boxed answers, no decorative symbols like ✅ or ---."""


_LATEX_WORDS = {
    r"\cdot": "·", r"\times": "×", r"\div": "÷", r"\pm": "±", r"\pi": "π", r"\Delta": "Δ", r"\delta": "δ",
    r"\theta": "θ", r"\alpha": "α", r"\beta": "β", r"\lambda": "λ", r"\mu": "μ", r"\sigma": "σ",
    r"\to": "→", r"\rightarrow": "→", r"\infty": "∞", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\approx": "≈", r"\left": "", r"\right": "", r"\,": " ", r"\;": " ", r"\!": "",
}


def _plainify_math(text):
    """The tutor chat shows plain text only. If the model writes LaTeX anyway, turn it into readable
    calculator-style text instead of showing raw backslashes."""
    t = re.sub(r"\$\$(.+?)\$\$", r"\1", text, flags=re.DOTALL)
    t = re.sub(r"\$([^$\n]*[\\^_][^$\n]*)\$", r"\1", t)
    t = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", t)
    for _ in range(3):
        t = re.sub(r"\\d?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
        t = re.sub(r"\\sqrt\{([^{}]*)\}", r"sqrt(\1)", t)
        t = re.sub(r"\\(?:text|mathrm|boxed|mathbf)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\^\{([^{}]*)\}", lambda mo: "^" + (mo.group(1) if len(mo.group(1)) == 1 else f"({mo.group(1)})"), t)
    t = re.sub(r"_\{([^{}]*)\}", r"_\1", t)
    for k in sorted(_LATEX_WORDS, key=len, reverse=True):
        t = t.replace(k, _LATEX_WORDS[k])
    t = re.sub(r"\\(sin|cos|tan|ln|log|lim|exp)\b", r"\1", t)
    return t


def _plainify_markdown(text):
    """The tutor chat shows plain text, so asterisks / hashes / rules would appear literally.
    Remove Markdown decoration but keep the structure (paragraphs, numbered lists, bullets)."""
    t = str(text or "").replace("\r\n", "\n")
    t = re.sub(r"```[a-zA-Z0-9]*\n?(.*?)```", r"\1", t, flags=re.DOTALL)           # code fences
    t = re.sub(r"(?m)^\s*([-*_])(?:\s*\1){2,}\s*$", "", t)                         # --- / *** / ___ rules
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)                                      # # headings
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)                                           # > quotes
    t = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", t, flags=re.DOTALL)                    # ***bold italic***
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.DOTALL)                          # **bold**
    t = re.sub(r"(?<![\w])__(.+?)__(?![\w])", r"\1", t, flags=re.DOTALL)              # __bold__
    t = re.sub(r"(?m)^(\s*)\*\s+", r"\1- ", t)                                       # "* item" -> "- item"
    t = re.sub(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])", r"\1", t)   # *italic* (not "a * b")
    t = re.sub(r"`([^`\n]+)`", r"\1", t)                                              # `code`
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def plainify_text(text):
    """Everything the tutor chat needs: no LaTeX, no Markdown symbols."""
    return _plainify_markdown(_plainify_math(str(text or "")))


def tutor_reply(history, student_message, mode="default", lesson_context=""):
    """history: list of {'role': 'user'|'assistant', 'content': str}
    lesson_context: optional lesson title, e.g. from the small per-lesson
    "ask about this lesson" box, so the reply stays relevant to what the
    student is currently looking at."""
    mode_hints = {
        "simplify": "Simplify your explanation as much as possible.",
        "eli10": "Explain like the student is 10 years old, using simple analogies.",
        "visual": "Describe it visually, as if sketching a diagram in words (still no LaTeX).",
        "math": "Be precise and rigorous, but keep all notation in plain text (x^2, not LaTeX).",
        "examples": "Focus on generating several worked examples.",
        "default": "",
    }
    system = TUTOR_SYSTEM_PROMPT
    if lesson_context:
        system += (
            f"\n\nThe student is currently viewing the lesson \"{lesson_context}\". "
            f"Assume their question relates to it unless it clearly doesn't."
        )
    if mode_hints.get(mode):
        system += f"\n\nSpecial instruction for this reply: {mode_hints[mode]}"

    messages = [{"role": "system", "content": system}]
    messages.extend({"role": h["role"], "content": plainify_text(h["content"]) if h["role"] == "assistant" else h["content"]}
                    for h in history[-20:])
    messages.append({"role": "user", "content": student_message})

    reply = _run("tutor", messages, json_mode=False, max_tokens=1200, temperature=0.6)
    return plainify_text(reply) if reply else _mock_tutor_reply(student_message, mode)


def _mock_tutor_reply(student_message, mode):
    return (
        f"(Offline demo tutor — add GROQ_API_KEY and/or OPENROUTER_API_KEY for real AI answers)\n\n"
        f"Great question about: \"{student_message}\". Here's a step-by-step explanation:\n"
        f"1. Let's identify what the question is really asking.\n"
        f"2. We break the idea into smaller parts.\n"
        f"3. We connect it to something you already know.\n"
        f"4. We check with a quick example.\n\n"
        f"Want me to simplify this further, give more examples, or quiz you on it?"
    )


# ---------------------------------------------------------------------------
# Auto-grading & the "Understanding Engine"
# ---------------------------------------------------------------------------
GRADING_SYSTEM_PROMPT = """You are DROP's auto-grader and Understanding Engine. Grade the
student's answer against the reference answer/rubric. Never just mark right or wrong —
diagnose WHY a wrong answer is wrong. Respond with a single JSON object:
{
  "score": 0-100,
  "is_correct": true/false,
  "feedback": "specific, encouraging feedback in 2-3 sentences",
  "misconception": "one of: none, calculation_error, concept_misunderstanding, guess,
                     carelessness, formula_forgotten, vocabulary_misunderstanding",
  "reteach_tip": "one short sentence on what to review"
}"""


def _mcq_correct(student_answer, reference_answer):
    """Exact-match check for multiple-choice answers ("B. 12" vs "B" vs "b)")."""
    sel, ans = str(student_answer or "").strip(), str(reference_answer or "").strip()
    if not sel or not ans:
        return False
    if sel.lower() == ans.lower():
        return True
    m_sel = re.match(r"^([A-Da-d])[.)]", sel)
    m_ans = re.match(r"^([A-Da-d])(?:[.)]|$)", ans)
    return bool(m_sel and m_ans and m_sel.group(1).lower() == m_ans.group(1).lower())


def grade_answer(question, reference_answer, student_answer, question_type="short_answer"):
    # A correct multiple-choice answer needs no AI call — it's an exact match.
    # (Wrong ones still go to the model so we can diagnose the misconception.)
    if question_type == "mcq" and _mcq_correct(student_answer, reference_answer):
        return {"score": 100, "is_correct": True, "feedback": "Correct — well done.",
                "misconception": "none", "reteach_tip": ""}
    prompt = (
        f"Question type: {question_type}\n"
        f"Question: {question}\n"
        f"Reference answer: {reference_answer}\n"
        f"Student answer: {student_answer}\n"
    )
    result = _chat(GRADING_SYSTEM_PROMPT, prompt, task="grading", max_tokens=500)
    if result is None:
        return _mock_grade(reference_answer, student_answer)
    if question_type == "mcq":  # never let the model contradict an exact-match wrong answer
        result["is_correct"] = False
        result["score"] = min(result.get("score", 0) or 0, 40)
    return result


def _mock_grade(reference_answer, student_answer):
    correct = str(student_answer).strip().lower() == str(reference_answer).strip().lower()
    return {
        "score": 100 if correct else 40,
        "is_correct": correct,
        "feedback": "Nice work, that matches the expected answer." if correct else
                    "Not quite — review the lesson notes and try comparing your steps to the worked example.",
        "misconception": "none" if correct else "concept_misunderstanding",
        "reteach_tip": "" if correct else "Revisit the definitions section of this lesson.",
    }


# ---------------------------------------------------------------------------
# Analytics insights
# ---------------------------------------------------------------------------
INSIGHTS_SYSTEM_PROMPT = """You are DROP's classroom analytics assistant. Given aggregate
class data, produce concise, actionable insights for a teacher. Respond with JSON:
{"insights": ["string", "string", "string"]}"""


def generate_class_insights(stats_summary):
    result = _chat(INSIGHTS_SYSTEM_PROMPT, json.dumps(stats_summary), task="insights", max_tokens=600)
    if result is None:
        return {"insights": [
            "Average scores are steady — consider adding a stretch challenge for top performers.",
            "A few students have incomplete assignments this week; a reminder nudge could help.",
            "Revisit topics with the lowest quiz accuracy in the next class session.",
        ]}
    return result

# ---------------------------------------------------------------------------
# Strengths / weaknesses analysis (written from the numbers in analytics.py)
# ---------------------------------------------------------------------------
STUDENT_ANALYSIS_SYSTEM_PROMPT = """You are DROP's learning analyst. You are given measured
learning data for ONE student (scores, study time, per-topic accuracy, types of mistakes).
Write an honest, specific, constructive analysis grounded ONLY in the numbers provided — never
invent topics or figures. If data is thin, say so. Always respond with a single JSON object:
{
  "summary": "2-3 sentences: how the student is doing overall and the single most important thing",
  "strengths": [{"topic": "string", "note": "short evidence-based note"}],
  "weaknesses": [{"topic": "string", "note": "what is going wrong, citing the numbers", "fix": "one concrete step"}],
  "study_habits": "1-2 sentences on how their study time relates to their results",
  "recommendations": ["3-4 short, encouraging, student-facing next steps"],
  "teacher_actions": ["2-3 concrete things the teacher could do to help this student"]
}"""

CLASS_ANALYSIS_SYSTEM_PROMPT = """You are DROP's classroom analyst. You are given aggregate learning data
for a whole class (per-topic accuracy, how study time relates to scores, common mistake types, per-student
summary). Write specific, actionable analysis grounded ONLY in the numbers provided. Always respond with a
single JSON object:
{
  "summary": "2-3 sentences on the state of the class",
  "class_strengths": [{"topic": "string", "note": "short evidence"}],
  "class_weaknesses": [{"topic": "string", "note": "short evidence incl. how many students struggle", "reteach": "how to re-teach it"}],
  "study_time_insight": "1-2 sentences on how study time relates to scores in this class",
  "teaching_actions": ["3-5 prioritised actions for the teacher this week"],
  "students_needing_attention": [{"name": "string", "reason": "string"}],
  "students_to_celebrate": [{"name": "string", "reason": "string"}]
}"""


def analyze_student(payload):
    """AI notes for one student. Returns (data_dict, source) with source 'ai' or 'rules'."""
    result = _chat(STUDENT_ANALYSIS_SYSTEM_PROMPT, json.dumps(payload), task="insights", max_tokens=1200)
    if result and result.get("summary"):
        return result, "ai"
    return _mock_student_analysis(payload), "rules"


def analyze_class(payload):
    """AI notes for a whole class. Returns (data_dict, source)."""
    result = _chat(CLASS_ANALYSIS_SYSTEM_PROMPT, json.dumps(payload), task="insights", max_tokens=1600)
    if result and result.get("summary"):
        return result, "ai"
    return _mock_class_analysis(payload), "rules"


def _mock_student_analysis(p):
    weak, strong = p.get("weak_topics") or [], p.get("strong_topics") or []
    avg = p.get("average_score")
    if avg is None and not weak and not strong:
        summary = "Not enough activity yet to analyse this student — check back after a few quizzes."
    else:
        summary = (f"Average score is {avg}%. " if avg is not None else "") + (
            f"Strongest in {strong[0]['topic']}. " if strong else "") + (
            f"Needs most help with {weak[0]['topic']} ({weak[0]['accuracy']}% correct)." if weak else "")
    mistakes = p.get("mistake_types") or []
    return {
        "summary": summary.strip(),
        "strengths": [{"topic": t["topic"], "note": f"{t['accuracy']}% correct over {t['attempts']} questions."} for t in strong],
        "weaknesses": [{"topic": t["topic"], "note": f"Only {t['accuracy']}% correct over {t['attempts']} questions.",
                        "fix": "Re-read the lesson notes, then retry the practice questions."} for t in weak],
        "study_habits": (f"{p.get('total_study_minutes', 0)} minutes studied across {p.get('active_days', 0)} days."
                         if p.get("total_study_minutes") else "No study time recorded yet."),
        "recommendations": (["Revisit " + weak[0]["topic"] + " before moving on."] if weak else []) + [
            "Study in short, regular sessions rather than one long push.",
            "Use the AI tutor to explain anything that feels unclear."] + (
            [f"Watch out for {mistakes[0]['type'].lower()} — it is your most common slip."] if mistakes else []),
        "teacher_actions": (["Offer a short re-teach of " + weak[0]["topic"] + "."] if weak else []) + [
            "Check in with the student about how they are finding the course."],
    }


def _mock_class_analysis(p):
    hard, easy = p.get("hardest_topics") or [], p.get("strongest_topics") or []
    students = p.get("students") or []
    return {
        "summary": f"Class average is {p.get('average_score')}% with {p.get('completion_rate')}% of assessments completed.",
        "class_strengths": [{"topic": t["topic"], "note": f"{t['accuracy']}% correct class-wide."} for t in easy],
        "class_weaknesses": [{"topic": t["topic"],
                              "note": f"{t['accuracy']}% correct; {t.get('students_struggling', 0)} students struggling.",
                              "reteach": "Re-teach with worked examples, then a short low-stakes quiz."} for t in hard],
        "study_time_insight": ("More study time is linked to higher scores." if (p.get("study_time_vs_score_correlation") or 0) > 0.3
                               else "Study time shows no strong link to scores yet."),
        "teaching_actions": ([f"Re-teach {hard[0]['topic']} next class."] if hard else []) + [
            "Follow up with students who have gone quiet this week.",
            "Share a quick recap of the topics the class found easiest to keep confidence up."],
        "students_needing_attention": [{"name": s["name"], "reason": f"Risk level {s['risk']}."}
                                       for s in students if s.get("risk") == "high"][:5],
        "students_to_celebrate": [{"name": s["name"], "reason": f"Averaging {s['avg_score']}%."}
                                  for s in sorted((x for x in students if x.get("avg_score") is not None),
                                                  key=lambda x: -x["avg_score"])[:3] if s["avg_score"] >= 85],
    }



# ---------------------------------------------------------------------------
# Diagnostics: "why is the AI giving me demo content?"
# ---------------------------------------------------------------------------
def startup_summary():
    """One line per task showing which model/key it will use. Printed at app start."""
    lines = []
    for task, provider in list(ROUTING.items()) + [("fallback", DEFAULT_FALLBACK)]:
        spec = _PROVIDERS[provider]
        key = current_app.config.get(spec["key"]) or ""
        fb = FALLBACKS.get(task)
        fb_ok = bool(fb and fb != provider and current_app.config.get(_PROVIDERS[fb]["key"]))
        state = (f"set ({len(key)} chars)" if key else
                 f"MISSING -> will use {fb} fallback" if fb_ok else "MISSING -> DEMO content")
        lines.append(f"[DROP] AI {task:<8} -> {provider:<6} model={_model_for(provider)}  {spec['key']}={state}")
    return "\n".join(lines)


def diagnose():
    """Send a tiny real request to each configured provider and report the result.

    Returns a list of dicts: provider, model, key_env, base_url, key_set, ok, ms, error.
    """
    results, tested = [], {}
    for task, provider in list(ROUTING.items()) + [("fallback", DEFAULT_FALLBACK)]:
        if provider in tested:
            tested[provider]["tasks"].append(task)
            continue
        spec = _PROVIDERS[provider]
        entry = {
            "provider": provider, "tasks": [task], "model": _model_for(provider),
            "key_env": spec["key"], "key_set": bool(current_app.config.get(spec["key"])),
            "base_url": current_app.config.get(spec["base"]) or spec["default_base"],
            "ok": False, "ms": None, "error": None,
        }
        client = _client(provider)
        if client is None:
            entry["error"] = f"{spec['key']} is not set in the environment / .env"
        else:
            started = time.time()
            try:
                resp = client.chat.completions.create(
                    model=entry["model"], temperature=0,
                    messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                    max_tokens=30 + spec["token_headroom"], **_extra_kwargs(provider, entry["model"]),
                )
                entry["ok"] = True
                entry["reply"] = (resp.choices[0].message.content or "").strip()[:60]
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:600]}"
            entry["ms"] = int((time.time() - started) * 1000)
        tested[provider] = entry
        results.append(entry)
    return results