"""
database.py — Firebase Realtime Database abstraction layer.

All paths follow the schema:
  /groups/{group_id}/warnings/{user_id}   → { count, muted_until, violations:[…] }
  /groups/{group_id}/settings             → { mute_minutes, … }
  /groups/{group_id}/domains/allow/{hash} → domain
  /groups/{group_id}/domains/block/{hash} → domain
  /processed/{group_id}/{msg_id}          → timestamp   (dedup log)
"""

import hashlib
import logging
import time
from typing import Any, Optional

import firebase_admin
from firebase_admin import credentials, db

import config

logger = logging.getLogger(__name__)

_initialized = False


# ──────────────────────────────────────────────
# Initialisation
# ──────────────────────────────────────────────

def init() -> bool:
    """
    Initialise the Firebase Admin SDK.
    Returns True on success, False on failure.
    Must be called once before any other function in this module.
    """
    global _initialized
    if _initialized:
        return True

    creds_dict = config.get_firebase_credentials()
    if creds_dict is None:
        logger.error("Firebase init failed: no credentials available.")
        return False

    try:
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(
            cred,
            {"databaseURL": config.FIREBASE_DATABASE_URL},
        )
        _initialized = True
        logger.info("Firebase Realtime Database initialised.")
        return True
    except Exception as exc:
        logger.error("Firebase init error: %s", exc)
        return False


def _ref(path: str):
    return db.reference(path)


# ──────────────────────────────────────────────
# Deduplication helpers
# ──────────────────────────────────────────────

def is_already_processed(group_id: int, message_id: int) -> bool:
    """
    Return True if this (group, message) pair was already handled.
    Uses a Firebase node with a TTL-based cleanup approach.
    """
    path = f"/processed/{group_id}/{message_id}"
    try:
        snap = _ref(path).get()
        return snap is not None
    except Exception as exc:
        logger.warning("Dedup check failed for msg %s: %s", message_id, exc)
        # Fail open: do not silently suppress moderation
        return False


def mark_as_processed(group_id: int, message_id: int) -> None:
    """Record that this message has been handled."""
    path = f"/processed/{group_id}/{message_id}"
    try:
        _ref(path).set(int(time.time()))
        _cleanup_old_processed(group_id)
    except Exception as exc:
        logger.warning("Could not mark message as processed: %s", exc)


def _cleanup_old_processed(group_id: int) -> None:
    """Remove dedup records older than DEDUP_TTL_SECONDS."""
    cutoff = int(time.time()) - config.DEDUP_TTL_SECONDS
    try:
        snap = _ref(f"/processed/{group_id}").get() or {}
        stale = [k for k, v in snap.items() if isinstance(v, int) and v < cutoff]
        for key in stale:
            _ref(f"/processed/{group_id}/{key}").delete()
    except Exception as exc:
        logger.debug("Dedup cleanup error: %s", exc)


# ──────────────────────────────────────────────
# Warning records
# ──────────────────────────────────────────────

def get_warnings(group_id: int, user_id: int) -> dict:
    """
    Return the warning record for (group, user).
    Shape: { "count": int, "muted_until": float|0, "violations": [...] }
    Returns a safe default if nothing is stored.
    """
    path = f"/groups/{group_id}/warnings/{user_id}"
    try:
        data = _ref(path).get()
        if data is None:
            return {"count": 0, "muted_until": 0, "violations": []}
        # Ensure required keys
        data.setdefault("count", 0)
        data.setdefault("muted_until", 0)
        data.setdefault("violations", [])
        return data
    except Exception as exc:
        logger.error("get_warnings(%s, %s) failed: %s", group_id, user_id, exc)
        raise


def increment_warning(
    group_id: int,
    user_id: int,
    reason: str,
) -> dict:
    """
    Atomically increment the warning count for (group, user).
    Appends a violation record (timestamp + reason, no media).
    Returns the updated record.
    Raises RuntimeError if the database is unavailable.
    """
    path = f"/groups/{group_id}/warnings/{user_id}"

    def _transaction(current: Any) -> Any:
        if current is None:
            current = {"count": 0, "muted_until": 0, "violations": []}
        current["count"] = current.get("count", 0) + 1
        violations = current.get("violations") or []
        violations.append({"ts": int(time.time()), "reason": reason})
        current["violations"] = violations
        return current

    try:
        result = _ref(path).transaction(_transaction)
        return result or {"count": 1, "muted_until": 0, "violations": []}
    except Exception as exc:
        logger.error("increment_warning failed: %s", exc)
        raise RuntimeError(f"Database error during warning update: {exc}") from exc


def set_muted_until(group_id: int, user_id: int, until_ts: float) -> None:
    """Record the timestamp until which a user is muted."""
    path = f"/groups/{group_id}/warnings/{user_id}/muted_until"
    try:
        _ref(path).set(until_ts)
    except Exception as exc:
        logger.error("set_muted_until failed: %s", exc)
        raise RuntimeError(f"Database error recording mute: {exc}") from exc


def decrement_warning(group_id: int, user_id: int) -> dict:
    """
    Remove one warning from a user (admin /unwarn command).
    Returns the updated record.
    """
    path = f"/groups/{group_id}/warnings/{user_id}"

    def _transaction(current: Any) -> Any:
        if current is None:
            return {"count": 0, "muted_until": 0, "violations": []}
        current["count"] = max(0, current.get("count", 0) - 1)
        return current

    try:
        return _ref(path).transaction(_transaction) or {"count": 0, "muted_until": 0, "violations": []}
    except Exception as exc:
        logger.error("decrement_warning failed: %s", exc)
        raise RuntimeError(f"Database error during unwarn: {exc}") from exc


def reset_warnings(group_id: int, user_id: int) -> None:
    """Reset all warnings for a user (admin /resetwarn command)."""
    path = f"/groups/{group_id}/warnings/{user_id}"
    try:
        _ref(path).set({"count": 0, "muted_until": 0, "violations": []})
    except Exception as exc:
        logger.error("reset_warnings failed: %s", exc)
        raise RuntimeError(f"Database error during reset: {exc}") from exc


# ──────────────────────────────────────────────
# Group settings
# ──────────────────────────────────────────────

def get_settings(group_id: int) -> dict:
    """Return moderation settings for a group, with safe defaults."""
    path = f"/groups/{group_id}/settings"
    defaults = {
        "mute_minutes": config.DEFAULT_MUTE_MINUTES,
        "max_warnings": config.MAX_WARNINGS,
        # 3-way fallback policy for uninspected media.
        # Values: "allow" | "block" (default, Strict Mode) | "notify"
        "media_fallback_policy": config.MEDIA_FALLBACK_POLICY,
        # BUG-3 FIX: configurable admin bypass.
        # True  = delete only (no warning). False = full warning pipeline.
        "admin_bypass_delete_only": config.ADMIN_BYPASS_DELETE_ONLY,
    }
    try:
        data = _ref(path).get() or {}
        return {**defaults, **data}
    except Exception as exc:
        logger.error("get_settings(%s) failed: %s", group_id, exc)
        return defaults



def get_settings_strict(group_id: int) -> dict:
    """Read settings or raise, allowing the moderation pipeline to detect DB failure."""
    path = f"/groups/{group_id}/settings"
    defaults = {
        "mute_minutes": config.DEFAULT_MUTE_MINUTES,
        "max_warnings": config.MAX_WARNINGS,
        "media_fallback_policy": config.MEDIA_FALLBACK_POLICY,
        "admin_bypass_delete_only": config.ADMIN_BYPASS_DELETE_ONLY,
    }
    data = _ref(path).get() or {}
    if not isinstance(data, dict):
        raise RuntimeError("Invalid group settings data")
    return {**defaults, **data}

def update_settings(group_id: int, updates: dict) -> None:
    """Merge updates into group settings."""
    path = f"/groups/{group_id}/settings"
    try:
        _ref(path).update(updates)
    except Exception as exc:
        logger.error("update_settings failed: %s", exc)
        raise RuntimeError(f"Database error updating settings: {exc}") from exc


# ──────────────────────────────────────────────
# Domain lists
# ──────────────────────────────────────────────

def _domain_key(domain: str) -> str:
    """Firebase keys cannot contain '.', so hash the domain."""
    return hashlib.md5(domain.lower().encode()).hexdigest()[:16]


def add_allowed_domain(group_id: int, domain: str) -> None:
    key = _domain_key(domain)
    _ref(f"/groups/{group_id}/domains/allow/{key}").set(domain.lower())


def add_blocked_domain(group_id: int, domain: str) -> None:
    key = _domain_key(domain)
    _ref(f"/groups/{group_id}/domains/block/{key}").set(domain.lower())


def get_allowed_domains(group_id: int) -> list[str]:
    try:
        data = _ref(f"/groups/{group_id}/domains/allow").get() or {}
        return list(data.values())
    except Exception as exc:
        logger.error("get_allowed_domains failed: %s", exc)
        return []


def get_blocked_domains(group_id: int) -> list[str]:
    try:
        data = _ref(f"/groups/{group_id}/domains/block").get() or {}
        return list(data.values())
    except Exception as exc:
        logger.error("get_blocked_domains failed: %s", exc)
        return []


def get_allowed_domains_strict(group_id: int) -> list[str]:
    """Read allowlist or raise so moderation can fail closed on DB errors."""
    data = _ref(f"/groups/{group_id}/domains/allow").get() or {}
    if not isinstance(data, dict):
        raise RuntimeError("Invalid allowed-domain data")
    return [str(value) for value in data.values()]

def get_blocked_domains_strict(group_id: int) -> list[str]:
    """Read blocklist or raise so moderation can fail closed on DB errors."""
    data = _ref(f"/groups/{group_id}/domains/block").get() or {}
    if not isinstance(data, dict):
        raise RuntimeError("Invalid blocked-domain data")
    return [str(value) for value in data.values()]


# ──────────────────────────────────────────────
# Feature toggles
# ──────────────────────────────────────────────

def get_features(group_id: int) -> dict:
    """গ্রুপের feature toggle state ফেরত দেয়।"""
    from features import FEATURE_DEFAULTS
    path = f"/groups/{group_id}/features"
    try:
        data = _ref(path).get() or {}
        return {**FEATURE_DEFAULTS, **data}
    except Exception as exc:
        logger.error("get_features(%s) failed: %s", group_id, exc)
        from features import FEATURE_DEFAULTS
        return dict(FEATURE_DEFAULTS)


def set_feature(group_id: int, feature: str, value: bool) -> None:
    """একটি feature on/off করে।"""
    _ref(f"/groups/{group_id}/features/{feature}").set(value)


def is_feature_on(group_id: int, feature: str) -> bool:
    """একটি feature চালু আছে কিনা দেখে।"""
    features = get_features(group_id)
    return bool(features.get(feature, True))


# ──────────────────────────────────────────────
# Welcome / Goodbye messages
# ──────────────────────────────────────────────

def get_welcome_message(group_id: int) -> str:
    """গ্রুপের custom welcome message ফেরত দেয়।"""
    try:
        msg = _ref(f"/groups/{group_id}/welcome_message").get()
        return msg or "👋 স্বাগতম {name}! আমাদের গ্রুপে আপনাকে স্বাগত জানাই।"
    except Exception:
        return "👋 স্বাগতম {name}! আমাদের গ্রুপে আপনাকে স্বাগত জানাই।"


def set_welcome_message(group_id: int, message: str) -> None:
    _ref(f"/groups/{group_id}/welcome_message").set(message)


def get_goodbye_message(group_id: int) -> str:
    try:
        msg = _ref(f"/groups/{group_id}/goodbye_message").get()
        return msg or "👋 {name} গ্রুপ ছেড়ে চলে গেছেন।"
    except Exception:
        return "👋 {name} গ্রুপ ছেড়ে চলে গেছেন।"


def set_goodbye_message(group_id: int, message: str) -> None:
    _ref(f"/groups/{group_id}/goodbye_message").set(message)


# ──────────────────────────────────────────────
# Rules
# ──────────────────────────────────────────────

def get_rules(group_id: int) -> str:
    try:
        rules = _ref(f"/groups/{group_id}/rules").get()
        return rules or "এই গ্রুপের কোনো নিয়ম এখনো সেট করা হয়নি।"
    except Exception:
        return "এই গ্রুপের কোনো নিয়ম এখনো সেট করা হয়নি।"


def set_rules(group_id: int, rules: str) -> None:
    _ref(f"/groups/{group_id}/rules").set(rules)


# ──────────────────────────────────────────────
# Log channel
# ──────────────────────────────────────────────

def get_log_channel(group_id: int) -> Optional[int]:
    try:
        ch = _ref(f"/groups/{group_id}/log_channel").get()
        return int(ch) if ch else None
    except Exception:
        return None


def set_log_channel(group_id: int, channel_id: int) -> None:
    _ref(f"/groups/{group_id}/log_channel").set(channel_id)


# ──────────────────────────────────────────────
# Mute / Ban
# ──────────────────────────────────────────────

def set_banned(group_id: int, user_id: int, banned: bool) -> None:
    _ref(f"/groups/{group_id}/bans/{user_id}").set(banned)


def is_banned(group_id: int, user_id: int) -> bool:
    try:
        return bool(_ref(f"/groups/{group_id}/bans/{user_id}").get())
    except Exception:
        return False


# ──────────────────────────────────────────────
# Anti-raid tracking
# ──────────────────────────────────────────────

def record_join(group_id: int) -> int:
    """Join event রেকর্ড করে, শেষ ৬০ সেকেন্ডের join count ফেরত দেয়।"""
    now = int(time.time())
    path = f"/groups/{group_id}/joins"
    try:
        joins = _ref(path).get() or []
        if not isinstance(joins, list):
            joins = list(joins.values()) if isinstance(joins, dict) else []
        # শুধু শেষ ৬০ সেকেন্ডের রাখো
        joins = [t for t in joins if now - t < 60]
        joins.append(now)
        _ref(path).set(joins)
        return len(joins)
    except Exception as exc:
        logger.warning("record_join failed: %s", exc)
        return 0


# ──────────────────────────────────────────────
# Owner system
# ──────────────────────────────────────────────

def get_bot_stats() -> dict:
    """Bot-এর সামগ্রিক stats ফেরত দেয়।"""
    try:
        groups = _ref("/groups").get() or {}
        group_count = len(groups)
        total_violations = 0
        for gdata in groups.values():
            if isinstance(gdata, dict):
                warnings = gdata.get("warnings", {})
                for udata in warnings.values():
                    if isinstance(udata, dict):
                        total_violations += len(udata.get("violations", []))
        return {"groups": group_count, "violations": total_violations}
    except Exception as exc:
        logger.error("get_bot_stats failed: %s", exc)
        return {"groups": 0, "violations": 0}


def get_group_list() -> list[dict]:
    """সব গ্রুপের তালিকা ফেরত দেয়।"""
    try:
        groups = _ref("/groups").get() or {}
        result = []
        for gid, gdata in groups.items():
            if isinstance(gdata, dict):
                result.append({"id": gid, "settings": gdata.get("settings", {})})
        return result
    except Exception:
        return []


def get_trusted_list(group_id: int) -> list[int]:
    """Trusted user-দের list ফেরত দেয়।"""
    try:
        data = _ref(f"/groups/{group_id}/trusted").get() or {}
        return [int(uid) for uid, val in data.items() if val]
    except Exception:
        return []


def get_vip_list(group_id: int) -> list[int]:
    """VIP user-দের list ফেরত দেয়।"""
    try:
        data = _ref(f"/groups/{group_id}/vip").get() or {}
        return [int(uid) for uid, val in data.items() if val]
    except Exception:
        return []


def add_global_ban(user_id: int, reason: str = "") -> None:
    """সব গ্রুপে এই user-কে ban করার জন্য global blacklist-এ যোগ করে।"""
    _ref(f"/global_bans/{user_id}").set({"reason": reason, "ts": int(time.time())})


def is_globally_banned(user_id: int) -> bool:
    try:
        return bool(_ref(f"/global_bans/{user_id}").get())
    except Exception:
        return False
