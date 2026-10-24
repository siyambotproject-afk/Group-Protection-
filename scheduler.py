"""
scheduler.py — Schedule & Auto Systems (ধাপ ৫)

একটি background loop (run_scheduler) যা চালায়:
  • Night mode          — রাত ১০টা–সকাল ৬টা গ্রুপ lock (feature: night_mode)
  • Auto delete         — admin-এর ঠিক করা সময় পরে সাধারণ মেসেজ delete (feature: auto_delete)
  • Scheduled broadcast — owner-এর ঠিক করা সময়ে সব গ্রুপে মেসেজ
  • Scheduled rules     — প্রতিদিন নির্দিষ্ট সময়ে গ্রুপে rules পোস্ট
  • Daily/weekly report — owner ও গ্রুপ admin-দের কাছে violation + growth রিপোর্ট

সময়ের zone: Asia/Dhaka (BOT_TIMEZONE env দিয়ে বদলানো যায়)।

Firebase paths:
  /groups/{gid}/schedule   = {night_locked, rules_time, rules_last_date}
  /autodelete/{gid}/m{mid} = delete করার unix timestamp
  /scheduled_broadcasts/{id} = {ts, text}
  /scheduler_state         = {daily_last, weekly_last, prune_last}
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from telegram import Bot, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import TelegramError

import analytics
import config
import database

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# কনফিগ (env দিয়ে বদলানো যায়)
# ══════════════════════════════════════════════

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


NIGHT_START_HOUR: int = _env_int("NIGHT_START_HOUR", 22)    # রাত ১০টা
NIGHT_END_HOUR: int = _env_int("NIGHT_END_HOUR", 6)         # সকাল ৬টা
REPORT_HOUR: int = _env_int("REPORT_HOUR", 8)               # রিপোর্ট পাঠানোর ঘণ্টা
WEEKLY_REPORT_WEEKDAY: int = _env_int("WEEKLY_REPORT_WEEKDAY", 4)  # Mon=0 … Fri=4

# Telegram: ৪৮ ঘণ্টার বেশি পুরনো মেসেজ বট delete করতে পারে না
AUTODELETE_MIN_SECONDS = 10
AUTODELETE_MAX_SECONDS = 48 * 3600

FAST_TICK_SECONDS = 15
SLOW_TICK_SECONDS = 60


def _full_permissions() -> ChatPermissions:
    # bot.py-এর /unlock কমান্ডের সাথে একই permission
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


def is_night(hour: int) -> bool:
    if NIGHT_START_HOUR > NIGHT_END_HOUR:  # মাঝরাত পার হয়
        return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
    return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR


# ══════════════════════════════════════════════
# Firebase helper
# ══════════════════════════════════════════════

def _group_ids() -> list[int]:
    """সব গ্রুপের ID। shallow read — পুরো /groups ডেটা নামায় না।"""
    try:
        data = database._ref("/groups").get(shallow=True) or {}
        return [int(k) for k in data if str(k).lstrip("-").isdigit()]
    except Exception as exc:
        logger.debug("shallow group list failed (%s) — fallback", exc)
        try:
            return [int(g["id"]) for g in database.get_group_list()]
        except Exception:
            return []


def _get_schedule(group_id: int) -> dict:
    try:
        data = database._ref(f"/groups/{group_id}/schedule").get() or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("get schedule(%s) failed: %s", group_id, exc)
        return {}


def _set_schedule(group_id: int, updates: dict) -> None:
    database._ref(f"/groups/{group_id}/schedule").update(updates)


# ══════════════════════════════════════════════
# Public helpers (bot.py-এর কমান্ড থেকে ব্যবহার হয়)
# ══════════════════════════════════════════════

def parse_hhmm(text: str) -> Optional[str]:
    """'9:5' → '09:05'; ভুল হলে None।"""
    try:
        hh, mm = text.strip().split(":")
        hh_i, mm_i = int(hh), int(mm)
        if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
            return f"{hh_i:02d}:{mm_i:02d}"
    except (ValueError, AttributeError):
        pass
    return None


def set_rules_schedule(group_id: int, hhmm: Optional[str]) -> None:
    """hhmm = 'HH:MM' বা None (বন্ধ)। আজকের সময় পার হয়ে গেলে আজ আর পাঠাবে না।"""
    if hhmm is None:
        _set_schedule(group_id, {"rules_time": None, "rules_last_date": None})
        return
    now = analytics.now_local()
    already_passed = now.strftime("%H:%M") >= hhmm
    _set_schedule(group_id, {
        "rules_time": hhmm,
        "rules_last_date": analytics.day_key() if already_passed else None,
    })


def get_rules_schedule(group_id: int) -> Optional[str]:
    return _get_schedule(group_id).get("rules_time")


def parse_duration(text: str) -> Optional[int]:
    """'30s' / '10m' / '2h' / '15' (সংখ্যা = মিনিট) → সেকেন্ড; ভুল হলে None।"""
    t = (text or "").strip().lower()
    if not t:
        return None
    unit = 60
    if t[-1] in ("s", "m", "h"):
        unit = {"s": 1, "m": 60, "h": 3600}[t[-1]]
        t = t[:-1]
    if not t.isdigit():
        return None
    return int(t) * unit


def queue_autodelete(group_id: int, message_id: int, seconds: int) -> None:
    try:
        database._ref(f"/autodelete/{group_id}/m{message_id}").set(int(time.time()) + int(seconds))
    except Exception as exc:
        logger.debug("queue_autodelete failed: %s", exc)


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def add_broadcast(run_ts: float, text: str) -> str:
    """ছোট unique id (মিলিসেকেন্ড-ভিত্তিক) — একই সেকেন্ডে দুটো দিলেও overwrite হয় না।"""
    bid = "b" + _base36(int(time.time() * 1000))[-7:]
    while database._ref(f"/scheduled_broadcasts/{bid}").get() is not None:
        bid = "b" + _base36(int(time.time() * 1000) + 1)[-7:]
    database._ref(f"/scheduled_broadcasts/{bid}").set({"ts": int(run_ts), "text": text})
    return bid


def list_broadcasts() -> list[tuple[str, int, str]]:
    data = database._ref("/scheduled_broadcasts").get() or {}
    items = [
        (bid, int(v.get("ts", 0)), str(v.get("text", "")))
        for bid, v in data.items() if isinstance(v, dict)
    ]
    items.sort(key=lambda x: x[1])
    return items


def cancel_broadcast(bid: str) -> bool:
    ref = database._ref(f"/scheduled_broadcasts/{bid}")
    if ref.get() is None:
        return False
    ref.delete()
    return True


# ══════════════════════════════════════════════
# Fast tick — auto delete + scheduled broadcast
# ══════════════════════════════════════════════

async def _autodelete_tick(bot: Bot) -> None:
    try:
        data = database._ref("/autodelete").get() or {}
    except Exception as exc:
        logger.debug("autodelete read failed: %s", exc)
        return
    if not isinstance(data, dict):
        return

    now = time.time()
    for gid_str, msgs in list(data.items()):
        if not isinstance(msgs, dict):
            continue
        for key, due_ts in list(msgs.items()):
            if not isinstance(due_ts, (int, float)) or due_ts > now:
                continue
            try:
                await bot.delete_message(int(gid_str), int(key.lstrip("m")))
            except TelegramError:
                pass  # আগেই delete হয়েছে / permission নেই — queue থেকে সরিয়ে দিলেই হবে
            except ValueError:
                pass
            try:
                database._ref(f"/autodelete/{gid_str}/{key}").delete()
            except Exception as exc:
                logger.debug("autodelete cleanup failed: %s", exc)
            await asyncio.sleep(0.05)


async def _broadcast_tick(bot: Bot) -> None:
    try:
        data = database._ref("/scheduled_broadcasts").get() or {}
    except Exception as exc:
        logger.debug("broadcast read failed: %s", exc)
        return
    if not isinstance(data, dict):
        return

    now = time.time()
    for bid, item in list(data.items()):
        if not isinstance(item, dict) or item.get("ts", 0) > now:
            continue
        # আগে মুছো, যাতে crash হলেও দুইবার না যায়
        try:
            database._ref(f"/scheduled_broadcasts/{bid}").delete()
        except Exception as exc:
            logger.error("Scheduled broadcast %s delete failed — skipped: %s", bid, exc)
            continue

        text = str(item.get("text", ""))
        sent = failed = 0
        for gid in _group_ids():
            try:
                await bot.send_message(gid, text)
                sent += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)
        logger.info("Scheduled broadcast %s done: sent=%s failed=%s", bid, sent, failed)

        if config.BOT_OWNER_ID:
            try:
                await bot.send_message(
                    config.BOT_OWNER_ID,
                    f"📢 Scheduled broadcast ({bid}) সম্পন্ন।\n✅ {sent} | ❌ {failed}",
                )
            except TelegramError:
                pass


# ══════════════════════════════════════════════
# Slow tick — night mode, rules post, reports
# ══════════════════════════════════════════════

# lock/unlock ব্যর্থ হলে প্রতি মিনিটে retry না করার জন্য
_retry_after: dict[tuple[int, str], float] = {}
_RETRY_DELAY = 30 * 60


async def _night_mode(bot: Bot, gid: int, features: dict, sched: dict, night: bool) -> None:
    locked = bool(sched.get("night_locked"))
    want_lock = night and bool(features.get("night_mode", False))

    if want_lock and not locked:
        action = "lock"
    elif locked and not want_lock:
        action = "unlock"
    else:
        return

    if _retry_after.get((gid, action), 0) > time.time():
        return

    try:
        if action == "lock":
            await bot.set_chat_permissions(gid, ChatPermissions(can_send_messages=False))
            _set_schedule(gid, {"night_locked": True})
            text = (
                f"🌙 <b>নাইট মোড চালু</b>\n"
                f"রাত {NIGHT_START_HOUR % 12 or 12}টা থেকে সকাল {NIGHT_END_HOUR % 12 or 12}টা পর্যন্ত "
                f"গ্রুপ লক থাকবে। শুভ রাত্রি!"
            )
        else:
            await bot.set_chat_permissions(gid, _full_permissions())
            _set_schedule(gid, {"night_locked": None})
            text = "☀️ <b>নাইট মোড শেষ</b> — গ্রুপ আনলক করা হয়েছে। শুভ সকাল!"
        _retry_after.pop((gid, action), None)
        try:
            await bot.send_message(gid, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
    except TelegramError as exc:
        logger.warning("Night mode %s failed for group %s: %s", action, gid, exc)
        _retry_after[(gid, action)] = time.time() + _RETRY_DELAY


async def _rules_post(bot: Bot, gid: int, sched: dict, now: datetime) -> None:
    rules_time = sched.get("rules_time")
    if not rules_time:
        return
    today = analytics.day_key()
    if sched.get("rules_last_date") == today or now.strftime("%H:%M") < rules_time:
        return
    # আগে mark করো, যাতে কোনো কারণে ব্যর্থ হলেও প্রতি মিনিটে spam না হয়
    try:
        _set_schedule(gid, {"rules_last_date": today})
    except Exception as exc:
        logger.error("rules_last_date save failed for %s — skipped: %s", gid, exc)
        return
    try:
        rules = database.get_rules(gid)
        await bot.send_message(
            gid, f"📜 <b>গ্রুপের নিয়মাবলী</b>\n\n{rules}", parse_mode=ParseMode.HTML
        )
    except TelegramError as exc:
        logger.warning("Scheduled rules post failed for %s: %s", gid, exc)


async def _recipients(bot: Bot, gid: int) -> set[int]:
    ids: set[int] = set()
    if config.BOT_OWNER_ID:
        ids.add(config.BOT_OWNER_ID)
    try:
        for admin in await bot.get_chat_administrators(gid):
            if not admin.user.is_bot:
                ids.add(admin.user.id)
    except TelegramError:
        pass
    return ids


async def _send_reports(bot: Bot, gids: list[int], days: int, title: str) -> None:
    start_ts, end_ts = analytics.window_previous_days(days)
    for gid in gids:
        try:
            try:
                chat = await bot.get_chat(gid)
                group_title = chat.title or str(gid)
            except TelegramError:
                group_title = str(gid)

            text, total = analytics.build_report(gid, group_title, start_ts, end_ts, title)
            if total == 0:
                continue  # কিছুই ঘটেনি — অকারণ মেসেজ নয়

            for uid in await _recipients(bot, gid):
                try:
                    await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
                except TelegramError:
                    pass  # admin বটকে /start করেননি — স্বাভাবিক
                await asyncio.sleep(0.05)
        except Exception as exc:
            logger.error("Report for group %s failed: %s", gid, exc)


def _get_state() -> dict:
    try:
        data = database._ref("/scheduler_state").get() or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _reports_tick(bot: Bot, gids: list[int], now: datetime) -> None:
    if now.hour < REPORT_HOUR:
        return
    state = _get_state()
    today = analytics.day_key()
    is_weekly_day = now.weekday() == WEEKLY_REPORT_WEEKDAY

    if is_weekly_day and state.get("weekly_last") != today:
        # আগে state লিখো — restart হলেও একই দিনে দুইবার যাবে না
        database._ref("/scheduler_state").update({"weekly_last": today, "daily_last": today})
        await _send_reports(bot, gids, 7, "সাপ্তাহিক রিপোর্ট")
    elif not is_weekly_day and state.get("daily_last") != today:
        database._ref("/scheduler_state").update({"daily_last": today})
        await _send_reports(bot, gids, 1, "দৈনিক রিপোর্ট")

    if state.get("prune_last") != today:
        database._ref("/scheduler_state").update({"prune_last": today})
        for gid in gids:
            analytics.prune_old(gid)


async def _slow_tick(bot: Bot) -> None:
    now = analytics.now_local()
    night = is_night(now.hour)
    gids = _group_ids()

    for gid in gids:
        try:
            features = database.get_features(gid)
            sched = _get_schedule(gid)
            await _night_mode(bot, gid, features, sched, night)
            await _rules_post(bot, gid, sched, now)
        except Exception as exc:
            logger.error("Slow tick failed for group %s: %s", gid, exc)

    await _reports_tick(bot, gids, now)


# ══════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════

async def run_scheduler(bot: Bot) -> None:
    """bot.py-এর asyncio.gather-এ যোগ করা হয়। কোনো error-এ loop থামে না।"""
    await asyncio.sleep(20)  # polling শুরু হতে সময় দাও
    last_slow = 0.0
    logger.info(
        "Scheduler started (tz=%s, night %02d:00–%02d:00, report %02d:00)",
        analytics.TZ, NIGHT_START_HOUR, NIGHT_END_HOUR, REPORT_HOUR,
    )
    while True:
        try:
            await _autodelete_tick(bot)
            await _broadcast_tick(bot)
        except Exception as exc:
            logger.error("Scheduler fast tick error: %s", exc)

        if time.time() - last_slow >= SLOW_TICK_SECONDS:
            last_slow = time.time()
            try:
                await _slow_tick(bot)
            except Exception as exc:
                logger.error("Scheduler slow tick error: %s", exc)

        await asyncio.sleep(FAST_TICK_SECONDS)
