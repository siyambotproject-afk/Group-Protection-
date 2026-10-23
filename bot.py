"""
bot.py — Startup, Telegram handlers, commands, routing, and lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Optional

from telegram import (
    Update,
    ChatPermissions,
    User,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import database
import moderation
from moderation import Verdict
from warn_system import ViolationHandler

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Health-check HTTP server (for Render Web Service)
# ──────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress access logs


def _start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", config.PORT), _HealthHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health-check server listening on port %s", config.PORT)


# ──────────────────────────────────────────────
# Telegram helper utilities
# ──────────────────────────────────────────────

def _mention(user: User) -> str:
    name = user.full_name or str(user.id)
    return f"[{name}](tg://user?id={user.id})"


async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def _delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int) -> bool:
    try:
        await context.bot.delete_message(chat_id, msg_id)
        return True
    except TelegramError as exc:
        logger.warning("Delete message %s failed: %s", msg_id, exc)
        return False


async def _mute_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    until_ts: float,
) -> bool:
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(until_ts),
        )
        return True
    except TelegramError as exc:
        logger.error("Mute user %s in %s failed: %s", user_id, chat_id, exc)
        return False


async def _send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    try:
        await context.bot.send_message(chat_id, text, parse_mode=parse_mode)
    except TelegramError as exc:
        logger.error("send_message to %s failed: %s", chat_id, exc)


def _build_handler(context: ContextTypes.DEFAULT_TYPE) -> ViolationHandler:
    return ViolationHandler(
        delete_message_fn=lambda cid, mid: _delete_message(context, cid, mid),
        mute_user_fn=lambda cid, uid, ts: _mute_user(context, cid, uid, ts),
        send_message_fn=lambda cid, text, pm: _send(context, cid, text, pm),
        is_admin_fn=lambda cid, uid: _is_admin(context, cid, uid),
    )


# ──────────────────────────────────────────────
# Message handler (moderation pipeline)
# ──────────────────────────────────────────────

async def _get_media_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> Optional[bytes]:
    """Download Telegram media via the Bot API. Returns None on failure."""
    try:
        tg_file = await context.bot.get_file(file_id)
        buf = await tg_file.download_as_bytearray()
        return bytes(buf)
    except TelegramError as exc:
        logger.warning("Media download failed for file_id %s: %s", file_id, exc)
        return None


async def _check_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: dict,
) -> Optional[moderation.ModerationResult]:
    """
    Inspect any media attached to the message.
    Returns a ModerationResult or None if the message contains no inspectable media.

    DEGRADED is returned (never None) when media exists but cannot be downloaded
    or inspected — ensuring the fallback policy is applied rather than silently
    treating uninspected media as safe.

    Media type → endpoint mapping:
      photo     → Sightengine /check.json   (image)
      video     → Sightengine /video/check-sync.json  (sync video, < 60 s)
      animation → Sightengine /check.json   (GIF via image endpoint)
      sticker   → static WebP inspected as image; animated/video stickers
                  that cannot be reliably decoded return DEGRADED (fail closed)
    """
    msg = update.effective_message

    # ── Photo ─────────────────────────────────────────────────────────────
    if msg.photo:
        largest = max(msg.photo, key=lambda p: p.file_size or 0)
        data = await _get_media_bytes(context, largest.file_id)
        if data is None:
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "ছবি ডাউনলোড ব্যর্থ — পরীক্ষা করা যায়নি।",
            )
        return await moderation.moderate_image_bytes(data)

    # ── Video ──────────────────────────────────────────────────────────────
    # Size is checked inside moderate_video_bytes; no pre-check here to avoid
    # two different code paths returning DEGRADED for the same reason.
    if msg.video:
        data = await _get_media_bytes(context, msg.video.file_id)
        if data is None:
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "ভিডিও ডাউনলোড ব্যর্থ — পরীক্ষা করা যায়নি।",
            )
        return await moderation.moderate_video_bytes(data)

    # ── Animation / GIF ───────────────────────────────────────────────────
    if msg.animation:
        data = await _get_media_bytes(context, msg.animation.file_id)
        if data is None:
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "GIF ডাউনলোড ব্যর্থ — পরীক্ষা করা যায়নি।",
            )
        return await moderation.moderate_gif_bytes(data)

    # ── Stickers ───────────────────────────────────────────────────────────
    if msg.sticker:
        data = await _get_media_bytes(context, msg.sticker.file_id)
        if data is None:
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "স্টিকার ডাউনলোড ব্যর্থ — পরীক্ষা করা যায়নি।",
            )
        if getattr(msg.sticker, "is_video", False):
            # Telegram video stickers are WebM; do not upload them as MP4 or
            # pretend the video API inspected an unsupported container.
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "ভিডিও স্টিকার (WebM) সরাসরি নির্ভরযোগ্যভাবে পরীক্ষা করা যায়নি।",
            )
        if getattr(msg.sticker, "is_animated", False):
            # TGS/vector animation is not a raster image; fail closed.
            return moderation.ModerationResult(
                Verdict.DEGRADED,
                "অ্যানিমেটেড TGS স্টিকার সরাসরি পরীক্ষা করা যায়নি।",
            )
        return await moderation.moderate_image_bytes(
            data, filename="sticker.webp", content_type="image/webp"
        )

    return None


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main moderation pipeline for group messages."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if user.is_bot:
        return

    group_id = chat.id
    user_id = user.id
    message_id = msg.message_id
    # Make it obvious in Render logs whether Telegram is delivering group messages.
    # This is intentionally INFO-level so it works with the default LOG_LEVEL.
    logger.info(
        "Moderation received group message: chat_id=%s message_id=%s user_id=%s has_text=%s has_media=%s",
        group_id,
        message_id,
        user_id,
        bool(msg.text or msg.caption),
        bool(msg.photo or msg.video or msg.animation or msg.sticker or msg.document),
    )

    # Read DB values with explicit failure detection. Legacy wrappers intentionally
    # return defaults on errors, so the moderation path uses strict readers.
    db_read_degraded = False
    try:
        settings = database.get_settings_strict(group_id)
        group_blocked = database.get_blocked_domains_strict(group_id)
        group_allowed = database.get_allowed_domains_strict(group_id)
    except Exception as exc:
        db_read_degraded = True
        logger.error("DB read failed for group %s; applying fail-closed local defaults: %s", group_id, exc)
        settings = {
            "mute_minutes": config.DEFAULT_MUTE_MINUTES,
            "max_warnings": config.MAX_WARNINGS,
            "media_fallback_policy": "block",
            "admin_bypass_delete_only": config.ADMIN_BYPASS_DELETE_ONLY,
        }
        group_blocked = []
        group_allowed = []

    handler = _build_handler(context)

    # Gather all text (body + caption)
    text_parts = []
    if msg.text:
        text_parts.append(msg.text)
    if msg.caption:
        text_parts.append(msg.caption)
    full_text = " ".join(text_parts)

    # — Text moderation
    if full_text:
        result = moderation.moderate_text(full_text)
        if result.is_violation:
            await handler.handle(
                group_id, user_id, message_id, _mention(user), result.reason
            )
            return

        # If Firebase failed, custom block/allow lists are unknown. Do not allow
        # URLs to bypass moderation while their group policy cannot be read.
        if db_read_degraded and moderation.extract_urls(full_text):
            await handler.handle(
                group_id, user_id, message_id, _mention(user),
                "গ্রুপের ডোমেইন সেটিংস সাময়িকভাবে পড়া যায়নি — নিরাপত্তার জন্য লিংকযুক্ত বার্তাটি মুছে ফেলা হয়েছে।",
            )
            return

        # — URL moderation (includes caption URLs)
        result = moderation.moderate_urls(full_text, group_blocked, group_allowed)
        if result.is_violation:
            await handler.handle(
                group_id, user_id, message_id, _mention(user), result.reason
            )
            return

    # — Media moderation
    media_result = await _check_media(update, context, settings)
    if media_result is not None:
        if media_result.is_violation:
            await handler.handle(
                group_id, user_id, message_id, _mention(user), media_result.reason
            )
            return

        if media_result.is_degraded:
            # BUG-2 FIX: 3-way fallback policy (was boolean, now string)
            # Per-group DB value "media_fallback_policy" overrides process default.
            fallback = ("block" if db_read_degraded else settings.get(
                "media_fallback_policy", config.MEDIA_FALLBACK_POLICY
            )).lower()

            logger.warning(
                "Media inspection DEGRADED for msg %s in group %s. "
                "Reason: %s. Fallback policy: %s.",
                message_id, group_id, media_result.reason, fallback,
            )

            if fallback == "block":
                await handler.handle(
                    group_id, user_id, message_id, _mention(user),
                    f"মিডিয়া পরীক্ষা করা যায়নি — নীতি (block) অনুযায়ী মুছে ফেলা হয়েছে। "
                    f"কারণ: {media_result.reason}",
                )
            elif fallback == "notify":
                # Delete the media but don't issue a user warning
                try:
                    await context.bot.delete_message(group_id, message_id)
                except Exception as exc:
                    logger.warning("notify-fallback delete failed: %s", exc)
                await _send(
                    context, group_id,
                    f"⚠️ *মিডিয়া পরীক্ষা ব্যর্থ* (notify নীতি)\n"
                    f"বার্তা মুছে ফেলা হয়েছে, কিন্তু কোনো সতর্কতা জারি হয়নি।\n"
                    f"কারণ: {media_result.reason}\n"
                    "অ্যাডমিনরা পরিস্থিতি যাচাই করুন।",
                )
            else:  # "allow" — default
                logger.info(
                    "Media degraded for msg %s in group %s — allowed by 'allow' fallback policy.",
                    message_id, group_id,
                )


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *গ্রুপ প্রোটেকশন বট সক্রিয়!*\n\n"
        "আমি এই গ্রুপকে অশ্লীল কন্টেন্ট, ক্ষতিকর লিংক এবং "
        "স্ক্যাম থেকে রক্ষা করি।\n\n"
        "/help — সকল কমান্ড দেখুন",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*📋 কমান্ড তালিকা*\n\n"
        "*সাধারণ কমান্ড:*\n"
        "/start — বট পরিচিতি\n"
        "/help — এই বার্তা\n"
        "/status — বটের বর্তমান অবস্থা\n"
        "/warnings — নিজের বা অন্যের সতর্কতা দেখুন\n\n"
        "*অ্যাডমিন কমান্ড:*\n"
        "/settings — গ্রুপ সেটিংস দেখুন\n"
        "/warn — কোনো ইউজারকে সতর্ক করুন (reply করে)\n"
        "/unwarn — একটি সতর্কতা কমান (reply করে)\n"
        "/resetwarn — সব সতর্কতা রিসেট করুন (reply করে)\n"
        "/allowdomain <domain> — domain অনুমতি দিন\n"
        "/blockdomain <domain> — domain ব্লক করুন\n"
        "/allowlist — অনুমোদিত domain তালিকা\n"
        "/blocklist — নিষিদ্ধ domain তালিকা",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("এই কমান্ড শুধুমাত্র গ্রুপে ব্যবহার করা যাবে।")
        return

    settings = database.get_settings(chat.id)
    ai_status = "✅ সক্রিয়" if config.SIGHTENGINE_AVAILABLE else "⚠️ নিষ্ক্রিয় (credential নেই)"
    db_status = "✅ সংযুক্ত"

    fallback_policy = settings.get("media_fallback_policy", config.MEDIA_FALLBACK_POLICY)
    admin_bypass = settings.get("admin_bypass_delete_only", config.ADMIN_BYPASS_DELETE_ONLY)
    await update.message.reply_text(
        f"*🤖 বট স্ট্যাটাস*\n\n"
        f"AI মডারেশন: {ai_status}\n"
        f"ডেটাবেস: {db_status}\n"
        f"সতর্কতা সীমা: {settings.get('max_warnings', config.MAX_WARNINGS)}\n"
        f"Mute সময়: {settings.get('mute_minutes', config.DEFAULT_MUTE_MINUTES)} মিনিট\n"
        f"মিডিয়া fallback নীতি: `{fallback_policy}` "
        f"(allow=অনুমতি, block=ব্লক, notify=শুধু অ্যাডমিন নোটিফিকেশন)\n"
        f"অ্যাডমিন bypass: {'শুধু ডিলিট' if admin_bypass else 'পূর্ণ সতর্কতা'}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if not await _is_admin(context, chat.id, user.id):
        await update.message.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    settings = database.get_settings(chat.id)
    await update.message.reply_text(
        f"*⚙️ গ্রুপ সেটিংস*\n\n"
        f"`max_warnings`: {settings.get('max_warnings')}\n"
        f"`mute_minutes`: {settings.get('mute_minutes')}\n"
        f"`media_fallback_policy`: `{settings.get('media_fallback_policy')}` "
        f"(allow / block / notify)\n"
        f"`admin_bypass_delete_only`: {settings.get('admin_bypass_delete_only')}\n\n"
        "Firebase-এ সরাসরি সেটিংস পরিবর্তন করা যায়।",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    if not await _is_admin(context, chat.id, user.id):
        await msg.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    if not msg.reply_to_message:
        await msg.reply_text("ℹ️ কাউকে warn করতে তার বার্তায় reply করে /warn লিখুন।")
        return

    target = msg.reply_to_message.from_user
    if not target:
        await msg.reply_text("ℹ️ ইউজার শনাক্ত করা যায়নি।")
        return

    handler = _build_handler(context)
    await handler.handle(
        chat.id, target.id,
        msg.reply_to_message.message_id,
        _mention(target),
        "অ্যাডমিন কর্তৃক ম্যানুয়াল সতর্কতা",
    )


async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or not msg or not user:
        return

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    else:
        target = user

    try:
        record = database.get_warnings(chat.id, target.id)
    except Exception:
        await msg.reply_text("⚠️ ডেটাবেস পড়তে ব্যর্থ।")
        return

    count = record.get("count", 0)
    muted_until = record.get("muted_until", 0)
    mute_info = ""
    if muted_until and muted_until > time.time():
        remaining = int((muted_until - time.time()) / 60)
        mute_info = f"\n🔇 আরও {remaining} মিনিট muted"

    await msg.reply_text(
        f"📊 *{target.full_name}* এর সতর্কতা: {count}/{config.MAX_WARNINGS}{mute_info}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    if not await _is_admin(context, chat.id, user.id):
        await msg.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার সতর্কতা কমাতে চান তার বার্তায় reply করুন।")
        return

    target = msg.reply_to_message.from_user
    try:
        record = database.decrement_warning(chat.id, target.id)
    except RuntimeError:
        await msg.reply_text("⚠️ ডেটাবেস আপডেট ব্যর্থ।")
        return

    await msg.reply_text(
        f"✅ {_mention(target)} এর একটি সতর্কতা কমানো হয়েছে। "
        f"বর্তমান: {record.get('count', 0)}/{config.MAX_WARNINGS}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_resetwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    if not await _is_admin(context, chat.id, user.id):
        await msg.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("ℹ️ কার সতর্কতা রিসেট করতে চান তার বার্তায় reply করুন।")
        return

    target = msg.reply_to_message.from_user
    try:
        database.reset_warnings(chat.id, target.id)
    except RuntimeError:
        await msg.reply_text("⚠️ রিসেট ব্যর্থ হয়েছে।")
        return

    await msg.reply_text(
        f"✅ {_mention(target)} এর সকল সতর্কতা রিসেট করা হয়েছে।",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_allowdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    if not await _is_admin(context, chat.id, user.id):
        await msg.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    args = context.args
    if not args:
        await msg.reply_text("ব্যবহার: /allowdomain <domain>\nউদাহরণ: /allowdomain example.com")
        return

    domain = args[0].lower().lstrip("www.")
    database.add_allowed_domain(chat.id, domain)
    await msg.reply_text(f"✅ `{domain}` অনুমোদিত তালিকায় যোগ করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)


async def cmd_blockdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    if not await _is_admin(context, chat.id, user.id):
        await msg.reply_text("⛔ শুধুমাত্র অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।")
        return

    args = context.args
    if not args:
        await msg.reply_text("ব্যবহার: /blockdomain <domain>\nউদাহরণ: /blockdomain badsite.com")
        return

    domain = args[0].lower().lstrip("www.")
    database.add_blocked_domain(chat.id, domain)
    await msg.reply_text(f"✅ `{domain}` নিষিদ্ধ তালিকায় যোগ করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)


async def cmd_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    domains = database.get_allowed_domains(chat.id)
    if domains:
        text = "✅ *অনুমোদিত domain তালিকা:*\n" + "\n".join(f"• `{d}`" for d in domains)
    else:
        text = "ℹ️ কোনো কাস্টম অনুমোদিত domain নেই।"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    domains = database.get_blocked_domains(chat.id)
    if domains:
        text = "🚫 *নিষিদ্ধ domain তালিকা:*\n" + "\n".join(f"• `{d}`" for d in domains)
    else:
        text = "ℹ️ কোনো কাস্টম নিষিদ্ধ domain নেই।"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ──────────────────────────────────────────────
# Error handler
# ──────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ──────────────────────────────────────────────
# Application setup & entry point
# ──────────────────────────────────────────────

def main() -> None:
    # 1. Validate config
    errors = config.validate()
    if errors:
        for err in errors:
            logger.critical("CONFIG ERROR: %s", err)
        sys.exit(1)

    # 2. Initialise Firebase
    if not database.init():
        logger.critical("Firebase initialisation failed — cannot start.")
        sys.exit(1)

    # 3. Start health-check server
    _start_health_server()

    # 4. Build and configure the Telegram application
    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("warn", cmd_warn))
    application.add_handler(CommandHandler("warnings", cmd_warnings))
    application.add_handler(CommandHandler("unwarn", cmd_unwarn))
    application.add_handler(CommandHandler("resetwarn", cmd_resetwarn))
    application.add_handler(CommandHandler("allowdomain", cmd_allowdomain))
    application.add_handler(CommandHandler("blockdomain", cmd_blockdomain))
    application.add_handler(CommandHandler("allowlist", cmd_allowlist))
    application.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # Message moderation (all group content)
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot starting — polling...")
    # Python 3.14 no longer creates a current event loop implicitly. Some
    # python-telegram-bot versions call get_event_loop() inside run_polling(),
    # which otherwise crashes before polling starts. Create one explicitly when
    # the process has none; on older Python versions this leaves the existing
    # loop untouched.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("Created an asyncio event loop for polling startup.")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
