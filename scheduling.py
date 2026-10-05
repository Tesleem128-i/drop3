"""Scheduling helpers for DROP: dates/times, the solo-study schedule, reminders, shuffling.

Storage rule: every datetime in the database is *naive UTC*. Browsers send either an explicit UTC
ISO string (preferred, DST-safe) or a local wall-clock value plus the browser's UTC offset.
Pages show times by emitting UTC ISO strings and letting the browser format them in the viewer's
own timezone (see base.html), so teacher and students in different zones all see the right time.
"""
import random
import re
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Time basics
# ---------------------------------------------------------------------------


def utcnow():
    """Current time as naive UTC (matches what is stored in the DB)."""
    return datetime.now(UTC).replace(tzinfo=None)


def naive_utc(dt):
    """Any datetime -> naive UTC. Naive values are assumed to be UTC already."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def iso_z(dt):
    """'2026-10-04T14:30:00Z' for templates / JSON. None -> ''."""
    dt = naive_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def parse_iso_z(value):
    try:
        value = (value or "").strip()
        if not value:
            return None
        return naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def parse_local(value, tz_offset_minutes=0):
    """'2026-10-04T14:30' (what <input type=datetime-local> sends) + JS getTimezoneOffset() -> naive UTC."""
    try:
        value = (value or "").strip()
        if not value:
            return None
        local = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M")
        off = max(-14 * 60, min(14 * 60, int(tz_offset_minutes or 0)))
        return local + timedelta(minutes=off)
    except (ValueError, TypeError):
        return None


def form_datetime(form, name):
    """Read a date-time field from a submitted form. Prefers the browser-computed '<name>_utc'."""
    dt = parse_iso_z(form.get(f"{name}_utc"))
    if dt is not None:
        return dt
    return parse_local(form.get(name), form.get("tz_offset", 0))


def window_state(start_at, due_at, now=None):
    """'upcoming' (not open yet) | 'open' | 'closed'."""
    now = now or utcnow()
    start_at, due_at = naive_utc(start_at), naive_utc(due_at)
    if start_at and now < start_at:
        return "upcoming"
    if due_at and now > due_at:
        return "closed"
    return "open"


def fmt_countdown(seconds):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    mnt, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {mnt}m"
    return f"{mnt}m {s:02d}s"


# ---------------------------------------------------------------------------
# Shuffling (per student, stable across refreshes)
# ---------------------------------------------------------------------------


def shuffled_layout(questions, seed, shuffle=True):
    """Display order for one student.

    Returns a list of {"index": original question index, "options": [original option positions]}.
    Answers are always stored against the ORIGINAL question index and option TEXT, so grading is
    unaffected by the shuffle.
    """
    rng = random.Random(seed)
    order = list(range(len(questions)))
    if shuffle:
        rng.shuffle(order)
    layout = []
    for qi in order:
        opts = list((questions[qi] or {}).get("options") or [])
        pos = list(range(len(opts)))
        if shuffle:
            rng.shuffle(pos)
        layout.append({"index": qi, "options": pos})
    return layout


# ---------------------------------------------------------------------------
# Solo study: schedule proposed by the AI (pacing) and laid out on the calendar
# ---------------------------------------------------------------------------
DEFAULT_PACING = {"sessions_per_week": 3, "minutes_per_session": 45, "exam_duration_minutes": 45}


def clean_pacing(raw, total_lessons=6):
    """Sanitise the pacing the AI suggested (it can return anything), falling back to sane defaults."""
    raw = raw if isinstance(raw, dict) else {}

    def pick(key, lo, hi, default):
        try:
            return max(lo, min(hi, int(raw.get(key))))
        except (TypeError, ValueError):
            return default

    default_exam = max(20, min(90, 10 * total_lessons))
    return {
        "sessions_per_week": pick("sessions_per_week", 2, 6, DEFAULT_PACING["sessions_per_week"]),
        "minutes_per_session": pick("minutes_per_session", 20, 120, DEFAULT_PACING["minutes_per_session"]),
        "exam_duration_minutes": pick("exam_duration_minutes", 20, 120, default_exam),
        "from_ai": bool(raw),
    }


def build_solo_schedule(course, pacing=None, tz_offset=0, now=None):
    """Lay the course out on a calendar: study slots for each week, a revision day, then the final exam.

    Times of day are chosen in the student's local time (tz_offset = JS getTimezoneOffset()).
    Returns a JSON-able dict with ISO-Z strings; the student can later edit any of it.
    """
    now = now or utcnow()
    weeks = course.get("weeks") or []
    total_lessons = sum(len(w.get("lesson_titles") or []) for w in weeks) or 6
    pace = clean_pacing(pacing, total_lessons)
    off = timedelta(minutes=int(tz_offset or 0))

    local_now = now - off
    first_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # first study slot: this evening if there is time, otherwise tomorrow evening
    study_hour = 18
    if local_now.hour >= study_hour - 1:
        first_day += timedelta(days=1)

    out_weeks, last_end = [], first_day
    for i, w in enumerate(weeks):
        wk_start = first_day + timedelta(days=7 * i)
        n = pace["sessions_per_week"]
        slots = []
        for k in range(n):
            day = wk_start + timedelta(days=round(k * 7 / n))
            slots.append(iso_z(day.replace(hour=study_hour) + off))
        wk_end = wk_start + timedelta(days=7)
        last_end = wk_end
        out_weeks.append({
            "number": w.get("number", i + 1),
            "start": iso_z(wk_start + off),
            "end": iso_z(wk_end + off),
            "slots": slots,
        })

    revision_day = last_end
    exam_day = last_end + timedelta(days=1)
    return {
        "version": 1,
        "generated_by": "ai" if pace["from_ai"] else "default",
        "pacing": {k: pace[k] for k in ("sessions_per_week", "minutes_per_session", "exam_duration_minutes")},
        "tz_offset": int(tz_offset or 0),
        "weeks": out_weeks,
        "revision_at": iso_z(revision_day.replace(hour=study_hour) + off),
        "final_exam": {"at": iso_z(exam_day.replace(hour=study_hour) + off),
                       "duration_minutes": pace["exam_duration_minutes"]},
    }


# ---------------------------------------------------------------------------
# Revision: which lessons does a test cover?
# ---------------------------------------------------------------------------
_WEEK_TITLE_RE = re.compile(r"week\s+(\d+)", re.I)


def covered_lessons(assignment):
    """Generated lessons an assessment is built from (same rules the generators use)."""
    classroom = assignment.classroom
    ready = [w for w in classroom.weeks if w.generated_count()]
    kind = assignment.kind
    if kind == "weekly_test":
        mo = _WEEK_TITLE_RE.search(assignment.title or "")
        if mo:
            ready = [w for w in ready if w.number == int(mo.group(1))] or ready
    elif kind == "midterm":
        ready = ready[:max(1, (len(ready) + 1) // 2)]
    elif assignment.lesson_id:
        wk = [w for w in ready if any(l.id == assignment.lesson_id for l in w.lessons)]
        ready = wk or ready
    return [(w, [l for l in w.lessons if l.is_generated()]) for w in ready]


def lesson_to_section(lesson):
    """Normalise a Lesson row OR a solo-course lesson dict into one dict for the revision page."""
    if hasattr(lesson, "get_json"):
        g = lesson.get_json
        return {
            "title": lesson.title, "objectives": g("objectives"), "definitions": g("definitions"),
            "common_mistakes": g("common_mistakes"), "revision": lesson.revision or "",
            "summary": lesson.summary or "", "practice": g("practice"),
        }
    return {
        "title": lesson.get("title", ""), "objectives": lesson.get("objectives") or [],
        "definitions": lesson.get("definitions") or [], "common_mistakes": lesson.get("common_mistakes") or [],
        "revision": lesson.get("revision") or "", "summary": lesson.get("summary") or "",
        "practice": lesson.get("practice") or [],
    }


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
# (minutes before, human label). When several are already due we only send the closest one.
REMIND_BIG = [(24 * 60, "in 1 day"), (60, "in 1 hour"), (10, "in 10 minutes")]
REMIND_SMALL = [(60, "in 1 hour"), (10, "in 10 minutes")]


def human_until(secs):
    """'in 3 hours', 'in 1 day', 'in 8 minutes' — what is actually left, not the threshold we crossed."""
    secs = max(0, int(secs))
    if secs >= 86400:
        n = round(secs / 86400)
        return f"in {n} day{'s' if n != 1 else ''}"
    if secs >= 3600:
        n = round(secs / 3600)
        return f"in {n} hour{'s' if n != 1 else ''}"
    n = max(1, round(secs / 60))
    return f"in {n} minute{'s' if n != 1 else ''}"


def due_threshold(at, now, thresholds):
    """The tightest threshold already crossed for an event at `at`, or None. Past events -> None.

    Returns (threshold_minutes, human_label_of_time_actually_left).
    """
    secs = (naive_utc(at) - now).total_seconds()
    if secs <= 0:
        return None
    hit = None
    for minutes, _label in thresholds:           # thresholds are ordered largest -> smallest
        if secs <= minutes * 60:
            hit = minutes
    return (hit, human_until(secs)) if hit is not None else None