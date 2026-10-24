"""
spam_filter.py — Spam & Flood Control Module
ধাপ ৩ — এই মডিউলে আছে:
  • Flood control      — ৫ সেকেন্ডে ৫+ মেসেজ দিলে auto mute
  • Spam detect        — একই মেসেজ বারবার দিলে delete
  • Arabic/Hindi block — Arabic বা Devanagari script spam
  • Behavior pattern   — বারবার সন্দেহজনক হলে auto warn
  • Warning expiry     — ৭ দিন পর warning এক step কমে
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# কনফিগ
# ══════════════════════════════════════════════

FLOOD_WINDOW_SECONDS: int = 5       # কত সেকেন্ডের মধ্যে
FLOOD_MAX_MESSAGES: int = 5         # কতটি মেসেজ হলে flood ধরবে
FLOOD_MUTE_MINUTES: int = 10        # flood হলে কত মিনিট mute

SPAM_REPEAT_THRESHOLD: int = 3      # একই মেসেজ কতবার দিলে spam
SPAM_REPEAT_WINDOW: int = 60        # কত সেকেন্ডের মধ্যে

PATTERN_VIOLATION_LIMIT: int = 5    # কতবার সন্দেহজনক হলে auto-warn
PATTERN_WINDOW_SECONDS: int = 300   # ৫ মিনিট

WARN_EXPIRE_DAYS: int = 7           # কত দিন পর warning কমবে


# ══════════════════════════════════════════════
# ফলাফল
# ══════════════════════════════════════════════

@dataclass
class SpamResult:
    is_violation: bool = False
    reason: str = ""
    should_mute: bool = False
    mute_minutes: int = 0
    should_delete: bool = False


ALLOW = SpamResult(is_violation=False)


# ══════════════════════════════════════════════
# In-memory state (per bot session)
# ══════════════════════════════════════════════

# flood tracker: {(group_id, user_id): deque of timestamps}
_flood_tracker: dict[tuple[int, int], deque] = defaultdict(lambda: deque())

# spam repeat tracker: {(group_id, user_id): {text_hash: [timestamps]}}
_repeat_tracker: dict[tuple[int, int], dict[str, list[float]]] = defaultdict(
    lambda: defaultdict(list)
)

# behavior pattern tracker: {(group_id, user_id): deque of violation timestamps}
_pattern_tracker: dict[tuple[int, int], deque] = defaultdict(lambda: deque())


# ══════════════════════════════════════════════
# ১. Flood Control
# ══════════════════════════════════════════════

def check_flood(group_id: int, user_id: int) -> SpamResult:
    """
    ৫ সেকেন্ডে ৫+ মেসেজ = flood।
    True হলে auto mute করতে হবে।
    """
    key = (group_id, user_id)
    now = time.time()
    window = _flood_tracker[key]

    # পুরনো timestamp সরাও
    while window and now - window[0] > FLOOD_WINDOW_SECONDS:
        window.popleft()

    window.append(now)

    if len(window) >= FLOOD_MAX_MESSAGES:
        # window clear করো যাতে পরের message এ আবার ধরা না দেয়
        window.clear()
        return SpamResult(
            is_violation=True,
            reason=f"⚡ ফ্লাড সনাক্ত! {FLOOD_WINDOW_SECONDS} সেকেন্ডে অনেক বেশি মেসেজ পাঠানো হয়েছে।",
            should_mute=True,
            mute_minutes=FLOOD_MUTE_MINUTES,
            should_delete=True,
        )

    return ALLOW


# ══════════════════════════════════════════════
# ২. Spam Repeat Detection
# ══════════════════════════════════════════════

def _normalize_text(text: str) -> str:
    """টেক্সট normalize করে comparison-এর জন্য।"""
    text = text.lower().strip()
    # extra whitespace সরাও
    text = re.sub(r"\s+", " ", text)
    return text


def check_repeat_spam(group_id: int, user_id: int, text: str) -> SpamResult:
    """
    একই বা খুব কাছাকাছি মেসেজ বারবার পাঠালে spam।
    """
    if not text or len(text) < 5:
        return ALLOW

    normalized = _normalize_text(text)
    key = (group_id, user_id)
    now = time.time()

    # এই user-এর repeat data
    user_repeats = _repeat_tracker[key]

    # পুরনো entries পরিষ্কার করো
    for h in list(user_repeats.keys()):
        user_repeats[h] = [t for t in user_repeats[h] if now - t < SPAM_REPEAT_WINDOW]
        if not user_repeats[h]:
            del user_repeats[h]

    user_repeats[normalized].append(now)
    count = len(user_repeats[normalized])

    if count >= SPAM_REPEAT_THRESHOLD:
        # tracker clear করো
        user_repeats.pop(normalized, None)
        return SpamResult(
            is_violation=True,
            reason=f"🔁 একই মেসেজ {count} বার পাঠানো হয়েছে — স্প্যাম সনাক্ত।",
            should_delete=True,
        )

    return ALLOW


# ══════════════════════════════════════════════
# ৩. Arabic / Hindi Spam Block
# ══════════════════════════════════════════════

# Arabic script range: U+0600–U+06FF, U+0750–U+077F, U+08A0–U+08FF
_ARABIC_PATTERN = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]{5,}"
)

# Devanagari (Hindi) script range: U+0900–U+097F
_DEVANAGARI_PATTERN = re.compile(r"[\u0900-\u097F]{5,}")


def check_arabic_hindi_spam(text: str) -> SpamResult:
    """
    Arabic বা Hindi script-এ ৫+ character এর block থাকলে block করো।
    features-এ arabic_block চালু থাকতে হবে।
    """
    if not text:
        return ALLOW

    if _ARABIC_PATTERN.search(text):
        return SpamResult(
            is_violation=True,
            reason="🌐 Arabic script মেসেজ এই গ্রুপে অনুমোদিত নয়।",
            should_delete=True,
        )

    if _DEVANAGARI_PATTERN.search(text):
        return SpamResult(
            is_violation=True,
            reason="🌐 Hindi (Devanagari) script মেসেজ এই গ্রুপে অনুমোদিত নয়।",
            should_delete=True,
        )

    return ALLOW


# ══════════════════════════════════════════════
# ৪. User Behavior Pattern Tracking
# ══════════════════════════════════════════════

def record_suspicious_behavior(group_id: int, user_id: int) -> bool:
    """
    সন্দেহজনক আচরণ রেকর্ড করো।
    PATTERN_VIOLATION_LIMIT বার হলে True ফেরত দেয় (auto-warn দরকার)।
    """
    key = (group_id, user_id)
    now = time.time()
    window = _pattern_tracker[key]

    # পুরনো sরাও
    while window and now - window[0] > PATTERN_WINDOW_SECONDS:
        window.popleft()

    window.append(now)

    if len(window) >= PATTERN_VIOLATION_LIMIT:
        window.clear()
        return True  # auto-warn দরকার

    return False


def get_suspicious_count(group_id: int, user_id: int) -> int:
    """এই user এর চলমান সন্দেহজনক আচরণের সংখ্যা।"""
    key = (group_id, user_id)
    now = time.time()
    window = _pattern_tracker.get(key, deque())
    return sum(1 for t in window if now - t <= PATTERN_WINDOW_SECONDS)


# ══════════════════════════════════════════════
# ৫. Warning Auto Expire (৭ দিন পর কমবে)
# ══════════════════════════════════════════════

def get_expired_warnings(group_id: int, user_data: dict) -> int:
    """
    ৭ দিনের বেশি পুরনো violations count করো — এগুলো expire হয়েছে।
    user_data = database.get_warnings() এর ফলাফল।
    Returns: কতটি warning expire হয়েছে।
    """
    now = time.time()
    cutoff = now - (WARN_EXPIRE_DAYS * 24 * 3600)

    violations = user_data.get("violations", [])
    if not violations:
        return 0

    expired_count = sum(
        1 for v in violations
        if isinstance(v, dict) and v.get("ts", now) < cutoff
    )
    return expired_count


def apply_warning_expiry(group_id: int, user_id: int, database_module) -> int:
    """
    ৭ দিনের পুরনো warning সরিয়ে updated count ফেরত দেয়।
    database_module = database (imported in bot.py)
    Returns: নতুন warning count।
    """
    try:
        user_data = database_module.get_warnings(group_id, user_id)
        expired = get_expired_warnings(group_id, user_data)

        if expired > 0:
            now = time.time()
            cutoff = now - (WARN_EXPIRE_DAYS * 24 * 3600)
            violations = user_data.get("violations", [])

            # expire হওয়া violations বাদ দাও
            fresh_violations = [
                v for v in violations
                if isinstance(v, dict) and v.get("ts", now) >= cutoff
            ]
            new_count = max(0, len(fresh_violations))

            # database আপডেট করো
            path = f"/groups/{group_id}/warnings/{user_id}"
            database_module._ref(path).set({
                "count": new_count,
                "muted_until": user_data.get("muted_until", 0),
                "violations": fresh_violations,
            })

            logger.info(
                "Warning expiry: group=%s user=%s expired=%s new_count=%s",
                group_id, user_id, expired, new_count,
            )
            return new_count

        return user_data.get("count", 0)

    except Exception as exc:
        logger.error("apply_warning_expiry failed: %s", exc)
        return 0


# ══════════════════════════════════════════════
# Master check function
# ══════════════════════════════════════════════

def check_spam_and_flood(
    group_id: int,
    user_id: int,
    text: str,
    features: dict,
) -> SpamResult:
    """
    সব spam/flood check একসাথে চালায়।
    features dict থেকে কোনগুলো চালু আছে দেখে।

    Returns SpamResult (is_violation=False মানে সব ঠিক আছে)।
    """

    # ১. Flood check (spam_filter feature চালু থাকলে)
    if features.get("spam_filter", True):
        result = check_flood(group_id, user_id)
        if result.is_violation:
            return result

    # ২. Repeat spam check
    if features.get("spam_filter", True) and text:
        result = check_repeat_spam(group_id, user_id, text)
        if result.is_violation:
            return result

    # ৩. Arabic/Hindi block
    if features.get("arabic_block", False) and text:
        result = check_arabic_hindi_spam(text)
        if result.is_violation:
            return result

    return ALLOW
