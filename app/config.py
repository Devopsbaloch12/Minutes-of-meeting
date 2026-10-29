"""Process-level configuration.

Everything a user is expected to change lives in the admin screen, not here.
These values only cover things that must be known before the database exists.
"""
import os

from dotenv import load_dotenv

load_dotenv(encoding="utf-8-sig")

from app.paths import data_dir, logs_dir, uploads_dir  # noqa: E402
from app.security import flask_secret_key  # noqa: E402


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _languages(raw: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in raw.split(","):
        code, _, label = item.strip().partition(":")
        if code.strip():
            pairs.append((code.strip().lower(), label.strip() or code.strip()))
    return tuple(pairs)


class Config:
    DATA_DIR = str(data_dir())
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "").strip() or str(uploads_dir())
    LOG_DIR = str(logs_dir())

    SECRET_KEY = flask_secret_key()
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    WTF_CSRF_TIME_LIMIT = None

    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{data_dir() / 'mom.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "").strip() or "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND = (
        os.environ.get("CELERY_RESULT_BACKEND", "").strip() or CELERY_BROKER_URL
    )

    ALLOWED_EXTENSIONS = {
        "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma",
        "mp4", "mkv", "mov", "webm", "avi",
    }

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    LOG_FORMAT = os.environ.get("LOG_FORMAT", "console").strip().lower()  # console | json
    BEHIND_PROXY = _bool("BEHIND_PROXY")
    # false: the app shows its users meetings and minutes, not machinery. Settings is just
    # the minutes prompt, the System page is gone, and meeting pages and exports leave out
    # the processing log, provider and model names and raw error text. For a team whose
    # providers are set up once by whoever runs the server; stored settings keep working,
    # and setting it back to true is how a key or a model gets changed.
    DEVELOPER_VIEW = _bool("DEVELOPER_VIEW", True)
    APP_VERSION = os.environ.get("APP_VERSION", "3.0.0")
    # Branding: the name in the sidebar, sign-in page and browser tab, and an optional
    # logo given as a path under app/static (e.g. brand/ips-logo.png). No logo by default.
    APP_NAME = os.environ.get("APP_NAME", "").strip() or "MoM AI"
    APP_LOGO = os.environ.get("APP_LOGO", "").strip()
    # Languages offered on the upload page as "code:Label,...", the first preselected,
    # e.g. "en:English,ur:Urdu". Empty: no choice, the Settings language applies.
    MEETING_LANGUAGES = _languages(os.environ.get("MEETING_LANGUAGES", ""))
