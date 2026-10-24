"""
bot.py — Group Protection Bot (Multi-group, Multi-feature)
Owner: একজন (BOT_OWNER_ID)
Admin: প্রতিটা গ্রুপের admin রা
Member: কোনো কমান্ড নেই
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import time
from datetime import datetime, timedelta

from telegram import (
    Bot,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

import config
import database
import moderation
import warn_system
import spam_filter
import captcha as captcha_module
import analytics
import scheduler
import security
from features import FEATURE_DEFAULTS, FEATURE_LABELS

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return config.BOT_OWNER_ID != 0 and user_id == config.BOT_OWNER_ID


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


def group_only(func):
    """গ্রুপ ছাড়া কমান্ড চলবে না।"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            await update.message.reply_text("⚠️ এই কমান্ড শুধু গ্রুপে ব্যবহার করা যাবে।")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def admin_only(func):
    """Admin ছাড়া কমান্ড চলবে না।"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat = update.effective_chat
        if not await is_admin(context.bot, chat.id, user.id):
            await update.message.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def owner_only(func):
    """শুধু bot owner চালাতে পারবেন।"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("⛔ এই কমান্ড শুধুমাত্র বট মালিক ব্যবহার করতে পারবেন।")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


async def send_log(bot: Bot, group_id: int, text: str) -> None:
    """Log channel-এ violation পাঠায়।"""
    try:
        ch = database.get_log_channel(group_id)
        if ch:
            await bot.send_message(ch, text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.debug("Log send failed: %s", exc)


_bg_tasks: set = set()


def _spawn(coro) -> None:
    """Background task চালায় এবং GC থেকে বাঁচাতে reference ধরে রাখে।"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _send_temp(bot: Bot, chat_id: int, text: str, seconds: int = 6) -> None:
    """ছোট নোটিশ পাঠিয়ে কয়েক সেকেন্ড পর নিজে মুছে ফেলে।"""
    try:
        sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        await asyncio.sleep(seconds)
        await sent.delete()
    except TelegramError:
        pass


async def mute_user(bot: Bot, chat_id: int, user_id: int, minutes: int) -> bool:
    until = datetime.now() + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return True
    except TelegramError as exc:
        logger.warning("Mute failed for %s: %s", user_id, exc)
        return False


async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        return True
    except TelegramError as exc:
        logger.warning("Unmute failed for %s: %s", user_id, exc)
        return False


def make_violation_handler(bot: Bot) -> warn_system.ViolationHandler:
    """
    warn_system.ViolationHandler-এর জন্য ৪টা async callback বানিয়ে দেয়।
    (আগে সরাসরি ৭টা argument দেওয়া হতো, যা ক্লাসের __init__-এর সাথে মিলত না।)
    """

    async def _delete(chat_id: int, message_id: int) -> bool:
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except TelegramError as exc:
            logger.debug("Delete failed (%s/%s): %s", chat_id, message_id, exc)
            return False

    async def _mute(chat_id: int, user_id: int, until_ts: float) -> bool:
        try:
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.fromtimestamp(until_ts),
            )
            return True
        except TelegramError as exc:
            logger.warning("Mute failed for %s: %s", user_id, exc)
            return False

    async def _send(chat_id: int, text: str, parse_mode: str | None = None) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode=parse_mode)
        except TelegramError as exc:
            logger.warning("Send failed (%s): %s", chat_id, exc)

    async def _is_admin(chat_id: int, user_id: int) -> bool:
        return await is_admin(bot, chat_id, user_id)

    return warn_system.ViolationHandler(_delete, _mute, _send, _is_admin)


# ══════════════════════════════════════════════
# Welcome / Goodbye
# ══════════════════════════════════════════════

async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chat_member
    if not result:
        return

    chat = result.chat
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user

    if user.is_bot:
        return

    group_id = chat.id
    features = database.get_features(group_id)

    # নতুন মেম্বার join
    if old_status in ("left", "kicked") and new_status == "member":
        analytics.record_join(group_id)

        # Global ban চেক
        if database.is_globally_banned(user.id):
            try:
                await context.bot.ban_chat_member(group_id, user.id)
                await context.bot.send_message(
                    group_id,
                    f"🚫 {user.mention_html()} গ্লোবাল ব্যানলিস্টে আছেন। স্বয়ংক্রিয়ভাবে বের করা হয়েছে।",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return

        # Anti-raid
        if features.get("anti_raid", True):
            join_count = database.record_join(group_id)
            if join_count >= config.ANTI_RAID_THRESHOLD:
                try:
                    await context.bot.set_chat_permissions(
                        group_id,
                        ChatPermissions(can_send_messages=False),
                    )
                    await context.bot.send_message(
                        group_id,
                        "🚨 <b>রেইড সনাক্ত হয়েছে!</b>\n"
                        f"৬০ সেকেন্ডে {join_count} জন join করেছেন। গ্রুপ সাময়িক লক করা হয়েছে।\n"
                        "অ্যাডমিন /unlock দিয়ে খুলতে পারবেন।",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
                return

        # Anti-clone: অ্যাডমিনের নকল অ্যাকাউন্ট (/anticlone on দিয়ে চালু হয়)
        if features.get("anti_clone", False):
            try:
                admins = await context.bot.get_chat_administrators(group_id)
                original = security.find_clone(user, [a.user for a in admins])
            except TelegramError:
                original = None
            if original:
                await mute_user(context.bot, group_id, user.id, 400 * 24 * 60)
                try:
                    await context.bot.send_message(
                        group_id,
                        f"🕵️ <b>ক্লোন সন্দেহ!</b>\n"
                        f"{user.mention_html()}-এর নাম/username অ্যাডমিন {original.mention_html()}-এর সাথে মিলে যাচ্ছে।\n"
                        f"🔇 যাচাই না হওয়া পর্যন্ত mute রাখা হয়েছে। ভুল হলে অ্যাডমিন reply করে /unmute দিন।",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
                await send_log(
                    context.bot, group_id,
                    f"🕵️ <b>Clone সন্দেহ</b>\n👤 {user.mention_html()} (ID: {user.id})\n"
                    f"🎭 অনুকরণ: {original.mention_html()}",
                )
                return

        # Anti-bot: খুব নতুন account
        if features.get("anti_bot", True) and user.id:
            # account age check করা যায় না সরাসরি, username ছাড়া account সন্দেহজনক
            pass

        # Captcha check
        if features.get("captcha", False):
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ আমি মানুষ — ক্লিক করুন",
                    callback_data=f"captcha_{group_id}_{user.id}",
                )
            ]])
            try:
                # নতুন member-কে restrict করো
                await context.bot.restrict_chat_member(
                    group_id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                )
                sent = await context.bot.send_message(
                    group_id,
                    f"👋 {user.mention_html()} গ্রুপে যোগ দিয়েছেন!\n\n"
                    f"🔐 <b>Captcha:</b> নিচের বাটনে ক্লিক করুন, নাহলে "
                    f"{captcha_module.CAPTCHA_TIMEOUT_SECONDS // 60} মিনিটে kick করা হবে।",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                captcha_module.add_pending_captcha(
                    group_id, user.id, user.full_name, sent.message_id
                )
            except TelegramError as exc:
                logger.warning("Captcha setup failed: %s", exc)
            return  # Welcome message পরে দেবো (captcha verify-এর পর)

        # Welcome message
        if features.get("welcome", True):
            msg_template = database.get_welcome_message(group_id)
            msg = msg_template.replace("{name}", user.mention_html()) \
                              .replace("{group}", chat.title or "গ্রুপ")
            try:
                await context.bot.send_message(
                    group_id, msg, parse_mode=ParseMode.HTML
                )
            except TelegramError as exc:
                logger.warning("Welcome message failed: %s", exc)

    # মেম্বার চলে গেলে
    elif old_status == "member" and new_status in ("left", "kicked"):
        analytics.record_left(group_id)
        if features.get("goodbye", True):
            msg_template = database.get_goodbye_message(group_id)
            msg = msg_template.replace("{name}", user.mention_html()) \
                              .replace("{group}", chat.title or "গ্রুপ")
            try:
                await context.bot.send_message(
                    group_id, msg, parse_mode=ParseMode.HTML
                )
            except TelegramError as exc:
                logger.warning("Goodbye message failed: %s", exc)


# ══════════════════════════════════════════════
# Message moderation
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat:
        return
    if user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return

    group_id = chat.id
    msg_id = msg.message_id

    # Dedup
    if database.is_already_processed(group_id, msg_id):
        return

    # Global ban
    if database.is_globally_banned(user.id):
        try:
            await context.bot.ban_chat_member(group_id, user.id)
            await msg.delete()
        except TelegramError:
            pass
        return

    # Activity record করো (inactive kick-এর জন্য)
    captcha_module.record_user_activity(database, group_id, user.id)

    # Admin bypass
    user_is_admin = await is_admin(context.bot, group_id, user.id)

    features = database.get_features(group_id)

    # VIP → সব bypass; Trusted → spam/flood bypass
    user_is_vip = captcha_module.is_vip_user(database, group_id, user.id)
    user_is_trusted = captcha_module.is_trusted_user(database, group_id, user.id)

    if user_is_vip:
        # VIP user — কোনো moderation নেই
        return
    if user_is_trusted:
        # Trusted user — spam/flood skip কিন্তু link/media moderation চলবে
        user_is_admin = True  # spam/flood check-এ admin হিসেবে দেখাবে

    try:
        settings = database.get_settings_strict(group_id)
        allowed_domains = database.get_allowed_domains_strict(group_id)
        blocked_domains = database.get_blocked_domains_strict(group_id)
    except Exception as exc:
        logger.error("DB read failed — moderation skipped: %s", exc)
        return

    # ── Slow mode (/slowmode) ──
    slow_secs = settings.get("slowmode_seconds")
    if isinstance(slow_secs, (int, float)) and slow_secs > 0 and not user_is_admin:
        wait = security.slowmode.check(group_id, user.id, int(slow_secs), msg.media_group_id)
        if wait > 0:
            database.mark_as_processed(group_id, msg_id)
            try:
                await msg.delete()
            except TelegramError:
                pass
            if security.slowmode.should_notify(group_id, user.id, int(slow_secs)):
                _spawn(_send_temp(
                    context.bot, group_id,
                    f"⏱️ {user.mention_html()} — স্লো মোড চালু আছে। আরও {wait} সেকেন্ড পরে মেসেজ দিন।",
                ))
            return

    text_parts = []
    if msg.text:
        text_parts.append(msg.text)
    if msg.caption:
        text_parts.append(msg.caption)
    full_text = " ".join(text_parts)

    violation_reason: str | None = None

    # ── Spam & Flood control ──
    if not user_is_admin:
        spam_result = spam_filter.check_spam_and_flood(
            group_id, user.id, full_text, features
        )
        if spam_result.is_violation:
            database.mark_as_processed(group_id, msg_id)
            analytics.record_violation(group_id, user.id, spam_result.reason)
            try:
                await msg.delete()
            except TelegramError:
                pass

            # Flood হলে auto mute
            if spam_result.should_mute:
                mute_mins = spam_result.mute_minutes
                await mute_user(context.bot, group_id, user.id, mute_mins)
                try:
                    await context.bot.send_message(
                        group_id,
                        f"⚡ {user.mention_html()} — {spam_result.reason}\n"
                        f"🔇 {mute_mins} মিনিটের জন্য mute করা হয়েছে।",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
            else:
                try:
                    await context.bot.send_message(
                        group_id,
                        f"🚫 {user.mention_html()} — {spam_result.reason}",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass

            # Behavior pattern track করো
            need_auto_warn = spam_filter.record_suspicious_behavior(group_id, user.id)
            if need_auto_warn:
                try:
                    handler = make_violation_handler(context.bot)
                    # মেসেজ আগেই delete + processed mark করা হয়েছে
                    await handler.handle(
                        group_id, user.id, msg_id, user.mention_html(),
                        "বারবার সন্দেহজনক আচরণ (auto-warn)",
                        check_dedup=False, delete_message=False,
                    )
                except Exception as exc:
                    logger.error("Auto-warn after pattern failed: %s", exc)

            await send_log(
                context.bot, group_id,
                f"🚫 <b>Spam/Flood</b>\n"
                f"👤 {user.mention_html()} (ID: {user.id})\n"
                f"📝 {spam_result.reason}",
            )
            return

    # ── ফোন নম্বর block ──
    if features.get("phone_block", True) and full_text and not user_is_admin:
        import re
        phone_re = re.compile(r"(?<!\d)(01[3-9]\d{8}|\+8801[3-9]\d{8})(?!\d)")
        if phone_re.search(full_text):
            violation_reason = "📵 ফোন নম্বর শেয়ার করা অনুমোদিত নয়"

    # ── Forward block ──
    if not violation_reason and features.get("forward_block", False) and not user_is_admin:
        if msg.forward_date or msg.forward_from or msg.forward_from_chat:
            violation_reason = "↩️ ফরওয়ার্ড মেসেজ অনুমোদিত নয়"

    # ── Voice block ──
    if not violation_reason and features.get("voice_block", False) and not user_is_admin:
        if msg.voice or msg.video_note:
            violation_reason = "🎙️ ভয়েস মেসেজ অনুমোদিত নয়"

    # ── Sticker block ──
    if not violation_reason and features.get("sticker_block", False) and not user_is_admin:
        if msg.sticker:
            violation_reason = "😀 স্টিকার পাঠানো অনুমোদিত নয়"

    # ── Voice transcribe moderation (/voicescan on) ──
    if not violation_reason and features.get("voice_moderation", False) and msg.voice and not user_is_admin:
        transcript = await security.transcribe_voice(context.bot, msg.voice)
        if transcript:
            voice_result = await moderation.moderate_text_async(transcript)
            if voice_result.is_violation:
                violation_reason = f"🎙️ ভয়েস মেসেজে আপত্তিকর কথা — {voice_result.reason or 'অনুপযুক্ত কনটেন্ট'}"

    # ── Text moderation ──
    if not violation_reason and features.get("text_moderation", True) and full_text:
        result = await moderation.moderate_text_async(full_text)
        if result.is_violation and not user_is_admin:
            violation_reason = result.reason or "অনুপযুক্ত টেক্সট"

    # ── URL filter ──
    if not violation_reason and features.get("url_filter", True) and full_text:
        url_result = await moderation.moderate_urls_async(full_text, blocked_domains, allowed_domains)
        if url_result.is_violation and not user_is_admin:
            violation_reason = url_result.reason or "অনুমোদিত নয় এমন লিংক"

    # ── Media moderation ──
    if not violation_reason and features.get("media_moderation", True):
        media_result = await moderation.moderate_media(msg)
        if media_result and media_result.is_violation and not user_is_admin:
            violation_reason = media_result.reason or "অনুপযুক্ত মিডিয়া"

    if not violation_reason:
        database.mark_as_processed(group_id, msg_id)
        # Auto delete: চালু থাকলে সাধারণ মেসেজ নির্দিষ্ট সময় পরে delete হবে
        if features.get("auto_delete", False) and not user_is_admin:
            auto_secs = settings.get("auto_delete_seconds")
            if isinstance(auto_secs, (int, float)) and auto_secs > 0:
                scheduler.queue_autodelete(group_id, msg_id, int(auto_secs))
        return

    # ── Violation handle ──
    database.mark_as_processed(group_id, msg_id)
    analytics.record_violation(group_id, user.id, violation_reason)

    # মেসেজ delete
    try:
        await msg.delete()
        logger.info("Deleted msg %s from user %s: %s", msg_id, user.id, violation_reason)
    except TelegramError as exc:
        logger.warning("Delete failed: %s", exc)

    # Admin হলে শুধু delete, warning নয়
    if user_is_admin and settings.get("admin_bypass_delete_only", True):
        logger.info("Admin %s — delete only, no warning", user.id)
        return

    # Warning দেওয়া
    try:
        handler = make_violation_handler(context.bot)
        # মেসেজ আগেই delete + processed mark করা হয়েছে
        warn_data = await handler.handle(
            group_id, user.id, msg_id, user.mention_html(), violation_reason,
            check_dedup=False, delete_message=False,
        )

        # Log channel-এ পাঠাও
        await send_log(
            context.bot, group_id,
            f"⚠️ <b>Violation</b>\n"
            f"👤 {user.mention_html()} (ID: {user.id})\n"
            f"📝 কারণ: {html.escape(violation_reason)}\n"
            f"⚠️ Warning: {warn_data.get('count', '?')}/{settings.get('max_warnings', 3)}",
        )
    except Exception as exc:
        logger.error("Warning handler error: %s", exc)


async def moderate_media(msg: Message):
    """Media moderation wrapper।"""
    try:
        if msg.photo:
            photo = msg.photo[-1]
            file = await msg.get_bot().get_file(photo.file_id)
            data = await file.download_as_bytearray()
            return await moderation.moderate_image_bytes(bytes(data))
        elif msg.video:
            file = await msg.get_bot().get_file(msg.video.file_id)
            data = await file.download_as_bytearray()
            return await moderation.moderate_video_bytes(bytes(data))
        elif msg.animation:
            file = await msg.get_bot().get_file(msg.animation.file_id)
            data = await file.download_as_bytearray()
            return await moderation.moderate_gif_bytes(bytes(data))
    except Exception as exc:
        logger.debug("Media moderate error: %s", exc)
    return None

moderation.moderate_media = moderate_media


# ══════════════════════════════════════════════
# Owner Panel (ইনবক্সে)
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if chat.type in ("group", "supergroup"):
        await update.message.reply_text(
            "✅ বট সক্রিয় আছে। /settings দিয়ে সেটিংস দেখুন।"
        )
        return

    # Private chat — owner panel দেখাও
    if is_owner(user.id):
        await show_owner_panel(update, context)
    else:
        await update.message.reply_text(
            "👋 আমি একটি গ্রুপ প্রটেকশন বট।\n"
            "আপনার গ্রুপে আমাকে যোগ করুন এবং অ্যাডমিন করুন।"
        )


async def show_owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = database.get_bot_stats()
    text = (
        "👑 <b>Owner Panel</b>\n\n"
        f"📊 মোট গ্রুপ: <b>{stats['groups']}</b>\n"
        f"⚠️ মোট Violation: <b>{stats['violations']}</b>"
    )
    keyboard = [
        [
            InlineKeyboardButton("📊 Stats", callback_data="owner_stats"),
            InlineKeyboardButton("📋 Group List", callback_data="owner_grouplist"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast"),
            InlineKeyboardButton("🚫 Global Ban", callback_data="owner_globalban"),
        ],
        [
            InlineKeyboardButton("🔧 Maintenance", callback_data="owner_maintenance"),
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ══════════════════════════════════════════════
# Settings Panel (গ্রুপে)
# ══════════════════════════════════════════════

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        if is_owner(user.id):
            await show_owner_panel(update, context)
        return

    if not await is_admin(context.bot, chat.id, user.id):
        await update.message.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা সেটিংস দেখতে পারবেন।")
        return

    await show_settings_panel(update, context, chat.id)


async def show_settings_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: int) -> None:
    features = database.get_features(group_id)

    def btn(feature: str) -> InlineKeyboardButton:
        on = features.get(feature, FEATURE_DEFAULTS.get(feature, True))
        label = FEATURE_LABELS.get(feature, feature)
        status = "🟢" if on else "🔴"
        return InlineKeyboardButton(f"{status} {label}", callback_data=f"toggle_{feature}_{group_id}")

    keyboard = [
        [btn("url_filter"), btn("text_moderation")],
        [btn("media_moderation"), btn("spam_filter")],
        [btn("phone_block"), btn("forward_block")],
        [btn("welcome"), btn("goodbye")],
        [btn("anti_raid"), btn("anti_bot")],
        [btn("sticker_block"), btn("voice_block")],
        [btn("night_mode"), btn("warn_expire")],
        [btn("log_channel"), btn("arabic_block")],
        [
            InlineKeyboardButton("📜 Rules সেট", callback_data=f"set_rules_{group_id}"),
            InlineKeyboardButton("👋 Welcome সেট", callback_data=f"set_welcome_{group_id}"),
        ],
        [InlineKeyboardButton("❌ বন্ধ করুন", callback_data="close")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"⚙️ <b>গ্রুপ সেটিংস</b>\n"
        f"🟢 = চালু  |  🔴 = বন্ধ\n\n"
        f"যেকোনো বাটনে চাপলে on/off হবে।"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ══════════════════════════════════════════════
# Callback Handler (button press)
# ══════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ── Captcha verify ──
    if data.startswith("captcha_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                cap_group_id = int(parts[1])
                cap_user_id = int(parts[2])
            except ValueError:
                return

            # শুধু ঐ user-ই বাটন চাপতে পারবে
            if user.id != cap_user_id:
                await query.answer("⚠️ এই বাটন আপনার জন্য নয়!", show_alert=True)
                return

            verified = captcha_module.verify_captcha(cap_group_id, cap_user_id)
            if verified:
                # Restrict সরাও
                try:
                    await context.bot.restrict_chat_member(
                        cap_group_id, cap_user_id,
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                        ),
                    )
                except TelegramError:
                    pass

                # Captcha message edit করো
                try:
                    await query.edit_message_text(
                        f"✅ {user.mention_html()} captcha সম্পন্ন করেছেন! স্বাগতম 🎉",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass

                # Welcome message পাঠাও
                features_cap = database.get_features(cap_group_id)
                if features_cap.get("welcome", True):
                    try:
                        chat_info = await context.bot.get_chat(cap_group_id)
                        msg_tmpl = database.get_welcome_message(cap_group_id)
                        welcome_msg = msg_tmpl \
                            .replace("{name}", user.mention_html()) \
                            .replace("{group}", chat_info.title or "গ্রুপ")
                        await context.bot.send_message(
                            cap_group_id, welcome_msg, parse_mode=ParseMode.HTML
                        )
                    except TelegramError:
                        pass

                await query.answer("✅ যাচাই সফল! আপনি এখন মেসেজ করতে পারবেন।")
            else:
                await query.answer("⚠️ আপনার captcha আর বৈধ নেই।", show_alert=True)
        return

    # ── Feature toggle ──
    if data.startswith("toggle_"):
        parts = data.split("_", 2)
        _, feature, group_id_str = parts[0], parts[1], parts[2]
        # toggle_ আলাদা করতে হবে
        # data format: toggle_{feature}_{group_id}
        rest = data[len("toggle_"):]
        # feature name শেষে _ আসবে group_id
        last_under = rest.rfind("_")
        feature = rest[:last_under]
        group_id = int(rest[last_under+1:])

        # Admin check
        if not await is_admin(context.bot, group_id, user.id) and not is_owner(user.id):
            await query.answer("⛔ শুধুমাত্র অ্যাডমিন পরিবর্তন করতে পারবেন।", show_alert=True)
            return

        current = database.is_feature_on(group_id, feature)
        database.set_feature(group_id, feature, not current)
        await show_settings_panel(update, context, group_id)

    # ── Owner callbacks ──
    elif data == "owner_stats":
        stats = database.get_bot_stats()
        await query.edit_message_text(
            f"📊 <b>Bot Stats</b>\n\n"
            f"গ্রুপ: {stats['groups']}\n"
            f"Violation: {stats['violations']}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="owner_back")
            ]])
        )

    elif data == "owner_grouplist":
        groups = database.get_group_list()
        if not groups:
            text = "📋 কোনো গ্রুপ নেই।"
        else:
            lines = [f"📋 <b>গ্রুপ তালিকা</b> ({len(groups)}টি)\n"]
            for g in groups[:20]:
                lines.append(f"• ID: <code>{g['id']}</code>")
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="owner_back")
            ]])
        )

    elif data == "owner_back":
        await show_owner_panel(update, context)

    elif data == "close":
        await query.delete_message()


# ══════════════════════════════════════════════
# Admin Commands
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message:
        await msg.reply_text("ℹ️ কাউকে warn করতে তার বার্তায় reply করে /warn লিখুন।")
        return
    target = msg.reply_to_message.from_user
    if not target:
        await msg.reply_text("ℹ️ ইউজার শনাক্ত করা যায়নি।")
        return
    if await is_admin(context.bot, msg.chat.id, target.id):
        await msg.reply_text("⚠️ অ্যাডমিনকে warn করা যাবে না।")
        return

    reason = " ".join(context.args) if context.args else "অ্যাডমিন কর্তৃক সতর্কতা"
    handler = make_violation_handler(context.bot)
    # reply করা মেসেজ আগেই "processed" হয়ে থাকতে পারে, তাই dedup বাদ
    await handler.handle(
        msg.chat.id, target.id, msg.reply_to_message.message_id,
        target.mention_html(), reason,
        check_dedup=False,
    )


@group_only
@admin_only
async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার সতর্কতা কমাতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user
    try:
        rec = database.decrement_warning(msg.chat.id, target.id)
        await msg.reply_text(
            f"✅ {target.mention_html()} এর একটি সতর্কতা কমানো হয়েছে।\n"
            f"বর্তমান সতর্কতা: {rec.get('count', 0)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await msg.reply_text(f"⚠️ ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_resetwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার সতর্কতা রিসেট করতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user
    try:
        database.reset_warnings(msg.chat.id, target.id)
        await msg.reply_text(
            f"✅ {target.mention_html()} এর সব সতর্কতা রিসেট করা হয়েছে।",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await msg.reply_text(f"⚠️ ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার সতর্কতা দেখতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user

    # Warning expiry check করো
    features = database.get_features(msg.chat.id)
    if features.get("warn_expire", True):
        spam_filter.apply_warning_expiry(msg.chat.id, target.id, database)

    rec = database.get_warnings(msg.chat.id, target.id)
    settings = database.get_settings(msg.chat.id)
    expired_note = " (৭ দিনের পুরনো warnings মুছে ফেলা হয়েছে)" if features.get("warn_expire", True) else ""
    await msg.reply_text(
        f"⚠️ {target.mention_html()} এর সতর্কতা: "
        f"{rec.get('count', 0)}/{settings.get('max_warnings', 3)}{expired_note}",
        parse_mode=ParseMode.HTML,
    )


@group_only
@admin_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাউকে mute করতে তার বার্তায় reply করে /mute [মিনিট] লিখুন।")
        return
    target = msg.reply_to_message.from_user
    if await is_admin(context.bot, msg.chat.id, target.id):
        await msg.reply_text("⚠️ অ্যাডমিনকে mute করা যাবে না।")
        return

    minutes = 60
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            pass

    ok = await mute_user(context.bot, msg.chat.id, target.id, minutes)
    if ok:
        await msg.reply_text(
            f"🔇 {target.mention_html()} কে {minutes} মিনিটের জন্য mute করা হয়েছে।",
            parse_mode=ParseMode.HTML,
        )
    else:
        await msg.reply_text("⚠️ Mute করতে ব্যর্থ। বটের Restrict Members অনুমতি আছে কিনা দেখুন।")


@group_only
@admin_only
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাউকে unmute করতে তার বার্তায় reply করে /unmute লিখুন।")
        return
    target = msg.reply_to_message.from_user
    ok = await unmute_user(context.bot, msg.chat.id, target.id)
    if ok:
        await msg.reply_text(
            f"🔊 {target.mention_html()} এর mute তুলে নেওয়া হয়েছে।",
            parse_mode=ParseMode.HTML,
        )


@group_only
@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাউকে ban করতে তার বার্তায় reply করে /ban লিখুন।")
        return
    target = msg.reply_to_message.from_user
    if await is_admin(context.bot, msg.chat.id, target.id):
        await msg.reply_text("⚠️ অ্যাডমিনকে ban করা যাবে না।")
        return
    try:
        await context.bot.ban_chat_member(msg.chat.id, target.id)
        database.set_banned(msg.chat.id, target.id, True)
        await msg.reply_text(
            f"🚫 {target.mention_html()} কে ban করা হয়েছে।",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        await msg.reply_text(f"⚠️ Ban করতে ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাউকে unban করতে তার বার্তায় reply করে /unban লিখুন।")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(msg.chat.id, target.id, only_if_banned=True)
        database.set_banned(msg.chat.id, target.id, False)
        await msg.reply_text(
            f"✅ {target.mention_html()} এর ban তুলে নেওয়া হয়েছে।",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        await msg.reply_text(f"⚠️ Unban করতে ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await scheduler.lock_group(context.bot, update.effective_chat.id)
        await update.message.reply_text("🔒 গ্রুপ লক করা হয়েছে। শুধু অ্যাডমিন মেসেজ দিতে পারবেন।")
    except TelegramError as exc:
        await update.message.reply_text(f"⚠️ Lock করতে ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        restored = await scheduler.unlock_group(context.bot, update.effective_chat.id)
        note = " আগের permission ফিরিয়ে আনা হয়েছে।" if restored else ""
        await update.message.reply_text(f"🔓 গ্রুপ আনলক করা হয়েছে।{note}")
    except TelegramError as exc:
        await update.message.reply_text(f"⚠️ Unlock করতে ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text(
            "ℹ️ ব্যবহার: /setwelcome <মেসেজ>\n"
            "ভেরিয়েবল: {name} = ইউজার নাম, {group} = গ্রুপ নাম\n\n"
            "উদাহরণ: /setwelcome 👋 স্বাগতম {name}! নিয়ম মেনে চলুন।"
        )
        return
    message = " ".join(context.args)
    database.set_welcome_message(msg.chat.id, message)
    await msg.reply_text(f"✅ Welcome মেসেজ সেট হয়েছে:\n{message}")


@group_only
@admin_only
async def cmd_setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text("ℹ️ ব্যবহার: /setgoodbye <মেসেজ>")
        return
    message = " ".join(context.args)
    database.set_goodbye_message(msg.chat.id, message)
    await msg.reply_text(f"✅ Goodbye মেসেজ সেট হয়েছে:\n{message}")


@group_only
@admin_only
async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text("ℹ️ ব্যবহার: /setrules <নিয়মাবলী>")
        return
    rules = " ".join(context.args)
    database.set_rules(msg.chat.id, rules)
    await msg.reply_text("✅ গ্রুপের নিয়ম সেট হয়েছে।")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    rules = database.get_rules(chat.id)
    await update.message.reply_text(f"📜 <b>গ্রুপের নিয়মাবলী</b>\n\n{rules}", parse_mode=ParseMode.HTML)


@group_only
@admin_only
async def cmd_setlogchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text("ℹ️ ব্যবহার: /setlogchannel <channel_id>")
        return
    try:
        ch_id = int(context.args[0])
        database.set_log_channel(msg.chat.id, ch_id)
        database.set_feature(msg.chat.id, "log_channel", True)
        await msg.reply_text(f"✅ Log channel সেট হয়েছে: {ch_id}")
    except ValueError:
        await msg.reply_text("⚠️ সঠিক channel ID দিন।")


@group_only
@admin_only
async def cmd_allowdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text("ব্যবহার: /allowdomain <domain>")
        return
    domain = context.args[0].lower().strip()
    database.add_allowed_domain(msg.chat.id, domain)
    await msg.reply_text(f"✅ `{domain}` অনুমোদিত তালিকায় যোগ করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)


@group_only
@admin_only
async def cmd_blockdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.args:
        await msg.reply_text("ব্যবহার: /blockdomain <domain>")
        return
    domain = context.args[0].lower().strip()
    database.add_blocked_domain(msg.chat.id, domain)
    await msg.reply_text(f"✅ `{domain}` নিষিদ্ধ তালিকায় যোগ করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)


@group_only
@admin_only
async def cmd_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    domains = database.get_allowed_domains(update.effective_chat.id)
    if not domains:
        await update.message.reply_text("✅ অনুমোদিত তালিকা খালি।")
        return
    await update.message.reply_text("✅ অনুমোদিত:\n" + "\n".join(f"• {d}" for d in domains))


@group_only
@admin_only
async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    domains = database.get_blocked_domains(update.effective_chat.id)
    if not domains:
        await update.message.reply_text("🚫 নিষিদ্ধ তালিকা খালি।")
        return
    await update.message.reply_text("🚫 নিষিদ্ধ:\n" + "\n".join(f"• {d}" for d in domains))


# ══════════════════════════════════════════════
# Trusted / VIP Commands
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_trust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """কোনো user-কে trusted করো।"""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাকে trusted করতে চান তার বার্তায় reply করে /trust লিখুন।")
        return
    target = msg.reply_to_message.from_user
    captcha_module.set_trusted_user(database, msg.chat.id, target.id, True)
    await msg.reply_text(
        f"✅ {target.mention_html()} কে <b>Trusted</b> করা হয়েছে।\n"
        f"এখন তিনি spam/flood filter থেকে মুক্ত।",
        parse_mode=ParseMode.HTML,
    )


@group_only
@admin_only
async def cmd_untrust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trusted status সরাও।"""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার trusted status সরাতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user
    captcha_module.set_trusted_user(database, msg.chat.id, target.id, False)
    await msg.reply_text(
        f"✅ {target.mention_html()} এর Trusted status সরানো হয়েছে।",
        parse_mode=ParseMode.HTML,
    )


@group_only
@admin_only
async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """কোনো user-কে VIP করো (সব moderation bypass)।"""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কাকে VIP করতে চান তার বার্তায় reply করে /vip লিখুন।")
        return
    target = msg.reply_to_message.from_user
    captcha_module.set_vip_user(database, msg.chat.id, target.id, True)
    await msg.reply_text(
        f"⭐ {target.mention_html()} কে <b>VIP</b> করা হয়েছে।\n"
        f"তিনি সকল moderation থেকে মুক্ত।",
        parse_mode=ParseMode.HTML,
    )


@group_only
@admin_only
async def cmd_unvip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """VIP status সরাও।"""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার VIP status সরাতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user
    captcha_module.set_vip_user(database, msg.chat.id, target.id, False)
    await msg.reply_text(
        f"✅ {target.mention_html()} এর VIP status সরানো হয়েছে।",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════
# User Profile Command
# ══════════════════════════════════════════════

@group_only
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/profile — নিজের বা reply করা user-এর প্রোফাইল দেখো।"""
    msg = update.message
    chat = update.effective_chat

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    else:
        target = update.effective_user

    target_is_admin = await is_admin(context.bot, chat.id, target.id)
    profile_text = captcha_module.build_user_profile(
        database, chat.id, target.id, target.full_name, target_is_admin
    )
    await msg.reply_text(profile_text, parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════
# Mute List / Ban List Commands
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_mutelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mutelist — বর্তমানে muted user-দের দেখাও।"""
    msg = update.message
    muted = captcha_module.get_muted_users(database, msg.chat.id)

    if not muted:
        await msg.reply_text("✅ বর্তমানে কেউ muted নেই।")
        return

    from datetime import datetime
    lines = [f"🔇 <b>Muted Users ({len(muted)})</b>\n"]
    for m in muted:
        uid = m["user_id"]
        until = datetime.fromtimestamp(int(m["until"])).strftime("%d/%m/%Y %H:%M")
        lines.append(f"• <code>{uid}</code> → পর্যন্ত: {until}")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@group_only
@admin_only
async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/banlist — ban করা user-দের দেখাও।"""
    msg = update.message
    banned = captcha_module.get_banned_users(database, msg.chat.id)

    if not banned:
        await msg.reply_text("✅ বর্তমানে কেউ banned নেই।")
        return

    lines = [f"🚫 <b>Banned Users ({len(banned)})</b>\n"]
    for uid in banned[:30]:  # সর্বোচ্চ ৩০টি
        lines.append(f"• <code>{uid}</code>")

    if len(banned) > 30:
        lines.append(f"\n...এবং আরো {len(banned) - 30} জন")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════
# Inactive User Kick Command
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_kickinactive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kickinactive — ৩০ দিন inactive user-দের kick করো।"""
    msg = update.message
    days = captcha_module.INACTIVE_DAYS

    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass

    await msg.reply_text(
        f"⏳ {days} দিনের বেশি inactive user খুঁজছি..."
    )

    inactive_users = captcha_module.get_inactive_users(database, msg.chat.id, days)

    if not inactive_users:
        await msg.reply_text(f"✅ {days} দিনে কোনো inactive user নেই।")
        return

    kicked, failed = 0, 0
    for uid in inactive_users:
        # Admin-দের kick করবো না
        try:
            if await is_admin(context.bot, msg.chat.id, uid):
                continue
            await context.bot.ban_chat_member(msg.chat.id, uid)
            await context.bot.unban_chat_member(msg.chat.id, uid)  # kick (ban then unban)
            captcha_module.remove_user_activity(database, msg.chat.id, uid)
            kicked += 1
        except TelegramError:
            failed += 1

    await msg.reply_text(
        f"✅ Inactive kick সম্পন্ন!\n"
        f"🚪 Kicked: {kicked} জন\n"
        f"❌ Failed: {failed} জন\n"
        f"(যারা {days} দিন কোনো মেসেজ দেননি)"
    )


# ══════════════════════════════════════════════
# Spam Stats Command
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_spamstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """কোনো user-এর বর্তমান spam pattern দেখায়।"""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার spam stats দেখতে চান তার বার্তায় reply করুন।")
        return
    target = msg.reply_to_message.from_user
    count = spam_filter.get_suspicious_count(msg.chat.id, target.id)
    await msg.reply_text(
        f"📊 {target.mention_html()} এর spam pattern:\n"
        f"সন্দেহজনক আচরণ (শেষ ৫ মিনিটে): {count}/{spam_filter.PATTERN_VIOLATION_LIMIT}\n"
        f"Flood window: {spam_filter.FLOOD_WINDOW_SECONDS} সেকেন্ডে {spam_filter.FLOOD_MAX_MESSAGES}+ মেসেজ",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════
# Owner Commands
# ══════════════════════════════════════════════

@owner_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("ℹ️ ব্যবহার: /broadcast <মেসেজ>")
        return
    text = " ".join(context.args)
    groups = database.get_group_list()
    sent, failed = 0, 0
    for g in groups:
        try:
            await context.bot.send_message(int(g["id"]), text)
            sent += 1
        except TelegramError:
            failed += 1
    await update.message.reply_text(f"📢 Broadcast সম্পন্ন। ✅ {sent} | ❌ {failed}")


@owner_only
async def cmd_globalban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ ব্যবহার: কারো বার্তায় reply করে /globalban [কারণ]")
        return
    target = msg.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "Owner কর্তৃক গ্লোবাল ব্যান"
    database.add_global_ban(target.id, reason)

    groups = database.get_group_list()
    banned = 0
    for g in groups:
        try:
            await context.bot.ban_chat_member(int(g["id"]), target.id)
            banned += 1
        except TelegramError:
            pass
    await msg.reply_text(
        f"🚫 {target.mention_html()} কে গ্লোবাল ব্যান করা হয়েছে।\n"
        f"{banned}টি গ্রুপ থেকে বের করা হয়েছে।",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = database.get_bot_stats()
    await update.message.reply_text(
        f"📊 <b>Bot Stats</b>\n\n"
        f"🏘️ মোট গ্রুপ: {stats['groups']}\n"
        f"⚠️ মোট Violation: {stats['violations']}",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════
# Help & Status
# ══════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if is_owner(user.id) and chat.type == "private":
        text = (
            "👑 <b>Owner Commands</b>\n"
            "/stats — Bot stats\n"
            "/broadcast — সব গ্রুপে মেসেজ\n"
            "/globalban — সব গ্রুপ থেকে ban\n\n"
            "⚙️ <b>Admin Commands</b>\n"
            "/settings — গ্রুপ সেটিংস\n"
            "/warn — সতর্কতা দিন\n"
            "/unwarn — সতর্কতা কমান\n"
            "/resetwarn — সব সতর্কতা মুছুন\n"
            "/warnings — সতর্কতা দেখুন\n"
            "/mute [মিনিট] — mute করুন\n"
            "/unmute — mute তুলুন\n"
            "/ban — ban করুন\n"
            "/unban — ban তুলুন\n"
            "/lock — গ্রুপ লক\n"
            "/unlock — গ্রুপ আনলক\n"
            "/setwelcome — welcome মেসেজ\n"
            "/setgoodbye — goodbye মেসেজ\n"
            "/setrules — নিয়ম সেট\n"
            "/rules — নিয়ম দেখুন\n"
            "/setlogchannel — log channel\n"
            "/allowdomain — domain allow\n"
            "/blockdomain — domain block\n"
            "/spamstats — user spam pattern দেখুন\n"
            "/trust /untrust — trusted user\n"
            "/vip /unvip — VIP user\n"
            "/profile — user প্রোফাইল\n"
            "/mutelist — muted user তালিকা\n"
            "/banlist — banned user তালিকা\n"
            "/kickinactive [দিন] — inactive kick\n"
            "/analytics [দিন] — রিপোর্ট ও চার্ট\n"
            "/exportviolations [দিন] — Sheet/CSV export\n"
            "/autodelete <সময়|off> — auto delete\n"
            "/schedulerules <HH:MM|off> — rules পোস্ট\n"
            "/schedulebroadcast — নির্ধারিত broadcast\n"
            "/schedulelist /schedulecancel — broadcast তালিকা/বাতিল\n"
            "/slowmode <সেকেন্ড|off> — স্লো মোড\n"
            "/anticlone on|off — ক্লোন অ্যাকাউন্ট ধরা\n"
            "/voicescan on|off — ভয়েস লিখে যাচাই\n"
            "/poll /quiz — পোল/কুইজ বানান\n"
            "/report — (reply করে) অ্যাডমিনকে রিপোর্ট\n"
        )
    elif chat.type in ("group", "supergroup") and await is_admin(context.bot, chat.id, user.id):
        text = (
            "⚙️ <b>Admin Commands</b>\n"
            "/settings — গ্রুপ সেটিংস (বাটন)\n"
            "/warn — সতর্কতা দিন (reply করে)\n"
            "/mute [মিনিট] — mute করুন\n"
            "/ban — ban করুন\n"
            "/lock /unlock — গ্রুপ লক\n"
            "/setwelcome — welcome মেসেজ\n"
            "/setrules — নিয়ম সেট\n"
            "/rules — নিয়ম দেখুন\n"
            "/spamstats — spam pattern দেখুন\n"
            "/trust /untrust — trusted user\n"
            "/vip /unvip — VIP user\n"
            "/profile — user প্রোফাইল\n"
            "/mutelist — muted তালিকা\n"
            "/banlist — banned তালিকা\n"
            "/kickinactive — inactive kick\n"
            "/analytics [দিন] — রিপোর্ট ও চার্ট\n"
            "/exportviolations [দিন] — Sheet/CSV export\n"
            "/autodelete <সময়|off> — auto delete\n"
            "/schedulerules <HH:MM|off> — rules পোস্ট\n"
            "/slowmode <সেকেন্ড|off> — স্লো মোড\n"
            "/anticlone on|off — ক্লোন অ্যাকাউন্ট ধরা\n"
            "/voicescan on|off — ভয়েস লিখে যাচাই\n"
            "/poll /quiz — পোল/কুইজ বানান\n"
        )
    else:
        text = (
            "ℹ️ এই বট গ্রুপ রক্ষা করে। /rules দিয়ে নিয়ম দেখুন।\n"
            "/profile — আপনার প্রোফাইল দেখুন।\n"
            "/report — আপত্তিকর মেসেজে reply করে অ্যাডমিনকে জানান।"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("✅ বট চালু আছে।")
        return
    features = database.get_features(chat.id)
    settings = database.get_settings(chat.id)
    on_features = [FEATURE_LABELS[f] for f, v in features.items() if v and f in FEATURE_LABELS]
    off_features = [FEATURE_LABELS[f] for f, v in features.items() if not v and f in FEATURE_LABELS]
    await update.message.reply_text(
        f"📊 <b>গ্রুপ স্ট্যাটাস</b>\n\n"
        f"⚠️ Warning limit: {settings.get('max_warnings', 3)}\n"
        f"⏱️ Mute: {settings.get('mute_minutes', 1440)} মিনিট\n\n"
        f"🟢 চালু:\n" + "\n".join(f"• {f}" for f in on_features) + "\n\n"
        f"🔴 বন্ধ:\n" + "\n".join(f"• {f}" for f in off_features),
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════
# Health check server
# ══════════════════════════════════════════════

async def health_check_server() -> None:
    import aiohttp
    from aiohttp import web

    async def health(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check server listening on port %s", port)


# ══════════════════════════════════════════════
# ধাপ ৫: Analytics, Schedule & Auto Systems — কমান্ড
# ══════════════════════════════════════════════

def _parse_days(args: list[str], default: int, maximum: int = analytics.RETENTION_DAYS) -> int | None:
    """কমান্ডের প্রথম argument দিন হিসেবে পড়ে। ভুল হলে None।"""
    if not args:
        return default
    if not args[0].isdigit():
        return None
    days = int(args[0])
    return days if 1 <= days <= maximum else None


@group_only
@admin_only
async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    days = _parse_days(context.args, 7)
    if days is None:
        await update.message.reply_text(
            f"ℹ️ ব্যবহার: /analytics [দিন]  (১–{analytics.RETENTION_DAYS}, ডিফল্ট ৭)"
        )
        return
    start_ts, end_ts = analytics.window_last_days(days)
    text, _ = analytics.build_report(
        chat.id, chat.title or str(chat.id), start_ts, end_ts, f"শেষ {days} দিনের রিপোর্ট"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@group_only
@admin_only
async def cmd_exportviolations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    days = _parse_days(context.args, 30)
    if days is None:
        await update.message.reply_text(
            f"ℹ️ ব্যবহার: /exportviolations [দিন]  (১–{analytics.RETENTION_DAYS}, ডিফল্ট ৩০)"
        )
        return

    start_ts, end_ts = analytics.window_last_days(days)
    rows = analytics.get_violations(chat.id, start_ts, end_ts)
    if not rows:
        await update.message.reply_text(f"✅ শেষ {days} দিনে কোনো violation নেই।")
        return

    sheet_note = ""
    if analytics.sheets_configured():
        ok, info = await analytics.export_to_google_sheet(chat.id, rows)
        if ok:
            await update.message.reply_text(
                f"📤 {len(rows)}টি violation Google Sheet-এ এক্সপোর্ট হয়েছে।\n"
                f"📄 ট্যাব: V_{chat.id}\n{info}"
            )
            return
        sheet_note = f"\n⚠️ Google Sheet ব্যর্থ: {info}\nতাই CSV পাঠানো হলো।"
    else:
        sheet_note = "\nℹ️ Google Sheet সেটআপ নেই (GOOGLE_SHEET_ID), তাই CSV পাঠানো হলো।"

    csv_file = io.BytesIO(analytics.violations_csv(rows))
    csv_file.name = f"violations_{chat.id}_{days}d.csv"
    await update.message.reply_document(
        csv_file, caption=f"📤 {len(rows)}টি violation (শেষ {days} দিন){sheet_note}"
    )


@group_only
@admin_only
async def cmd_autodelete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    usage = (
        "ℹ️ ব্যবহার:\n"
        "/autodelete 30s — ৩০ সেকেন্ড পর মেসেজ delete\n"
        "/autodelete 10m — ১০ মিনিট পর\n"
        "/autodelete 2h — ২ ঘণ্টা পর\n"
        "/autodelete off — বন্ধ"
    )
    if not context.args:
        features = database.get_features(chat.id)
        secs = database.get_settings(chat.id).get("auto_delete_seconds")
        if features.get("auto_delete", False) and secs:
            await update.message.reply_text(f"🗑️ Auto delete চালু: {secs} সেকেন্ড পর।\n\n{usage}")
        else:
            await update.message.reply_text(f"🗑️ Auto delete বন্ধ আছে।\n\n{usage}")
        return

    arg = context.args[0].lower()
    if arg == "off":
        database.set_feature(chat.id, "auto_delete", False)
        await update.message.reply_text("✅ Auto delete বন্ধ করা হয়েছে।")
        return

    secs = scheduler.parse_duration(arg)
    if secs is None or not (scheduler.AUTODELETE_MIN_SECONDS <= secs <= scheduler.AUTODELETE_MAX_SECONDS):
        await update.message.reply_text(
            f"⚠️ সময় ভুল। সর্বনিম্ন {scheduler.AUTODELETE_MIN_SECONDS} সেকেন্ড, সর্বোচ্চ ৪৮ ঘণ্টা।\n\n{usage}"
        )
        return

    try:
        database.update_settings(chat.id, {"auto_delete_seconds": secs})
        database.set_feature(chat.id, "auto_delete", True)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ সেভ করতে ব্যর্থ: {exc}")
        return
    await update.message.reply_text(
        f"✅ Auto delete চালু — এখন থেকে সাধারণ সদস্যদের মেসেজ {secs} সেকেন্ড পরে delete হবে।\n"
        "(অ্যাডমিন ও trusted/VIP-এর মেসেজ delete হবে না।)"
    )


@group_only
@admin_only
async def cmd_schedulerules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not context.args:
        current = scheduler.get_rules_schedule(chat.id)
        status = f"প্রতিদিন {current}-এ পোস্ট হয়" if current else "বন্ধ আছে"
        await update.message.reply_text(
            f"📜 Scheduled rules: {status}\n\n"
            "ব্যবহার:\n/schedulerules 09:00 — প্রতিদিন সকাল ৯টায় (২৪ ঘণ্টা format)\n/schedulerules off — বন্ধ"
        )
        return

    if context.args[0].lower() == "off":
        scheduler.set_rules_schedule(chat.id, None)
        await update.message.reply_text("✅ Scheduled rules বন্ধ করা হয়েছে।")
        return

    hhmm = scheduler.parse_hhmm(context.args[0])
    if not hhmm:
        await update.message.reply_text("⚠️ সময় ভুল। উদাহরণ: /schedulerules 09:00")
        return
    scheduler.set_rules_schedule(chat.id, hhmm)
    await update.message.reply_text(
        f"✅ প্রতিদিন {hhmm}-এ ({analytics.TZ}) গ্রুপের নিয়মাবলী পোস্ট হবে।\n"
        "নিয়ম বদলাতে: /setrules"
    )


@owner_only
async def cmd_schedulebroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    usage = (
        "ℹ️ ব্যবহার:\n"
        "/schedulebroadcast 18:30 মেসেজ — আজ (বা পার হয়ে গেলে আগামীকাল) ১৮:৩০-এ\n"
        "/schedulebroadcast 2026-11-01 18:30 মেসেজ — নির্দিষ্ট তারিখে\n"
        f"সময় {analytics.TZ} অনুযায়ী। /schedulelist, /schedulecancel <id>"
    )
    parts = (msg.text or "").split(None, 3)  # [cmd, a, b, rest]
    if len(parts) < 3:
        await msg.reply_text(usage)
        return

    now = analytics.now_local()
    run_at = None
    text = ""
    hhmm = scheduler.parse_hhmm(parts[1])
    if hhmm:
        text = (msg.text or "").split(None, 2)[2]
        hh, mm = map(int, hhmm.split(":"))
        run_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_at <= now:
            run_at = run_at + timedelta(days=1)
    elif len(parts) == 4:
        hhmm = scheduler.parse_hhmm(parts[2])
        try:
            day = datetime.strptime(parts[1], "%Y-%m-%d")
        except ValueError:
            day = None
        if hhmm and day:
            hh, mm = map(int, hhmm.split(":"))
            run_at = day.replace(hour=hh, minute=mm, tzinfo=analytics.TZ)
            text = parts[3]

    if run_at is None or not text.strip():
        await msg.reply_text(f"⚠️ ফরম্যাট ভুল।\n\n{usage}")
        return
    if run_at <= now:
        await msg.reply_text("⚠️ এই সময় পার হয়ে গেছে। ভবিষ্যতের সময় দিন।")
        return

    try:
        bid = scheduler.add_broadcast(run_at.timestamp(), text)
    except Exception as exc:
        await msg.reply_text(f"⚠️ সেভ করতে ব্যর্থ: {exc}")
        return
    await msg.reply_text(
        f"✅ Broadcast নির্ধারিত হয়েছে।\n🆔 {bid}\n🕒 {run_at.strftime('%Y-%m-%d %H:%M')}"
    )


@owner_only
async def cmd_schedulelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        items = scheduler.list_broadcasts()
    except Exception as exc:
        await update.message.reply_text(f"⚠️ পড়তে ব্যর্থ: {exc}")
        return
    if not items:
        await update.message.reply_text("📭 কোনো scheduled broadcast নেই।")
        return
    lines = ["🗓️ Scheduled broadcasts:\n"]
    for bid, ts, text in items[:20]:
        when = datetime.fromtimestamp(ts, analytics.TZ).strftime("%Y-%m-%d %H:%M")
        preview = text if len(text) <= 40 else text[:40] + "…"
        lines.append(f"• {bid} — {when}\n  {preview}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_schedulecancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("ℹ️ ব্যবহার: /schedulecancel <id>  (id দেখতে /schedulelist)")
        return
    try:
        removed = scheduler.cancel_broadcast(context.args[0])
    except Exception as exc:
        await update.message.reply_text(f"⚠️ বাতিল করতে ব্যর্থ: {exc}")
        return
    await update.message.reply_text("✅ বাতিল করা হয়েছে।" if removed else "⚠️ এই id পাওয়া যায়নি।")


# ══════════════════════════════════════════════
# ধাপ ৬: Advanced Security — কমান্ড
# ══════════════════════════════════════════════

@group_only
@admin_only
async def cmd_slowmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    usage = (
        "ℹ️ ব্যবহার:\n"
        "/slowmode 30 — ৩০ সেকেন্ড পর পর মেসেজ\n"
        "/slowmode off — বন্ধ\n"
        f"(১ – {security.SLOWMODE_MAX_SECONDS} সেকেন্ড; অ্যাডমিন ও trusted/VIP-এর জন্য প্রযোজ্য নয়)"
    )
    if not context.args:
        current = database.get_settings(chat.id).get("slowmode_seconds") or 0
        status = f"চালু — {current} সেকেন্ড" if current else "বন্ধ"
        await update.message.reply_text(f"⏱️ Slow mode: {status}\n\n{usage}")
        return

    arg = context.args[0].lower()
    if arg in ("off", "0"):
        seconds = 0
    elif arg.isdigit() and 1 <= int(arg) <= security.SLOWMODE_MAX_SECONDS:
        seconds = int(arg)
    else:
        await update.message.reply_text(f"⚠️ সংখ্যা ভুল।\n\n{usage}")
        return

    try:
        database.update_settings(chat.id, {"slowmode_seconds": seconds})
    except Exception as exc:
        await update.message.reply_text(f"⚠️ সেভ করতে ব্যর্থ: {exc}")
        return
    if seconds:
        await update.message.reply_text(
            f"✅ Slow mode চালু — প্রতি সদস্য {seconds} সেকেন্ড পর পর মেসেজ দিতে পারবেন।"
        )
    else:
        await update.message.reply_text("✅ Slow mode বন্ধ করা হয়েছে।")


async def _toggle_feature_command(update: Update, context: ContextTypes.DEFAULT_TYPE, feature: str, label: str) -> bool | None:
    """/cmd on|off — feature চালু/বন্ধ। on/off ছাড়া হলে বর্তমান অবস্থা দেখায় এবং None ফেরত দেয়।"""
    chat = update.effective_chat
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        on = database.get_features(chat.id).get(feature, False)
        await update.message.reply_text(
            f"{label}: {'🟢 চালু' if on else '🔴 বন্ধ'}\nব্যবহার: {update.message.text.split()[0]} on | off"
        )
        return None
    database.set_feature(chat.id, feature, arg == "on")
    return arg == "on"


@group_only
@admin_only
async def cmd_anticlone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await _toggle_feature_command(update, context, "anti_clone", "🕵️ Anti-clone")
    if state is None:
        return
    if state:
        await update.message.reply_text(
            "✅ Anti-clone চালু। নতুন সদস্যের নাম/username কোনো অ্যাডমিনের সাথে মিলে গেলে সে mute হবে।\n"
            "⚠️ বটকে Restrict Members permission ও admin তালিকা দেখার সুযোগ দিন।"
        )
    else:
        await update.message.reply_text("✅ Anti-clone বন্ধ করা হয়েছে।")


@group_only
@admin_only
async def cmd_voicescan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await _toggle_feature_command(update, context, "voice_moderation", "🎙️ ভয়েস স্ক্যান")
    if state is None:
        return
    if state and not security.voice_scan_available():
        await update.message.reply_text(
            "⚠️ চালু করা হয়েছে, কিন্তু GROQ_API_KEY সেট নেই — তাই ভয়েস লিখে যাচাই করা যাবে না।\n"
            "Render-এ GROQ_API_KEY যোগ করুন।"
        )
    elif state:
        await update.message.reply_text(
            f"✅ ভয়েস স্ক্যান চালু। {security.VOICE_MAX_SECONDS} সেকেন্ড/৫MB পর্যন্ত ভয়েস লিখে যাচাই হবে।"
        )
    else:
        await update.message.reply_text("✅ ভয়েস স্ক্যান বন্ধ করা হয়েছে।")


@group_only
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat = update.effective_chat
    reporter = update.effective_user

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        _spawn(_send_temp(
            context.bot, chat.id,
            "ℹ️ ব্যবহার: আপত্তিকর মেসেজে reply করে /report [কারণ] লিখুন।",
        ))
        return

    target_msg = msg.reply_to_message
    target = target_msg.from_user
    if target.id == reporter.id or target.is_bot:
        _spawn(_send_temp(context.bot, chat.id, "⚠️ এই মেসেজ রিপোর্ট করা যাবে না।"))
        return

    left = security.report_cooldown_left(chat.id, reporter.id)
    if left:
        _spawn(_send_temp(context.bot, chat.id, f"⏳ আবার রিপোর্ট করতে {left} সেকেন্ড অপেক্ষা করুন।"))
        return

    reason = html.escape(" ".join(context.args)[:300]) if context.args else "কারণ দেওয়া হয়নি"
    excerpt_src = target_msg.text or target_msg.caption or "(টেক্সট নেই — মিডিয়া/স্টিকার)"
    excerpt = html.escape(excerpt_src[:200])
    link = security.message_link(chat, target_msg.message_id)

    report_text = (
        f"🚩 <b>নতুন রিপোর্ট</b>\n"
        f"🏘️ গ্রুপ: {html.escape(chat.title or str(chat.id))}\n"
        f"👤 রিপোর্টকারী: {reporter.mention_html()}\n"
        f"🎯 অভিযুক্ত: {target.mention_html()} (ID: <code>{target.id}</code>)\n"
        f"📝 কারণ: {reason}\n"
        f"💬 মেসেজ: <i>{excerpt}</i>"
        + (f"\n🔗 <a href=\"{link}\">মেসেজ দেখুন</a>" if link else "")
    )

    delivered = 0
    admin_users = []
    try:
        admin_users = [a.user for a in await context.bot.get_chat_administrators(chat.id) if not a.user.is_bot]
    except TelegramError as exc:
        logger.warning("Report: admin list failed: %s", exc)

    for admin_user in admin_users:
        try:
            await context.bot.send_message(
                admin_user.id, report_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            delivered += 1
        except TelegramError:
            pass  # অ্যাডমিন বটকে /start করেননি

    log_channel = database.get_log_channel(chat.id)
    if log_channel:
        try:
            await context.bot.send_message(
                log_channel, report_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            delivered += 1
        except TelegramError:
            pass

    try:
        await msg.delete()  # /report কমান্ডটি মুছে ফেলা (রিপোর্টকারীর গোপনীয়তা)
    except TelegramError:
        pass

    if delivered:
        _spawn(_send_temp(context.bot, chat.id, "✅ রিপোর্ট অ্যাডমিনদের জানানো হয়েছে। ধন্যবাদ!"))
    else:
        # কেউ DM/log পায়নি — গ্রুপে অ্যাডমিনদের mention করো
        mentions = " ".join(a.mention_html() for a in admin_users[:10])
        try:
            await context.bot.send_message(
                chat.id,
                f"🚩 {reporter.mention_html()} একটি মেসেজ রিপোর্ট করেছেন ({target.mention_html()})।\n"
                f"কারণ: {reason}\n{mentions}",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=target_msg.message_id,
            )
        except TelegramError:
            pass


async def _send_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz: bool) -> None:
    msg = update.message
    chat = update.effective_chat
    cmd = "/quiz" if quiz else "/poll"
    example = "/quiz প্রশ্ন | ক | *খ | গ   (সঠিক উত্তরের আগে *)" if quiz else "/poll প্রশ্ন | অপশন১ | অপশন২ | অপশন৩"

    parts = (msg.text or "").split(None, 1)
    if len(parts) < 2:
        await msg.reply_text(f"ℹ️ ব্যবহার:\n{example}\n(অপশন ২–১০টি, | দিয়ে আলাদা)")
        return
    try:
        question, options, correct = security.parse_poll(parts[1], quiz)
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}\n\n{example}")
        return

    try:
        if quiz:
            await context.bot.send_poll(
                chat.id, question, options,
                type="quiz", correct_option_id=correct, is_anonymous=True,
            )
        else:
            await context.bot.send_poll(chat.id, question, options, is_anonymous=True)
    except TelegramError as exc:
        await msg.reply_text(f"⚠️ {cmd} পাঠানো যায়নি: {exc}")
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


@group_only
@admin_only
async def cmd_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_poll_command(update, context, quiz=False)


@group_only
@admin_only
async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_poll_command(update, context, quiz=True)


# ══════════════════════════════════════════════
# Background jobs (post_init থেকে শুরু হয়)
# ══════════════════════════════════════════════

async def captcha_timeout_job(app: Application):
    """প্রতি ৩০ সেকেন্ডে captcha timeout চেক করে kick করে।"""
    while True:
        await asyncio.sleep(30)
        try:
            expired = captcha_module.get_expired_captchas()
            for gid, uid, data in expired:
                captcha_module.remove_captcha(gid, uid)
                try:
                    await app.bot.ban_chat_member(gid, uid)
                    await app.bot.unban_chat_member(gid, uid)
                    # Captcha message delete
                    try:
                        await app.bot.delete_message(gid, data["msg_id"])
                    except TelegramError:
                        pass
                    await app.bot.send_message(
                        gid,
                        f"⏰ Captcha সময় শেষ — <b>{data['name']}</b> কে kick করা হয়েছে।",
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info("Captcha kick: group=%s user=%s", gid, uid)
                except TelegramError as exc:
                    logger.warning("Captcha kick failed: %s", exc)
        except Exception as exc:
            logger.error("Captcha timeout job error: %s", exc)

async def warning_expiry_job(app: Application):
    """প্রতিদিন একবার সব গ্রুপের পুরনো warning expire করে।"""
    while True:
        await asyncio.sleep(24 * 3600)  # ২৪ ঘণ্টা পর পর
        try:
            groups = database.get_group_list()
            for g in groups:
                gid = int(g["id"])
                features = database.get_features(gid)
                if not features.get("warn_expire", True):
                    continue
                # এই গ্রুপের সব user এর warning check করো
                try:
                    warnings_data = database._ref(f"/groups/{gid}/warnings").get() or {}
                    for uid_str in warnings_data.keys():
                        try:
                            spam_filter.apply_warning_expiry(gid, int(uid_str), database)
                        except Exception:
                            pass
                except Exception as exc:
                    logger.debug("Expiry job group %s failed: %s", gid, exc)
            logger.info("Warning expiry job completed for %d groups", len(groups))
        except Exception as exc:
            logger.error("Warning expiry job error: %s", exc)


async def _on_startup(app: Application) -> None:
    """Polling শুরুর ঠিক আগে — চলমান event loop-এর ভেতরে।"""
    tasks = [
        asyncio.create_task(health_check_server(), name="health"),
        asyncio.create_task(captcha_timeout_job(app), name="captcha"),
        asyncio.create_task(warning_expiry_job(app), name="warn_expiry"),
        asyncio.create_task(scheduler.run_scheduler(app.bot), name="scheduler"),
    ]
    app.bot_data["_bg_jobs"] = tasks
    for t in tasks:
        t.add_done_callback(_log_job_exit)


def _log_job_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background job %s crashed: %r", task.get_name(), exc)


async def _on_shutdown(app: Application) -> None:
    for t in app.bot_data.get("_bg_jobs", []):
        t.cancel()
    await asyncio.gather(*app.bot_data.get("_bg_jobs", []), return_exceptions=True)


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

def main() -> None:
    """
    sync entry point — app.run_polling() নিজেই event loop বানায় ও signal সামলায়।
    (আগে async main()-এর ভেতর asyncio.gather-এ run_polling ছিল; PTB 21.x-এ সেটা
    চলমান loop-এর ভেতর থেকে চালানো যায় না, তাই বট শুরুতেই থেমে যেত।)
    Background কাজ (health server, captcha, warning expiry, scheduler) চলে
    post_init hook থেকে, আর post_shutdown-এ বন্ধ হয়।
    """
    if not database.init():
        logger.critical("Firebase init failed — exiting.")
        return

    config.validate()

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Owner commands
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("globalban", cmd_globalban))

    # Admin commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("resetwarn", cmd_resetwarn))
    app.add_handler(CommandHandler("warnings", cmd_warnings))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("setgoodbye", cmd_setgoodbye))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("setlogchannel", cmd_setlogchannel))
    app.add_handler(CommandHandler("allowdomain", cmd_allowdomain))
    app.add_handler(CommandHandler("blockdomain", cmd_blockdomain))
    app.add_handler(CommandHandler("allowlist", cmd_allowlist))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))
    app.add_handler(CommandHandler("spamstats", cmd_spamstats))

    # Step 4: Captcha & Member Management
    app.add_handler(CommandHandler("trust", cmd_trust))
    app.add_handler(CommandHandler("untrust", cmd_untrust))
    app.add_handler(CommandHandler("vip", cmd_vip))
    app.add_handler(CommandHandler("unvip", cmd_unvip))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mutelist", cmd_mutelist))
    app.add_handler(CommandHandler("banlist", cmd_banlist))
    app.add_handler(CommandHandler("kickinactive", cmd_kickinactive))

    # Step 5: Analytics, Schedule & Auto Systems
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("exportviolations", cmd_exportviolations))
    app.add_handler(CommandHandler("autodelete", cmd_autodelete))
    app.add_handler(CommandHandler("schedulerules", cmd_schedulerules))
    app.add_handler(CommandHandler("schedulebroadcast", cmd_schedulebroadcast))
    app.add_handler(CommandHandler("schedulelist", cmd_schedulelist))
    app.add_handler(CommandHandler("schedulecancel", cmd_schedulecancel))

    # Step 6: Advanced Security
    app.add_handler(CommandHandler("slowmode", cmd_slowmode))
    app.add_handler(CommandHandler("anticlone", cmd_anticlone))
    app.add_handler(CommandHandler("voicescan", cmd_voicescan))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("poll", cmd_poll))
    app.add_handler(CommandHandler("quiz", cmd_quiz))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Welcome/Goodbye (ChatMember updates)
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Message moderation
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_message,
    ))

    logger.info("Bot starting — polling...")
    app.run_polling(
        allowed_updates=["message", "chat_member", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
