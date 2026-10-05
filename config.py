import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _sqlite_safe_uri(uri):
    """Normalize a sqlite:/// URI to forward slashes.

    On Windows, SQLAlchemy/sqlite3 fails with "unable to open database file"
    if the path portion contains backslashes. This guards against that no
    matter where the URI came from (a .env DATABASE_URL, an OS env var, or
    our own default), since a user-supplied value always overrides the
    default and could reintroduce the same bug.
    """
    if uri.startswith("sqlite:///"):
        prefix = "sqlite:///"
        path_part = uri[len(prefix):].replace("\\", "/")
        return prefix + path_part
    return uri


def _normalize_postgres_uri(uri):
    """Render (and Heroku-style providers) hand out DATABASE_URL as
    postgres://..., but SQLAlchemy 1.4+/2.x requires the postgresql://
    scheme. Rewrite it so deployment doesn't crash on boot.
    """
    if uri.startswith("postgres://"):
        return "postgresql://" + uri[len("postgres://"):]
    return uri


class Config:
    """Central configuration for the DROP platform."""

    # --- Core Flask ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "drop-dev-secret-change-me")

    _default_db_path = os.path.join(BASE_DIR, "instance", "drop.db").replace("\\", "/")
    _raw_db_uri = os.environ.get("DATABASE_URL", f"sqlite:///{_default_db_path}")
    SQLALCHEMY_DATABASE_URI = _sqlite_safe_uri(_normalize_postgres_uri(_raw_db_uri))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Uploads ---
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    ALLOWED_EXTENSIONS = {"pdf", "docx", "txt", "md"}

    # --- Sessions ---
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    # Render terminates TLS at a proxy in front of the app, so cookies should
    # be marked secure/http-only. IS_PRODUCTION is set via an env var on
    # Render (see render deployment notes) and defaults off for local dev.
    IS_PRODUCTION = os.environ.get("RENDER", "") != "" or os.environ.get("IS_PRODUCTION", "0") == "1"
    SESSION_COOKIE_SECURE = IS_PRODUCTION
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # --- AI configuration (see ROUTING in ai_engine.py) ---
    #   Groq        -> primary model for every AI job
    #   OpenRouter  -> automatic fallback + backup
    # Both speak the OpenAI-compatible Chat Completions protocol. Model IDs change
    # often, so keep them in .env rather than hard-coding them.

    # Groq
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

    # OpenRouter
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free")

    # If a provider's API key is missing (or a call fails), DROP falls back to structured mock
    # content so the whole product still works end-to-end in a demo.
    AI_MOCK_FALLBACK = os.environ.get("AI_MOCK_FALLBACK", "1") == "1"