"""
moderation.py — URL normalisation, text rules, and media inspection.

Sightengine API endpoints (verified against official documentation):

  IMAGE:        POST https://api.sightengine.com/1.0/check.json
                Field: media (bytes), models, api_user, api_secret
                Response: { status, nudity:{sexual_activity,sexual_display,erotica,...},
                            offensive:{prob,...} }
                Source: https://sightengine.com/docs/getstarted

  VIDEO (sync): POST https://api.sightengine.com/1.0/video/check-sync.json
                Field: media (bytes), models, api_user, api_secret
                Constraint: video must be < 60 seconds
                Response: { status, data:{ frames:[{info:{id,position},
                            nudity:{sexual_activity,...}, offensive:{prob}},...] } }
                Source: https://sightengine.com/docs/moderate-stored-video
                Verified: 2026-10-23

  GIF:          Same as IMAGE endpoint — Sightengine accepts GIFs via /check.json.

  NOTE: /video/check-sync.json IS the correct documented sync endpoint for videos
  up to 60 seconds. It is NOT enterprise-only; it is available to all accounts.
  The PREVIOUS BUG-4 "fix" (v1.1) replaced this correct endpoint with /check.json
  (image endpoint), which is WRONG for video — the image endpoint does not accept
  video files and returns a different response schema. This file corrects that error.

Returns ModerationResult objects. Never raises on API failures —
instead returns DEGRADED so the caller can apply the fallback policy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse, unquote

import aiohttp

import config

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────

class Verdict(Enum):
    ALLOW    = "allow"
    BLOCK    = "block"
    DEGRADED = "degraded"   # could not inspect; caller applies fallback policy


@dataclass
class ModerationResult:
    verdict: Verdict
    reason:  str  = ""
    details: dict = field(default_factory=dict)

    @property
    def is_violation(self) -> bool:
        return self.verdict == Verdict.BLOCK

    @property
    def is_degraded(self) -> bool:
        return self.verdict == Verdict.DEGRADED


ALLOW = ModerationResult(Verdict.ALLOW)


# ──────────────────────────────────────────────
# Built-in domain lists
# ──────────────────────────────────────────────

_DEFAULT_BLOCKLIST: set[str] = {
    # Adult
    "pornhub.com", "xvideos.com", "xnxx.com", "redtube.com",
    "youporn.com", "tube8.com", "xtube.com", "spankbang.com",
    "xhamster.com", "brazzers.com", "bangbros.com",
    # Phishing / scam look-alikes
    "paypa1.com", "arnazon.com", "g00gle.com",
}

_DEFAULT_ALLOWLIST: set[str] = {
    "google.com", "youtube.com", "facebook.com", "twitter.com",
    "github.com", "stackoverflow.com", "wikipedia.org",
    "bbc.com", "prothomalo.com", "thedailystar.net",
    "t.me", "telegram.org",
}

_URL_SHORTENERS: set[str] = {
    "bit.ly", "tinyurl.com", "t.co", "ow.ly", "is.gd",
    "buff.ly", "adf.ly", "cutt.ly",
}


# ──────────────────────────────────────────────
# URL utilities
# ──────────────────────────────────────────────

_CONFUSABLE: dict[str, str] = {
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s",
    "@": "a", "!": "i",
}

def _deconfuse(text: str) -> str:
    return "".join(_CONFUSABLE.get(c, c) for c in text.lower())


def normalise_url(raw: str) -> Optional[str]:
    """
    Normalise a raw URL string.
    Returns the lowercase registered domain (without www.), or None if unparseable.
    """
    raw = raw.strip().rstrip(".,;:!?'\")")
    raw = unquote(raw)
    raw = unicodedata.normalize("NFKC", raw)

    # Add scheme only when none present (not starting with word://...)
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', raw):
        if "." not in raw.split("/")[0]:
            return None
        raw = "http://" + raw

    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
    except Exception:
        return None

    if not host:
        return None

    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    if "." not in host:
        return None

    return host


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from a text string."""
    pattern = re.compile(
        r"(?:https?://|www\.)\S+|"
        r"(?<!\w)[a-zA-Z0-9\-]+\.[a-z]{2,}(?:/\S*)?",
        re.IGNORECASE,
    )
    return pattern.findall(text)


def _root_domain(domain: str) -> str:
    parts = domain.rsplit(".", 2)
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


# ──────────────────────────────────────────────
# URL moderation
# ──────────────────────────────────────────────

def moderate_urls(
    text: str,
    group_blocked: list[str],
    group_allowed: list[str],
) -> ModerationResult:
    """
    Check all URLs in *text* against blocklists/allowlists.

    DEFAULT POLICY (closed/strict): any URL from an unknown domain is BLOCKED.
    Only explicitly allowlisted domains (default or group) are permitted.

    Precedence (highest → lowest):
      1. DEFAULT blocklist  — always blocks; group allowlist cannot override.
      2. GROUP blocklist    — /blockdomain entries.
      3. DEFAULT allowlist  — known-safe domains (checked BEFORE confusable to
                              prevent false positives, e.g. google.com ≠ g00gle.com).
      4. GROUP allowlist    — custom allowed domains (only if not in default blocklist).
      5. Confusable check   — look-alike domains against combined blocklist.
                              Runs AFTER allowlists so legitimate safe domains
                              are not blocked by their blocklisted look-alikes.
      6. URL shorteners     — blocked (destination is unverifiable; fail closed).
      7. Unknown domains    — BLOCKED (default closed policy).

    BUG-A FIX: Rule 7 changed from ALLOW to BLOCK.
      Root cause of the reported issue: any URL not in a known list was silently
      allowed (open policy). The intended policy is the opposite — only explicitly
      allowlisted domains pass; everything else is deleted.

    BUG-B FIX: Allowlist check (Rules 3 & 4) moved before confusable check (Rule 5).
      Previously Rule 3 (confusable) ran before Rules 4 & 5 (allowlists), causing
      legitimate allowlisted domains (e.g. google.com) to be blocked because a
      look-alike was in the blocklist (g00gle.com deconfuses to the same string).
    """
    urls = extract_urls(text)
    if not urls:
        return ALLOW

    group_blocked_set = {d.lower() for d in group_blocked}
    group_allowed_set = {d.lower() for d in group_allowed}

    for raw in urls:
        domain = normalise_url(raw)
        if domain is None:
            continue

        root = _root_domain(domain)

        # Rule 1: Default blocklist — always wins, group allowlist cannot override
        if domain in _DEFAULT_BLOCKLIST or root in _DEFAULT_BLOCKLIST:
            return ModerationResult(
                Verdict.BLOCK,
                reason=f"নিষিদ্ধ লিংক (ডিফল্ট তালিকা): {domain}",
                details={"domain": domain, "list": "default_block"},
            )

        # Rule 2: Group blocklist
        if domain in group_blocked_set or root in group_blocked_set:
            return ModerationResult(
                Verdict.BLOCK,
                reason=f"নিষিদ্ধ লিংক (গ্রুপ তালিকা): {domain}",
                details={"domain": domain, "list": "group_block"},
            )

        # BUG-B FIX — Rules 3 & 4: Allowlists checked BEFORE confusable check.
        # This prevents google.com being blocked because g00gle.com is in the blocklist.
        if (domain in _DEFAULT_ALLOWLIST or root in _DEFAULT_ALLOWLIST
                or domain in group_allowed_set or root in group_allowed_set):
            continue

        # Rule 5 (was 3): Confusable-character check against combined blocklist.
        # Only reached for domains not already in an allowlist.
        combined_blocked = _DEFAULT_BLOCKLIST | group_blocked_set
        deconfused = _deconfuse(domain)
        if any(_deconfuse(b) == deconfused for b in combined_blocked):
            return ModerationResult(
                Verdict.BLOCK,
                reason=f"সন্দেহজনক লিংক (ছদ্মবেশী ডোমেইন): {domain}",
                details={"domain": domain, "list": "confusable"},
            )

        # Rule 6: URL shorteners — blocked (destination is unverifiable; fail closed)
        if domain in _URL_SHORTENERS:
            logger.info("URL shortener blocked (destination unverifiable): %s", domain)
            return ModerationResult(
                Verdict.BLOCK,
                reason=f"URL shortener লিংক অনুমোদিত নয় (গন্তব্য যাচাইযোগ্য নয়): {domain}",
                details={"domain": domain, "list": "shortener"},
            )

        # BUG-A FIX — Rule 7: Unknown domain → BLOCK (closed/strict default policy).
        # Previously this returned ALLOW (open policy), causing the reported bug where
        # ordinary members' links (e.g. https://example.com) were never deleted.
        logger.info("Unknown domain blocked by default closed policy: %s", domain)
        return ModerationResult(
            Verdict.BLOCK,
            reason=f"অজানা লিংক — গ্রুপ নীতি অনুযায়ী অনুমোদিত নয়: {domain}",
            details={"domain": domain, "list": "unknown_blocked"},
        )

    return ALLOW


# ──────────────────────────────────────────────
# Text moderation
# ──────────────────────────────────────────────

_ALWAYS_BLOCK_PATTERNS: list[re.Pattern] = [
    # Explicit Bengali adult terms
    re.compile(r"চুদ|মাগি|রান্ডি|খানকি|বেশ্যা"),
    # Explicit adult solicitation in English (commercial/service framing)
    re.compile(
        r"sex\s*chat|nude\s*pic(?:s|ture)?|cam\s*girl|escort\s*service|"
        r"adult\s*video\s*(?:call|chat|service)|xxx\s*video",
        re.IGNORECASE,
    ),
    # Scam / fraud phrases
    re.compile(
        r"send\s+bkash|bikash\s+kore|free\s+taka|lottery\s+winner|"
        r"bkash\s+(?:kore\s+)?(?:den|deben|path\u09be\u09a8)",
        re.IGNORECASE,
    ),
]

_CONTEXT_BLOCK_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bxxx\b", re.IGNORECASE),
]

_SAFE_CONTEXT_RE: re.Pattern = re.compile(
    r"\b(doctor|physician|\u099a\u09bf\u0995\u09bf\u09ce\u09b8\u09be|hospital|"
    r"\u09b8\u09cd\u09ac\u09be\u09b8\u09cd\u09a5\u09cd\u09af|research|study|"
    r"education|\u09b6\u09bf\u0995\u09cd\u09b7\u09be|medical|clinical|journal|"
    r"textbook|pathology|anatomy)\b",
    re.IGNORECASE,
)


def _deobfuscate_text(text: str) -> str:
    """
    Strip zero-width/format characters and collapse inter-letter spaces used
    to evade filters (e.g. "s e x" → "sex").

    Only collapses spaces between SINGLE isolated characters, never between
    multi-character words, so "send bkash" stays intact for pattern matching.
    """
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    text = re.sub(r"(?<!\w)(\w) (?=\w(?!\w))", r"\1", text)
    return text


def moderate_text(text: str) -> ModerationResult:
    """
    Check message text for prohibited content.

    Tier 1 (ALWAYS_BLOCK) fires unconditionally — safe-context words like
    "doctor" or "study" cannot override these.
    Tier 2 (CONTEXT_BLOCK) fires only when no educational/medical context exists.
    """
    if not text:
        return ALLOW

    cleaned = _deobfuscate_text(text)

    # Tier 1 — always block
    for pattern in _ALWAYS_BLOCK_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return ModerationResult(
                Verdict.BLOCK,
                reason="অনুপযুক্ত বা নিষিদ্ধ টেক্সট",
                details={"matched": match.group(), "tier": "always_block"},
            )

    # Tier 2 — context-dependent
    has_safe_context = bool(_SAFE_CONTEXT_RE.search(cleaned))
    for pattern in _CONTEXT_BLOCK_PATTERNS:
        match = pattern.search(cleaned)
        if match and not has_safe_context:
            return ModerationResult(
                Verdict.BLOCK,
                reason="অনুপযুক্ত বা নিষিদ্ধ টেক্সট",
                details={"matched": match.group(), "tier": "context_block"},
            )

    return ALLOW


# ──────────────────────────────────────────────
# Sightengine endpoints (verified from official docs 2026-10-23)
# ──────────────────────────────────────────────

# Image endpoint — POST with multipart/form-data
# Fields: media (bytes), models, api_user, api_secret
# Response: { status, nudity:{...}, offensive:{prob,...} }
# Source: https://sightengine.com/docs/getstarted
_SIGHTENGINE_IMAGE_URL = "https://api.sightengine.com/1.0/check.json"

# Video sync endpoint — POST with multipart/form-data
# Fields: media (bytes), models, api_user, api_secret
# Constraint: video < 60 seconds; for longer videos use async /video/check.json
# Response: { status, data:{ frames:[{info:{id,position}, nudity:{...}, ...}] } }
# Source: https://sightengine.com/docs/moderate-stored-video
_SIGHTENGINE_VIDEO_SYNC_URL = "https://api.sightengine.com/1.0/video/check-sync.json"

# These two endpoints have DIFFERENT response schemas:
#   Image: top-level nudity/offensive keys
#   Video: data.frames[] array, each frame has its own nudity/offensive keys
# They must NOT share a response parser.


# ──────────────────────────────────────────────
# Image moderation
# ──────────────────────────────────────────────

async def moderate_image_bytes(
    image_bytes: bytes, *, filename: str = "image.jpg", content_type: str = "image/jpeg"
) -> ModerationResult:
    """
    Submit image bytes to Sightengine /check.json.
    Returns BLOCK, ALLOW, or DEGRADED.
    """
    if not config.SIGHTENGINE_AVAILABLE:
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine credentials not configured — image not inspected.",
        )
    if len(image_bytes) > config.MEDIA_MAX_BYTES:
        return ModerationResult(
            Verdict.DEGRADED,
            reason=(
                f"Image size {len(image_bytes) // 1024} KB exceeds "
                f"limit {config.MEDIA_MAX_BYTES // 1024 // 1024} MB — not inspected."
            ),
        )

    try:
        form = aiohttp.FormData()
        form.add_field(
            "media", image_bytes,
            filename=filename,
            content_type=content_type,
        )
        form.add_field("models", config.SIGHTENGINE_IMAGE_MODELS)
        form.add_field("api_user",   config.SIGHTENGINE_API_USER)
        form.add_field("api_secret", config.SIGHTENGINE_API_SECRET)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.post(_SIGHTENGINE_IMAGE_URL, data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Sightengine image HTTP %s: %s", resp.status, body[:300]
                    )
                    return ModerationResult(
                        Verdict.DEGRADED,
                        reason=f"Sightengine image API returned HTTP {resp.status}.",
                    )
                data = await resp.json()

        return _parse_image_response(data)

    except asyncio.TimeoutError:
        logger.warning("Sightengine image request timed out.")
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine image request timed out (>15 s).",
        )
    except Exception as exc:
        logger.error("Sightengine image error: %s", exc)
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"Sightengine image error: {exc}",
        )


def _parse_image_response(data: dict) -> ModerationResult:
    """
    Parse /check.json image response.
    Schema: { status, nudity:{sexual_activity, sexual_display, erotica, ...},
              offensive:{prob, ...} }
    """
    if data.get("status") != "success":
        err_msg = data.get("error", {}).get("message", "unknown")
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"Sightengine image: non-success status — {err_msg}",
        )

    nudity    = data.get("nudity", {})
    offensive = data.get("offensive", {})

    sexual_score = max(
        nudity.get("sexual_activity", 0.0),
        nudity.get("sexual_display",  0.0),
        nudity.get("erotica",         0.0),
    )
    offensive_score = offensive.get("prob", 0.0)

    if sexual_score >= config.SIGHTENGINE_NUDITY_THRESHOLD:
        return ModerationResult(
            Verdict.BLOCK,
            reason="স্পষ্ট যৌন ছবি শনাক্ত হয়েছে।",
            details={"nudity_score": round(sexual_score, 3)},
        )
    if offensive_score >= config.SIGHTENGINE_OFFENSIVE_THRESHOLD:
        return ModerationResult(
            Verdict.BLOCK,
            reason="আপত্তিকর ছবি শনাক্ত হয়েছে।",
            details={"offensive_score": round(offensive_score, 3)},
        )
    return ALLOW


# ──────────────────────────────────────────────
# Video moderation
# ──────────────────────────────────────────────

async def moderate_video_bytes(video_bytes: bytes) -> ModerationResult:
    """
    Submit a short video (< 60 s) to Sightengine /video/check-sync.json.

    Endpoint: POST https://api.sightengine.com/1.0/video/check-sync.json
    Fields:   media (bytes), models, api_user, api_secret
    Response: { status, data:{ frames:[{info:{id,position},
                nudity:{sexual_activity,...}, offensive:{prob}}] } }

    Source: https://sightengine.com/docs/moderate-stored-video (verified 2026-10-23)

    This endpoint is available on all Sightengine account tiers.
    It is NOT the same as the async /video/check.json endpoint (which requires
    a callback_url and does not return results in the HTTP response).

    Limitations (documented):
      • Videos > MEDIA_MAX_BYTES (default 20 MB) → DEGRADED (not sent).
      • Videos > ~60 s → likely timeout → DEGRADED.
      • Frame sampling rate is determined by Sightengine internally.
    """
    if not config.SIGHTENGINE_AVAILABLE:
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine credentials not configured — video not inspected.",
        )
    if len(video_bytes) > config.MEDIA_MAX_BYTES:
        return ModerationResult(
            Verdict.DEGRADED,
            reason=(
                f"Video size {len(video_bytes) // 1024} KB exceeds "
                f"limit {config.MEDIA_MAX_BYTES // 1024 // 1024} MB — not inspected."
            ),
        )

    try:
        form = aiohttp.FormData()
        form.add_field(
            "media", video_bytes,
            filename="video.mp4",
            content_type="video/mp4",
        )
        form.add_field("models",     config.SIGHTENGINE_IMAGE_MODELS)
        form.add_field("api_user",   config.SIGHTENGINE_API_USER)
        form.add_field("api_secret", config.SIGHTENGINE_API_SECRET)

        # Videos take longer to process — use a 90 s timeout
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=90)
        ) as session:
            async with session.post(
                _SIGHTENGINE_VIDEO_SYNC_URL, data=form
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Sightengine video HTTP %s: %s", resp.status, body[:300]
                    )
                    return ModerationResult(
                        Verdict.DEGRADED,
                        reason=f"Sightengine video API returned HTTP {resp.status}.",
                    )
                data = await resp.json()

        return _parse_video_response(data)

    except asyncio.TimeoutError:
        logger.warning("Sightengine video request timed out (>90 s).")
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine video timed out — clip may exceed 60 s limit.",
        )
    except Exception as exc:
        logger.error("Sightengine video error: %s", exc)
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"Sightengine video error: {exc}",
        )


def _parse_video_response(data: dict) -> ModerationResult:
    """
    Parse /video/check-sync.json response.

    Schema (from official docs):
      {
        "status": "success",
        "data": {
          "frames": [
            {
              "info": { "id": "...", "position": 0 },
              "nudity": {
                "sexual_activity": 0.01,
                "sexual_display": 0.01,
                "erotica": 0.01,
                ...
              },
              "offensive": { "prob": 0.01 }
            },
            ...
          ]
        }
      }

    A single frame exceeding the threshold is enough to BLOCK.
    """
    if data.get("status") != "success":
        err_msg = data.get("error", {}).get("message", "unknown")
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"Sightengine video: non-success status — {err_msg}",
        )

    frames = data.get("data", {}).get("frames", [])

    if not frames:
        # API returned success but no frames — treat as DEGRADED, not ALLOW
        logger.warning("Sightengine video response contained no frames.")
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine video returned no frames — could not inspect.",
        )

    worst_nudity    = 0.0
    worst_offensive = 0.0
    worst_frame_pos = 0

    for frame in frames:
        nudity    = frame.get("nudity",    {})
        offensive = frame.get("offensive", {})

        sexual_score = max(
            nudity.get("sexual_activity", 0.0),
            nudity.get("sexual_display",  0.0),
            nudity.get("erotica",         0.0),
        )
        offensive_score = offensive.get("prob", 0.0)

        if sexual_score > worst_nudity:
            worst_nudity    = sexual_score
            worst_frame_pos = frame.get("info", {}).get("position", 0)
        if offensive_score > worst_offensive:
            worst_offensive = offensive_score

        # Early exit on clear violation
        if sexual_score >= config.SIGHTENGINE_NUDITY_THRESHOLD:
            return ModerationResult(
                Verdict.BLOCK,
                reason="ভিডিওতে অশ্লীল দৃশ্য শনাক্ত হয়েছে।",
                details={
                    "nudity_score": round(sexual_score, 3),
                    "frame_position_ms": worst_frame_pos,
                },
            )
        if offensive_score >= config.SIGHTENGINE_OFFENSIVE_THRESHOLD:
            return ModerationResult(
                Verdict.BLOCK,
                reason="ভিডিওতে আপত্তিকর দৃশ্য শনাক্ত হয়েছে।",
                details={"offensive_score": round(offensive_score, 3)},
            )

    return ALLOW


# ──────────────────────────────────────────────
# GIF moderation
# ──────────────────────────────────────────────

async def moderate_gif_bytes(gif_bytes: bytes) -> ModerationResult:
    """Inspect every frame of a short GIF; fail closed if it is too long/invalid.

    The image endpoint only analyzes a still image. Sending the original GIF once
    could inspect only a subset of its frames, so decode the animation and inspect
    every frame individually. If the GIF exceeds VIDEO_MAX_FRAMES, the caller's
    fallback policy is applied rather than claiming the uninspected frames are safe.
    """
    if not config.SIGHTENGINE_AVAILABLE:
        return ModerationResult(
            Verdict.DEGRADED,
            reason="Sightengine credentials not configured — GIF not inspected.",
        )
    if len(gif_bytes) > config.MEDIA_MAX_BYTES:
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"GIF exceeds {config.MEDIA_MAX_BYTES // 1024 // 1024} MB limit — not inspected.",
        )

    try:
        import io
        from PIL import Image, ImageSequence
        with Image.open(io.BytesIO(gif_bytes)) as gif:
            frame_count = int(getattr(gif, "n_frames", 1))
            if frame_count < 1 or frame_count > config.VIDEO_MAX_FRAMES:
                return ModerationResult(
                    Verdict.DEGRADED,
                    reason=(f"GIF has {frame_count} frames; inspection limit is "
                            f"{config.VIDEO_MAX_FRAMES} — not all frames inspected."),
                )
            for index, frame in enumerate(ImageSequence.Iterator(gif)):
                frame_buffer = io.BytesIO()
                frame.convert("RGB").save(frame_buffer, format="JPEG", quality=90)
                result = await moderate_image_bytes(
                    frame_buffer.getvalue(),
                    filename=f"gif-frame-{index + 1}.jpg",
                    content_type="image/jpeg",
                )
                if result.verdict == Verdict.BLOCK:
                    return ModerationResult(
                        Verdict.BLOCK,
                        reason=f"GIF-এর {index + 1} নম্বর ফ্রেমে নিষিদ্ধ কনটেন্ট শনাক্ত হয়েছে। {result.reason}",
                        details={**result.details, "gif_frame": index + 1},
                    )
                if result.verdict == Verdict.DEGRADED:
                    return ModerationResult(
                        Verdict.DEGRADED,
                        reason=f"GIF-এর {index + 1} নম্বর ফ্রেম পরীক্ষা করা যায়নি: {result.reason}",
                    )
        return ALLOW
    except Exception as exc:
        logger.warning("GIF decode/inspection failed: %s", exc)
        return ModerationResult(
            Verdict.DEGRADED,
            reason=f"GIF decode/inspection failed — not treated as safe ({type(exc).__name__}).",
        )
