"""
captcha.py — Captcha & Member Management Module
ধাপ ৪ — এই মডিউলে আছে:
  • Captcha         — নতুন join করলে বাটন চাপতে হবে, না চাপলে kick
  • Trusted user    — admin যাকে trusted করবে সে কিছু restriction থেকে মুক্ত
  • VIP user        — আরো বেশি bypass (spam/flood/arabic সব)
  • User profile    — /profile দিলে warning, violation ইতিহাস দেখা যাবে
  • Inactive kick   — ৩০ দিন কোনো মেসেজ না দিলে kick
  • Mute/Ban list   — /mutelist /banlist command support
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# কনফিগ
# ══════════════════════════════════════════════

CAPTCHA_TIMEOUT_SECONDS: int = 120      # ২ মিনিটে বাটন না চাপলে kick
INACTIVE_DAYS: int = 30                 # ৩০ দিন inactive হলে kick


# ══════════════════════════════════════════════
# In-memory captcha pending state
# {(group_id, user_id): {"name": str, "ts": float, "msg_id": int}}
# ══════════════════════════════════════════════

_pending_captcha: dict[tuple[int, int], dict] = {}


# ══════════════════════════════════════════════
# Captcha Functions
# ══════════════════════════════════════════════

def add_pending_captcha(group_id: int, user_id: int, user_name: str, msg_id: int) -> None:
    """নতুন join করা user-কে captcha pending list-এ রাখো।"""
    _pending_captcha[(group_id, user_id)] = {
        "name": user_name,
        "ts": time.time(),
        "msg_id": msg_id,
    }


def verify_captcha(group_id: int, user_id: int) -> bool:
    """
    User বাটন চাপলে captcha verify করো।
    Returns True যদি pending ছিল, False যদি ছিল না।
    """
    key = (group_id, user_id)
    if key in _pending_captcha:
        del _pending_captcha[key]
        return True
    return False


def get_expired_captchas(timeout: int = CAPTCHA_TIMEOUT_SECONDS) -> list[tuple[int, int, dict]]:
    """
    Timeout পার হয়ে গেছে এমন captcha pending user-দের list।
    Returns: [(group_id, user_id, data), ...]
    """
    now = time.time()
    expired = []
    for (gid, uid), data in list(_pending_captcha.items()):
        if now - data["ts"] >= timeout:
            expired.append((gid, uid, data))
    return expired


def remove_captcha(group_id: int, user_id: int) -> None:
    """Captcha entry সরিয়ে দাও (kick করার পর)।"""
    _pending_captcha.pop((group_id, user_id), None)


def is_captcha_pending(group_id: int, user_id: int) -> bool:
    """এই user-এর captcha pending আছে কিনা।"""
    return (group_id, user_id) in _pending_captcha


# ══════════════════════════════════════════════
# Trusted / VIP User Functions (database-backed)
# ══════════════════════════════════════════════

def set_trusted_user(database_module, group_id: int, user_id: int, trusted: bool) -> None:
    """User-কে trusted করো বা সরাও।"""
    database_module._ref(f"/groups/{group_id}/trusted/{user_id}").set(trusted if trusted else None)


def is_trusted_user(database_module, group_id: int, user_id: int) -> bool:
    """User trusted কিনা দেখো।"""
    try:
        val = database_module._ref(f"/groups/{group_id}/trusted/{user_id}").get()
        return bool(val)
    except Exception:
        return False


def set_vip_user(database_module, group_id: int, user_id: int, vip: bool) -> None:
    """User-কে VIP করো বা সরাও।"""
    database_module._ref(f"/groups/{group_id}/vip/{user_id}").set(vip if vip else None)


def is_vip_user(database_module, group_id: int, user_id: int) -> bool:
    """User VIP কিনা দেখো।"""
    try:
        val = database_module._ref(f"/groups/{group_id}/vip/{user_id}").get()
        return bool(val)
    except Exception:
        return False


def get_user_role(database_module, group_id: int, user_id: int) -> str:
    """
    User-এর role ফেরত দেয়।
    Returns: "vip" | "trusted" | "member"
    """
    if is_vip_user(database_module, group_id, user_id):
        return "vip"
    if is_trusted_user(database_module, group_id, user_id):
        return "trusted"
    return "member"


# ══════════════════════════════════════════════
# User Activity Tracking
# ══════════════════════════════════════════════

def record_user_activity(database_module, group_id: int, user_id: int) -> None:
    """User-এর শেষ activity timestamp রেকর্ড করো।"""
    try:
        database_module._ref(
            f"/groups/{group_id}/activity/{user_id}"
        ).set(int(time.time()))
    except Exception as exc:
        logger.debug("record_activity failed: %s", exc)


def get_inactive_users(database_module, group_id: int, days: int = INACTIVE_DAYS) -> list[int]:
    """
    ৩০ দিন বা তার বেশি দিন inactive user-দের list।
    Returns: [user_id, ...]
    """
    cutoff = time.time() - (days * 24 * 3600)
    try:
        activity = database_module._ref(f"/groups/{group_id}/activity").get() or {}
        inactive = []
        for uid_str, last_ts in activity.items():
            if isinstance(last_ts, (int, float)) and last_ts < cutoff:
                try:
                    inactive.append(int(uid_str))
                except ValueError:
                    pass
        return inactive
    except Exception as exc:
        logger.error("get_inactive_users failed: %s", exc)
        return []


def remove_user_activity(database_module, group_id: int, user_id: int) -> None:
    """Kick করার পর activity record মুছে দাও।"""
    try:
        database_module._ref(f"/groups/{group_id}/activity/{user_id}").delete()
    except Exception:
        pass


# ══════════════════════════════════════════════
# User Profile Builder
# ══════════════════════════════════════════════

def build_user_profile(
    database_module,
    group_id: int,
    user_id: int,
    user_name: str,
    is_admin: bool,
) -> str:
    """
    /profile কমান্ডের জন্য user-এর সম্পূর্ণ প্রোফাইল তৈরি করো।
    """
    # Warning data
    warn_data = database_module.get_warnings(group_id, user_id)
    warn_count = warn_data.get("count", 0)
    violations = warn_data.get("violations", []) or []

    # Role
    role = get_user_role(database_module, group_id, user_id)
    role_labels = {
        "vip": "⭐ VIP",
        "trusted": "✅ Trusted",
        "member": "👤 সাধারণ সদস্য",
    }
    role_label = role_labels.get(role, "👤 সদস্য")
    if is_admin:
        role_label = "🛡️ অ্যাডমিন"

    # Last activity
    try:
        last_ts = database_module._ref(
            f"/groups/{group_id}/activity/{user_id}"
        ).get()
        if last_ts:
            from datetime import datetime
            last_active = datetime.fromtimestamp(int(last_ts)).strftime("%d/%m/%Y %H:%M")
        else:
            last_active = "অজানা"
    except Exception:
        last_active = "অজানা"

    # Ban status
    is_banned = database_module.is_banned(group_id, user_id)
    ban_status = "🚫 Banned" if is_banned else "✅ সক্রিয়"

    # Violation history (শেষ ৫টি)
    recent_violations = violations[-5:] if violations else []
    violation_lines = []
    for v in reversed(recent_violations):
        if isinstance(v, dict):
            ts = v.get("ts", 0)
            reason = v.get("reason", "অজানা")
            from datetime import datetime
            date_str = datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M") if ts else "?"
            violation_lines.append(f"  • [{date_str}] {reason}")

    violation_text = "\n".join(violation_lines) if violation_lines else "  কোনো violation নেই ✅"

    # Mute status
    muted_until = warn_data.get("muted_until", 0)
    now = time.time()
    if muted_until and muted_until > now:
        from datetime import datetime
        mute_end = datetime.fromtimestamp(int(muted_until)).strftime("%d/%m/%Y %H:%M")
        mute_status = f"🔇 Muted (পর্যন্ত: {mute_end})"
    else:
        mute_status = "🔊 Unmuted"

    profile_text = (
        f"👤 <b>User Profile</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷️ নাম: <b>{user_name}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"🎭 ভূমিকা: {role_label}\n"
        f"📊 স্ট্যাটাস: {ban_status}\n"
        f"🔊 Mute: {mute_status}\n"
        f"⚠️ Warning: <b>{warn_count}</b>\n"
        f"🕐 শেষ সক্রিয়: {last_active}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 সাম্প্রতিক Violation (শেষ ৫টি):\n"
        f"{violation_text}"
    )
    return profile_text


# ══════════════════════════════════════════════
# Mute / Ban List Builder
# ══════════════════════════════════════════════

def get_muted_users(database_module, group_id: int) -> list[dict]:
    """
    বর্তমানে muted আছে এমন user-দের list।
    Returns: [{"user_id": int, "until": float}, ...]
    """
    now = time.time()
    try:
        warnings_data = database_module._ref(
            f"/groups/{group_id}/warnings"
        ).get() or {}
        muted = []
        for uid_str, udata in warnings_data.items():
            if not isinstance(udata, dict):
                continue
            muted_until = udata.get("muted_until", 0)
            if muted_until and muted_until > now:
                try:
                    muted.append({
                        "user_id": int(uid_str),
                        "until": muted_until,
                    })
                except ValueError:
                    pass
        return muted
    except Exception as exc:
        logger.error("get_muted_users failed: %s", exc)
        return []


def get_banned_users(database_module, group_id: int) -> list[int]:
    """
    Ban করা user-দের list।
    Returns: [user_id, ...]
    """
    try:
        bans = database_module._ref(f"/groups/{group_id}/bans").get() or {}
        return [int(uid) for uid, banned in bans.items() if banned]
    except Exception as exc:
        logger.error("get_banned_users failed: %s", exc)
        return []
