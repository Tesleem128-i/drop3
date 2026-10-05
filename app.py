"""DROP — The AI Teacher That Never Stops Teaching.

ALL routes live in this single file per project spec. Helper logic lives in
models.py (database), extensions.py (Flask extensions), ai_engine.py (AI
integration), analytics.py (learning analytics), and config.py (settings).
"""
import html as _html
import json
import os
import random
import re
import string
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
# Load .env from the same folder as app.py (not whatever folder the terminal happens to be in).
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_ENV_LOADED = load_dotenv(_ENV_PATH)

from flask import (
    Flask, render_template, redirect, url_for, request, flash, jsonify, abort, session,
    current_app
)
from flask_login import (
    login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import Markup

try:
    import markdown as _markdown  # pip install markdown
except ImportError:  # pragma: no cover
    _markdown = None

from config import Config
from extensions import db, login_manager, bcrypt
import models as m
import ai_engine as ai
import analytics
import exports
import scheduling as sched

# ---------------------------------------------------------------------------
# App factory / bootstrap
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(Config)

# Render (like most PaaS providers) puts the app behind a reverse proxy that
# terminates TLS. Without this, Flask thinks every request is plain HTTP,
# which breaks secure cookies and any https:// URL generation.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init_app(app)
login_manager.init_app(app)


@login_manager.unauthorized_handler
def _unauthorized():
    # Background requests (study timer, tutor chat) get a clean 401 instead of an HTML login page.
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "login required"}), 401
    flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(url_for(login_manager.login_view, next=request.path))
bcrypt.init_app(app)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# Ensure the directory for the SQLite file actually exists, derived from the
# resolved URI itself (not just a hardcoded guess), so this works even if
# DATABASE_URL was overridden via .env to point somewhere else.
_db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
if _db_uri.startswith("sqlite:///"):
    _db_file_path = _db_uri[len("sqlite:///"):]
    _db_dir = os.path.dirname(_db_file_path)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)

print(f"[DROP] Using database: {_db_uri}")
if _ENV_LOADED:
    print(f"[DROP] .env loaded from {_ENV_PATH}")
else:
    print(f"[DROP] No .env file at {_ENV_PATH}  (fine on a host like Render where variables are set in its dashboard)")
    for _wrong in (".env.txt", "env", "env.txt", ".env.example"):
        if os.path.exists(os.path.join(os.path.dirname(_ENV_PATH), _wrong)):
            print(f"[DROP]   found '{_wrong}' in that folder, but the file must be named exactly '.env'")
with app.app_context():
    print(ai.startup_summary())

with app.app_context():
    db.create_all()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(m.User, int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def gen_join_code(length=6):
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if not m.Classroom.query.filter_by(join_code=code).first():
            return code


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def extract_text_from_upload(file_storage):
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file_storage.save(path)
    text = ""
    try:
        if ext == "pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif ext == "docx":
            import docx
            doc = docx.Document(path)
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            with open(path, "r", errors="ignore") as f:
                text = f.read()
    except Exception:
        text = ""
    return filename, text


def require_role(role):
    if not current_user.is_authenticated or current_user.role != role:
        abort(403)


def teacher_owns_classroom(classroom):
    if classroom.teacher_id != current_user.id:
        abort(403)


def student_enrolled(classroom):
    enrollment = m.Enrollment.query.filter_by(
        classroom_id=classroom.id, student_id=current_user.id
    ).first()
    if not enrollment:
        abort(403)
    return enrollment


def push_notification(user_id, content, kind="info"):
    n = m.Notification(user_id=user_id, content=content, kind=kind)
    db.session.add(n)
    db.session.commit()


def unread_notification_count():
    if not current_user.is_authenticated:
        return 0
    return m.Notification.query.filter_by(user_id=current_user.id, read=False).count()


def safe_refresh_metrics(student_id, classroom_id=None):
    """Recompute risk / predicted grade / strengths & weaknesses. Never breaks a request."""
    try:
        analytics.refresh_student_metrics(student_id, classroom_id)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Refreshing student metrics failed")


INSIGHT_TTL = timedelta(hours=12)


def _is_stale(row, ttl=INSIGHT_TTL):
    if row is None:
        return True
    ts = row.updated_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts > ttl


def get_student_insight(student, classroom_id, stats, force=False):
    """Cached AI strengths/weaknesses for a student (regenerated when stale or forced)."""
    row = m.StudentInsight.query.filter_by(student_id=student.id, classroom_id=classroom_id).first()
    if not analytics.has_data(stats):
        return row
    if force or _is_stale(row):
        try:
            data, source = ai.analyze_student(analytics.student_ai_payload(student.name, stats))
            if row is None:
                row = m.StudentInsight(student_id=student.id, classroom_id=classroom_id)
                db.session.add(row)
            row.data_json, row.source = json.dumps(data), source
            row.updated_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Student AI analysis failed")
    return row


def get_class_insight(classroom, cs, force=False):
    """Cached AI class-wide strengths/weaknesses (regenerated when stale or forced)."""
    row = m.ClassInsight.query.filter_by(classroom_id=classroom.id).first()
    if not cs["has_data"]:
        return row
    if force or _is_stale(row):
        try:
            data, source = ai.analyze_class(analytics.class_ai_payload(classroom, cs))
            if row is None:
                row = m.ClassInsight(classroom_id=classroom.id)
                db.session.add(row)
            row.data_json, row.source = json.dumps(data), source
            row.updated_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Class AI analysis failed")
    return row


# ---------------------------------------------------------------------------
# Rendering AI text: markdown + maths
#
# Lessons are written by the AI as markdown with LaTeX maths. They used to be dumped on the page as
# raw text (so students saw "$$\frac{a}{b}$$" and "## 1. Heading"). These filters (|md and
# |md_inline) turn markdown into HTML and normalise every maths style the model might use
# ($..$, $$..$$, \(..\), \[..\]) into the \( \) / \[ \] that MathJax (base.html) renders.
# Maths is lifted out before markdown runs so underscores / asterisks inside formulas survive.
# ---------------------------------------------------------------------------
_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$"
    r"|\\\s?\[(.+?)\\\s?\]"
    r"|\\\s?\((.+?)\\\s?\)"
    r"|(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\d$])",
    re.DOTALL,
)


def _lift_math(text):
    stash = []

    def repl(mo):
        display = mo.group(1) is not None or mo.group(2) is not None
        body = next(g for g in mo.groups() if g is not None).strip()
        body = _html.escape(body, quote=False)
        stash.append(f"\\[{body}\\]" if display else f"\\({body}\\)")
        return f"DROPMATH{len(stash) - 1}X"

    return _MATH_RE.sub(repl, text), stash


def _restore_math(html_text, stash):
    return re.sub(r"DROPMATH(\d+)X", lambda mo: stash[int(mo.group(1))], html_text)


def render_md(text):
    """Markdown (tables, lists, headings, code) + maths -> safe HTML."""
    if not text:
        return Markup("")
    text, stash = _lift_math(str(text).replace("\r\n", "\n"))
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    if _markdown is not None:
        out = _markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    else:
        out = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return Markup(_restore_math(out, stash))


def render_inline(text):
    """Escape plain text but still render maths inside it (objectives, options, definitions...)."""
    if not text:
        return Markup("")
    text, stash = _lift_math(str(text))
    text = _html.escape(text, quote=False)
    return Markup(_restore_math(text, stash))


app.add_template_filter(render_md, "md")
app.add_template_filter(render_inline, "md_inline")


@app.context_processor
def inject_globals():
    return {
        "unread_notifications": unread_notification_count() if current_user.is_authenticated else 0,
        "now": datetime.now(timezone.utc),
        "assess_state": lambda a: sched.window_state(a.start_at, a.due_date),
    }


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "teacher":
            return redirect(url_for("teacher_dashboard"))
        return redirect(url_for("student_dashboard"))
    # Logged-out visitors get the animated landing page (templates/index.html)
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")

        if not name or not email or not password:
            flash("Please fill in every field.", "error")
            return render_template("auth/signup.html")

        if role not in ("teacher", "student"):
            role = "student"

        if m.User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("auth/signup.html")

        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        user = m.User(name=name, email=email, password_hash=pw_hash, role=role)
        db.session.add(user)
        db.session.commit()

        if role == "student":
            db.session.add(m.LearningProfile(student_id=user.id))
            db.session.commit()

        login_user(user)
        flash(f"Welcome to DROP, {name.split(' ')[0]}!", "success")
        return redirect(url_for("index"))

    return render_template("auth/signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = m.User.query.filter_by(email=email).first()

        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            user.last_active = datetime.now(timezone.utc)
            db.session.commit()
            return redirect(url_for("index"))

        flash("Incorrect email or password.", "error")

    return render_template("auth/login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        flash("If that email exists in DROP, a reset link has been sent.", "success")
        return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Notifications (shared)
# ---------------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications():
    items = m.Notification.query.filter_by(user_id=current_user.id).order_by(
        m.Notification.created_at.desc()
    ).all()
    for n in items:
        n.read = True
    db.session.commit()
    return render_template("shared/notifications.html", items=items)


# ---------------------------------------------------------------------------
# TEACHER — Dashboard
# ---------------------------------------------------------------------------
@app.route("/teacher/dashboard")
@login_required
def teacher_dashboard():
    require_role("teacher")
    classrooms = m.Classroom.query.filter_by(teacher_id=current_user.id).order_by(
        m.Classroom.created_at.desc()
    ).all()

    total_students = sum(c.student_count() for c in classrooms)
    pending_grading = (
        m.Submission.query.join(m.Assignment).join(m.Classroom)
        .filter(m.Classroom.teacher_id == current_user.id, m.Submission.status == "submitted")
        .count()
    )

    return render_template(
        "teacher/dashboard.html",
        classrooms=classrooms,
        total_students=total_students,
        pending_grading=pending_grading,
    )


@app.route("/teacher/classroom/create", methods=["GET", "POST"])
@login_required
def teacher_classroom_create():
    require_role("teacher")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        subject = request.form.get("subject", "").strip()
        duration_weeks = int(request.form.get("duration_weeks") or 8)
        target_grade = request.form.get("target_grade", "").strip()
        syllabus_text = ""

        syllabus_file = request.files.get("syllabus")
        if syllabus_file and syllabus_file.filename and allowed_file(syllabus_file.filename):
            _, syllabus_text = extract_text_from_upload(syllabus_file)

        classroom = m.Classroom(
            teacher_id=current_user.id,
            name=name,
            subject=subject,
            duration_weeks=duration_weeks,
            target_grade=target_grade,
            syllabus_text=syllabus_text,
            join_code=gen_join_code(),
            ai_status="pending",
        )
        db.session.add(classroom)
        db.session.commit()

        # --- Only the OUTLINE (weeks + lesson titles) is built now: one small, fast call. ---
        # Lessons, week tests, exams and assignments are generated later, one at a time, when
        # the teacher presses that item's Generate button.
        try:
            plan = ai.generate_course_plan(subject, duration_weeks, target_grade, syllabus_text)
            _persist_plan(classroom, plan)
            classroom.ai_status = "ready"
            flash("Classroom created — your course outline is ready. Press Generate on each week when you want its lessons.", "success")
        except Exception:
            current_app.logger.exception("Classroom outline generation failed")
            db.session.rollback()
            classroom = db.session.get(m.Classroom, classroom.id)
            classroom.ai_status = "failed"
            flash("Classroom created, but the AI couldn't build the outline. Try again from a new classroom.", "error")
        db.session.commit()

        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))

    return render_template("teacher/classroom_create.html")


def _persist_plan(classroom, plan):
    """Save the AI outline as Week rows + title-only Lesson rows. Lesson content is filled in later."""
    classroom.ai_course_json = json.dumps({"overview": plan.get("overview", ""), "weeks": plan.get("weeks", [])})
    for week_data in plan.get("weeks", []):
        week = m.Week(
            classroom_id=classroom.id,
            number=week_data.get("number", 1),
            title=week_data.get("title", f"Week {week_data.get('number', 1)}"),
            summary=week_data.get("summary", ""),
        )
        db.session.add(week)
        db.session.flush()
        for idx, title in enumerate(week_data.get("lesson_titles") or [week.title]):
            db.session.add(m.Lesson(week_id=week.id, order=idx, title=str(title)[:200]))
    db.session.commit()


def _classroom_plan(classroom):
    """The outline, rebuilt from the database rows (always current)."""
    return {
        "overview": (classroom.course() or {}).get("overview", ""),
        "weeks": [{"number": w.number, "title": w.title, "summary": w.summary or "",
                   "lesson_titles": [l.title for l in w.lessons]} for w in classroom.weeks],
    }


def _lesson_dict(lesson):
    return {"title": lesson.title, "notes": lesson.notes or "",
            "examples": lesson.examples or "", "summary": lesson.summary or ""}


def _fill_lesson(lesson, data):
    lesson.objectives = json.dumps(data.get("objectives", []))
    lesson.notes = data.get("notes", "")
    lesson.definitions = json.dumps(data.get("definitions", []))
    lesson.examples = data.get("examples", "")
    lesson.applications = data.get("applications", "")
    lesson.common_mistakes = json.dumps(data.get("common_mistakes", []))
    lesson.practice = json.dumps(data.get("practice", []))
    lesson.revision = data.get("revision", "")
    lesson.summary = data.get("summary", "")
    lesson.homework = json.dumps(data.get("homework", []))
    lesson.quiz_json = json.dumps(data.get("quiz", []))


def _classroom_source_text(classroom):
    """Material to ground assessments in: the teacher's notes, else whatever lessons exist."""
    if classroom.syllabus_text:
        return classroom.syllabus_text
    lessons = [_lesson_dict(l) for w in classroom.weeks for l in w.lessons if l.is_generated()]
    return ai._lesson_material(lessons, limit=7000)


def _own_week(classroom_id, week_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    week = db.get_or_404(m.Week, week_id)
    if week.classroom_id != classroom.id:
        abort(404)
    return classroom, week


def _save_assessment(classroom, kind, title, description, questions, due_date):
    a = m.Assignment.query.filter_by(classroom_id=classroom.id, kind=kind, title=title).first()
    if a is None:
        a = m.Assignment(classroom_id=classroom.id, kind=kind, title=title)
        db.session.add(a)
    a.description, a.questions_json = description, json.dumps(questions)
    if a.due_date is None:
        a.due_date = due_date            # default only; never overwrite a date the teacher chose
    if a.secure_mode is None:
        a.secure_mode = kind in m.SECURE_KINDS
    if a.duration_minutes is None and kind in m.DEFAULT_DURATIONS:
        a.duration_minutes = m.DEFAULT_DURATIONS[kind]
    db.session.commit()
    for e in classroom.enrollments:
        push_notification(e.student_id, f"New {kind.replace('_', ' ')}: {title}", "info")
    return a


@app.route("/teacher/classroom/<int:classroom_id>/week/<int:week_id>/generate", methods=["POST"])
@login_required
def teacher_week_generate(classroom_id, week_id):
    """Generate the lessons for ONE week (or just the ones that failed last time)."""
    classroom, week = _own_week(classroom_id, week_id)
    redo = request.form.get("redo") == "1"   # "Regenerate in detail": rewrite lessons that already exist
    pending = list(week.lessons) if redo else [l for l in week.lessons if not l.is_generated()]
    if not pending:
        flash(f"Week {week.number} is already generated.", "info")
        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))

    week_plan = {"number": week.number, "title": week.title, "summary": week.summary or "",
                 "lesson_titles": [l.title for l in week.lessons]}
    try:
        lessons = ai.generate_week_lessons(
            classroom.subject, _classroom_plan(classroom), week_plan,
            source_text=classroom.syllabus_text or "", titles=[l.title for l in pending],
            target_grade=classroom.target_grade or "",
        )
    except Exception:
        current_app.logger.exception("Week generation failed")
        lessons = []

    by_title = {l["title"]: l for l in lessons}
    filled = 0
    for lesson in pending:
        data = by_title.get(lesson.title)
        if data:
            _fill_lesson(lesson, data)
            filled += 1
    db.session.commit()

    if filled == len(pending):
        flash(f"Week {week.number} is ready — {filled} lesson{'s' if filled != 1 else ''} generated.", "success")
    elif filled:
        flash(f"Generated {filled} of {len(pending)} lessons for week {week.number}. Press Generate again for the rest.", "info")
    else:
        flash("The AI couldn't generate this week right now (check the terminal for the reason). Please try again.", "error")
    return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))


@app.route("/teacher/classroom/<int:classroom_id>/week/<int:week_id>/generate-test", methods=["POST"])
@login_required
def teacher_week_test_generate(classroom_id, week_id):
    """Generate the end-of-week test for ONE week, from that week's generated lessons."""
    classroom, week = _own_week(classroom_id, week_id)
    ready = [l for l in week.lessons if l.is_generated()]
    if not ready:
        flash("Generate this week's lessons first — the test is built from them.", "error")
        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))
    title = f"Week {week.number} Test"
    existing = m.Assignment.query.filter_by(classroom_id=classroom.id, kind="weekly_test", title=title).first()
    if existing and existing.has_questions():
        flash(f"{title} is already generated.", "info")
        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))
    try:
        questions = ai.generate_week_test(classroom.subject, week.title, week.number, [_lesson_dict(l) for l in ready])
    except Exception:
        current_app.logger.exception("Week test generation failed")
        questions = []
    if not questions:
        flash("The AI couldn't write this test right now. Please try again.", "error")
    else:
        _save_assessment(classroom, "weekly_test", title, f"Weekly test for {week.title}", questions,
                         datetime.now(timezone.utc) + timedelta(weeks=week.number))
        flash(f"{title} generated — {len(questions)} questions.", "success")
    return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))


@app.route("/teacher/classroom/<int:classroom_id>/exam/<kind>/generate", methods=["POST"])
@login_required
def teacher_exam_generate(classroom_id, kind):
    """Generate the midterm or the final exam from the weeks that have been generated so far."""
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    if kind not in ("midterm", "final_exam"):
        abort(404)
    ready = [w for w in classroom.weeks if w.generated_count()]
    if not ready:
        flash("Generate at least one week of lessons first — exams are built from them.", "error")
        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))

    title = "Midterm Exam" if kind == "midterm" else "Final Exam"
    existing = m.Assignment.query.filter_by(classroom_id=classroom.id, kind=kind, title=title).first()
    if existing and existing.has_questions():
        flash(f"{title} is already generated.", "info")
        return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))

    covered = ready[:max(1, (len(ready) + 1) // 2)] if kind == "midterm" else ready
    weeks = [{"number": w.number, "title": w.title, "lessons": [_lesson_dict(l) for l in w.lessons if l.is_generated()]}
             for w in covered]
    try:
        exam = ai.generate_exam(classroom.subject, weeks, num_questions=15 if kind == "midterm" else 30)
    except Exception:
        current_app.logger.exception("Exam generation failed")
        exam = {"questions": []}
    if not exam["questions"]:
        flash("The AI couldn't write this exam right now. Please try again.", "error")
    else:
        due = (max(classroom.duration_weeks // 2, 1) if kind == "midterm" else classroom.duration_weeks)
        _save_assessment(classroom, kind, title, f"{title} covering weeks {covered[0].number}-{covered[-1].number}.",
                         exam["questions"], datetime.now(timezone.utc) + timedelta(weeks=due))
        flash(f"{title} generated — {len(exam['questions'])} questions.", "success")
    return redirect(url_for("teacher_classroom_overview", classroom_id=classroom.id))


@app.route("/teacher/classroom/<int:classroom_id>")
@login_required
def teacher_classroom_overview(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    week_tests = {a.title: a for a in classroom.assignments if a.kind == "weekly_test"}
    exams = {a.kind: a for a in classroom.assignments if a.kind in ("midterm", "final_exam")}
    any_ready = any(w.generated_count() for w in classroom.weeks)
    return render_template("teacher/classroom_overview.html", classroom=classroom,
                           week_tests=week_tests, exams=exams, any_ready=any_ready)


@app.route("/teacher/classroom/<int:classroom_id>/students")
@login_required
def teacher_classroom_students(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    enrollments = classroom.enrollments
    cs = analytics.classroom_stats(classroom)
    rows = {r["student_id"]: r for r in cs["students"]}
    return render_template(
        "teacher/classroom_students.html", classroom=classroom, enrollments=enrollments, rows=rows,
    )


@app.route("/teacher/ai-check")
@login_required
def teacher_ai_check():
    """Pings every configured AI model and shows exactly what works and what error comes back."""
    require_role("teacher")
    return jsonify(ai.diagnose())


@app.route("/teacher/insights")
@login_required
def teacher_insights_home():
    """Sidebar entry for Students / Analytics: pick a classroom (or jump straight in)."""
    require_role("teacher")
    classrooms = m.Classroom.query.filter_by(teacher_id=current_user.id).order_by(
        m.Classroom.created_at.desc()
    ).all()
    if len(classrooms) == 1:
        return redirect(url_for("teacher_classroom_analytics", classroom_id=classrooms[0].id))
    return render_template("teacher/insights_home.html", classrooms=classrooms)


@app.route("/teacher/classroom/<int:classroom_id>/student/<int:student_id>")
@login_required
def teacher_student_detail(classroom_id, student_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    student = db.get_or_404(m.User, student_id)
    enrollment = m.Enrollment.query.filter_by(classroom_id=classroom.id, student_id=student.id).first_or_404()

    submissions = (
        m.Submission.query.join(m.Assignment)
        .filter(m.Assignment.classroom_id == classroom.id, m.Submission.student_id == student.id)
        .order_by(m.Submission.submitted_at.desc()).all()
    )
    profile = m.LearningProfile.query.filter_by(student_id=student.id).first()
    stats = analytics.student_stats(student.id, classroom.id)
    insight = get_student_insight(student, classroom.id, stats)

    return render_template(
        "teacher/student_detail.html", classroom=classroom, student=student,
        enrollment=enrollment, submissions=submissions, profile=profile,
        avg_score=stats["avg_score"], stats=stats,
        insight=insight.data() if insight else {}, insight_row=insight,
    )


@app.route("/teacher/classroom/<int:classroom_id>/student/<int:student_id>/analyze", methods=["POST"])
@login_required
def teacher_student_analyze(classroom_id, student_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    student = db.get_or_404(m.User, student_id)
    m.Enrollment.query.filter_by(classroom_id=classroom.id, student_id=student.id).first_or_404()
    safe_refresh_metrics(student.id, classroom.id)
    stats = analytics.student_stats(student.id, classroom.id)
    get_student_insight(student, classroom.id, stats, force=True)
    flash("Student analysis refreshed.", "success")
    return redirect(url_for("teacher_student_detail", classroom_id=classroom.id, student_id=student.id))


@app.route("/teacher/classroom/<int:classroom_id>/analytics")
@login_required
def teacher_classroom_analytics(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)

    cs = analytics.classroom_stats(classroom)
    insight = get_class_insight(classroom, cs)
    return render_template(
        "teacher/analytics.html", classroom=classroom, cs=cs,
        insight=insight.data() if insight else {}, insight_row=insight,
    )


@app.route("/teacher/classroom/<int:classroom_id>/analytics/refresh", methods=["POST"])
@login_required
def teacher_classroom_analytics_refresh(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    for e in classroom.enrollments:  # make sure risk / grades / topic lists are current
        safe_refresh_metrics(e.student_id, classroom.id)
    cs = analytics.classroom_stats(classroom)
    get_class_insight(classroom, cs, force=True)
    flash("Class analysis refreshed.", "success")
    return redirect(url_for("teacher_classroom_analytics", classroom_id=classroom.id))


@app.route("/teacher/classroom/<int:classroom_id>/settings", methods=["GET", "POST"])
@login_required
def teacher_classroom_settings(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)

    if request.method == "POST":
        classroom.name = request.form.get("name", classroom.name)
        classroom.subject = request.form.get("subject", classroom.subject)
        classroom.target_grade = request.form.get("target_grade", classroom.target_grade)
        db.session.commit()
        flash("Classroom settings updated.", "success")
        return redirect(url_for("teacher_classroom_settings", classroom_id=classroom.id))

    return render_template("teacher/classroom_settings.html", classroom=classroom)


@app.route("/teacher/classroom/<int:classroom_id>/messages", methods=["GET", "POST"])
@login_required
def teacher_classroom_messages(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            msg = m.Message(
                classroom_id=classroom.id, sender_id=current_user.id,
                content=content, is_announcement=True,
            )
            db.session.add(msg)
            db.session.commit()
            for e in classroom.enrollments:
                push_notification(e.student_id, f"New announcement in {classroom.name}", "info")
        return redirect(url_for("teacher_classroom_messages", classroom_id=classroom.id))

    messages = m.Message.query.filter_by(classroom_id=classroom.id).order_by(
        m.Message.created_at.desc()
    ).all()
    return render_template("teacher/classroom_messages.html", classroom=classroom, messages=messages)


@app.route("/teacher/assignment/<int:assignment_id>/submissions")
@login_required
def teacher_assignment_submissions(assignment_id):
    require_role("teacher")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    classroom = assignment.classroom
    teacher_owns_classroom(classroom)
    _finalize_expired_for(m.AssessmentAttempt.query.filter_by(assignment_id=assignment.id))
    submissions = m.Submission.query.filter_by(assignment_id=assignment.id).order_by(
        m.Submission.submitted_at.desc()
    ).all()
    attempts = {t.student_id: t for t in m.AssessmentAttempt.query.filter_by(assignment_id=assignment.id)}
    done = {s.student_id for s in submissions}
    waiting = [e.student for e in classroom.enrollments if e.student_id not in done]
    return render_template(
        "teacher/assignment_submissions.html", assignment=assignment,
        classroom=classroom, submissions=submissions, attempts=attempts, waiting=waiting,
        state=sched.window_state(assignment.start_at, assignment.due_date),
    )


@app.route("/teacher/classroom/<int:classroom_id>/assignment/create", methods=["GET", "POST"])
@login_required
def teacher_assignment_create(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        kind = request.form.get("kind", "assignment")
        description = request.form.get("description", "")
        # Questions are NOT generated here any more — the teacher presses "Generate questions"
        # on the assignment page when ready (see teacher_assignment_generate).
        assignment = m.Assignment(
            classroom_id=classroom.id, title=title, description=description,
            kind=kind, questions_json=json.dumps([]),
        )
        err = _apply_schedule_form(assignment, request.form, creating=True)
        if err:
            flash(err, "error")
            return render_template("teacher/assignment_create.html", classroom=classroom)
        db.session.add(assignment)
        db.session.commit()
        flash("Created. Press “Generate questions” when you're ready — students can't see it until then.", "success")
        return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))

    return render_template("teacher/assignment_create.html", classroom=classroom)


@app.route("/teacher/assignment/<int:assignment_id>/generate", methods=["POST"])
@login_required
def teacher_assignment_generate(assignment_id):
    """Generate the questions for ONE classwork / assignment / test, on demand."""
    require_role("teacher")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    classroom = assignment.classroom
    teacher_owns_classroom(classroom)
    if assignment.has_questions():
        flash("Questions are already generated.", "info")
        return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))
    try:
        questions = ai.generate_assignment_questions(
            classroom.subject, assignment.title, assignment.description, assignment.kind,
            source_text=_classroom_source_text(classroom),
        )
    except Exception:
        current_app.logger.exception("Assignment question generation failed")
        questions = []
    if not questions:
        flash("The AI couldn't write the questions right now. Please try again.", "error")
    else:
        assignment.questions_json = json.dumps(questions)
        db.session.commit()
        for e in classroom.enrollments:
            push_notification(e.student_id, f"New {assignment.kind.replace('_', ' ')}: {assignment.title}", "info")
        flash(f"Generated {len(questions)} questions — students can see it now.", "success")
    return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))


@app.route("/teacher/submission/<int:submission_id>/grade", methods=["GET", "POST"])
@login_required
def teacher_grade_submission(submission_id):
    require_role("teacher")
    submission = db.get_or_404(m.Submission, submission_id)
    assignment = submission.assignment
    teacher_owns_classroom(assignment.classroom)

    if request.method == "POST":
        overall_score = float(request.form.get("score", submission.score or 0))
        feedback_note = request.form.get("feedback", "")
        submission.score = overall_score
        submission.feedback = json.dumps([{"note": feedback_note}])
        submission.status = "graded"
        submission.graded_at = datetime.now(timezone.utc)
        db.session.commit()
        push_notification(submission.student_id, f"Your submission for {assignment.title} was graded.", "info")
        flash("Submission graded.", "success")
        return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))

    return render_template("teacher/grade_submission.html", submission=submission, assignment=assignment)


@app.route("/teacher/submission/<int:submission_id>/auto-grade", methods=["POST"])
@login_required
def teacher_auto_grade_submission(submission_id):
    """Trigger AI understanding-engine grading for every answer in a submission."""
    require_role("teacher")
    submission = db.get_or_404(m.Submission, submission_id)
    assignment = submission.assignment
    teacher_owns_classroom(assignment.classroom)

    questions = assignment.questions()
    answers = submission.answers()
    feedback_list, misconceptions, total = [], [], 0

    for i, q in enumerate(questions):
        student_answer = answers[i] if i < len(answers) else ""
        result = ai.grade_answer(
            q.get("question", ""), q.get("answer", ""), student_answer,
            q.get("type", "short_answer"),
        )
        feedback_list.append(result)
        misconceptions.append(result.get("misconception", "none"))
        total += result.get("score", 0)

    submission.score = round(total / max(len(questions), 1), 1)
    submission.feedback = json.dumps(feedback_list)
    submission.misconceptions = json.dumps(misconceptions)
    submission.status = "graded"
    submission.graded_at = datetime.now(timezone.utc)
    analytics.record_assessment_attempts(submission, assignment, feedback_list)
    db.session.commit()
    safe_refresh_metrics(submission.student_id, assignment.classroom_id)
    push_notification(submission.student_id, f"Your submission for {assignment.title} was graded by the AI.", "info")
    flash("Auto-graded with the AI understanding engine.", "success")
    return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))


# ---------------------------------------------------------------------------
# STUDENT — Dashboard
# ---------------------------------------------------------------------------
@app.route("/student/dashboard")
@login_required
def student_dashboard():
    require_role("student")
    enrollments = m.Enrollment.query.filter_by(student_id=current_user.id).all()
    classrooms = [e.classroom for e in enrollments]

    upcoming = (
        m.Assignment.query.filter(
            m.Assignment.classroom_id.in_([c.id for c in classrooms]),
            m.Assignment.questions_json.notin_(["", "[]"]),   # hide anything not generated yet
        ).order_by(m.Assignment.due_date.asc()).limit(5).all()
        if classrooms else []
    )
    achievements = m.Achievement.query.filter_by(student_id=current_user.id).order_by(
        m.Achievement.earned_at.desc()
    ).limit(4).all()

    return render_template(
        "student/dashboard.html", classrooms=classrooms, enrollments=enrollments,
        upcoming=upcoming, achievements=achievements,
    )


@app.route("/student/join-classroom", methods=["POST"])
@login_required
def student_join_classroom():
    require_role("student")
    code = request.form.get("join_code", "").strip().upper()
    classroom = m.Classroom.query.filter_by(join_code=code).first()
    if not classroom:
        flash("Invalid join code.", "error")
        return redirect(url_for("student_dashboard"))

    existing = m.Enrollment.query.filter_by(classroom_id=classroom.id, student_id=current_user.id).first()
    if existing:
        flash("You're already enrolled in that classroom.", "info")
        return redirect(url_for("student_dashboard"))

    db.session.add(m.Enrollment(classroom_id=classroom.id, student_id=current_user.id))
    db.session.commit()
    push_notification(classroom.teacher_id, f"{current_user.name} joined {classroom.name}.", "info")
    flash(f"Joined {classroom.name}!", "success")
    return redirect(url_for("student_classroom", classroom_id=classroom.id))


@app.route("/student/classroom/<int:classroom_id>")
@login_required
def student_classroom(classroom_id):
    require_role("student")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    enrollment = student_enrolled(classroom)
    return render_template("student/classroom.html", classroom=classroom, enrollment=enrollment)


@app.route("/student/classroom/<int:classroom_id>/lesson/<int:lesson_id>")
@login_required
def student_lesson(classroom_id, lesson_id):
    require_role("student")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    student_enrolled(classroom)
    lesson = db.get_or_404(m.Lesson, lesson_id)
    if lesson.week.classroom_id != classroom.id:
        abort(404)
    if not lesson.is_generated():
        flash("Your teacher hasn't released this lesson yet — check back soon.", "info")
        return redirect(url_for("student_classroom", classroom_id=classroom.id))

    progress = m.LessonProgress.query.filter_by(
        student_id=current_user.id, lesson_id=lesson.id
    ).first()
    if not progress:
        progress = m.LessonProgress(student_id=current_user.id, lesson_id=lesson.id)
        db.session.add(progress)
        db.session.commit()

    return render_template(
        "student/lesson.html", classroom=classroom, lesson=lesson, progress=progress
    )


@app.route("/student/classroom/<int:classroom_id>/lesson/<int:lesson_id>/quiz", methods=["POST"])
@login_required
def student_lesson_quiz_submit(classroom_id, lesson_id):
    require_role("student")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    enrollment = student_enrolled(classroom)
    lesson = db.get_or_404(m.Lesson, lesson_id)
    quiz = lesson.get_json("quiz_json")

    correct, results = 0, []
    for i, q in enumerate(quiz):
        submitted = request.form.get(f"q{i}")
        ok = bool(submitted and ai._mcq_correct(submitted, q.get("answer")))
        results.append(ok)
        correct += 1 if ok else 0
    score = round((correct / max(len(quiz), 1)) * 100, 1)
    analytics.record_lesson_quiz_attempts(current_user.id, classroom.id, lesson, results)

    progress = m.LessonProgress.query.filter_by(student_id=current_user.id, lesson_id=lesson.id).first()
    progress.completed = True
    progress.quiz_score = score
    progress.completed_at = datetime.now(timezone.utc)

    # bump enrollment progress
    total_lessons = sum(1 for w in classroom.weeks for l in w.lessons if l.is_generated()) or 1
    done_lessons = (
        m.LessonProgress.query.join(m.Lesson).join(m.Week)
        .filter(m.Week.classroom_id == classroom.id, m.LessonProgress.student_id == current_user.id,
                m.LessonProgress.completed == True).count()  # noqa: E712
    )
    enrollment.progress_percent = round(done_lessons / total_lessons * 100, 1)

    current_user.add_xp(50 if score >= 70 else 20)
    if score == 100 and not m.Achievement.query.filter_by(
        student_id=current_user.id, title="Perfect Quiz"
    ).first():
        db.session.add(m.Achievement(
            student_id=current_user.id, title="Perfect Quiz",
            description="Scored 100% on a lesson quiz.", icon="star",
        ))

    db.session.commit()
    safe_refresh_metrics(current_user.id, classroom.id)
    flash(f"Quiz complete — you scored {score}%. +XP earned!", "success")
    return redirect(url_for("student_lesson", classroom_id=classroom.id, lesson_id=lesson.id))


@app.route("/student/study-alone", methods=["GET", "POST"])
@login_required
def student_study_alone():
    require_role("student")

    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        source_text = ""
        filename = None

        upload = request.files.get("document")
        if upload and upload.filename and allowed_file(upload.filename):
            filename, source_text = extract_text_from_upload(upload)

        if not topic and not source_text:
            flash("Give DROP a topic or upload a document to study.", "error")
            return redirect(url_for("student_study_alone"))

        study = m.StudySession(
            student_id=current_user.id, topic=topic or filename,
            source_filename=filename, source_text=source_text, status="pending",
        )
        db.session.add(study)
        db.session.commit()

        try:
            course = ai.generate_solo_plan(topic or filename, source_text)   # outline only
            try:
                tz = int(request.form.get("tz_offset", 0) or 0)
            except ValueError:
                tz = 0
            course["schedule"] = sched.build_solo_schedule(course, course.get("pacing"), tz)   # AI pacing -> dates
            study.ai_course_json = json.dumps(course)
            study.status = "ready"
        except Exception:
            current_app.logger.exception("Solo study course generation failed")
            study.status = "failed"
        db.session.commit()

        return redirect(url_for("student_study_session", session_id=study.id))

    sessions = m.StudySession.query.filter_by(student_id=current_user.id).order_by(
        m.StudySession.created_at.desc()
    ).all()
    return render_template("student/study_alone.html", sessions=sessions)


@app.route("/student/study-alone/<int:session_id>")
@login_required
def student_study_session(session_id):
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    attempt = _finalize_if_expired(_solo_attempt(study.id, current_user.id))
    if attempt is not None and attempt.status == "in_progress":
        return redirect(url_for("student_study_exam_take", session_id=study.id))
    info = _solo_exam_info(study)
    review = _solo_exam_review(study, attempt) if attempt is not None else []
    return render_template("student/study_session.html", study=study, attempt=attempt, exam_info=info,
                           review=review, exam_open=(not info["at"] or sched.utcnow() >= info["at"]))


@app.route("/student/study-alone/<int:session_id>/week/<int:week_index>/generate", methods=["POST"])
@login_required
def student_study_week_generate(session_id, week_index):
    """Generate the lessons for ONE week of a solo course (or only the ones that failed before)."""
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    course = study.course()
    weeks = course.get("weeks", [])
    if not (0 <= week_index < len(weeks)):
        abort(404)
    week = weeks[week_index]

    redo = request.form.get("redo") == "1"   # "Regenerate in detail"
    have = week.get("lessons") or []
    have_titles = {l.get("title") for l in have}
    planned = week.get("lesson_titles") or []
    missing = list(planned) if redo else [t for t in planned if t not in have_titles]
    if have and not missing:
        flash("That week is already generated.", "info")
        return redirect(url_for("student_study_session", session_id=study.id))

    try:
        new = ai.generate_week_lessons(study.topic, course, week, source_text=study.source_text or "",
                                       titles=missing or None)
    except Exception:
        current_app.logger.exception("Solo week generation failed")
        new = []
    if not new:
        flash("The AI couldn't generate this week right now. Please try again in a moment.", "error")
        return redirect(url_for("student_study_session", session_id=study.id))

    new_titles = {l.get("title") for l in new}
    lessons = new + [l for l in have if l.get("title") not in new_titles]   # failed redo keeps old lesson
    if planned:   # keep lessons in outline order
        lessons.sort(key=lambda l: planned.index(l["title"]) if l.get("title") in planned else 99)
    week["lessons"] = lessons
    week["generated"] = not planned or len(lessons) >= len(planned)
    study.ai_course_json = json.dumps(course)
    db.session.commit()
    flash(f"Week {week.get('number')} is ready." if week["generated"] else
          f"Generated {len(new)} lesson(s). Press Generate again for the rest.", "success")
    return redirect(url_for("student_study_session", session_id=study.id))


@app.route("/student/study-alone/<int:session_id>/finish", methods=["POST"])
@login_required
def student_study_session_finish(session_id):
    """Triggered by the "I'm done with this course" button. Generates the
    50-question final exam on demand from the full course content, rather
    than baking it in at course-creation time."""
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)

    course = study.course()
    ready_weeks = [w for w in course.get("weeks", []) if w.get("lessons")]
    if not ready_weeks:
        flash("Generate at least one week first — the exam is built from the weeks you've generated.", "error")
        return redirect(url_for("student_study_session", session_id=study.id))

    num_questions = min(50, 12 * len(ready_weeks))
    try:
        exam = ai.generate_exam(study.topic, ready_weeks, num_questions=num_questions)
    except Exception:
        current_app.logger.exception("Solo study final exam generation failed")
        exam = {"questions": []}
    if not exam["questions"]:
        flash("Couldn't generate the final exam right now — try again in a moment.", "error")
        return redirect(url_for("student_study_session", session_id=study.id))

    study.final_exam_json = json.dumps(exam)
    study.status = "completed"
    db.session.commit()
    flash(f"Final exam ready — {len(exam['questions'])} questions covering the weeks you generated.", "success")
    return redirect(url_for("student_study_session", session_id=study.id))


@app.route("/student/assignments")
@login_required
def student_assignments():
    require_role("student")
    classroom_ids = [e.classroom_id for e in current_user.enrollments]
    assignments = (
        m.Assignment.query.filter(
            m.Assignment.classroom_id.in_(classroom_ids), m.Assignment.kind.in_(["assignment", "classwork"])
        ).order_by(m.Assignment.due_date.asc()).all()
        if classroom_ids else []
    )
    my_subs = {s.assignment_id: s for s in m.Submission.query.filter_by(student_id=current_user.id).all()}
    assignments = [a for a in assignments if a.has_questions()]   # teacher must press Generate first
    return render_template("student/assignments.html", assignments=assignments, my_subs=my_subs)


@app.route("/student/tests")
@login_required
def student_tests():
    require_role("student")
    classroom_ids = [e.classroom_id for e in current_user.enrollments]
    tests = (
        m.Assignment.query.filter(
            m.Assignment.classroom_id.in_(classroom_ids),
            m.Assignment.kind.in_(["weekly_test", "monthly_test", "midterm", "final_exam"]),
        ).order_by(m.Assignment.due_date.asc()).all()
        if classroom_ids else []
    )
    my_subs = {s.assignment_id: s for s in m.Submission.query.filter_by(student_id=current_user.id).all()}
    tests = [a for a in tests if a.has_questions()]   # teacher must press Generate first
    attempts = {t.assignment_id: t for t in m.AssessmentAttempt.query.filter_by(student_id=current_user.id)
                if t.assignment_id}
    return render_template("student/tests.html", tests=tests, my_subs=my_subs, attempts=attempts)


@app.route("/student/revision")
@login_required
def student_revision():
    require_role("student")
    profile = m.LearningProfile.query.filter_by(student_id=current_user.id).first()
    classroom_ids = [e.classroom_id for e in current_user.enrollments]
    recent_lessons = (
        m.LessonProgress.query.filter_by(student_id=current_user.id, completed=True)
        .order_by(m.LessonProgress.completed_at.desc()).limit(8).all()
    )
    stats = analytics.student_stats(current_user.id)

    upcoming_tests, solo_upcoming = [], []
    if classroom_ids:
        mine = {s.assignment_id for s in m.Submission.query.filter_by(student_id=current_user.id)}
        started = {t.assignment_id for t in m.AssessmentAttempt.query.filter_by(student_id=current_user.id) if t.assignment_id}
        for a in m.Assignment.query.filter(
            m.Assignment.classroom_id.in_(classroom_ids),
            m.Assignment.kind.in_(["weekly_test", "monthly_test", "midterm", "final_exam"]),
        ).all():
            if a.has_questions() and a.id not in mine and a.id not in started and sched.window_state(a.start_at, a.due_date) != "closed":
                upcoming_tests.append(a)
        upcoming_tests.sort(key=lambda a: a.start_at or a.due_date or datetime.max)
    for study in m.StudySession.query.filter_by(student_id=current_user.id).all():
        info = _solo_exam_info(study)
        if info["at"] and info["at"] > sched.utcnow() and not _solo_attempt(study.id, current_user.id):
            solo_upcoming.append({"study": study, "at": info["at"]})
    return render_template("student/revision.html", profile=profile, recent_lessons=recent_lessons, stats=stats,
                           upcoming_tests=upcoming_tests, solo_upcoming=solo_upcoming)


@app.route("/student/messages", methods=["GET", "POST"])
@login_required
def student_messages():
    require_role("student")
    classroom_ids = [e.classroom_id for e in current_user.enrollments]

    if request.method == "POST":
        classroom_id = int(request.form.get("classroom_id"))
        content = request.form.get("content", "").strip()
        if content and classroom_id in classroom_ids:
            classroom = db.get_or_404(m.Classroom, classroom_id)
            db.session.add(m.Message(
                classroom_id=classroom_id, sender_id=current_user.id,
                recipient_id=classroom.teacher_id, content=content,
            ))
            db.session.commit()
        return redirect(url_for("student_messages"))

    messages = (
        m.Message.query.filter(m.Message.classroom_id.in_(classroom_ids)).order_by(
            m.Message.created_at.desc()
        ).all() if classroom_ids else []
    )
    classrooms = [e.classroom for e in current_user.enrollments]
    return render_template("student/messages.html", messages=messages, classrooms=classrooms)


@app.route("/student/tutor")
@login_required
def student_tutor():
    require_role("student")
    history = m.AIChatMessage.query.filter_by(user_id=current_user.id).order_by(
        m.AIChatMessage.created_at.asc()
    ).all()
    return render_template("student/tutor.html", history=history)


@app.route("/api/tutor/chat", methods=["POST"])
@login_required
def api_tutor_chat():
    require_role("student")
    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    mode = data.get("mode", "default")
    context = data.get("context", "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400

    db.session.add(m.AIChatMessage(user_id=current_user.id, role="user", content=message))
    db.session.commit()

    history_rows = m.AIChatMessage.query.filter_by(user_id=current_user.id).order_by(
        m.AIChatMessage.created_at.asc()
    ).all()
    history = [{"role": r.role, "content": r.content} for r in history_rows[:-1]]

    try:
        reply = ai.tutor_reply(history, message, mode, lesson_context=context)
    except Exception as e:
        current_app.logger.exception("AI tutor call failed")
        reply = "Sorry, the AI tutor hit a snag. Please try again in a moment."

    db.session.add(m.AIChatMessage(user_id=current_user.id, role="assistant", content=reply))
    current_user.add_xp(5)
    db.session.commit()

    return jsonify({"reply": reply})


@app.route("/student/progress")
@login_required
def student_progress():
    require_role("student")
    enrollments = current_user.enrollments
    submissions = m.Submission.query.filter_by(student_id=current_user.id).order_by(
        m.Submission.submitted_at.asc()
    ).all()
    profile = m.LearningProfile.query.filter_by(student_id=current_user.id).first()
    stats = analytics.student_stats(current_user.id)
    coach = m.StudentInsight.query.filter_by(student_id=current_user.id, classroom_id=None).first()
    return render_template(
        "student/progress.html", enrollments=enrollments, submissions=submissions, profile=profile,
        stats=stats, coach=coach.data() if coach else {}, coach_row=coach,
    )


@app.route("/student/progress/coach", methods=["POST"])
@login_required
def student_coach_refresh():
    """The student's own AI coaching notes (strengths, weaknesses, next steps)."""
    require_role("student")
    safe_refresh_metrics(current_user.id)
    stats = analytics.student_stats(current_user.id)
    if not analytics.has_data(stats):
        flash("Complete a quiz or study a lesson first — then your AI coach has something to work with.", "info")
    else:
        get_student_insight(current_user, None, stats, force=True)
        flash("Your coaching notes are up to date.", "success")
    return redirect(url_for("student_progress"))


@app.route("/student/achievements")
@login_required
def student_achievements():
    require_role("student")
    achievements = m.Achievement.query.filter_by(student_id=current_user.id).order_by(
        m.Achievement.earned_at.desc()
    ).all()
    return render_template("student/achievements.html", achievements=achievements)


# ---------------------------------------------------------------------------
# Study tracking (called by static/js/main.js and the solo study page)
# ---------------------------------------------------------------------------
@app.route("/api/track/time", methods=["POST"])
@login_required
def api_track_time():
    """Credit active study seconds to a lesson or a solo study session."""
    if current_user.role != "student":
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    try:
        ref_id, seconds = int(data.get("id")), int(data.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad request"}), 400
    new_session = bool(data.get("new_session"))

    if data.get("kind") == "lesson":
        lesson = db.session.get(m.Lesson, ref_id)
        if lesson is None:
            return jsonify({"ok": False}), 404
        classroom = lesson.week.classroom
        if not m.Enrollment.query.filter_by(classroom_id=classroom.id, student_id=current_user.id).first():
            return jsonify({"ok": False}), 403
        analytics.add_study_seconds(current_user.id, seconds, lesson=lesson,
                                    classroom_id=classroom.id, new_session=new_session)
    elif data.get("kind") == "study_session":
        study = db.session.get(m.StudySession, ref_id)
        if study is None or study.student_id != current_user.id:
            return jsonify({"ok": False}), 403
        analytics.add_study_seconds(current_user.id, seconds, study_session_id=study.id, new_session=new_session)
    else:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    return jsonify({"ok": True})


@app.route("/api/track/solo-attempts", methods=["POST"])
@login_required
def api_track_solo_attempts():
    """Record per-question results from the client-side solo quizzes / final exam."""
    if current_user.role != "student":
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    study = db.session.get(m.StudySession, int(data.get("session_id") or 0))
    source = data.get("source")
    if study is None or study.student_id != current_user.id or source not in ("solo_quiz", "solo_exam"):
        return jsonify({"ok": False}), 400
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    analytics.record_solo_attempts(current_user.id, study.id, source, items)
    db.session.commit()
    safe_refresh_metrics(current_user.id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# SCHEDULING · SECURE EXAMS · CALENDAR · REMINDERS · REVISION · EXPORTS
#
# Times are stored as naive UTC (see scheduling.py). Secure exams are enforced on the SERVER
# (clock, strike counting, auto-submit, page lockdown); the browser script (static/js/exam_guard.js)
# only detects and reports what the server can't see.
# ---------------------------------------------------------------------------
EXAM_GRACE_SECONDS = 20     # answers posted this long after the deadline are still accepted
AWAY_SECONDS = 20           # coming back to the exam page after this long away counts as a strike
SOLO_STRIKES = 3
MAX_EVENTS_PER_ATTEMPT = 600

# These count as a strike. Everything else is logged for the teacher but never auto-submits.
COUNTED_EVENTS = {"fullscreen_exit", "tab_hidden", "window_blur", "left_exam_page"}
SOFT_EVENTS = {
    "copy_attempt", "cut_attempt", "paste_attempt", "context_menu", "devtools_key", "devtools_suspected",
    "print_attempt", "screenshot_key", "back_button", "reload_key", "blocked_key", "page_left",
    "fullscreen_unsupported", "multi_monitor", "started", "other",
}
EVENT_LABELS = {
    "fullscreen_exit": "Left fullscreen", "tab_hidden": "Switched tab / window", "window_blur": "Window lost focus",
    "left_exam_page": "Left the exam page", "copy_attempt": "Tried to copy", "cut_attempt": "Tried to cut",
    "paste_attempt": "Tried to paste", "context_menu": "Right-click", "devtools_key": "Dev-tools shortcut",
    "devtools_suspected": "Dev tools may be open", "print_attempt": "Tried to print",
    "screenshot_key": "Screenshot key", "back_button": "Back / forward", "reload_key": "Tried to reload",
    "blocked_key": "Blocked shortcut", "page_left": "Page closed / navigated", "fullscreen_unsupported":
    "Browser can't do fullscreen", "multi_monitor": "Multiple displays", "started": "Started", "other": "Other",
}
# Endpoints a student may still reach while an exam is running.
LOCKDOWN_ALLOWED = {
    "static", "logout", "student_assignment_take", "student_study_exam_take", "student_attempt_submit",
    "api_attempt_save", "api_attempt_event",
}
KIND_LABEL = analytics.KIND_LABELS


def _kind_label(kind):
    return KIND_LABEL.get(kind, (kind or "").replace("_", " ").title())


def _ensure_columns():
    """db.create_all() adds new TABLES but never new columns to existing ones, so add ours if missing."""
    from sqlalchemy import inspect, text
    wanted = {"assignments": [
        ("start_at", "DATETIME"), ("duration_minutes", "INTEGER"),
        ("secure_mode", "BOOLEAN"), ("max_violations", "INTEGER DEFAULT 3"),
    ]}
    try:
        insp = inspect(db.engine)
        pg = db.engine.dialect.name == "postgresql"
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.replace('DATETIME', 'TIMESTAMP') if pg else ddl}"))
                    print(f"[DROP] added column {table}.{name}")
        db.session.commit()
    except Exception:
        db.session.rollback()
        print("[DROP] WARNING: couldn't add the new scheduling columns automatically:")
        current_app.logger.exception("Column migration failed")


with app.app_context():
    _ensure_columns()


app.add_template_filter(sched.iso_z, "utc_iso")
app.add_template_filter(sched.parse_iso_z, "iso_dt")
app.add_template_filter(ai.plainify_text, "plain")   # old tutor messages were saved with Markdown symbols


# ---------------------------------------------------------------------------
# Attempt plumbing
# ---------------------------------------------------------------------------
def _attempt_for(assignment, student_id):
    return m.AssessmentAttempt.query.filter_by(assignment_id=assignment.id, student_id=student_id).first()


def _solo_attempt(study_id, student_id):
    return m.AssessmentAttempt.query.filter_by(study_session_id=study_id, student_id=student_id).first()


def _expired(att):
    return att.status == "in_progress" and sched.utcnow() > sched.naive_utc(att.deadline_at)


def _remaining(att):
    return max(0, int((sched.naive_utc(att.deadline_at) - sched.utcnow()).total_seconds()))


def _attempt_questions(att):
    if att.assignment_id:
        return att.assignment.questions()
    study = db.session.get(m.StudySession, att.study_session_id)
    return (study.final_exam().get("questions") or []) if study else []


def _attempt_limit(att):
    return att.assignment.strike_limit() if att.assignment_id else SOLO_STRIKES


def _attempt_after_url(att):
    if att.assignment_id:
        return url_for("student_assignment_detail", assignment_id=att.assignment_id)
    return url_for("student_study_session", session_id=att.study_session_id)


def _attempt_take_url(att):
    if att.assignment_id:
        return url_for("student_assignment_take", assignment_id=att.assignment_id)
    return url_for("student_study_exam_take", session_id=att.study_session_id)


def _option_body(o):
    return re.sub(r"^[A-Da-d][.)]\s*", "", str(o)).strip()


def _grade_questions(questions, answers, ai_for_wrong_mcq=True):
    """Per-question feedback in the shape the templates already use. Blank answers never cost an AI call."""
    feedback, misconceptions, total = [], [], 0
    for i, q in enumerate(questions):
        ans = str(answers[i]).strip() if i < len(answers) and answers[i] is not None else ""
        qtype = q.get("type") or ("mcq" if q.get("options") else "short_answer")
        if not ans:
            res = {"score": 0, "is_correct": False, "feedback": "No answer given.", "misconception": "none", "reteach_tip": ""}
        elif qtype == "mcq":
            ok = ai._mcq_correct(ans, q.get("answer"))
            if ok or not ai_for_wrong_mcq:
                res = {"score": 100 if ok else 0, "is_correct": ok, "misconception": "none", "reteach_tip": "",
                       "feedback": "Correct — well done." if ok else f"Not quite. The correct answer was: {q.get('answer', '')}"}
            else:
                res = ai.grade_answer(q.get("question", ""), q.get("answer", ""), ans, qtype)
        else:
            res = ai.grade_answer(q.get("question", ""), q.get("answer", ""), ans, qtype)
        feedback.append(res)
        misconceptions.append(res.get("misconception", "none"))
        total += res.get("score", 0) or 0
    return feedback, misconceptions, round(total / max(len(questions), 1), 1)


def _grade_submission(submission, assignment, answers, student, ai_for_wrong_mcq=True):
    """Grade + save one Submission and update analytics/XP. Never raises: ungraded work stays 'submitted'."""
    try:
        feedback, misconceptions, score = _grade_questions(assignment.questions(), answers, ai_for_wrong_mcq)
        submission.score = score
        submission.feedback = json.dumps(feedback)
        submission.misconceptions = json.dumps(misconceptions)
        submission.status = "graded"
        submission.graded_at = datetime.now(timezone.utc)
        analytics.record_assessment_attempts(submission, assignment, feedback)
        student.add_xp(30)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Grading submission failed")
    db.session.commit()
    safe_refresh_metrics(student.id, assignment.classroom_id)


def _finalize_attempt(att, reason, final_answers=None):
    """End an attempt exactly once: save answers, create the Submission, grade. Safe to call repeatedly."""
    db.session.refresh(att)
    if att.status == "submitted":
        return att
    now_ = sched.utcnow()
    deadline = sched.naive_utc(att.deadline_at)
    questions = _attempt_questions(att)
    merged = dict(att.draft())
    if final_answers and now_ <= deadline + timedelta(seconds=EXAM_GRACE_SECONDS):
        merged.update({str(k): v for k, v in final_answers.items()})
    answers = [str(merged.get(str(i)) or "") for i in range(len(questions))]

    att.status = "submitted"
    att.end_reason = reason
    att.submitted_at = min(now_, deadline) if reason == "time" else now_
    att.draft_json = json.dumps({str(i): a for i, a in enumerate(answers)})
    db.session.commit()

    student = db.session.get(m.User, att.student_id)
    if att.assignment_id:
        assignment = att.assignment
        if not m.Submission.query.filter_by(assignment_id=assignment.id, student_id=student.id).first():
            sub = m.Submission(assignment_id=assignment.id, student_id=student.id,
                               answers_json=json.dumps(answers), status="submitted", submitted_at=att.submitted_at)
            db.session.add(sub)
            db.session.commit()
            # Exams grade multiple-choice locally (instant, no AI calls while the student waits).
            _grade_submission(sub, assignment, answers, student, ai_for_wrong_mcq=False)
        if reason == "violations":
            push_notification(assignment.classroom.teacher_id,
                              f"{student.name} was auto-submitted from “{assignment.title}” after {att.violation_count} violations.",
                              "exam_risk")
    else:
        study = db.session.get(m.StudySession, att.study_session_id)
        correct, items = 0, []
        for q, a in zip(questions, answers):
            ok = bool(a) and ai._mcq_correct(a, q.get("answer"))
            correct += 1 if ok else 0
            items.append({"topic": q.get("topic") or study.topic, "correct": ok})
        att.correct, att.total = correct, len(questions)
        att.score = round(correct / max(len(questions), 1) * 100, 1)
        try:
            analytics.record_solo_attempts(student.id, study.id, "solo_exam", items)
            student.add_xp(40 if att.score >= 70 else 15)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Recording solo exam attempts failed")
        db.session.commit()
        safe_refresh_metrics(student.id)
    return att


def _finalize_if_expired(att):
    if att is not None and _expired(att):
        _finalize_attempt(att, "time")
    return att


def _finalize_expired_for(query):
    for att in query.filter(m.AssessmentAttempt.status == "in_progress").all():
        _finalize_if_expired(att)


def _record_event(att, kind, detail=""):
    """Log an event; count it as a strike if it qualifies; auto-submit at the limit. -> (counted, ended)."""
    kind = kind if (kind in COUNTED_EVENTS or kind in SOFT_EVENTS) else "other"
    counted = kind in COUNTED_EVENTS
    now_ = sched.utcnow()
    if m.ProctorEvent.query.filter_by(attempt_id=att.id).count() >= MAX_EVENTS_PER_ATTEMPT and not counted:
        return False, False
    if counted:
        last = (m.ProctorEvent.query.filter_by(attempt_id=att.id, counted=True)
                .order_by(m.ProctorEvent.created_at.desc()).first())
        # One tab switch fires several browser events at once; only the first is a strike.
        if last and (now_ - sched.naive_utc(last.created_at)).total_seconds() < 2.0:
            counted = False
            detail = (detail + " (same incident)").strip()
    db.session.add(m.ProctorEvent(attempt_id=att.id, kind=kind, detail=(detail or "")[:300], counted=counted))
    if counted:
        att.violation_count = (att.violation_count or 0) + 1
    db.session.commit()
    if counted and att.violation_count >= _attempt_limit(att):
        _finalize_attempt(att, "violations")
        return True, True
    return counted, False


def _active_attempt(student_id):
    """The student's running exam, if any. Expired ones are finalised on the spot."""
    for att in m.AssessmentAttempt.query.filter_by(student_id=student_id, status="in_progress").all():
        if _expired(att):
            _finalize_attempt(att, "time")
            continue
        return att
    return None


@app.before_request
def _exam_lockdown():
    """While a student has an exam running, every other page is off limits (so a second tab can't
    be used for lessons, the AI tutor, or anything else). They are sent back to the exam."""
    if not current_user.is_authenticated or current_user.role != "student":
        return None
    ep = request.endpoint or ""
    if ep in LOCKDOWN_ALLOWED or request.path.startswith("/static/"):
        return None
    att = _active_attempt(current_user.id)
    if att is None:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "An exam is in progress."}), 423
    return redirect(_attempt_take_url(att))


def _render_take(att, title, subtitle):
    """The locked-down exam page. Question + option order is shuffled per student (stable on refresh)."""
    now_ = sched.utcnow()
    gap = (now_ - sched.naive_utc(att.last_seen_at or att.started_at)).total_seconds()
    att.last_seen_at = now_
    db.session.commit()
    if gap > AWAY_SECONDS:
        _, ended = _record_event(att, "left_exam_page", f"away for {int(gap)}s")
        if ended or att.status != "in_progress":
            flash("Your exam was submitted automatically.", "info")
            return redirect(_attempt_after_url(att))

    questions = _attempt_questions(att)
    draft = att.draft()
    items = []
    for pos, lay in enumerate(sched.shuffled_layout(questions, att.seed, shuffle=True), 1):
        q = questions[lay["index"]]
        opts = list(q.get("options") or [])
        shown = [{"value": opts[j], "label": f"{'ABCD'[k] if k < 4 else k + 1}. {_option_body(opts[j])}"}
                 for k, j in enumerate(lay["options"])]
        items.append({"n": pos, "index": lay["index"], "question": q.get("question", ""),
                      "options": shown, "saved": draft.get(str(lay["index"]), "")})
    cfg = {
        "attemptId": att.id, "remaining": _remaining(att), "limit": _attempt_limit(att),
        "violations": att.violation_count or 0, "total": len(items),
        "saveUrl": url_for("api_attempt_save", attempt_id=att.id),
        "eventUrl": url_for("api_attempt_event", attempt_id=att.id),
    }
    return render_template("student/exam_take.html", items=items, cfg=cfg, title=title, subtitle=subtitle,
                           submit_url=url_for("student_attempt_submit", attempt_id=att.id))


def _own_attempt(attempt_id):
    require_role("student")
    att = db.get_or_404(m.AssessmentAttempt, attempt_id)
    if att.student_id != current_user.id:
        abort(403)
    return att


@app.route("/api/attempt/<int:attempt_id>/event", methods=["POST"])
@login_required
def api_attempt_event(attempt_id):
    att = _own_attempt(attempt_id)
    _finalize_if_expired(att)
    if att.status != "in_progress":
        return jsonify({"ok": True, "ended": True, "redirect": _attempt_after_url(att)})
    data = request.get_json(force=True, silent=True) or {}
    counted, ended = _record_event(att, str(data.get("kind", "other"))[:40], str(data.get("detail", ""))[:300])
    return jsonify({"ok": True, "counted": counted, "count": att.violation_count or 0, "limit": _attempt_limit(att),
                    "ended": ended, "redirect": _attempt_after_url(att) if ended else None,
                    "remaining": _remaining(att)})


@app.route("/api/attempt/<int:attempt_id>/save", methods=["POST"])
@login_required
def api_attempt_save(attempt_id):
    """Autosave + heartbeat. Also the channel the server uses to tell the page the exam is over."""
    att = _own_attempt(attempt_id)
    _finalize_if_expired(att)
    if att.status != "in_progress":
        return jsonify({"ok": True, "ended": True, "redirect": _attempt_after_url(att)})
    data = request.get_json(force=True, silent=True) or {}
    n = len(_attempt_questions(att))
    merged = att.draft()
    for k, v in (data.get("answers") or {}).items():
        if str(k).isdigit() and int(k) < n:
            merged[str(int(k))] = str(v)[:4000]
    att.draft_json = json.dumps(merged)
    att.last_seen_at = sched.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "remaining": _remaining(att), "count": att.violation_count or 0,
                    "answered": sum(1 for v in merged.values() if str(v).strip())})


@app.route("/student/attempt/<int:attempt_id>/submit", methods=["POST"])
@login_required
def student_attempt_submit(attempt_id):
    att = _own_attempt(attempt_id)
    if att.status == "in_progress":
        n = len(_attempt_questions(att))
        final = {i: request.form.get(f"answer_{i}", "") for i in range(n) if f"answer_{i}" in request.form}
        reason = "time" if (_expired(att) or request.form.get("reason") == "time") else "student"
        _finalize_attempt(att, reason, final)
        flash("Time's up — your exam was submitted automatically." if reason == "time"
              else "Exam submitted. Well done!", "info" if reason == "time" else "success")
    return redirect(_attempt_after_url(att))


# ---------------------------------------------------------------------------
# Teacher: scheduling an assessment
# ---------------------------------------------------------------------------
def _apply_schedule_form(a, form, creating=False):
    """Copy the schedule fields from a submitted form onto an Assignment. Returns an error string or None."""
    start, due = sched.form_datetime(form, "start_at"), sched.form_datetime(form, "due_at")
    if start and due and due <= start:
        return "The close time has to be after the opening time."
    if creating and due is None:
        due = (start or sched.utcnow()) + timedelta(days=7)
    a.start_at, a.due_date = start, due
    a.secure_mode = form.get("secure_mode") == "on"
    try:
        dur = int(form.get("duration_minutes") or 0)
    except ValueError:
        dur = 0
    a.duration_minutes = max(5, min(300, dur)) if dur else None
    try:
        a.max_violations = max(1, min(10, int(form.get("max_violations") or 3)))
    except ValueError:
        a.max_violations = 3
    if a.secure_mode and start and due and (due - start).total_seconds() / 60 < a.time_limit():
        flash("Heads up: the open window is shorter than the time limit, so students starting late get less time.", "info")
    return None


@app.route("/teacher/assignment/<int:assignment_id>/schedule", methods=["GET", "POST"])
@login_required
def teacher_assignment_schedule(assignment_id):
    require_role("teacher")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    classroom = assignment.classroom
    teacher_owns_classroom(classroom)
    if request.method == "POST":
        err = _apply_schedule_form(assignment, request.form)
        if err:
            flash(err, "error")
        else:
            db.session.commit()
            if assignment.has_questions():
                for e in classroom.enrollments:
                    push_notification(e.student_id, f"Schedule updated for “{assignment.title}” — check your calendar.", "info")
            flash("Schedule saved.", "success")
            return redirect(url_for("teacher_assignment_submissions", assignment_id=assignment.id))
    return render_template("teacher/assignment_schedule.html", assignment=assignment, classroom=classroom)


# ---------------------------------------------------------------------------
# Student: opening a test / exam, the lobby, and revision
# ---------------------------------------------------------------------------
def _lobby_for_assignment(assignment, classroom, state):
    secure = assignment.is_secure()
    return {
        "title": assignment.title, "kind_label": _kind_label(assignment.kind), "where": classroom.name,
        "state": state, "secure": secure, "start": assignment.start_at, "due": assignment.due_date,
        "duration": assignment.time_limit() if secure else None, "questions": len(assignment.questions()),
        "limit": assignment.strike_limit(),
        "start_url": url_for("student_assignment_start", assignment_id=assignment.id) if (secure and state == "open") else None,
        "revise_url": url_for("student_assignment_revise", assignment_id=assignment.id),
        "back_url": url_for("student_tests" if secure else "student_assignments"),
    }


@app.route("/student/assignment/<int:assignment_id>", methods=["GET", "POST"])
@login_required
def student_assignment_detail(assignment_id):
    require_role("student")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    classroom = assignment.classroom
    student_enrolled(classroom)
    if not assignment.has_questions():
        flash("This isn't available yet — your teacher hasn't released it.", "info")
        return redirect(url_for("student_dashboard"))

    attempt = _finalize_if_expired(_attempt_for(assignment, current_user.id))
    existing = m.Submission.query.filter_by(assignment_id=assignment.id, student_id=current_user.id).first()
    if existing:
        return render_template("student/assignment_detail.html", assignment=assignment, classroom=classroom,
                               submission=existing, attempt=attempt)

    state = sched.window_state(assignment.start_at, assignment.due_date)
    if assignment.is_secure():
        if attempt and attempt.status == "in_progress":
            return redirect(url_for("student_assignment_take", assignment_id=assignment.id))
        return render_template("student/exam_lobby.html", info=_lobby_for_assignment(assignment, classroom, state))

    # Classwork / assignments: not secure, but they still respect the opening time.
    if state == "upcoming":
        return render_template("student/exam_lobby.html", info=_lobby_for_assignment(assignment, classroom, state))
    if request.method == "POST":
        questions = assignment.questions()
        answers = [request.form.get(f"answer_{i}", "") for i in range(len(questions))]
        submission = m.Submission(assignment_id=assignment.id, student_id=current_user.id,
                                  answers_json=json.dumps(answers), status="submitted")
        db.session.add(submission)
        db.session.commit()
        _grade_submission(submission, assignment, answers, current_user)
        flash("Submitted! Your work was graded instantly." + (" (It was past the due time, so your teacher will see it as late.)" if state == "closed" else ""), "success")
        return redirect(url_for("student_assignment_detail", assignment_id=assignment.id))
    return render_template("student/assignment_detail.html", assignment=assignment, classroom=classroom,
                           submission=None, attempt=None, late=(state == "closed"))


@app.route("/student/assignment/<int:assignment_id>/start", methods=["POST"])
@login_required
def student_assignment_start(assignment_id):
    require_role("student")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    student_enrolled(assignment.classroom)
    back = redirect(url_for("student_assignment_detail", assignment_id=assignment.id))
    if not (assignment.has_questions() and assignment.is_secure()):
        return back
    if m.Submission.query.filter_by(assignment_id=assignment.id, student_id=current_user.id).first():
        return back
    existing = _attempt_for(assignment, current_user.id)
    if existing:
        return back
    state = sched.window_state(assignment.start_at, assignment.due_date)
    if state != "open":
        flash("This isn't open right now.", "error")
        return back
    now_ = sched.utcnow()
    deadline = now_ + timedelta(minutes=assignment.time_limit())
    if assignment.due_date:
        deadline = min(deadline, sched.naive_utc(assignment.due_date))
    if deadline <= now_ + timedelta(seconds=30):
        flash("The window closed just now.", "error")
        return back
    att = m.AssessmentAttempt(student_id=current_user.id, assignment_id=assignment.id, started_at=now_,
                              last_seen_at=now_, deadline_at=deadline, seed=random.randint(1, 2 ** 31 - 1))
    db.session.add(att)
    db.session.flush()
    db.session.add(m.ProctorEvent(attempt_id=att.id, kind="started", detail=request.headers.get("User-Agent", "")[:200]))
    db.session.commit()
    return redirect(url_for("student_assignment_take", assignment_id=assignment.id))


@app.route("/student/assignment/<int:assignment_id>/take")
@login_required
def student_assignment_take(assignment_id):
    require_role("student")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    student_enrolled(assignment.classroom)
    att = _finalize_if_expired(_attempt_for(assignment, current_user.id))
    if att is None or att.status != "in_progress":
        return redirect(url_for("student_assignment_detail", assignment_id=assignment.id))
    return _render_take(att, assignment.title, f"{_kind_label(assignment.kind)} · {assignment.classroom.name}")


def _revision_page(title, back_url, sections, weak, note):
    return render_template("student/revise.html", title=title, back_url=back_url, sections=sections,
                           weak=weak, note=note, tutor_url=url_for("student_tutor"))


@app.route("/student/assignment/<int:assignment_id>/revise")
@login_required
def student_assignment_revise(assignment_id):
    """Revision pack for ONE test/exam, built from the lessons it covers plus the student's weak topics."""
    require_role("student")
    assignment = db.get_or_404(m.Assignment, assignment_id)
    classroom = assignment.classroom
    student_enrolled(classroom)
    if (_attempt_for(assignment, current_user.id)
            or m.Submission.query.filter_by(assignment_id=assignment.id, student_id=current_user.id).first()):
        flash("Revision closes once you've started or finished the assessment.", "info")
        return redirect(url_for("student_assignment_detail", assignment_id=assignment.id))
    groups = sched.covered_lessons(assignment)
    sections = [dict(sched.lesson_to_section(l), week=f"Week {w.number}: {w.title}") for w, ls in groups for l in ls]
    stats = analytics.student_stats(current_user.id, classroom.id)
    weak = [t["topic"] for t in stats.get("weak_topics", [])[:6]]
    return _revision_page(f"Revise: {assignment.title}", url_for("student_assignment_detail", assignment_id=assignment.id),
                          sections, weak, "These are the lessons this " + _kind_label(assignment.kind).lower()
                          + " is built from. Revision closes when you start the attempt.")


# ---------------------------------------------------------------------------
# Study Alone: AI-proposed schedule, secure final exam, revision
# ---------------------------------------------------------------------------
def _solo_exam_info(study):
    """Schedule facts for a solo course's final exam (works for older sessions without a schedule)."""
    s = (study.course() or {}).get("schedule") or {}
    fe = s.get("final_exam") or {}
    questions = (study.final_exam().get("questions") or [])
    duration = fe.get("duration_minutes") or max(15, min(120, len(questions)))
    return {"at": sched.parse_iso_z(fe.get("at")), "duration": int(duration), "questions": len(questions),
            "revision_at": sched.parse_iso_z(s.get("revision_at"))}


@app.route("/student/study-alone/<int:session_id>/schedule", methods=["POST"])
@login_required
def student_study_schedule(session_id):
    """Edit the AI's proposed schedule (or create one for a course that doesn't have it yet)."""
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    course = study.course()
    if request.form.get("action") == "auto" or not course.get("schedule"):
        try:
            tz = int(request.form.get("tz_offset", 0) or 0)
        except ValueError:
            tz = 0
        course["schedule"] = sched.build_solo_schedule(course, course.get("pacing"), tz)
        flash("Schedule created.", "success")
    else:
        s = course["schedule"]
        for wi, wk in enumerate(s.get("weeks", [])):
            slots = []
            for si, old in enumerate(wk.get("slots", [])):
                new = sched.form_datetime(request.form, f"slot_{wi}_{si}")
                slots.append(sched.iso_z(new) if new else old)
            wk["slots"] = sorted(slots)
        rev, exam = sched.form_datetime(request.form, "revision_at"), sched.form_datetime(request.form, "exam_at")
        if rev:
            s["revision_at"] = sched.iso_z(rev)
        if exam:
            s["final_exam"]["at"] = sched.iso_z(exam)
        try:
            s["final_exam"]["duration_minutes"] = max(10, min(180, int(request.form.get("exam_duration") or s["final_exam"]["duration_minutes"])))
        except (ValueError, KeyError):
            pass
        s["generated_by"] = "edited"
        flash("Schedule updated.", "success")
    study.ai_course_json = json.dumps(course)
    db.session.commit()
    return redirect(url_for("student_study_session", session_id=study.id))


@app.route("/student/study-alone/<int:session_id>/exam/start", methods=["POST"])
@login_required
def student_study_exam_start(session_id):
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    back = redirect(url_for("student_study_session", session_id=study.id))
    info = _solo_exam_info(study)
    if study.status != "completed" or not info["questions"] or _solo_attempt(study.id, current_user.id):
        return back
    if info["at"] and sched.utcnow() < info["at"]:
        flash("Your exam is scheduled for later. You can move the date in your schedule if you're ready sooner.", "info")
        return back
    now_ = sched.utcnow()
    att = m.AssessmentAttempt(student_id=current_user.id, study_session_id=study.id, started_at=now_, last_seen_at=now_,
                              deadline_at=now_ + timedelta(minutes=info["duration"]), seed=random.randint(1, 2 ** 31 - 1))
    db.session.add(att)
    db.session.flush()
    db.session.add(m.ProctorEvent(attempt_id=att.id, kind="started", detail=request.headers.get("User-Agent", "")[:200]))
    db.session.commit()
    return redirect(url_for("student_study_exam_take", session_id=study.id))


@app.route("/student/study-alone/<int:session_id>/exam/take")
@login_required
def student_study_exam_take(session_id):
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    att = _finalize_if_expired(_solo_attempt(study.id, current_user.id))
    if att is None or att.status != "in_progress":
        return redirect(url_for("student_study_session", session_id=study.id))
    return _render_take(att, f"Final exam · {study.topic}", "Study Alone")


@app.route("/student/study-alone/<int:session_id>/revise")
@login_required
def student_study_revise(session_id):
    require_role("student")
    study = db.get_or_404(m.StudySession, session_id)
    if study.student_id != current_user.id:
        abort(403)
    if _solo_attempt(study.id, current_user.id):
        flash("Revision closes once you've started the exam.", "info")
        return redirect(url_for("student_study_session", session_id=study.id))
    sections = []
    for w in (study.course().get("weeks") or []):
        for l in (w.get("lessons") or []):
            sections.append(dict(sched.lesson_to_section(l), week=f"Week {w.get('number')}: {w.get('title')}"))
    stats = analytics.student_stats(current_user.id)
    weak = [t["topic"] for t in stats.get("weak_topics", [])[:6]]
    return _revision_page(f"Revise: {study.topic}", url_for("student_study_session", session_id=study.id),
                          sections, weak, "Everything you've generated so far, in revision form.")


def _solo_exam_review(study, att):
    """After the exam: each question with the student's answer and the right one."""
    questions = _attempt_questions(att)
    draft = att.draft()
    out = []
    for i, q in enumerate(questions):
        a = str(draft.get(str(i)) or "")
        out.append({"question": q.get("question", ""), "yours": a, "answer": q.get("answer", ""),
                    "ok": bool(a) and ai._mcq_correct(a, q.get("answer"))})
    return out


# ---------------------------------------------------------------------------
# Calendar (teacher + student)
# ---------------------------------------------------------------------------
def _cal_kind(kind):
    return {"weekly_test": "test", "monthly_test": "test", "midterm": "exam", "final_exam": "exam"}.get(kind, kind)


def _assessment_events(a, teacher_view):
    ev = []
    url = (url_for("teacher_assignment_submissions", assignment_id=a.id) if teacher_view
           else url_for("student_assignment_detail", assignment_id=a.id))
    kind, label = _cal_kind(a.kind), _kind_label(a.kind)
    where = a.classroom.name
    if a.is_secure():
        at = a.start_at or a.due_date
        if at:
            end = at + timedelta(minutes=a.time_limit()) if a.start_at else at
            ev.append({"title": f"{label}: {a.title}", "kind": kind, "start": sched.iso_z(at), "end": sched.iso_z(end),
                       "url": url, "sub": f"{where} · {a.time_limit()} min" + (f" · closes {sched.iso_z(a.due_date)}" if a.start_at and a.due_date else "")})
    else:
        if a.start_at:
            ev.append({"title": f"Opens: {a.title}", "kind": kind, "start": sched.iso_z(a.start_at), "end": sched.iso_z(a.start_at), "url": url, "sub": where})
        if a.due_date:
            ev.append({"title": f"Due: {a.title}", "kind": kind, "start": sched.iso_z(a.due_date), "end": sched.iso_z(a.due_date), "url": url, "sub": f"{where} · {label}"})
    return ev


def _calendar_events(user):
    events = []
    if user.role == "teacher":
        for c in m.Classroom.query.filter_by(teacher_id=user.id).all():
            for a in c.assignments:
                if a.has_questions():
                    events.extend(_assessment_events(a, True))
        return events
    cids = [e.classroom_id for e in user.enrollments]
    if cids:
        for a in m.Assignment.query.filter(m.Assignment.classroom_id.in_(cids)).all():
            if a.has_questions():
                events.extend(_assessment_events(a, False))
    for study in m.StudySession.query.filter_by(student_id=user.id).all():
        s = (study.course() or {}).get("schedule") or {}
        url = url_for("student_study_session", session_id=study.id)
        mins = (s.get("pacing") or {}).get("minutes_per_session", 45)
        for w in s.get("weeks", []):
            for slot in w.get("slots", []):
                t = sched.parse_iso_z(slot)
                if t:
                    events.append({"title": f"Study: {study.topic} (week {w.get('number')})", "kind": "study", "start": slot,
                                   "end": sched.iso_z(t + timedelta(minutes=mins)), "url": url, "sub": f"Study Alone · {mins} min"})
        if s.get("revision_at"):
            events.append({"title": f"Revision: {study.topic}", "kind": "revision", "start": s["revision_at"], "end": s["revision_at"],
                           "url": url_for("student_study_revise", session_id=study.id), "sub": "Study Alone"})
        fe = s.get("final_exam") or {}
        if fe.get("at") and not _solo_attempt(study.id, user.id):
            t = sched.parse_iso_z(fe["at"])
            events.append({"title": f"Final exam: {study.topic}", "kind": "exam", "start": fe["at"],
                           "end": sched.iso_z(t + timedelta(minutes=fe.get("duration_minutes", 45))) if t else fe["at"],
                           "url": url, "sub": f"Study Alone · {fe.get('duration_minutes', 45)} min"})
    return events


@app.route("/calendar")
@login_required
def calendar_page():
    return render_template("shared/calendar.html", events=_calendar_events(current_user))


# ---------------------------------------------------------------------------
# Reminders (1 day / 1 hour / 10 minutes before things happen)
#
# Reminders are created when the person has DROP open (the page checks every minute) or opens it.
# They land in Notifications and pop up as an on-screen toast. Nothing is sent by email or push
# while DROP is closed — that would need a background scheduler / email service.
# ---------------------------------------------------------------------------
def _reminder_candidates(user):
    now_ = sched.utcnow()
    out = []   # (key, when, text, url, thresholds)
    if user.role == "teacher":
        for c in m.Classroom.query.filter_by(teacher_id=user.id).all():
            for a in c.assignments:
                if a.has_questions() and a.start_at and a.start_at > now_:
                    out.append((f"t{a.id}:start", a.start_at, f"Your {_kind_label(a.kind).lower()} “{a.title}” ({c.name}) opens",
                                url_for("teacher_assignment_submissions", assignment_id=a.id), sched.REMIND_BIG))
        return out
    cids = [e.classroom_id for e in user.enrollments]
    if cids:
        done = {s.assignment_id for s in m.Submission.query.filter_by(student_id=user.id)}
        for a in m.Assignment.query.filter(m.Assignment.classroom_id.in_(cids)).all():
            if not a.has_questions() or a.id in done:
                continue
            label = _kind_label(a.kind).lower()
            url = url_for("student_assignment_detail", assignment_id=a.id)
            if a.start_at and a.start_at > now_:
                tail = " — revise now." if a.is_secure() else "."
                out.append((f"a{a.id}:start", a.start_at, f"Your {label} “{a.title}” opens", url, sched.REMIND_BIG, tail))
            elif a.due_date and a.due_date > now_:
                out.append((f"a{a.id}:due", a.due_date, f"Your {label} “{a.title}” is due" if not a.is_secure()
                            else f"Your {label} “{a.title}” closes", url, sched.REMIND_BIG))
    for study in m.StudySession.query.filter_by(student_id=user.id).all():
        s = (study.course() or {}).get("schedule") or {}
        url = url_for("student_study_session", session_id=study.id)
        for w in s.get("weeks", []):
            for slot in w.get("slots", []):
                t = sched.parse_iso_z(slot)
                if t and t > now_:
                    out.append((f"s{study.id}:slot", t, f"Study session for “{study.topic}” (week {w.get('number')}) starts", url, sched.REMIND_SMALL))
        t = sched.parse_iso_z(s.get("revision_at"))
        if t and t > now_:
            out.append((f"s{study.id}:rev", t, f"Revision day for “{study.topic}” is", url_for("student_study_revise", session_id=study.id), sched.REMIND_BIG))
        t = sched.parse_iso_z((s.get("final_exam") or {}).get("at"))
        if t and t > now_ and not _solo_attempt(study.id, user.id):
            out.append((f"s{study.id}:exam", t, f"Your final exam for “{study.topic}” starts", url, sched.REMIND_BIG))
    return out


def _run_reminders(user):
    """Create any reminders that have just become due. Each fires once; only the closest threshold is sent."""
    from sqlalchemy.exc import IntegrityError
    now_, created = sched.utcnow(), []
    for cand in _reminder_candidates(user):
        key, at, text, url, thresholds = cand[:5]
        tail = cand[5] if len(cand) > 5 else "."
        hit = sched.due_threshold(at, now_, thresholds)
        if not hit:
            continue
        minutes, label = hit
        base = f"{key}@{sched.iso_z(at)}"
        keys = [f"{base}|{mn}" for mn, _ in thresholds if mn >= minutes]
        have = {r.key for r in m.ReminderSent.query.filter(m.ReminderSent.user_id == user.id, m.ReminderSent.key.in_(keys))}
        if f"{base}|{minutes}" in have:
            continue
        for k in keys:
            if k not in have:
                db.session.add(m.ReminderSent(user_id=user.id, key=k))
        content = f"⏰ {text} {label}{tail}"[:300]
        db.session.add(m.Notification(user_id=user.id, content=content, kind="reminder"))
        created.append({"text": content, "url": url})
    try:
        db.session.commit()
    except IntegrityError:        # two tabs polled at once; the other one already sent it
        db.session.rollback()
        return []
    return created


@app.route("/api/reminders/poll")
@login_required
def api_reminders_poll():
    return jsonify({"ok": True, "reminders": _run_reminders(current_user)})


# ---------------------------------------------------------------------------
# Teacher: reports, exports, integrity log
# ---------------------------------------------------------------------------
def _fmt_minutes(att):
    if att and att.submitted_at and att.started_at:
        return max(0.0, round((sched.naive_utc(att.submitted_at) - sched.naive_utc(att.started_at)).total_seconds() / 60, 1))
    return ""


def _is_late(a, sub):
    return bool(a.due_date and sub and sched.naive_utc(sub.submitted_at) > sched.naive_utc(a.due_date) + timedelta(seconds=EXAM_GRACE_SECONDS))


def _results_sheet(a):
    classroom = a.classroom
    subs = {s.student_id: s for s in m.Submission.query.filter_by(assignment_id=a.id)}
    atts = {t.student_id: t for t in m.AssessmentAttempt.query.filter_by(assignment_id=a.id)}
    rows = []
    for e in sorted(classroom.enrollments, key=lambda e: (e.student.name or "").lower()):
        s, t = subs.get(e.student_id), atts.get(e.student_id)
        status = ("Graded" if s and s.status == "graded" else "Submitted" if s else
                  "In progress" if t and t.status == "in_progress" else "Not submitted")
        rows.append([e.student.name, e.student.email, status, s.score if s and s.score is not None else "",
                     s.submitted_at if s else "", t.started_at if t else "", _fmt_minutes(t),
                     (t.violation_count or 0) if t else "", (t.end_reason or "") if t else "",
                     "Yes" if _is_late(a, s) else ("No" if s else "")])
    return ("Results", ["Student", "Email", "Status", "Score (%)", "Submitted (UTC)", "Started (UTC)",
                        "Minutes taken", "Violations", "Ended by", "Late"], rows)


def _questions_sheet(a):
    qs = a.questions()
    subs = list(m.Submission.query.filter_by(assignment_id=a.id))
    rows = []
    for i, q in enumerate(qs):
        answered = correct = 0
        wrong = {}
        for s in subs:
            ans = s.answers()
            val = str(ans[i]).strip() if i < len(ans) and ans[i] is not None else ""
            if not val:
                continue
            answered += 1
            if ai._mcq_correct(val, q.get("answer")):
                correct += 1
            else:
                wrong[val] = wrong.get(val, 0) + 1
        top = max(wrong.items(), key=lambda kv: kv[1]) if wrong else ("", 0)
        rows.append([i + 1, ai.plainify_text(q.get("question", "")), q.get("topic", ""), ai.plainify_text(q.get("answer", "")),
                     answered, len(subs) - answered, round(correct / answered * 100, 1) if answered else "",
                     ai.plainify_text(top[0]), top[1] or ""])
    return ("Question analysis", ["#", "Question", "Topic", "Correct answer", "Answered", "Skipped", "% correct",
                                  "Most common wrong answer", "Times chosen"], rows)


def _violations_sheet(assignments):
    rows = []
    for a in assignments:
        for t in m.AssessmentAttempt.query.filter_by(assignment_id=a.id):
            for ev in t.events:
                rows.append([t.student.name, t.student.email, a.title, ev.created_at, EVENT_LABELS.get(ev.kind, ev.kind),
                             ev.detail or "", "Yes" if ev.counted else "No"])
    return ("Integrity log", ["Student", "Email", "Assessment", "Time (UTC)", "Event", "Detail", "Counted as strike"], rows)


def _gradebook_sheets(classroom):
    assessments = sorted([a for a in classroom.assignments if a.has_questions()],
                         key=lambda a: (a.start_at or a.due_date or a.created_at or datetime.min))
    subs = {}
    for s in m.Submission.query.join(m.Assignment).filter(m.Assignment.classroom_id == classroom.id):
        subs[(s.student_id, s.assignment_id)] = s
    enrolls = sorted(classroom.enrollments, key=lambda e: (e.student.name or "").lower())
    head = ["Student", "Email"] + [a.title for a in assessments] + ["Average (%)"]
    grid = []
    for e in enrolls:
        scores = []
        row = [e.student.name, e.student.email]
        for a in assessments:
            s = subs.get((e.student_id, a.id))
            v = s.score if s and s.score is not None else ""
            row.append(v)
            if v != "":
                scores.append(v)
        row.append(round(sum(scores) / len(scores), 1) if scores else "")
        grid.append(row)
    summary = []
    for a in assessments:
        sc = [s.score for (sid, aid), s in subs.items() if aid == a.id and s.score is not None]
        summary.append([a.title, _kind_label(a.kind), a.start_at or "", a.due_date or "", a.time_limit() if a.is_secure() else "",
                        "Yes" if a.is_secure() else "No", len(sc), round(sum(sc) / len(sc), 1) if sc else "",
                        max(sc) if sc else "", min(sc) if sc else ""])
    students = [[e.student.name, e.student.email, e.progress_percent, e.predicted_grade or "", e.risk_level or ""] for e in enrolls]
    return [
        ("Gradebook", head, grid),
        ("Assessments", ["Title", "Type", "Opens (UTC)", "Closes (UTC)", "Time limit (min)", "Secure", "Graded", "Average", "Highest", "Lowest"], summary),
        ("Students", ["Student", "Email", "Course progress (%)", "Predicted grade", "Risk"], students),
    ]


def _own_assignment_t(assignment_id):
    require_role("teacher")
    a = db.get_or_404(m.Assignment, assignment_id)
    teacher_owns_classroom(a.classroom)
    return a


@app.route("/teacher/assignment/<int:assignment_id>/export/<what>/<fmt>")
@login_required
def teacher_assignment_export(assignment_id, what, fmt):
    a = _own_assignment_t(assignment_id)
    if fmt not in ("csv", "xlsx") or what not in ("results", "questions", "violations", "all"):
        abort(404)
    _finalize_expired_for(m.AssessmentAttempt.query.filter_by(assignment_id=a.id))
    sheets = {"results": [_results_sheet(a)], "questions": [_questions_sheet(a)], "violations": [_violations_sheet([a])],
              "all": [_results_sheet(a), _questions_sheet(a), _violations_sheet([a])]}[what]
    return exports.table_response(f"{a.classroom.name}-{a.title}-{what}", sheets, "xlsx" if what == "all" else fmt)


@app.route("/teacher/classroom/<int:classroom_id>/reports")
@login_required
def teacher_classroom_reports(classroom_id):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    ids = [a.id for a in classroom.assignments]
    if ids:
        _finalize_expired_for(m.AssessmentAttempt.query.filter(m.AssessmentAttempt.assignment_id.in_(ids)))
    rows = []
    for a in sorted([a for a in classroom.assignments if a.has_questions()],
                    key=lambda a: (a.start_at or a.due_date or a.created_at or datetime.min)):
        atts = m.AssessmentAttempt.query.filter_by(assignment_id=a.id).all()
        rows.append({"a": a, "submitted": len(a.submissions), "flagged": sum(1 for t in atts if (t.violation_count or 0) > 0),
                     "auto": sum(1 for t in atts if t.end_reason == "violations")})
    return render_template("teacher/classroom_reports.html", classroom=classroom, rows=rows, xlsx=exports.xlsx_available())


@app.route("/teacher/classroom/<int:classroom_id>/export/<what>/<fmt>")
@login_required
def teacher_classroom_export(classroom_id, what, fmt):
    require_role("teacher")
    classroom = db.get_or_404(m.Classroom, classroom_id)
    teacher_owns_classroom(classroom)
    if fmt not in ("csv", "xlsx") or what not in ("gradebook", "violations", "all"):
        abort(404)
    ids = [a.id for a in classroom.assignments]
    if ids:
        _finalize_expired_for(m.AssessmentAttempt.query.filter(m.AssessmentAttempt.assignment_id.in_(ids)))
    assessments = [a for a in classroom.assignments if a.has_questions()]
    book = _gradebook_sheets(classroom)
    if what == "gradebook":
        sheets = [book[0]]
    elif what == "violations":
        sheets = [_violations_sheet(assessments)]
    else:
        sheets = book + [_violations_sheet(assessments)] + [_results_sheet(a) for a in assessments[:20]]
    return exports.table_response(f"{classroom.name}-{what}", sheets, "xlsx" if what == "all" else fmt)


@app.route("/teacher/assignment/<int:assignment_id>/integrity/<int:student_id>")
@login_required
def teacher_attempt_log(assignment_id, student_id):
    a = _own_assignment_t(assignment_id)
    att = m.AssessmentAttempt.query.filter_by(assignment_id=a.id, student_id=student_id).first_or_404()
    _finalize_if_expired(att)
    return render_template("teacher/proctor_log.html", assignment=a, classroom=a.classroom, attempt=att,
                           labels=EVENT_LABELS, minutes=_fmt_minutes(att))


@app.route("/teacher/assignment/<int:assignment_id>/reset/<int:student_id>", methods=["POST"])
@login_required
def teacher_attempt_reset(assignment_id, student_id):
    """Let one student sit the assessment again (e.g. their internet dropped). Deletes their attempt + result."""
    a = _own_assignment_t(assignment_id)
    att = m.AssessmentAttempt.query.filter_by(assignment_id=a.id, student_id=student_id).first()
    sub = m.Submission.query.filter_by(assignment_id=a.id, student_id=student_id).first()
    if sub:
        m.QuestionAttempt.query.filter_by(submission_id=sub.id).delete()
        db.session.delete(sub)
    if att:
        db.session.delete(att)
    db.session.commit()
    safe_refresh_metrics(student_id, a.classroom_id)
    push_notification(student_id, f"Your teacher reopened “{a.title}” so you can take it again.", "info")
    flash("Reset — that student can take it again.", "success")
    return redirect(url_for("teacher_assignment_submissions", assignment_id=a.id))


# ---------------------------------------------------------------------------
# Settings (shared)
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        form_type = request.form.get("form_type")
        if form_type == "profile":
            current_user.name = request.form.get("name", current_user.name)
            db.session.commit()
            flash("Profile updated.", "success")
        elif form_type == "password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            if bcrypt.check_password_hash(current_user.password_hash, current_pw):
                current_user.password_hash = bcrypt.generate_password_hash(new_pw).decode("utf-8")
                db.session.commit()
                flash("Password changed.", "success")
            else:
                flash("Current password is incorrect.", "error")
        elif form_type == "theme":
            current_user.theme = request.form.get("theme", "light")
            db.session.commit()
        return redirect(url_for("settings"))

    return render_template("shared/settings.html")


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("shared/error.html", code=403, message="You don't have access to that page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("shared/error.html", code=404, message="That page doesn't exist."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("shared/error.html", code=500, message="Something went wrong on our end."), 500


if __name__ == "__main__":
    # Local dev only. On Render, gunicorn imports `app` directly (see
    # Procfile) and this block never runs.
    # host="0.0.0.0" makes the dev server reachable from other devices on
    # the same WiFi network (not just this machine) — see the terminal
    # output on startup for the exact URL to open on another device.
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", debug=debug_mode, port=int(os.environ.get("PORT", 5000)))