"""
bot.py — Group Protection Bot (Multi-group, Multi-feature)
Owner: একজন (BOT_OWNER_ID)
Admin: প্রতিটা গ্রুপের admin রা
Member: কোনো কমান্ড নেই
"""

from __future__ import annotations

import asyncio
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

        # Anti-bot: খুব নতুন account
        if features.get("anti_bot", True) and user.id:
            # account age check করা যায় না সরাসরি, username ছাড়া account সন্দেহজনক
            pass

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

    # Admin bypass
    user_is_admin = await is_admin(context.bot, group_id, user.id)

    features = database.get_features(group_id)

    try:
        settings = database.get_settings_strict(group_id)
        allowed_domains = database.get_allowed_domains_strict(group_id)
        blocked_domains = database.get_blocked_domains_strict(group_id)
    except Exception as exc:
        logger.error("DB read failed — moderation skipped: %s", exc)
        return

    text_parts = []
    if msg.text:
        text_parts.append(msg.text)
    if msg.caption:
        text_parts.append(msg.caption)
    full_text = " ".join(text_parts)

    violation_reason: str | None = None

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

    # ── Text moderation ──
    if not violation_reason and features.get("text_moderation", True) and full_text:
        result = await moderation.moderate_text_async(full_text)
        if result.is_violation and not user_is_admin:
            violation_reason = result.reason or "অনুপযুক্ত টেক্সট"

    # ── URL filter ──
    if not violation_reason and features.get("url_filter", True) and full_text:
        url_result = moderation.moderate_urls(full_text, blocked_domains, allowed_domains)
        if url_result.is_violation and not user_is_admin:
            violation_reason = url_result.reason or "অনুমোদিত নয় এমন লিংক"

    # ── Media moderation ──
    if not violation_reason and features.get("media_moderation", True):
        media_result = await moderation.moderate_media(msg)
        if media_result and media_result.is_violation and not user_is_admin:
            violation_reason = media_result.reason or "অনুপযুক্ত মিডিয়া"

    if not violation_reason:
        database.mark_as_processed(group_id, msg_id)
        return

    # ── Violation handle ──
    database.mark_as_processed(group_id, msg_id)

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
        handler = warn_system.ViolationHandler(
            context.bot, group_id, user.id, user.full_name,
            msg_id, violation_reason, settings,
        )
        warn_data = await handler.handle()

        # Log channel-এ পাঠাও
        await send_log(
            context.bot, group_id,
            f"⚠️ <b>Violation</b>\n"
            f"👤 {user.mention_html()} (ID: {user.id})\n"
            f"📝 কারণ: {violation_reason}\n"
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
    settings = database.get_settings(msg.chat.id)
    handler = warn_system.ViolationHandler(
        context.bot, msg.chat.id, target.id, target.full_name,
        msg.reply_to_message.message_id, reason, settings,
    )
    await handler.handle()


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
    rec = database.get_warnings(msg.chat.id, target.id)
    settings = database.get_settings(msg.chat.id)
    await msg.reply_text(
        f"⚠️ {target.mention_html()} এর সতর্কতা: "
        f"{rec.get('count', 0)}/{settings.get('max_warnings', 3)}",
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
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text("🔒 গ্রুপ লক করা হয়েছে। শুধু অ্যাডমিন মেসেজ দিতে পারবেন।")
    except TelegramError as exc:
        await update.message.reply_text(f"⚠️ Lock করতে ব্যর্থ: {exc}")


@group_only
@admin_only
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await update.message.reply_text("🔓 গ্রুপ আনলক করা হয়েছে।")
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
        )
    else:
        text = "ℹ️ এই বট গ্রুপ রক্ষা করে। /rules দিয়ে নিয়ম দেখুন।"

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
# Main
# ══════════════════════════════════════════════

async def main() -> None:
    if not database.init():
        logger.critical("Firebase init failed — exiting.")
        return

    config.validate()

    app = Application.builder().token(config.BOT_TOKEN).build()

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

    # Callback buttons
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Welcome/Goodbye (ChatMember updates)
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Message moderation
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_message,
    ))

    await asyncio.gather(
        health_check_server(),
        app.run_polling(
            allowed_updates=["message", "chat_member", "callback_query"],
            drop_pending_updates=True,
        ),
    )


if __name__ == "__main__":
    logger.info("Bot starting — polling...")
    asyncio.run(main())
