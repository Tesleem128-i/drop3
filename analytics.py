"""Learning analytics for DROP.

Turns the raw tracking tables (StudyTimeLog, QuestionAttempt, graded
Submissions, LessonProgress) into the numbers behind every chart:

    student_stats()    -> one student's time, topics, errors, trend
    classroom_stats()  -> the whole class: study-time-vs-score, topic
                          accuracy, heatmap, error types, risk, ...
    refresh_student_metrics() -> keeps Enrollment.risk_level /
                          predicted_grade and LearningProfile strengths &
                          weaknesses up to date (rules-based, no AI needed)

Everything here is plain SQL + arithmetic. The AI (ai_engine) only writes the
*narrative* on top of these numbers, so the dashboards work even with no API
key configured.
"""
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import selectinload

import models as m
from extensions import db

# --- tunables ---------------------------------------------------------------
MIN_ATTEMPTS = 2        # attempts needed before a topic counts as strong/weak
WEAK_BELOW = 60         # topic accuracy (%) under this is a weakness
STRONG_AT = 85          # topic accuracy (%) at/above this is a strength
INACTIVE_DAYS = 7       # no activity for this long => "inactive"
MAX_SESSION_SECONDS = 120  # most seconds the tracker may credit per request

MISCONCEPTION_LABELS = {
    "calculation_error": "Calculation errors",
    "concept_misunderstanding": "Concept misunderstanding",
    "guess": "Guessing",
    "carelessness": "Carelessness",
    "formula_forgotten": "Forgotten formula",
    "vocabulary_misunderstanding": "Vocabulary confusion",
}

KIND_LABELS = {
    "lesson_quiz": "Lesson quiz", "classwork": "Classwork", "assignment": "Assignment",
    "weekly_test": "Weekly test", "monthly_test": "Monthly test", "midterm": "Midterm",
    "final_exam": "Final exam", "solo_quiz": "Solo quiz", "solo_exam": "Solo exam",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _utc_naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _day(dt):
    if dt is None:
        return None
    return dt.date() if isinstance(dt, datetime) else dt


def _monday(d):
    return d - timedelta(days=d.weekday())


def _fmt(d):
    return d.strftime("%b %d").replace(" 0", " ")


def _avg(values, nd=1):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), nd) if values else None


def pearson(xs, ys):
    """Pearson correlation, or None when there isn't enough signal."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / math.sqrt(sxx * syy), 2)


def linear_fit(xs, ys):
    """Least-squares (slope, intercept) or None."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def describe_correlation(r, n):
    if r is None:
        return "Not enough data yet to measure how study time relates to scores."
    strength = ("negligible" if abs(r) < 0.2 else "weak" if abs(r) < 0.4 else
                "moderate" if abs(r) < 0.6 else "strong" if abs(r) < 0.8 else "very strong")
    if strength == "negligible":
        text = "Study time and scores show no clear link in this class so far."
    elif r > 0:
        text = f"A {strength} positive link: students who study more tend to score higher."
    else:
        text = (f"A {strength} negative link: more study time goes with lower scores — "
                "often a sign that the struggling students are working hard but studying "
                "ineffectively, and need a different approach rather than more time.")
    if n < 8:
        text += " (Small group — treat this as a hint, not a rule.)"
    return text


def _letter(score):
    if score is None:
        return None
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"


# ---------------------------------------------------------------------------
# Recording (called from the routes in app.py)
# ---------------------------------------------------------------------------
def add_study_seconds(student_id, seconds, lesson=None, classroom_id=None, study_session_id=None,
                      new_session=False):
    """Add active-study seconds to today's row for a lesson or solo session."""
    seconds = max(0, min(int(seconds), MAX_SESSION_SECONDS))
    if seconds == 0:
        return
    today = datetime.now(timezone.utc).date()
    q = m.StudyTimeLog.query.filter_by(student_id=student_id, day=today)
    q = q.filter_by(lesson_id=lesson.id) if lesson is not None else q.filter(m.StudyTimeLog.lesson_id.is_(None))
    q = q.filter_by(study_session_id=study_session_id) if study_session_id else q.filter(
        m.StudyTimeLog.study_session_id.is_(None))
    row = q.first()
    if row is None:
        row = m.StudyTimeLog(
            student_id=student_id, classroom_id=classroom_id,
            lesson_id=lesson.id if lesson is not None else None,
            study_session_id=study_session_id, day=today, seconds=0, sessions=1,
        )
        db.session.add(row)
    elif new_session:
        row.sessions = (row.sessions or 1) + 1
    row.seconds = (row.seconds or 0) + seconds
    row.updated_at = datetime.now(timezone.utc)
    db.session.commit()


def record_lesson_quiz_attempts(student_id, classroom_id, lesson, results):
    """results: list[bool] — one entry per quiz question, in order.

    A retake replaces the previous attempts so accuracy isn't double counted.
    """
    m.QuestionAttempt.query.filter_by(
        student_id=student_id, lesson_id=lesson.id, source="lesson_quiz"
    ).delete()
    for ok in results:
        db.session.add(m.QuestionAttempt(
            student_id=student_id, classroom_id=classroom_id, lesson_id=lesson.id,
            source="lesson_quiz", topic=lesson.title, category=KIND_LABELS["lesson_quiz"],
            is_correct=bool(ok), score=100.0 if ok else 0.0,
            misconception="none" if ok else None,
        ))


def record_assessment_attempts(submission, assignment, feedback_list):
    """One QuestionAttempt per graded question of a submission (idempotent)."""
    m.QuestionAttempt.query.filter_by(submission_id=submission.id).delete()
    category = KIND_LABELS.get(assignment.kind, assignment.kind.replace("_", " ").title())
    for i, q in enumerate(assignment.questions()):
        fb = feedback_list[i] if i < len(feedback_list) and isinstance(feedback_list[i], dict) else {}
        score = fb.get("score")
        is_correct = fb.get("is_correct")
        if is_correct is None:
            is_correct = bool(score is not None and score >= 70)
        db.session.add(m.QuestionAttempt(
            student_id=submission.student_id, classroom_id=assignment.classroom_id,
            assignment_id=assignment.id, submission_id=submission.id, source="assessment",
            topic=(q.get("topic") or assignment.title)[:200], category=category,
            is_correct=bool(is_correct), score=score,
            misconception=(fb.get("misconception") or ("none" if is_correct else None)),
        ))


def record_solo_attempts(student_id, study_session_id, source, items):
    """items: list of {"topic": str, "correct": bool}. Replaces earlier attempts of
    the same source for the session so re-checking answers doesn't inflate numbers."""
    m.QuestionAttempt.query.filter_by(
        student_id=student_id, study_session_id=study_session_id, source=source
    ).delete()
    for it in items[:200]:
        db.session.add(m.QuestionAttempt(
            student_id=student_id, classroom_id=None, study_session_id=study_session_id,
            source=source, topic=str(it.get("topic") or "General")[:200],
            category=KIND_LABELS[source], is_correct=bool(it.get("correct")),
            score=100.0 if it.get("correct") else 0.0,
        ))


# ---------------------------------------------------------------------------
# Core statistics builder (works on already-loaded rows for ONE student)
# ---------------------------------------------------------------------------
def _topic_rows(attempts):
    agg = defaultdict(lambda: {"attempts": 0, "correct": 0})
    for a in attempts:
        t = agg[a.topic]
        t["attempts"] += 1
        t["correct"] += 1 if a.is_correct else 0
    rows = [{"topic": k, "attempts": v["attempts"], "correct": v["correct"],
             "accuracy": round(v["correct"] / v["attempts"] * 100, 1)} for k, v in agg.items()]
    return sorted(rows, key=lambda r: (r["accuracy"], -r["attempts"]))


def _kind_rows(attempts):
    agg = defaultdict(lambda: {"attempts": 0, "correct": 0})
    for a in attempts:
        k = a.category or "Other"
        agg[k]["attempts"] += 1
        agg[k]["correct"] += 1 if a.is_correct else 0
    return sorted(
        [{"kind": k, "attempts": v["attempts"], "accuracy": round(v["correct"] / v["attempts"] * 100, 1)}
         for k, v in agg.items()], key=lambda r: -r["accuracy"])


def _misconception_rows(attempts):
    c = Counter(a.misconception for a in attempts
                if not a.is_correct and a.misconception and a.misconception != "none")
    return [{"key": k, "label": MISCONCEPTION_LABELS.get(k, k.replace("_", " ").capitalize()), "count": n}
            for k, n in c.most_common()]


def _score_events(subs, progresses):
    events = []
    for s in subs:
        if s.score is not None:
            events.append({"date": _day(s.graded_at or s.submitted_at), "score": float(s.score),
                           "label": s.assignment.title, "kind": "assessment"})
    for p in progresses:
        if p.quiz_score is not None and p.completed_at:
            events.append({"date": _day(p.completed_at), "score": float(p.quiz_score),
                           "label": p.lesson.title, "kind": "quiz"})
    return sorted((e for e in events if e["date"]), key=lambda e: e["date"])


def _build_stats(attempts, logs, subs, progresses, lessons_by_id, missing=0):
    today = datetime.now(timezone.utc).date()
    topics = _topic_rows(attempts)
    scored = [t for t in topics if t["attempts"] >= MIN_ATTEMPTS]
    weak = [t for t in scored if t["accuracy"] < WEAK_BELOW]
    strong = sorted([t for t in scored if t["accuracy"] >= STRONG_AT], key=lambda r: -r["accuracy"])

    events = _score_events(subs, progresses)
    scores = [e["score"] for e in events]
    avg_score = _avg(scores)
    recent_avg = _avg(scores[-3:]) if len(scores) >= 3 else None
    earlier_avg = _avg(scores[:-3]) if len(scores) >= 5 else None
    trend_delta = round(recent_avg - earlier_avg, 1) if recent_avg is not None and earlier_avg is not None else None

    total_seconds = sum(l.seconds or 0 for l in logs)
    total_sessions = sum(l.sessions or 1 for l in logs)
    active_days = len({l.day for l in logs})

    last_dates = [l.day for l in logs] + [e["date"] for e in events] + [_day(a.created_at) for a in attempts]
    last_active = max((d for d in last_dates if d), default=None)
    days_inactive = (today - last_active).days if last_active else None

    # per-lesson time vs quiz result
    lesson_minutes = defaultdict(float)
    for l in logs:
        if l.lesson_id:
            lesson_minutes[l.lesson_id] += (l.seconds or 0) / 60
    lesson_scores = {p.lesson_id: p.quiz_score for p in progresses if p.quiz_score is not None}
    lessons = []
    for lid in set(lesson_minutes) | set(lesson_scores):
        les = lessons_by_id.get(lid)
        if les is None:
            continue
        lessons.append({
            "lesson_id": lid, "title": les.title,
            "week": les.week.number if les.week else 0, "order": les.order or 0,
            "minutes": round(lesson_minutes.get(lid, 0), 1), "quiz_score": lesson_scores.get(lid),
        })
    lessons.sort(key=lambda r: (r["week"], r["order"]))

    # weekly time vs score (the "does studying more help over time?" series)
    wk_minutes, wk_scores = defaultdict(float), defaultdict(list)
    for l in logs:
        wk_minutes[_monday(l.day)] += (l.seconds or 0) / 60
    for e in events:
        wk_scores[_monday(e["date"])].append(e["score"])
    weekly = [{"week": _fmt(w), "week_start": w.isoformat(), "minutes": round(wk_minutes.get(w, 0), 1),
               "score": _avg(wk_scores.get(w, []))} for w in sorted(set(wk_minutes) | set(wk_scores))]

    return {
        "total_minutes": round(total_seconds / 60, 1),
        "active_days": active_days,
        "avg_session_minutes": round(total_seconds / 60 / total_sessions, 1) if total_sessions else 0,
        "last_active": last_active.isoformat() if last_active else None,
        "days_inactive": days_inactive,
        "attempts": len(attempts),
        "accuracy": round(sum(1 for a in attempts if a.is_correct) / len(attempts) * 100, 1) if attempts else None,
        "topics": topics, "weak_topics": weak, "strong_topics": strong,
        "by_kind": _kind_rows(attempts),
        "misconceptions": _misconception_rows(attempts),
        "score_trend": [{"date": _fmt(e["date"]), "score": e["score"], "label": e["label"], "kind": e["kind"]}
                        for e in events],
        "avg_score": avg_score, "recent_avg": recent_avg, "trend_delta": trend_delta,
        "weekly": weekly,
        "lessons": lessons,
        "time_vs_score": [{"x": l["minutes"], "y": l["quiz_score"], "label": l["title"]}
                          for l in lessons if l["quiz_score"] is not None and l["minutes"] > 0],
        "missing_assignments": missing,
        "completed_lessons": len(lesson_scores),
    }


def has_data(stats):
    return bool(stats and (stats["attempts"] or stats["total_minutes"] or stats["avg_score"] is not None))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _lessons_by_id(ids):
    if not ids:
        return {}
    rows = m.Lesson.query.options(selectinload(m.Lesson.week)).filter(m.Lesson.id.in_(list(ids))).all()
    return {l.id: l for l in rows}


def _missing_count(student_id, classroom_id):
    """Past-due assessments in the classroom this student never submitted."""
    if not classroom_id:
        return 0
    now = _utc_naive_now()
    assignments = m.Assignment.query.filter_by(classroom_id=classroom_id).all()
    due = [a for a in assignments if a.due_date and a.due_date.replace(tzinfo=None) < now]
    if not due:
        return 0
    done = {s.assignment_id for s in m.Submission.query.filter(
        m.Submission.student_id == student_id,
        m.Submission.assignment_id.in_([a.id for a in due])).all()}
    return sum(1 for a in due if a.id not in done)


def student_stats(student_id, classroom_id=None):
    """All statistics for one student; scoped to one classroom if given,
    otherwise across everything they've done (including solo study)."""
    q_att = m.QuestionAttempt.query.filter_by(student_id=student_id)
    q_log = m.StudyTimeLog.query.filter_by(student_id=student_id)
    q_sub = (m.Submission.query.options(selectinload(m.Submission.assignment))
             .join(m.Assignment).filter(m.Submission.student_id == student_id, m.Submission.status == "graded"))
    q_prog = (m.LessonProgress.query.options(selectinload(m.LessonProgress.lesson))
              .join(m.Lesson).join(m.Week)
              .filter(m.LessonProgress.student_id == student_id, m.LessonProgress.completed.is_(True)))
    if classroom_id:
        q_att = q_att.filter(m.QuestionAttempt.classroom_id == classroom_id)
        q_log = q_log.filter(m.StudyTimeLog.classroom_id == classroom_id)
        q_sub = q_sub.filter(m.Assignment.classroom_id == classroom_id)
        q_prog = q_prog.filter(m.Week.classroom_id == classroom_id)

    attempts, logs, subs, progs = q_att.all(), q_log.all(), q_sub.all(), q_prog.all()
    lesson_ids = {l.lesson_id for l in logs if l.lesson_id} | {p.lesson_id for p in progs}
    return _build_stats(attempts, logs, subs, progs, _lessons_by_id(lesson_ids),
                        missing=_missing_count(student_id, classroom_id))


# ---------------------------------------------------------------------------
# Classroom-wide statistics
# ---------------------------------------------------------------------------
def classroom_stats(classroom):
    cid = classroom.id
    enrollments = list(classroom.enrollments)
    sids = [e.student_id for e in enrollments]
    names = {e.student_id: e.student.name for e in enrollments}
    n_students = len(sids)

    attempts = m.QuestionAttempt.query.filter(m.QuestionAttempt.classroom_id == cid).all() if sids else []
    logs = m.StudyTimeLog.query.filter(m.StudyTimeLog.classroom_id == cid).all() if sids else []
    subs = (m.Submission.query.options(selectinload(m.Submission.assignment)).join(m.Assignment)
            .filter(m.Assignment.classroom_id == cid, m.Submission.status == "graded",
                    m.Submission.student_id.in_(sids)).all()) if sids else []
    progs = (m.LessonProgress.query.options(selectinload(m.LessonProgress.lesson)).join(m.Lesson).join(m.Week)
             .filter(m.Week.classroom_id == cid, m.LessonProgress.completed.is_(True),
                     m.LessonProgress.student_id.in_(sids)).all()) if sids else []
    all_lessons = [l for w in classroom.weeks for l in w.lessons]
    lessons_by_id = {l.id: l for l in all_lessons}

    by_student = lambda rows, attr="student_id": _group(rows, attr)  # noqa: E731
    g_att, g_log, g_sub, g_prog = by_student(attempts), by_student(logs), by_student(subs), by_student(progs)

    # missing (past-due, never submitted) in one pass
    now = _utc_naive_now()
    assignments = list(classroom.assignments)
    due = [a for a in assignments if a.due_date and a.due_date.replace(tzinfo=None) < now]
    submitted = {(s.assignment_id, s.student_id) for s in m.Submission.query.filter(
        m.Submission.assignment_id.in_([a.id for a in assignments])).all()} if assignments else set()

    per_student, rows = {}, []
    for e in enrollments:
        sid = e.student_id
        missing = sum(1 for a in due if (a.id, sid) not in submitted)
        st = _build_stats(g_att.get(sid, []), g_log.get(sid, []), g_sub.get(sid, []),
                          g_prog.get(sid, []), lessons_by_id, missing=missing)
        per_student[sid] = st
        rows.append({
            "student_id": sid, "name": names[sid], "avg_score": st["avg_score"],
            "minutes": st["total_minutes"], "accuracy": st["accuracy"],
            "trend_delta": st["trend_delta"], "days_inactive": st["days_inactive"],
            "missing": missing, "risk": e.risk_level, "predicted_grade": e.predicted_grade,
            "progress": e.progress_percent or 0,
            "weak": [t["topic"] for t in st["weak_topics"][:3]],
            "strong": [t["topic"] for t in st["strong_topics"][:3]],
        })

    # ---- class-wide topic / kind / misconception aggregates ----
    topics = _topic_rows(attempts)
    struggling = defaultdict(int)  # topic -> number of students under WEAK_BELOW
    for st in per_student.values():
        for t in st["topics"]:
            if t["attempts"] >= MIN_ATTEMPTS and t["accuracy"] < WEAK_BELOW:
                struggling[t["topic"]] += 1
    for t in topics:
        t["students_struggling"] = struggling.get(t["topic"], 0)
    ranked = [t for t in topics if t["attempts"] >= 3] or topics
    hardest = ranked[:6]
    easiest = sorted(ranked, key=lambda r: -r["accuracy"])[:5]
    hardest_names = {t["topic"] for t in hardest}
    easiest = [t for t in easiest if t["topic"] not in hardest_names]

    # ---- study time vs score ----
    pts = [(r["minutes"], r["avg_score"], r["name"]) for r in rows
           if r["avg_score"] is not None and r["minutes"] > 0]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    r_val = pearson(xs, ys)
    fit = linear_fit(xs, ys)
    scatter = {
        "points": [{"x": x, "y": y, "label": n} for x, y, n in pts],
        "line": ([{"x": min(xs), "y": round(fit[0] * min(xs) + fit[1], 1)},
                  {"x": max(xs), "y": round(fit[0] * max(xs) + fit[1], 1)}] if fit else []),
    }
    buckets = _time_buckets(pts)

    # ---- weekly class series ----
    wk_minutes, wk_scores = defaultdict(float), defaultdict(list)
    for l in logs:
        wk_minutes[_monday(l.day)] += (l.seconds or 0) / 60
    for st_id in per_student:
        for e in _score_events(g_sub.get(st_id, []), g_prog.get(st_id, [])):
            wk_scores[_monday(e["date"])].append(e["score"])
    weekly = [{"week": _fmt(w), "minutes_per_student": round(wk_minutes.get(w, 0) / max(n_students, 1), 1),
               "score": _avg(wk_scores.get(w, []))} for w in sorted(set(wk_minutes) | set(wk_scores))]

    # ---- per-lesson ("chapter") time vs success ----
    lesson_time, lesson_students = defaultdict(float), defaultdict(set)
    for l in logs:
        if l.lesson_id:
            lesson_time[l.lesson_id] += (l.seconds or 0) / 60
            lesson_students[l.lesson_id].add(l.student_id)
    lesson_q = defaultdict(list)
    for p in progs:
        if p.quiz_score is not None:
            lesson_q[p.lesson_id].append(p.quiz_score)
    lesson_rows = []
    for l in all_lessons:
        n_time = len(lesson_students.get(l.id, ()))
        if not n_time and not lesson_q.get(l.id):
            continue
        lesson_rows.append({
            "lesson_id": l.id, "title": l.title, "week": l.week.number,
            "label": f"W{l.week.number} · {l.title}"[:34],
            "avg_minutes": round(lesson_time.get(l.id, 0) / n_time, 1) if n_time else 0,
            "avg_quiz": _avg(lesson_q.get(l.id, [])), "students": max(n_time, len(lesson_q.get(l.id, []))),
        })

    # ---- heatmap: top topics x students ----
    top_topics = [t["topic"] for t in sorted(topics, key=lambda r: -r["attempts"])[:8]]
    heat_rows = []
    for r in rows:
        tmap = {t["topic"]: t for t in per_student[r["student_id"]]["topics"]}
        heat_rows.append({"name": r["name"], "student_id": r["student_id"], "cells": [
            (tmap[t]["accuracy"] if t in tmap else None) for t in top_topics]})

    # ---- distribution & engagement ----
    dist_labels = ["<50", "50-59", "60-69", "70-79", "80-89", "90-100"]
    dist = [0] * 6
    for r in rows:
        s = r["avg_score"]
        if s is None:
            continue
        dist[0 if s < 50 else 1 if s < 60 else 2 if s < 70 else 3 if s < 80 else 4 if s < 90 else 5] += 1

    all_events = [e["score"] for sid in per_student for e in
                  _score_events(g_sub.get(sid, []), g_prog.get(sid, []))]
    total_minutes = round(sum(r["minutes"] for r in rows), 1)
    possible = len(assignments) * max(n_students, 1)
    completion_rate = round(sum(len(a.submissions) for a in assignments) / possible * 100, 1) if possible else 0

    return {
        "n_students": n_students,
        "kpis": {
            "avg_score": _avg(all_events) or 0, "completion_rate": completion_rate,
            "total_minutes": total_minutes,
            "avg_minutes_per_student": round(total_minutes / n_students, 1) if n_students else 0,
            "correlation": r_val, "correlation_text": describe_correlation(r_val, len(pts)),
            "n_points": len(pts),
            "active_week": sum(1 for r in rows if r["days_inactive"] is not None and r["days_inactive"] < INACTIVE_DAYS),
            "at_risk": sum(1 for r in rows if r["risk"] == "high"),
            "attempts": len(attempts),
        },
        "students": sorted(rows, key=lambda r: (r["avg_score"] is None, -(r["avg_score"] or 0))),
        "scatter": scatter, "time_buckets": buckets, "weekly": weekly,
        "topics": topics, "hardest": hardest, "easiest": easiest,
        "by_kind": _kind_rows(attempts), "misconceptions": _misconception_rows(attempts),
        "lessons": lesson_rows,
        "heatmap": {"topics": top_topics, "rows": heat_rows},
        "distribution": {"labels": dist_labels, "counts": dist},
        "inactive": [r["name"] for r in rows if r["days_inactive"] is None or r["days_inactive"] >= INACTIVE_DAYS],
        "at_risk": [r["name"] for r in rows if r["risk"] == "high"],
        "has_data": bool(attempts or logs or all_events),
    }


def _group(rows, attr):
    out = defaultdict(list)
    for r in rows:
        out[getattr(r, attr)].append(r)
    return out


def _time_buckets(pts):
    """Compare average scores of low / mid / high study-time students."""
    n = len(pts)
    if n < 4:
        return []
    ordered = sorted(pts, key=lambda p: p[0])
    if n >= 6:
        k = n // 3
        groups = [("Least study time", ordered[:k]), ("Middle", ordered[k:n - k]), ("Most study time", ordered[n - k:])]
    else:
        k = n // 2
        groups = [("Less study time", ordered[:k]), ("More study time", ordered[k:])]
    return [{"label": label, "avg_score": _avg([p[1] for p in g]), "n": len(g),
             "avg_minutes": _avg([p[0] for p in g])} for label, g in groups if g]


# ---------------------------------------------------------------------------
# Rules-based profile / risk refresh
# ---------------------------------------------------------------------------
def _risk_level(stats):
    avg, delta, days, missing = (stats["avg_score"], stats["trend_delta"],
                                 stats["days_inactive"], stats["missing_assignments"])
    pts = 0
    if avg is not None:
        pts += 4 if avg < 50 else 2 if avg < 65 else 1 if avg < 75 else 0   # <50% alone is high risk
    if delta is not None and delta <= -10:
        pts += 1
    if days is not None:
        pts += 2 if days >= 14 else 1 if days >= INACTIVE_DAYS else 0
    pts += 2 if missing >= 3 else 1 if missing >= 1 else 0
    return "high" if pts >= 4 else "medium" if pts >= 2 else "low"


def refresh_student_metrics(student_id, classroom_id=None):
    """Recompute and store risk, predicted grade and the learning profile.

    Cheap and AI-free, so it runs after every quiz/assessment. Safe to call
    repeatedly. Commits.
    """
    overall = student_stats(student_id)
    profile = m.LearningProfile.query.filter_by(student_id=student_id).first()
    if profile is None:
        profile = m.LearningProfile(student_id=student_id)
        db.session.add(profile)

    if has_data(overall):
        profile.weak_topics_json = json.dumps([t["topic"] for t in overall["weak_topics"][:6]])
        profile.strong_topics_json = json.dumps([t["topic"] for t in overall["strong_topics"][:6]])

        lesson_min = [l["minutes"] for l in overall["lessons"] if l["quiz_score"] is not None and l["minutes"] > 0]
        avg_min, avg_score = _avg(lesson_min), overall["avg_score"]
        if avg_min is not None:
            # Heuristic on minutes spent per lesson (lessons are ~15-25 min of reading).
            if avg_min <= 6 and (avg_score or 0) >= 75:
                profile.learning_speed = "fast"
            elif avg_min >= 40 or ((avg_score or 100) < 60 and avg_min >= 25):
                profile.learning_speed = "slow"
            else:
                profile.learning_speed = "average"
        sess = overall["avg_session_minutes"]
        if sess:
            profile.attention_span = "short" if sess < 8 else "medium" if sess < 20 else "long"
        acc = overall["accuracy"]
        if acc is not None:
            profile.preferred_difficulty = "hard" if acc >= 85 else "medium" if acc >= 65 else "easy"
        basis = [v for v in (overall["recent_avg"] or overall["avg_score"], acc) if v is not None]
        if basis:
            profile.confidence = int(round(sum(basis) / len(basis)))
        profile.updated_at = datetime.now(timezone.utc)

    if classroom_id:
        enrollment = m.Enrollment.query.filter_by(classroom_id=classroom_id, student_id=student_id).first()
        if enrollment is not None:
            cs = student_stats(student_id, classroom_id)
            enrollment.risk_level = _risk_level(cs)
            if cs["avg_score"] is not None:
                blend = cs["avg_score"] if cs["recent_avg"] is None else 0.6 * cs["avg_score"] + 0.4 * cs["recent_avg"]
                enrollment.predicted_grade = _letter(blend)
    db.session.commit()


# ---------------------------------------------------------------------------
# Compact payloads for the AI (numbers only — no free text from students)
# ---------------------------------------------------------------------------
def _slim_topics(rows, n=5):
    return [{"topic": t["topic"], "accuracy": t["accuracy"], "attempts": t["attempts"]} for t in rows[:n]]


def student_ai_payload(name, stats):
    lessons = sorted(stats["lessons"], key=lambda l: -l["minutes"])[:6]
    return {
        "student": name,
        "average_score": stats["avg_score"], "recent_average": stats["recent_avg"],
        "trend_change_points": stats["trend_delta"],
        "total_study_minutes": stats["total_minutes"], "active_days": stats["active_days"],
        "avg_session_minutes": stats["avg_session_minutes"], "days_since_last_activity": stats["days_inactive"],
        "missing_assignments": stats["missing_assignments"],
        "weak_topics": _slim_topics(stats["weak_topics"]),
        "strong_topics": _slim_topics(stats["strong_topics"]),
        "mistake_types": [{"type": x["label"], "count": x["count"]} for x in stats["misconceptions"][:5]],
        "accuracy_by_assessment_type": stats["by_kind"][:6],
        "time_per_lesson": [{"lesson": l["title"], "minutes": l["minutes"], "quiz_score": l["quiz_score"]}
                            for l in lessons],
    }


def class_ai_payload(classroom, cs):
    return {
        "classroom": classroom.name, "subject": classroom.subject, "students": cs["n_students"],
        "average_score": cs["kpis"]["avg_score"], "completion_rate": cs["kpis"]["completion_rate"],
        "avg_study_minutes_per_student": cs["kpis"]["avg_minutes_per_student"],
        "study_time_vs_score_correlation": cs["kpis"]["correlation"],
        "score_by_study_time_group": cs["time_buckets"],
        "hardest_topics": [{"topic": t["topic"], "accuracy": t["accuracy"], "students_struggling": t["students_struggling"]}
                           for t in cs["hardest"]],
        "strongest_topics": _slim_topics(cs["easiest"]),
        "common_mistake_types": [{"type": x["label"], "count": x["count"]} for x in cs["misconceptions"][:5]],
        "students": [{"name": r["name"], "avg_score": r["avg_score"], "study_minutes": r["minutes"],
                      "risk": r["risk"], "weak_topics": r["weak"][:2], "days_inactive": r["days_inactive"]}
                     for r in cs["students"][:40]],
    }