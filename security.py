"""
security.py — Advanced Security (ধাপ ৬)

এই মডিউলে আছে:
  • Anti-clone        — অ্যাডমিনের নাম/username-এর নকল অ্যাকাউন্ট ধরা
  • Short link resolver — bit.ly ইত্যাদির আসল গন্তব্য বের করা (SSRF-সুরক্ষিত)
  • Voice transcribe  — ভয়েস মেসেজ লিখে ফেলা (Groq Whisper), তারপর টেক্সট মডারেশন
  • Slow mode         — প্রতি user-এর মেসেজের মাঝে নির্দিষ্ট বিরতি
  • Report / Poll     — /report ও /poll /quiz এর সহায়ক ফাংশন

এখানকার কোনো ফাংশন moderation pipeline ভাঙবে না — ব্যর্থ হলে None/False ফেরত দেয়।
"""

from __future__ import annotations

import asyncio
import difflib
import ipaddress
import logging
import os
import socket
import time
import unicodedata
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp

import config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# Anti-clone
# ══════════════════════════════════════════════

CLONE_SIMILARITY: float = 0.88   # এর ওপরে মিল থাকলে clone সন্দেহ
CLONE_MIN_LENGTH: int = 4        # এর চেয়ে ছোট নামে তুলনা করা হয় না (false positive এড়াতে)

_LOOKALIKE = str.maketrans({
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
})


def _norm(text: Optional[str]) -> str:
    """নাম তুলনার জন্য: ছোট হাতের, look-alike অক্ষর/সংখ্যা এক করা, চিহ্ন ও ফাঁকা বাদ।"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_LOOKALIKE)
    return "".join(c for c in text if c.isalnum())


def _similar(a: str, b: str) -> bool:
    if len(a) < CLONE_MIN_LENGTH or len(b) < CLONE_MIN_LENGTH:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= CLONE_SIMILARITY


def is_clone_of(user, admin) -> bool:
    """user কি admin-এর নকল? (full name ও username দুটোই মিলিয়ে দেখে)"""
    if user.id == admin.id or admin.is_bot:
        return False
    user_keys = (_norm(user.full_name), _norm(user.username))
    admin_keys = (_norm(admin.full_name), _norm(admin.username))
    return any(_similar(u, a) for u in user_keys for a in admin_keys if u and a)


def find_clone(user, admin_users) -> Optional[object]:
    """admin_users-এর মধ্যে যার সাথে user মিলে যায় তাকে ফেরত দেয়, না হলে None।"""
    for admin in admin_users:
        if is_clone_of(user, admin):
            return admin
    return None


# ══════════════════════════════════════════════
# Short link resolver
# ══════════════════════════════════════════════

RESOLVE_MAX_HOPS: int = 5
RESOLVE_TIMEOUT: int = 6
_RESOLVE_CACHE_TTL: int = 3600
_RESOLVE_CACHE_MAX: int = 500
_resolve_cache: dict[str, tuple[float, str]] = {}


async def _is_public_host(host: str) -> bool:
    """SSRF সুরক্ষা: private/loopback/link-local IP-তে বট request পাঠাবে না।"""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


async def resolve_url(url: str) -> Optional[str]:
    """
    Short URL-এর আসল গন্তব্য ফেরত দেয় (redirect অনুসরণ করে, body ডাউনলোড করে না)।
    যেকোনো সমস্যায় (timeout, বেশি redirect, private IP, অপ্রচলিত port) None — অর্থাৎ যাচাই করা যায়নি।
    """
    raw = url.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    cached = _resolve_cache.get(raw)
    if cached and time.time() - cached[0] < _RESOLVE_CACHE_TTL:
        return cached[1]

    headers = {"User-Agent": "Mozilla/5.0 (compatible; GroupProtectionBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=RESOLVE_TIMEOUT)
    current = raw
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for _ in range(RESOLVE_MAX_HOPS):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return None
                if parsed.port not in (None, 80, 443):
                    return None
                if not await _is_public_host(parsed.hostname):
                    logger.info("Resolver: non-public host skipped: %s", parsed.hostname)
                    return None

                location = None
                status = 0
                for method in ("HEAD", "GET"):  # কিছু shortener HEAD সাপোর্ট করে না
                    try:
                        async with session.request(method, current, allow_redirects=False) as resp:
                            status = resp.status
                            location = resp.headers.get("Location")
                        if status not in (403, 405, 501):
                            break
                    except aiohttp.ClientError:
                        if method == "GET":
                            return None

                if 300 <= status < 400 and location:
                    current = urljoin(current, location)
                    continue

                # redirect নেই — এটাই শেষ গন্তব্য
                if len(_resolve_cache) >= _RESOLVE_CACHE_MAX:
                    _resolve_cache.clear()
                _resolve_cache[raw] = (time.time(), current)
                return current
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
        logger.info("Resolver failed for %s: %s", raw, exc)
        return None
    return None  # অতিরিক্ত redirect


# ══════════════════════════════════════════════
# Voice transcribe (Groq Whisper)
# ══════════════════════════════════════════════

VOICE_MAX_BYTES: int = 5 * 1024 * 1024
VOICE_MAX_SECONDS: int = 120
GROQ_WHISPER_MODEL: str = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
_GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def voice_scan_available() -> bool:
    return config.GROQ_AVAILABLE


async def transcribe_voice(bot, voice) -> Optional[str]:
    """
    ভয়েস মেসেজ → টেক্সট। GROQ_API_KEY না থাকলে, ফাইল বেশি বড়/লম্বা হলে বা ব্যর্থ হলে None।
    (None মানে "যাচাই করা যায়নি" — ভয়েস অমডারেটেড থাকে। কড়া নীতি চাইলে voice_block চালু রাখুন।)
    """
    if not config.GROQ_AVAILABLE:
        return None
    if (voice.file_size and voice.file_size > VOICE_MAX_BYTES) or (
        voice.duration and voice.duration > VOICE_MAX_SECONDS
    ):
        return None
    try:
        tg_file = await bot.get_file(voice.file_id)
        data = bytes(await tg_file.download_as_bytearray())

        form = aiohttp.FormData()
        form.add_field("file", data, filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", GROQ_WHISPER_MODEL)
        form.add_field("response_format", "json")

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}
        ) as session:
            async with session.post(_GROQ_TRANSCRIBE_URL, data=form) as resp:
                if resp.status != 200:
                    logger.warning("Groq transcribe HTTP %s: %s", resp.status, (await resp.text())[:150])
                    return None
                payload = await resp.json()
        text = (payload.get("text") or "").strip()
        return text or None
    except Exception as exc:
        logger.warning("Voice transcribe failed: %s", exc)
        return None


# ══════════════════════════════════════════════
# Slow mode
# ══════════════════════════════════════════════

class SlowMode:
    """In-memory slow mode। restart হলে সবার timer রিসেট হয় (ক্ষতিকর নয়)।"""

    def __init__(self) -> None:
        self._last_ok: dict[tuple[int, int], float] = {}
        self._last_group: dict[tuple[int, int], str] = {}
        self._last_notice: dict[tuple[int, int], float] = {}

    def check(self, group_id: int, user_id: int, seconds: int, media_group_id: Optional[str] = None) -> int:
        """
        0 ফেরত দিলে মেসেজ অনুমোদিত (এবং timer শুরু হয়)।
        >0 ফেরত দিলে এত সেকেন্ড অপেক্ষা করতে হবে।
        একসাথে পাঠানো অ্যালবামের (একই media_group_id) বাকি ছবিগুলো আটকায় না।
        """
        key = (group_id, user_id)
        now = time.time()

        if media_group_id and self._last_group.get(key) == media_group_id:
            return 0

        last = self._last_ok.get(key)
        if last is not None:
            remaining = seconds - (now - last)
            if remaining > 0:
                return max(1, int(remaining + 0.999))

        self._last_ok[key] = now
        if media_group_id:
            self._last_group[key] = media_group_id
        else:
            self._last_group.pop(key, None)
        if len(self._last_ok) > 5000:
            self._prune(now)
        return 0

    def should_notify(self, group_id: int, user_id: int, seconds: int) -> bool:
        """একই user-কে slow mode সময়ের মধ্যে বারবার নোটিশ না দেওয়ার জন্য।"""
        key = (group_id, user_id)
        now = time.time()
        if now - self._last_notice.get(key, 0) < max(seconds, 5):
            return False
        self._last_notice[key] = now
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        for store in (self._last_ok, self._last_notice):
            for key in [k for k, ts in store.items() if ts < cutoff]:
                store.pop(key, None)
                self._last_group.pop(key, None)


slowmode = SlowMode()

SLOWMODE_MAX_SECONDS: int = 3600


# ══════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════

REPORT_COOLDOWN_SECONDS: int = 60
_last_report: dict[tuple[int, int], float] = {}


def report_cooldown_left(group_id: int, user_id: int) -> int:
    """cooldown বাকি থাকলে সেকেন্ড ফেরত দেয়; না থাকলে 0 এবং এখনই cooldown শুরু করে।"""
    key = (group_id, user_id)
    now = time.time()
    left = REPORT_COOLDOWN_SECONDS - (now - _last_report.get(key, 0))
    if left > 0:
        return int(left) + 1
    if len(_last_report) > 5000:
        for k in [k for k, ts in _last_report.items() if now - ts > 3600]:
            _last_report.pop(k, None)
    _last_report[key] = now
    return 0


def message_link(chat, message_id: int) -> Optional[str]:
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


# ══════════════════════════════════════════════
# Poll / Quiz
# ══════════════════════════════════════════════

POLL_MAX_QUESTION = 300
POLL_MAX_OPTION = 100


def parse_poll(text: str, quiz: bool) -> tuple[str, list[str], Optional[int]]:
    """
    'প্রশ্ন | অপশন১ | অপশন২' → (প্রশ্ন, [অপশন], None)
    Quiz-এ সঠিক উত্তরের আগে * দিতে হয়: 'প্রশ্ন | ক | *খ | গ' → correct index = 1
    ভুল হলে ValueError (বার্তা বাংলায়)।
    """
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3:
        raise ValueError("কমপক্ষে একটি প্রশ্ন ও ২টি অপশন দিতে হবে (| দিয়ে আলাদা করুন)।")
    question, options = parts[0], parts[1:]

    correct: Optional[int] = None
    if quiz:
        cleaned = []
        for i, opt in enumerate(options):
            if opt.startswith("*"):
                if correct is not None:
                    raise ValueError("সঠিক উত্তর একটিই (*) চিহ্নিত করুন।")
                correct = i
                opt = opt[1:].strip()
            cleaned.append(opt)
        options = cleaned
        if correct is None:
            raise ValueError("সঠিক উত্তরের আগে * চিহ্ন দিন। যেমন: /quiz প্রশ্ন | ক | *খ | গ")

    if not question or len(question) > POLL_MAX_QUESTION:
        raise ValueError(f"প্রশ্ন ১–{POLL_MAX_QUESTION} অক্ষরের মধ্যে হতে হবে।")
    if not 2 <= len(options) <= 10:
        raise ValueError("অপশন ২ থেকে ১০টির মধ্যে হতে হবে।")
    if any(not o or len(o) > POLL_MAX_OPTION for o in options):
        raise ValueError(f"প্রতিটি অপশন ১–{POLL_MAX_OPTION} অক্ষরের মধ্যে হতে হবে।")
    return question, options, correct
