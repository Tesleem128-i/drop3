"""SQLAlchemy models for DROP.

Covers users, classrooms, the AI-generated curriculum (weeks/lessons),
assessments, submissions, messaging, notifications, AI chat history,
solo study sessions, gamification, and adaptive learning profiles.
"""
import json
from datetime import datetime, timezone

from flask_login import UserMixin

from extensions import db


def now():
    return datetime.now(timezone.utc)


# Assessment kinds that run in locked-down exam mode by default, and their default time limits.
SECURE_KINDS = ("weekly_test", "monthly_test", "midterm", "final_exam")
DEFAULT_DURATIONS = {"weekly_test": 30, "monthly_test": 45, "midterm": 60, "final_exam": 90}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'teacher' | 'student'
    avatar_seed = db.Column(db.String(40), default="drop")
    theme = db.Column(db.String(10), default="light")  # 'light' | 'dark'

    # Gamification (students)
    xp = db.Column(db.Integer, default=0)
    level = db.Column(db.Integer, default=1)
    coins = db.Column(db.Integer, default=0)
    streak_days = db.Column(db.Integer, default=0)
    last_active = db.Column(db.DateTime, default=now)

    created_at = db.Column(db.DateTime, default=now)

    classrooms = db.relationship(
        "Classroom", backref="teacher", lazy=True, foreign_keys="Classroom.teacher_id"
    )
    enrollments = db.relationship("Enrollment", backref="student", lazy=True)

    def xp_to_next_level(self):
        return self.level * 500

    def add_xp(self, amount):
        self.xp += amount
        while self.xp >= self.xp_to_next_level():
            self.xp -= self.xp_to_next_level()
            self.level += 1
        db.session.commit()


# ---------------------------------------------------------------------------
# Classrooms
# ---------------------------------------------------------------------------
class Classroom(db.Model):
    __tablename__ = "classrooms"

    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    duration_weeks = db.Column(db.Integer, default=8)
    target_grade = db.Column(db.String(60))
    syllabus_text = db.Column(db.Text)
    join_code = db.Column(db.String(10), unique=True, nullable=False)

    ai_course_json = db.Column(db.Text)  # raw AI course plan (exams etc.)
    ai_status = db.Column(db.String(20), default="pending")  # pending|ready|failed

    created_at = db.Column(db.DateTime, default=now)

    weeks = db.relationship(
        "Week", backref="classroom", lazy=True, cascade="all, delete-orphan",
        order_by="Week.number",
    )
    enrollments = db.relationship(
        "Enrollment", backref="classroom", lazy=True, cascade="all, delete-orphan"
    )
    assignments = db.relationship(
        "Assignment", backref="classroom", lazy=True, cascade="all, delete-orphan"
    )
    messages = db.relationship(
        "Message", backref="classroom", lazy=True, cascade="all, delete-orphan"
    )

    def course(self):
        try:
            return json.loads(self.ai_course_json) if self.ai_course_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def student_count(self):
        return len(self.enrollments)


class Enrollment(db.Model):
    __tablename__ = "enrollments"

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    joined_at = db.Column(db.DateTime, default=now)
    progress_percent = db.Column(db.Float, default=0.0)
    predicted_grade = db.Column(db.String(20))
    risk_level = db.Column(db.String(20), default="low")  # low|medium|high


class Week(db.Model):
    __tablename__ = "weeks"

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200))
    summary = db.Column(db.Text)

    lessons = db.relationship(
        "Lesson", backref="week", lazy=True, cascade="all, delete-orphan",
        order_by="Lesson.order",
    )

    def generated_count(self):
        return sum(1 for l in self.lessons if l.is_generated())

    def is_generated(self):
        return bool(self.lessons) and self.generated_count() == len(self.lessons)


class Lesson(db.Model):
    __tablename__ = "lessons"

    id = db.Column(db.Integer, primary_key=True)
    week_id = db.Column(db.Integer, db.ForeignKey("weeks.id"), nullable=False)
    order = db.Column(db.Integer, default=0)
    title = db.Column(db.String(200), nullable=False)

    objectives = db.Column(db.Text)          # JSON list
    notes = db.Column(db.Text)                # lecture notes (markdown)
    definitions = db.Column(db.Text)          # JSON list
    examples = db.Column(db.Text)             # markdown
    applications = db.Column(db.Text)         # markdown
    common_mistakes = db.Column(db.Text)      # JSON list
    practice = db.Column(db.Text)             # JSON list of practice Qs
    revision = db.Column(db.Text)             # markdown
    summary = db.Column(db.Text)              # markdown
    homework = db.Column(db.Text)             # JSON list
    quiz_json = db.Column(db.Text)            # JSON mini-quiz

    def get_json(self, field):
        raw = getattr(self, field)
        try:
            return json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            return []

    def is_generated(self):
        """A lesson row is created with just a title from the outline; it is 'generated' once notes exist."""
        return bool((self.notes or "").strip())


class LessonProgress(db.Model):
    """Tracks a student's progress through a specific lesson."""
    __tablename__ = "lesson_progress"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False)
    completed = db.Column(db.Boolean, default=False)
    quiz_score = db.Column(db.Float)
    completed_at = db.Column(db.DateTime)

    lesson = db.relationship("Lesson")


# ---------------------------------------------------------------------------
# Assessments
# ---------------------------------------------------------------------------
class Assignment(db.Model):
    __tablename__ = "assignments"

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    kind = db.Column(db.String(20), default="assignment")
    # kind in: classwork | assignment | weekly_test | monthly_test | midterm | final_exam
    questions_json = db.Column(db.Text)  # JSON list of question dicts
    due_date = db.Column(db.DateTime)    # when it closes (UTC)
    created_at = db.Column(db.DateTime, default=now)

    # --- Scheduling & exam security (all times are stored as naive UTC) ---
    start_at = db.Column(db.DateTime)            # when students may open it (NULL = open now)
    duration_minutes = db.Column(db.Integer)     # time limit once a student starts (secure mode)
    secure_mode = db.Column(db.Boolean)          # NULL = decide by kind (tests/exams are secure)
    max_violations = db.Column(db.Integer, default=3)  # strikes before auto-submit

    submissions = db.relationship(
        "Submission", backref="assignment", lazy=True, cascade="all, delete-orphan"
    )

    def questions(self):
        try:
            return json.loads(self.questions_json) if self.questions_json else []
        except (json.JSONDecodeError, TypeError):
            return []

    def has_questions(self):
        """False until the teacher presses Generate (students never see an empty assessment)."""
        return len(self.questions()) > 0

    def is_secure(self):
        """Locked-down exam mode. Tests/exams are secure unless the teacher switched it off."""
        if self.secure_mode is not None:
            return bool(self.secure_mode)
        return self.kind in SECURE_KINDS

    def time_limit(self):
        """Minutes allowed once started (secure assessments only)."""
        return self.duration_minutes or DEFAULT_DURATIONS.get(self.kind, 30)

    def strike_limit(self):
        return self.max_violations or 3


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    answers_json = db.Column(db.Text)
    score = db.Column(db.Float)
    max_score = db.Column(db.Float, default=100)
    feedback = db.Column(db.Text)             # JSON list of per-question feedback
    misconceptions = db.Column(db.Text)       # JSON list e.g. "concept misunderstanding"
    status = db.Column(db.String(20), default="submitted")  # submitted|graded
    submitted_at = db.Column(db.DateTime, default=now)
    graded_at = db.Column(db.DateTime)

    student = db.relationship("User", foreign_keys=[student_id])

    def answers(self):
        try:
            return json.loads(self.answers_json) if self.answers_json else []
        except (json.JSONDecodeError, TypeError):
            return []

    def feedback_list(self):
        try:
            return json.loads(self.feedback) if self.feedback else []
        except (json.JSONDecodeError, TypeError):
            return []


# ---------------------------------------------------------------------------
# Messaging & notifications
# ---------------------------------------------------------------------------
class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    content = db.Column(db.Text, nullable=False)
    is_announcement = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)

    sender = db.relationship("User", foreign_keys=[sender_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.String(300), nullable=False)
    kind = db.Column(db.String(30), default="info")
    # kind: inactive|missing_assignment|performance_drop|performance_up|exam_risk|info
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)


# ---------------------------------------------------------------------------
# AI Tutor / Solo Study
# ---------------------------------------------------------------------------
class AIChatMessage(db.Model):
    __tablename__ = "ai_chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=True)
    role = db.Column(db.String(10), nullable=False)  # 'user' | 'assistant'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now)


class StudySession(db.Model):
    """Solo study: student uploads a topic/PDF and AI builds a mini-course."""
    __tablename__ = "study_sessions"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    topic = db.Column(db.String(200))
    source_filename = db.Column(db.String(300))
    source_text = db.Column(db.Text)
    ai_course_json = db.Column(db.Text)
    final_exam_json = db.Column(db.Text)  # generated on demand when the student clicks "I'm done"
    status = db.Column(db.String(20), default="pending")  # pending|ready|failed|completed
    created_at = db.Column(db.DateTime, default=now)

    def course(self):
        try:
            return json.loads(self.ai_course_json) if self.ai_course_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def final_exam(self):
        try:
            return json.loads(self.final_exam_json) if self.final_exam_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}


# ---------------------------------------------------------------------------
# Gamification & adaptive learning
# ---------------------------------------------------------------------------
class Achievement(db.Model):
    __tablename__ = "achievements"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(300))
    icon = db.Column(db.String(50), default="award")
    earned_at = db.Column(db.DateTime, default=now)


class LearningProfile(db.Model):
    __tablename__ = "learning_profiles"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    weak_topics_json = db.Column(db.Text, default="[]")
    strong_topics_json = db.Column(db.Text, default="[]")
    learning_speed = db.Column(db.String(20), default="average")  # slow|average|fast
    attention_span = db.Column(db.String(20), default="medium")
    preferred_difficulty = db.Column(db.String(20), default="medium")
    confidence = db.Column(db.Integer, default=50)  # 0-100
    updated_at = db.Column(db.DateTime, default=now)

    def weak_topics(self):
        try:
            return json.loads(self.weak_topics_json) if self.weak_topics_json else []
        except (json.JSONDecodeError, TypeError):
            return []

    def strong_topics(self):
        try:
            return json.loads(self.strong_topics_json) if self.strong_topics_json else []
        except (json.JSONDecodeError, TypeError):
            return []

# ---------------------------------------------------------------------------
# Learning analytics (study time, question attempts, AI notes)
#
# These are all NEW tables, so db.create_all() creates them on an existing
# database without any migration. They feed the teacher analytics dashboards
# and each student's own "coach" view.
# ---------------------------------------------------------------------------
class StudyTimeLog(db.Model):
    """Seconds a student actively spent on one lesson (or solo session) on one day.

    One row per (student, lesson/session, day); the tracker adds to `seconds`.
    """
    __tablename__ = "study_time_logs"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=True, index=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=True)
    study_session_id = db.Column(db.Integer, db.ForeignKey("study_sessions.id"), nullable=True)
    day = db.Column(db.Date, nullable=False, index=True)
    seconds = db.Column(db.Integer, default=0)
    sessions = db.Column(db.Integer, default=1)  # how many times it was opened that day
    updated_at = db.Column(db.DateTime, default=now)

    lesson = db.relationship("Lesson")


class QuestionAttempt(db.Model):
    """One answered question. The raw material for every topic/accuracy chart.

    topic    = the specific concept (AI-tagged on assessments, lesson title on quizzes)
    category = the broader grouping (the week/chapter, or the assessment type)
    """
    __tablename__ = "question_attempts"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=True, index=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submissions.id"), nullable=True, index=True)
    study_session_id = db.Column(db.Integer, db.ForeignKey("study_sessions.id"), nullable=True)
    source = db.Column(db.String(20), nullable=False)  # lesson_quiz|assessment|solo_quiz|solo_exam
    topic = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(200))
    is_correct = db.Column(db.Boolean, default=False)
    score = db.Column(db.Float)  # 0-100
    misconception = db.Column(db.String(40))  # none|calculation_error|concept_misunderstanding|...
    created_at = db.Column(db.DateTime, default=now, index=True)


class StudentInsight(db.Model):
    """AI-written strengths/weaknesses/recommendations for a student.

    classroom_id set  -> the teacher's view of this student in that classroom
    classroom_id NULL -> the student's own coaching notes (across everything)
    """
    __tablename__ = "student_insights"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=True)
    data_json = db.Column(db.Text)
    source = db.Column(db.String(10), default="rules")  # ai|rules
    updated_at = db.Column(db.DateTime, default=now)

    def data(self):
        try:
            return json.loads(self.data_json) if self.data_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}


class ClassInsight(db.Model):
    """AI-written class-wide strengths/weaknesses and teaching suggestions."""
    __tablename__ = "class_insights"

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey("classrooms.id"), nullable=False, unique=True)
    data_json = db.Column(db.Text)
    source = db.Column(db.String(10), default="rules")
    updated_at = db.Column(db.DateTime, default=now)

    def data(self):
        try:
            return json.loads(self.data_json) if self.data_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}


# ---------------------------------------------------------------------------
# Secure exam attempts, proctoring log, reminders
#
# All NEW tables, so db.create_all() adds them to an existing database.
# ---------------------------------------------------------------------------
class AssessmentAttempt(db.Model):
    """One student's sitting of a secure test/exam.

    Created when the student presses Start. The server owns the clock: `deadline_at` is fixed at
    that moment, so refreshing the page or closing the tab never resets or pauses the timer.
    Works for class assessments (assignment_id) and for the solo final exam (study_session_id).
    """
    __tablename__ = "assessment_attempts"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=True, index=True)
    study_session_id = db.Column(db.Integer, db.ForeignKey("study_sessions.id"), nullable=True, index=True)

    started_at = db.Column(db.DateTime, default=now)
    deadline_at = db.Column(db.DateTime, nullable=False)
    submitted_at = db.Column(db.DateTime)
    last_seen_at = db.Column(db.DateTime, default=now)
    status = db.Column(db.String(20), default="in_progress", index=True)  # in_progress|submitted
    end_reason = db.Column(db.String(20))   # student | time | violations
    violation_count = db.Column(db.Integer, default=0)
    seed = db.Column(db.Integer, default=0)           # drives per-student question/option shuffling
    draft_json = db.Column(db.Text)                   # autosaved answers {"<question index>": "answer"}

    # Solo exams have no Submission row, so the result lives here.
    score = db.Column(db.Float)
    correct = db.Column(db.Integer)
    total = db.Column(db.Integer)

    student = db.relationship("User", foreign_keys=[student_id])
    assignment = db.relationship("Assignment", foreign_keys=[assignment_id])
    events = db.relationship(
        "ProctorEvent", backref="attempt", lazy=True, cascade="all, delete-orphan",
        order_by="ProctorEvent.created_at",
    )

    def draft(self):
        try:
            d = json.loads(self.draft_json) if self.draft_json else {}
            return d if isinstance(d, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}


class ProctorEvent(db.Model):
    """Something noteworthy that happened during an attempt (leaving fullscreen, tab switch, ...)."""
    __tablename__ = "proctor_events"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("assessment_attempts.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(300))
    counted = db.Column(db.Boolean, default=False)   # True = counted as a strike
    created_at = db.Column(db.DateTime, default=now)


class ReminderSent(db.Model):
    """Remembers which reminders a user already got so each one fires once."""
    __tablename__ = "reminders_sent"
    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_reminder_user_key"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key = db.Column(db.String(160), nullable=False)
    created_at = db.Column(db.DateTime, default=now)