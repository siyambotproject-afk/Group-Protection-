"""
config.py — Central configuration loader.
Reads environment variables, validates required values at startup,
and exposes typed constants to the rest of the application.
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Required
# ──────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
FIREBASE_DATABASE_URL: str = os.environ.get("FIREBASE_DATABASE_URL", "")

# Firebase credentials: accept either a raw JSON string or a file path.
_FIREBASE_JSON_RAW: str = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
_FIREBASE_JSON_PATH: str = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "")


def get_firebase_credentials() -> Optional[dict]:
    """Return Firebase service-account credentials as a dict, or None."""
    if _FIREBASE_JSON_RAW:
        try:
            return json.loads(_FIREBASE_JSON_RAW)
        except json.JSONDecodeError as exc:
            logger.error("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: %s", exc)
            return None
    if _FIREBASE_JSON_PATH and os.path.isfile(_FIREBASE_JSON_PATH):
        try:
            with open(_FIREBASE_JSON_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Cannot read Firebase credentials file: %s", exc)
            return None
    return None


# ──────────────────────────────────────────────
# AI Moderation — Sightengine
# ──────────────────────────────────────────────
SIGHTENGINE_API_USER: str = os.environ.get("SIGHTENGINE_API_USER", "")
SIGHTENGINE_API_SECRET: str = os.environ.get("SIGHTENGINE_API_SECRET", "")
SIGHTENGINE_AVAILABLE: bool = bool(SIGHTENGINE_API_USER and SIGHTENGINE_API_SECRET)

# Models requested from Sightengine for images
SIGHTENGINE_IMAGE_MODELS: str = os.environ.get(
    "SIGHTENGINE_IMAGE_MODELS",
    "nudity-2.1,offensive,gore",
)

# Confidence threshold above which content is considered a violation (0.0–1.0)
SIGHTENGINE_NUDITY_THRESHOLD: float = float(
    os.environ.get("SIGHTENGINE_NUDITY_THRESHOLD", "0.80")
)
SIGHTENGINE_OFFENSIVE_THRESHOLD: float = float(
    os.environ.get("SIGHTENGINE_OFFENSIVE_THRESHOLD", "0.85")
)

# Maximum file size (bytes) sent to Sightengine — default 20 MB
MEDIA_MAX_BYTES: int = int(os.environ.get("MEDIA_MAX_BYTES", str(20 * 1024 * 1024)))

# Video: max frames sampled per video
VIDEO_MAX_FRAMES: int = int(os.environ.get("VIDEO_MAX_FRAMES", "5"))

# ──────────────────────────────────────────────
# Media fallback policy (BUG-2 fix)
# ──────────────────────────────────────────────
# What to do when Sightengine is unavailable OR returns an error:
#   "allow"  — let the media through (permissive; use only when AI is unreliable).
#   "block"  — delete the media and issue a warning (strict; DEFAULT).
#   "notify" — delete the media, notify admins, but do NOT issue a user warning.
#
# DEFAULT is "block" (Strict Mode): uninspected media is never silently treated
# as safe. Set MEDIA_FALLBACK_POLICY=allow in .env to opt in to permissive mode.
# This applies per-group via the DB "media_fallback_policy" setting;
# MEDIA_FALLBACK_POLICY is the process-wide default used when no DB value exists.
MEDIA_FALLBACK_POLICY: str = os.environ.get("MEDIA_FALLBACK_POLICY", "block").lower()

# ──────────────────────────────────────────────
# Admin bypass policy (BUG-3 fix)
# ──────────────────────────────────────────────
# True  — admin violations: delete message only, no warning (default).
# False — admin violations: delete + full warning pipeline.
ADMIN_BYPASS_DELETE_ONLY: bool = os.environ.get("ADMIN_BYPASS_DELETE_ONLY", "true").lower() == "true"

# ──────────────────────────────────────────────
# Warning & mute policy
# ──────────────────────────────────────────────
MAX_WARNINGS: int = int(os.environ.get("MAX_WARNINGS", "3"))
DEFAULT_MUTE_MINUTES: int = int(os.environ.get("DEFAULT_MUTE_MINUTES", str(24 * 60)))

# How long (seconds) to keep processed-message IDs to prevent duplicate handling
DEDUP_TTL_SECONDS: int = int(os.environ.get("DEDUP_TTL_SECONDS", "300"))

# ──────────────────────────────────────────────
# Bot Owner & System Config
# ──────────────────────────────────────────────
BOT_OWNER_ID: int = int(os.environ.get("BOT_OWNER_ID", "0"))
ANTI_RAID_THRESHOLD: int = int(os.environ.get("ANTI_RAID_THRESHOLD", "10"))
WARN_EXPIRE_DAYS: int = int(os.environ.get("WARN_EXPIRE_DAYS", "7"))

# ──────────────────────────────────────────────
# Gemini AI Text Moderation
# ──────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_AVAILABLE: bool = bool(GEMINI_API_KEY)

# Gemini model to use for text moderation (flash = fast + free tier)
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# Gemini moderation threshold — 0.0 to 1.0
# Score এর উপরে গেলে BLOCK হবে
GEMINI_BLOCK_THRESHOLD: float = float(
    os.environ.get("GEMINI_BLOCK_THRESHOLD", "0.7")
)

# ──────────────────────────────────────────────
# Groq AI (Fallback #1) — groq.com থেকে বিনামূল্যে
# ──────────────────────────────────────────────
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
GROQ_AVAILABLE: bool = bool(GROQ_API_KEY)
GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

# ──────────────────────────────────────────────
# Together AI (Fallback #2) — api.together.ai
# ──────────────────────────────────────────────
TOGETHER_API_KEY: str = os.environ.get("TOGETHER_API_KEY", "")
TOGETHER_AVAILABLE: bool = bool(TOGETHER_API_KEY)
TOGETHER_MODEL: str = os.environ.get("TOGETHER_MODEL", "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo")

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ──────────────────────────────────────────────
# Render / health-check
# ──────────────────────────────────────────────
PORT: int = int(os.environ.get("PORT", "8000"))


# ──────────────────────────────────────────────
# Startup validation
# ──────────────────────────────────────────────
def validate() -> list[str]:
    """
    Validate configuration at startup.
    Returns a list of fatal error messages (empty = OK).
    Logs warnings for optional missing values.
    """
    errors: list[str] = []

    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set.")
    if not FIREBASE_DATABASE_URL:
        errors.append("FIREBASE_DATABASE_URL is not set.")
    if get_firebase_credentials() is None:
        errors.append(
            "Firebase credentials are missing. "
            "Set FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_PATH."
        )

    if BOT_OWNER_ID == 0:
        logger.warning(
            "BOT_OWNER_ID সেট করা নেই — Owner Panel ও owner কমান্ড কেউ ব্যবহার করতে পারবে না।"
        )

    if not SIGHTENGINE_AVAILABLE:
        logger.warning(
            "Sightengine credentials not set "
            "(SIGHTENGINE_API_USER / SIGHTENGINE_API_SECRET). "
            "Image and video moderation will run in DEGRADED MODE — "
            "media will not be inspected."
        )

    if not GEMINI_AVAILABLE:
        logger.warning(
            "GEMINI_API_KEY সেট করা নেই — "
            "AI text moderation বন্ধ থাকবে। "
            "শুধু regex-based text moderation চলবে।"
        )

    if not (0.0 <= SIGHTENGINE_NUDITY_THRESHOLD <= 1.0):
        errors.append("SIGHTENGINE_NUDITY_THRESHOLD must be between 0.0 and 1.0.")
    if not (0.0 <= SIGHTENGINE_OFFENSIVE_THRESHOLD <= 1.0):
        errors.append("SIGHTENGINE_OFFENSIVE_THRESHOLD must be between 0.0 and 1.0.")
    if MEDIA_FALLBACK_POLICY not in ("allow", "block", "notify"):
        errors.append(
            f"MEDIA_FALLBACK_POLICY must be 'allow', 'block', or 'notify' — got '{MEDIA_FALLBACK_POLICY}'."
        )

    return errors
