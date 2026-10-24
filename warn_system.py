"""
warn_system.py — Warning enforcement, mute operations, and dedup protection.
(File renamed from warnings.py to avoid shadowing Python's stdlib warnings module.)

KEY DESIGN DECISIONS (see AUDIT-REPORT.md for full rationale):

  BUG-2 FIX — Dedup ordering:
    mark_as_processed() is now called AFTER a successful warning increment,
    not at the top of handle(). An interrupted workflow (crash between dedup-mark
    and DB write) previously caused permanent message suppression with no warning
    issued. New order: check → delete → increment → mark-processed → notify.

  BUG-3 FIX — Admin bypass is now configurable:
    The "admin_bypass_delete_only" group setting controls behaviour:
      True  (default) — admin violations: delete message, no warning (old behaviour).
      False           — admin violations: delete + warn like any other user.
    The legacy "log_admin_violations" key in DB is retired; new key is
    "admin_bypass_delete_only".

  BUG-8 (partial) — Sightengine video:
    Degraded-media policy is enforced by bot.py; warn_system.py only receives
    confirmed violations and degraded-media block decisions.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Callable, Awaitable

import database
import config

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Warning messages (Bengali)
# ──────────────────────────────────────────────

def warning_message(count: int, max_warnings: int, reason: str) -> str:
    """HTML parse mode-এর জন্য। reason escape করা হয়।"""
    return (
        f"⚠️ <b>সতর্কবার্তা {count}/{max_warnings}</b>\n"
        f"কারণ: {html.escape(reason)}\n"
        "দয়া করে গ্রুপের নিয়ম মেনে চলুন।"
    )


def mute_message(user_mention: str, minutes: int, reason: str) -> str:
    hours = minutes // 60
    duration_text = f"{hours} ঘণ্টার" if hours >= 1 else f"{minutes} মিনিটের"
    return (
        f"🔇 {user_mention} <b>{duration_text} জন্য নিষিদ্ধ (muted)</b>\n"
        f"কারণ: {html.escape(reason)}\n"
        "সতর্কতা সীমা অতিক্রমের পর স্বয়ংক্রিয়ভাবে mute করা হয়েছে।"
    )


# ──────────────────────────────────────────────
# Core enforcement
# ──────────────────────────────────────────────

class ViolationHandler:
    """
    Processes a confirmed moderation violation.

    Parameters
    ----------
    delete_message_fn : async (chat_id, message_id) → bool
    mute_user_fn      : async (chat_id, user_id, until_ts) → bool
    send_message_fn   : async (chat_id, text, parse_mode) → None
    is_admin_fn       : async (chat_id, user_id) → bool

    সব message HTML parse mode-এ যায়; user_mention অবশ্যই HTML mention
    (যেমন user.mention_html()) হতে হবে।
    """

    def __init__(
        self,
        delete_message_fn: Callable[..., Awaitable[bool]],
        mute_user_fn: Callable[..., Awaitable[bool]],
        send_message_fn: Callable[..., Awaitable[None]],
        is_admin_fn: Callable[..., Awaitable[bool]],
    ):
        self._delete = delete_message_fn
        self._mute = mute_user_fn
        self._send = send_message_fn
        self._is_admin = is_admin_fn

    async def handle(
        self,
        group_id: int,
        user_id: int,
        message_id: int,
        user_mention: str,
        reason: str,
        *,
        check_dedup: bool = True,
        delete_message: bool = True,
    ) -> dict:
        """
        check_dedup    : False দিলে ধাপ ১ বাদ যায়। bot.py-এর handle_message নিজেই
                         আগে mark_as_processed() করে, তাই সেখান থেকে False দিতে হয়
                         (নইলে handler নিজেকেই duplicate ভেবে skip করবে)। /warn-এও
                         False — কারণ reply করা মেসেজ আগেই processed হয়ে থাকতে পারে।
        delete_message : False দিলে মেসেজ delete করা হয় না (caller আগেই করেছে)।

        Returns the warning record (e.g. {"count": 2, ...}); {} if skipped/failed.

        Full violation pipeline — order is critical:

          1. Dedup check   — skip if already fully processed.
          2. Load settings — fetch group config (needed for admin policy too).
          3. Admin check   — configurable: delete-only or full warning.
          4. Delete msg    — attempt deletion; log but continue on failure.
          5. Increment     — atomic Firebase write; bail with error msg on failure.
          6. Mark dedup    — only now mark as processed (crash-safe: if we crash
                             before this line the message can be re-processed).
          7. Notify        — send warning or mute notification.

        Why dedup is LAST (BUG-2 fix):
          Previously mark_as_processed() ran at step 1 before the warning was
          written. A crash between steps 1 and 5 would permanently suppress the
          message without ever issuing a warning. By marking only after a
          successful DB write, a crash is recoverable: the next Telegram update
          delivery retries the full pipeline and the warning is issued correctly.
          Duplicate Telegram deliveries of the same message_id within the TTL
          window are still deduplicated because is_already_processed() returns
          True on the second call (step 1 of the retry).
        """

        # ── 1. Dedup check ────────────────────────────────────────────────
        if check_dedup and database.is_already_processed(group_id, message_id):
            logger.debug("Duplicate event for msg %s in group %s — skipped.", message_id, group_id)
            return {}

        # ── 2. Load group settings ────────────────────────────────────────
        settings = database.get_settings(group_id)
        max_warnings: int = settings.get("max_warnings", config.MAX_WARNINGS)
        mute_minutes: int = settings.get("mute_minutes", config.DEFAULT_MUTE_MINUTES)
        # BUG-3 FIX: read configurable admin-bypass policy
        admin_bypass_delete_only: bool = settings.get("admin_bypass_delete_only", True)

        # ── 3. Admin check ────────────────────────────────────────────────
        try:
            user_is_admin = await self._is_admin(group_id, user_id)
        except Exception as exc:
            logger.warning("Admin check failed for user %s: %s — proceeding as non-admin.", user_id, exc)
            user_is_admin = False

        if user_is_admin and admin_bypass_delete_only:
            logger.info(
                "Admin %s in group %s violated policy — deleting message only (admin_bypass_delete_only=True).",
                user_id, group_id,
            )
            if delete_message:
                await self._delete(group_id, message_id)
            # Mark processed so we don't retry this admin message either
            database.mark_as_processed(group_id, message_id)
            return {}

        # ── 4. Delete the offending message ──────────────────────────────
        if delete_message:
            deleted = await self._delete(group_id, message_id)
            if not deleted:
                logger.warning(
                    "Could not delete msg %s in group %s — bot may lack Delete Messages permission.",
                    message_id, group_id,
                )
                # Continue: still issue the warning even if deletion failed.

        # ── 5. Increment warning (atomic Firebase transaction) ────────────
        try:
            record = database.increment_warning(group_id, user_id, reason)
        except RuntimeError as exc:
            logger.error("Warning increment failed for user %s in group %s: %s", user_id, group_id, exc)
            await self._send(
                group_id,
                "⚠️ ডেটাবেস ত্রুটি — সতর্কতা সংরক্ষণ করা যায়নি। অ্যাডমিনকে জানান।",
                "HTML",
            )
            # Do NOT mark as processed — allow retry when DB recovers.
            return {}

        count: int = record.get("count", 1)

        # ── 6. Mark as processed (AFTER successful DB write) ─────────────
        database.mark_as_processed(group_id, message_id)

        # ── 7. Notify ─────────────────────────────────────────────────────
        if count < max_warnings:
            msg = warning_message(count, max_warnings, reason)
            await self._send(group_id, f"{user_mention}\n{msg}", "HTML")

        else:
            # Threshold reached — attempt mute
            until_ts = time.time() + mute_minutes * 60
            muted = await self._mute(group_id, user_id, until_ts)

            if muted:
                try:
                    database.set_muted_until(group_id, user_id, until_ts)
                except RuntimeError as exc:
                    logger.error(
                        "Mute applied in Telegram but not saved to DB for user %s: %s",
                        user_id, exc,
                    )

                msg = warning_message(count, max_warnings, reason)
                mute_msg = mute_message(user_mention, mute_minutes, reason)
                await self._send(
                    group_id,
                    f"{user_mention}\n{msg}\n\n{mute_msg}",
                    "HTML",
                )
            else:
                logger.error(
                    "Telegram mute failed for user %s in group %s — bot lacks Restrict Members permission?",
                    user_id, group_id,
                )
                msg = warning_message(count, max_warnings, reason)
                await self._send(
                    group_id,
                    f"{user_mention}\n{msg}\n"
                    "⚠️ Bot-এর Restrict Members permission নেই — mute করা সম্ভব হয়নি। "
                    "Bot-কে Admin করে 'Restrict Members' permission দিন।",
                    "HTML",
                )

        return record
