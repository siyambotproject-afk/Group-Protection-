"""
analytics.py — Analytics & Reports (ধাপ ৫)

এই মডিউলে আছে:
  • Violation tracking   — কোন ধরনের violation কতটি (category অনুযায়ী)
  • Group growth         — কতজন join / left
  • Report builder       — daily / weekly / custom দিনের রিপোর্ট + টেক্সট চার্ট
  • Export               — CSV, এবং Google Sheet (Sheets REST API, নতুন কোনো প্যাকেজ লাগে না)

ডেটা Firebase-এ থাকে:
  /analytics/{group_id}/violations/{push_id} = {ts, uid, cat, reason}
  /analytics/{group_id}/growth/{YYYY-MM-DD}  = {joined, left}

নোট: এখানকার কোনো ফাংশন exception ছুঁড়ে moderation pipeline ভাঙবে না —
record_* ফাংশনগুলো সব error নিজেই log করে চুপ থাকে।
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

import config
import database

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# কনফিগ
# ══════════════════════════════════════════════

RETENTION_DAYS: int = 90   # এর চেয়ে পুরনো analytics ডেটা মুছে ফেলা হবে
GOOGLE_SHEET_ID: str = os.environ.get("GOOGLE_SHEET_ID", "").strip()
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _load_timezone():
    """Default Asia/Dhaka (UTC+6). BOT_TIMEZONE env দিয়ে বদলানো যায়।"""
    name = os.environ.get("BOT_TIMEZONE", "Asia/Dhaka")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:
        logger.warning("Timezone '%s' লোড হয়নি (%s) — UTC+6 ব্যবহার হচ্ছে।", name, exc)
        return timezone(timedelta(hours=6))


TZ = _load_timezone()


# ══════════════════════════════════════════════
# সময় সংক্রান্ত helper
# ══════════════════════════════════════════════

def now_local() -> datetime:
    return datetime.now(TZ)


def day_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts, TZ) if ts is not None else now_local()
    return dt.strftime("%Y-%m-%d")


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def window_last_days(days: int) -> tuple[float, float]:
    """আজকের দিনসহ শেষ `days` দিন (এখন পর্যন্ত)।"""
    start = _midnight(now_local() - timedelta(days=days - 1))
    return start.timestamp(), time.time()


def window_previous_days(days: int) -> tuple[float, float]:
    """আজকের আগের পুরো `days` দিন (আজ বাদ)। Daily/weekly রিপোর্টে ব্যবহার হয়।"""
    today0 = _midnight(now_local())
    return (today0 - timedelta(days=days)).timestamp(), today0.timestamp()


# ══════════════════════════════════════════════
# Violation category
# ══════════════════════════════════════════════

CAT_FLOOD = "⚡ ফ্লাড"
CAT_REPEAT = "🔁 রিপিট স্প্যাম"
CAT_SCRIPT = "🌐 Arabic/Hindi"
CAT_PHONE = "📵 ফোন নম্বর"
CAT_FORWARD = "↩️ ফরওয়ার্ড"
CAT_VOICE = "🎙️ ভয়েস"
CAT_STICKER = "😀 স্টিকার"
CAT_LINK = "🔗 লিংক"
CAT_SCAM = "🚨 স্ক্যাম"
CAT_MEDIA = "🖼️ মিডিয়া"
CAT_TEXT = "🤖 টেক্সট"
CAT_OTHER = "📌 অন্যান্য"

# (keyword, category) — ওপর থেকে নিচে প্রথম মিললেই হবে
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("ফ্লাড",), CAT_FLOOD),
    (("একই মেসেজ",), CAT_REPEAT),
    (("Arabic", "Hindi", "Devanagari"), CAT_SCRIPT),
    (("ফোন",), CAT_PHONE),
    (("ফরওয়ার্ড",), CAT_FORWARD),
    (("ভয়েস",), CAT_VOICE),
    (("স্টিকার",), CAT_STICKER),
    (("স্ক্যাম", "প্রতারণা"), CAT_SCAM),
    (("লিংক", "URL", "ডোমেইন"), CAT_LINK),
    (("ছবি", "ভিডিও", "GIF", "Sightengine", "মিডিয়া", "দৃশ্য"), CAT_MEDIA),
    (("টেক্সট", "AI মডারেশন"), CAT_TEXT),
]


def categorize(reason: str) -> str:
    text = reason or ""
    for keywords, category in _CATEGORY_RULES:
        if any(k in text for k in keywords):
            return category
    return CAT_OTHER


# ══════════════════════════════════════════════
# Recording (কখনো exception ছুঁড়ে না)
# ══════════════════════════════════════════════

def record_violation(group_id: int, user_id: int, reason: str) -> None:
    try:
        database._ref(f"/analytics/{group_id}/violations").push({
            "ts": int(time.time()),
            "uid": int(user_id),
            "cat": categorize(reason),
            "reason": (reason or "")[:200],
        })
    except Exception as exc:
        logger.debug("record_violation failed: %s", exc)


def _bump_growth(group_id: int, field: str) -> None:
    try:
        database._ref(f"/analytics/{group_id}/growth/{day_key()}/{field}").transaction(
            lambda current: (current or 0) + 1
        )
    except Exception as exc:
        logger.debug("growth %s bump failed: %s", field, exc)


def record_join(group_id: int) -> None:
    _bump_growth(group_id, "joined")


def record_left(group_id: int) -> None:
    _bump_growth(group_id, "left")


# ══════════════════════════════════════════════
# Reading
# ══════════════════════════════════════════════

def get_violations(group_id: int, start_ts: float, end_ts: float) -> list[dict]:
    try:
        data = database._ref(f"/analytics/{group_id}/violations").get() or {}
    except Exception as exc:
        logger.error("get_violations(%s) failed: %s", group_id, exc)
        return []
    items = data.values() if isinstance(data, dict) else data
    rows = [
        v for v in items
        if isinstance(v, dict) and start_ts <= v.get("ts", 0) < end_ts
    ]
    rows.sort(key=lambda v: v.get("ts", 0))
    return rows


def get_growth(group_id: int, start_ts: float, end_ts: float) -> tuple[int, int]:
    """(joined, left) ফেরত দেয়।"""
    try:
        data = database._ref(f"/analytics/{group_id}/growth").get() or {}
    except Exception as exc:
        logger.error("get_growth(%s) failed: %s", group_id, exc)
        return 0, 0
    if not isinstance(data, dict):
        return 0, 0
    first, last = day_key(start_ts), day_key(end_ts - 1)
    joined = left = 0
    for key, val in data.items():
        if first <= key <= last and isinstance(val, dict):
            joined += int(val.get("joined", 0) or 0)
            left += int(val.get("left", 0) or 0)
    return joined, left


# ══════════════════════════════════════════════
# Report builder
# ══════════════════════════════════════════════

def _bar(count: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0 or count <= 0:
        return "░" * width
    filled = max(1, round(count / maximum * width))
    return "█" * filled + "░" * (width - filled)


def build_report(
    group_id: int,
    group_title: str,
    start_ts: float,
    end_ts: float,
    title: str,
) -> tuple[str, int]:
    """
    HTML রিপোর্ট টেক্সট এবং মোট ঘটনার সংখ্যা (violation + join + left) ফেরত দেয়।
    মোট ঘটনা 0 হলে scheduler রিপোর্ট পাঠায় না।
    """
    violations = get_violations(group_id, start_ts, end_ts)
    joined, left = get_growth(group_id, start_ts, end_ts)

    start_s = datetime.fromtimestamp(start_ts, TZ).strftime("%d %b")
    end_s = datetime.fromtimestamp(end_ts - 1, TZ).strftime("%d %b")
    period = start_s if start_s == end_s else f"{start_s} – {end_s}"

    net = joined - left
    net_s = f"+{net}" if net > 0 else str(net)

    lines = [
        f"📊 <b>{html.escape(title)}</b>",
        f"🏘️ {html.escape(group_title or str(group_id))}",
        f"🗓️ {period}",
        "",
        "👥 <b>গ্রুপ গ্রোথ</b>",
        f"➕ Join: <b>{joined}</b>   ➖ Left: <b>{left}</b>   📈 নেট: <b>{net_s}</b>",
        "",
        f"⚠️ <b>Violation: {len(violations)}</b>",
    ]

    if violations:
        by_cat = Counter(v.get("cat", CAT_OTHER) for v in violations)
        top = by_cat.most_common()
        maximum = top[0][1]
        lines.append("<pre>")
        for cat, count in top:
            lines.append(f"{_bar(count, maximum)} {count:>3}  {html.escape(cat)}")
        lines.append("</pre>")
        lines.append(f"🏆 সবচেয়ে বেশি: {html.escape(top[0][0])} ({top[0][1]})")

        by_user = Counter(v.get("uid") for v in violations if v.get("uid"))
        if by_user:
            lines.append("")
            lines.append("👤 <b>সবচেয়ে বেশি violation</b>")
            for uid, count in by_user.most_common(3):
                lines.append(f"• <a href=\"tg://user?id={uid}\">{uid}</a> — {count}টি")
    else:
        lines.append("✅ কোনো violation নেই।")

    return "\n".join(lines), len(violations) + joined + left


# ══════════════════════════════════════════════
# Export
# ══════════════════════════════════════════════

_HEADER = ["Time", "User ID", "Category", "Reason"]


def _rows_for_export(violations: list[dict]) -> list[list]:
    return [
        [
            datetime.fromtimestamp(v.get("ts", 0), TZ).strftime("%Y-%m-%d %H:%M:%S"),
            v.get("uid", ""),
            v.get("cat", ""),
            v.get("reason", ""),
        ]
        for v in violations
    ]


def violations_csv(violations: list[dict]) -> bytes:
    """Excel-এ বাংলা ঠিকমতো দেখাতে BOM সহ UTF-8।"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADER)
    writer.writerows(_rows_for_export(violations))
    return buf.getvalue().encode("utf-8-sig")


def sheets_configured() -> bool:
    return bool(GOOGLE_SHEET_ID and config.get_firebase_credentials())


def _get_access_token(info: dict) -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds = service_account.Credentials.from_service_account_info(info, scopes=[_SHEETS_SCOPE])
    creds.refresh(Request())
    return creds.token


async def export_to_google_sheet(group_id: int, violations: list[dict]) -> tuple[bool, str]:
    """
    Violation ডেটা Google Sheet-এ লেখে। প্রতিটা গ্রুপের জন্য আলাদা ট্যাব: V_<group_id>।
    প্রতিবার ট্যাবটি পরিষ্কার করে নতুন করে লেখা হয় (ডুপ্লিকেট হয় না)।

    শর্ত:
      • GOOGLE_SHEET_ID env সেট থাকতে হবে
      • Firebase service account-এর client_email-কে Sheet-এ Editor হিসেবে share করতে হবে
      • ঐ Google Cloud প্রজেক্টে Google Sheets API enable থাকতে হবে

    Returns (success, message_or_url).
    """
    if not GOOGLE_SHEET_ID:
        return False, "GOOGLE_SHEET_ID সেট করা নেই।"
    info = config.get_firebase_credentials()
    if not info:
        return False, "Service account credentials পাওয়া যায়নি।"

    try:
        token = await asyncio.to_thread(_get_access_token, info)
    except Exception as exc:
        return False, f"Google auth ব্যর্থ: {exc}"

    base = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"
    tab = f"V_{group_id}"
    tab_q = urllib.parse.quote(tab, safe="")
    values = [_HEADER] + _rows_for_export(violations)

    async def _fail(resp: aiohttp.ClientResponse) -> tuple[bool, str]:
        body = (await resp.text())[:200]
        return False, f"Sheets API HTTP {resp.status}: {body}"

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        ) as session:
            # ১. ট্যাব আছে কিনা
            async with session.get(f"{base}?fields=sheets.properties.title") as resp:
                if resp.status != 200:
                    return await _fail(resp)
                meta = await resp.json()
            titles = {s["properties"]["title"] for s in meta.get("sheets", [])}

            # ২. না থাকলে বানাও
            if tab not in titles:
                body = {"requests": [{"addSheet": {"properties": {"title": tab}}}]}
                async with session.post(f"{base}:batchUpdate", json=body) as resp:
                    if resp.status != 200:
                        return await _fail(resp)

            # ৩. পুরনো ডেটা মুছে নতুন লেখো
            async with session.post(f"{base}/values/{tab_q}:clear", json={}) as resp:
                if resp.status != 200:
                    return await _fail(resp)
            async with session.put(
                f"{base}/values/{tab_q}!A1?valueInputOption=RAW",
                json={"values": values},
            ) as resp:
                if resp.status != 200:
                    return await _fail(resp)
    except asyncio.TimeoutError:
        return False, "Google Sheets timeout (৩০ সেকেন্ড)।"
    except aiohttp.ClientError as exc:
        return False, f"Network error: {exc}"

    return True, f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"


# ══════════════════════════════════════════════
# Cleanup
# ══════════════════════════════════════════════

def prune_old(group_id: int, keep_days: int = RETENTION_DAYS) -> None:
    """keep_days-এর চেয়ে পুরনো violation ও growth ডেটা মুছে ফেলে।"""
    cutoff_ts = time.time() - keep_days * 86400
    cutoff_day = day_key(cutoff_ts)
    try:
        vref = database._ref(f"/analytics/{group_id}/violations")
        data = vref.get() or {}
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, dict) and val.get("ts", 0) < cutoff_ts:
                    vref.child(key).delete()
        gref = database._ref(f"/analytics/{group_id}/growth")
        growth = gref.get() or {}
        if isinstance(growth, dict):
            for key in growth:
                if key < cutoff_day:
                    gref.child(key).delete()
    except Exception as exc:
        logger.debug("prune_old(%s) failed: %s", group_id, exc)
