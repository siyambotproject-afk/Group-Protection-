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
import json
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


async def moderate_urls_async(
    text: str,
    group_blocked: list[str],
    group_allowed: list[str],
) -> ModerationResult:
    """
    ধাপ ৬ — Suspicious link checker।

    টেক্সটে URL shortener (bit.ly ইত্যাদি) থাকলে আগে তার আসল গন্তব্য বের করা হয়,
    তারপর শর্ট লিংকের জায়গায় আসল লিংক বসিয়ে moderate_urls()-এর সব নিয়ম প্রয়োগ হয়।

      • গন্তব্য allowlist-এ থাকলে → অনুমোদিত
      • গন্তব্য blocklist/অজানা/ছদ্মবেশী হলে → block (কারণে আসল গন্তব্য দেখানো হয়)
      • গন্তব্য যাচাই করা না গেলে (timeout, অতিরিক্ত redirect, private IP) → আগের মতোই block (fail closed)

    group/default allowlist-এ থাকা shortener-এ কিছু বদলায় না (আগের আচরণই থাকে)।
    """
    urls = extract_urls(text)
    if not urls:
        return ALLOW

    group_allowed_set = {d.lower() for d in group_allowed}
    resolved_text = text
    replaced: dict[str, str] = {}

    for raw in urls:
        domain = normalise_url(raw)
        if domain is None or domain not in _URL_SHORTENERS:
            continue
        root = _root_domain(domain)
        if (domain in _DEFAULT_ALLOWLIST or root in _DEFAULT_ALLOWLIST
                or domain in group_allowed_set or root in group_allowed_set):
            continue
        # নির্ভরতা চক্র এড়াতে এখানে import (security → moderation)
        import security
        destination = await security.resolve_url(raw)
        if destination:
            replaced[raw] = destination
            resolved_text = resolved_text.replace(raw, destination)

    result = moderate_urls(resolved_text, group_blocked, group_allowed)
    if result.is_violation and replaced:
        short, real = next(iter(replaced.items()))
        real_domain = normalise_url(real) or real
        return ModerationResult(
            Verdict.BLOCK,
            reason=f"শর্ট লিংকের আসল গন্তব্য অনুমোদিত নয় ({real_domain}) — {result.reason}",
            details={**result.details, "short_url": short, "resolved_url": real},
        )
    return result


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
    Regex-only synchronous text check.

    Tier 1 (ALWAYS_BLOCK): স্পষ্ট গালি/অশ্লীল — সবসময় block।
    Tier 2 (CONTEXT_BLOCK): context ছাড়া block।

    এই ফাংশন শুধু regex চেক করে।
    Gemini AI চেকের জন্য moderate_text_async() ব্যবহার করুন।
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


# ══════════════════════════════════════════════
# AI Text Moderation — Fallback Chain
# Gemini (primary) → Groq (fallback 1) → Together AI (fallback 2)
# ══════════════════════════════════════════════

# ── Shared prompt ─────────────────────────────

_AI_MODERATION_PROMPT = """\
তুমি একটি Telegram গ্রুপের content moderator। নিচের মেসেজটি বিশ্লেষণ করো।

মেসেজ:
\"\"\"
{text}
\"\"\"

এই মেসেজটি কি নিচের যেকোনো একটি বিভাগে পড়ে?
1. যৌন বা অশ্লীল কথা (সরাসরি বা ইঙ্গিতপূর্ণ)
2. যৌন উত্তেজনা বা flirting (যেমন "তুমি হট", "সেক্স করবা")
3. গালিগালাজ বা অপমানজনক ভাষা
4. হয়রানি বা ভয় দেখানো
5. স্ক্যাম বা প্রতারণা (বিকাশ/নগদ/ফ্রি টাকা/লটারি)

শুধুমাত্র নিচের JSON format-এ উত্তর দাও, অন্য কিছু লিখবে না:
{{"is_violation": true/false, "category": "sexual/abusive/harassment/scam/none", "reason": "সংক্ষিপ্ত কারণ বাংলায়", "score": 0.0}}

score হবে 0.0 থেকে 1.0 এর মধ্যে (1.0 = নিশ্চিতভাবে লঙ্ঘন)।
সাধারণ কথাবার্তা বা নিরীহ মেসেজের জন্য is_violation: false দাও।
"""


def _parse_ai_json(raw: str) -> Optional[dict]:
    """AI response থেকে JSON parse করে।"""
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # partial match চেষ্টা
        import re
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


# ── Gemini ────────────────────────────────────

_GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/{model}:generateContent"
)


async def _call_gemini(text: str) -> Optional[dict]:
    """Gemini API call। ব্যর্থ হলে None।"""
    if not config.GEMINI_AVAILABLE:
        return None

    url = _GEMINI_API_URL.format(model=config.GEMINI_MODEL)
    prompt = _AI_MODERATION_PROMPT.format(text=text[:1500])
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            # নতুন Gemini model "thinking" করতে token খরচ করে; 150 হলে JSON কেটে যায়
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.post(
                url, json=payload, params={"key": config.GEMINI_API_KEY}
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Gemini HTTP %s: %s", resp.status, body[:150])
                    return None
                data = await resp.json()

        parts = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        if not parts:
            return None
        return _parse_ai_json(parts[0].get("text", ""))

    except asyncio.TimeoutError:
        logger.warning("Gemini: timeout")
        return None
    except Exception as exc:
        logger.warning("Gemini: %s", exc)
        return None


# ── Groq ──────────────────────────────────────

_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


async def _call_groq(text: str) -> Optional[dict]:
    """Groq API call (OpenAI-compatible)। ব্যর্থ হলে None।"""
    if not config.GROQ_AVAILABLE:
        return None

    prompt = _AI_MODERATION_PROMPT.format(text=text[:1500])
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150,
    }
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.post(
                _GROQ_API_URL, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq HTTP %s: %s", resp.status, body[:150])
                    return None
                data = await resp.json()

        raw = data["choices"][0]["message"]["content"]
        return _parse_ai_json(raw)

    except asyncio.TimeoutError:
        logger.warning("Groq: timeout")
        return None
    except Exception as exc:
        logger.warning("Groq: %s", exc)
        return None


# ── Together AI ───────────────────────────────

_TOGETHER_API_URL = "https://api.together.xyz/v1/chat/completions"


async def _call_together(text: str) -> Optional[dict]:
    """Together AI API call। ব্যর্থ হলে None।"""
    if not config.TOGETHER_AVAILABLE:
        return None

    prompt = _AI_MODERATION_PROMPT.format(text=text[:1500])
    payload = {
        "model": config.TOGETHER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150,
    }
    headers = {
        "Authorization": f"Bearer {config.TOGETHER_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12)
        ) as session:
            async with session.post(
                _TOGETHER_API_URL, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Together HTTP %s: %s", resp.status, body[:150])
                    return None
                data = await resp.json()

        raw = data["choices"][0]["message"]["content"]
        return _parse_ai_json(raw)

    except asyncio.TimeoutError:
        logger.warning("Together: timeout")
        return None
    except Exception as exc:
        logger.warning("Together: %s", exc)
        return None


# ── Scam detection (regex, বাংলা) ─────────────

_SCAM_PATTERNS = [
    re.compile(
        r"(বিকাশ|নগদ|রকেট).{0,20}(পাঠান|দিন|করুন|নম্বর|একাউন্ট)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(ফ্রি|free).{0,15}(টাকা|taka|bdt|পয়েন্ট)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(লটারি|lottery|jackpot|জ্যাকপট).{0,20}(জিত|win|পেয়েছেন)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(invest|বিনিয়োগ).{0,20}(profit|মুনাফা|return).{0,10}%",
        re.IGNORECASE,
    ),
    re.compile(
        r"(click|ক্লিক).{0,20}(link|লিংক).{0,20}(earn|আয়|টাকা)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(০১[3-9]\d{8}|01[3-9]\d{8}).{0,20}(বিকাশ|নগদ|পাঠান)",
        re.IGNORECASE,
    ),
]


def _is_scam(text: str) -> bool:
    """Regex দিয়ে scam/প্রতারণামূলক মেসেজ ধরে।"""
    for pat in _SCAM_PATTERNS:
        if pat.search(text):
            return True
    return False


# ── Main async moderation ──────────────────────

async def moderate_text_async(text: str) -> ModerationResult:
    """
    Smart hybrid text moderation — ৪ স্তর:

    ধাপ ১ — Regex:        স্পষ্ট গালি/অশ্লীল → সাথে সাথে BLOCK
    ধাপ ২ — Scam Regex:   বিকাশ স্ক্যাম/ফ্রি টাকা → BLOCK
    ধাপ ৩ — Gemini:       primary AI (fail → Groq)
    ধাপ ৪ — Groq:         fallback #1 (fail → Together)
    ধাপ ৫ — Together AI:  fallback #2 (fail → ALLOW)
    """
    if not text or len(text.strip()) < 3:
        return ALLOW

    # ── ধাপ ১: Regex ──────────────────────────
    regex_result = moderate_text(text)
    if regex_result.is_violation:
        return regex_result

    # ── ধাপ ২: Scam detect ────────────────────
    if _is_scam(text):
        return ModerationResult(
            Verdict.BLOCK,
            reason="🚨 সম্ভাব্য স্ক্যাম বা প্রতারণামূলক মেসেজ",
            details={"tier": "scam_regex"},
        )

    # ── ধাপ ৩–৫: AI chain ─────────────────────
    any_ai = (
        config.GEMINI_AVAILABLE
        or config.GROQ_AVAILABLE
        or config.TOGETHER_AVAILABLE
    )
    if not any_ai:
        return ALLOW

    if len(text.strip()) < 5:
        return ALLOW

    # Gemini → Groq → Together (প্রথমটা সফল হলে পরেরটা চলে না)
    ai_data: Optional[dict] = None
    ai_source = "none"

    ai_data = await _call_gemini(text)
    if ai_data is not None:
        ai_source = "gemini"
    else:
        logger.info("Gemini unavailable — trying Groq")
        ai_data = await _call_groq(text)
        if ai_data is not None:
            ai_source = "groq"
        else:
            logger.info("Groq unavailable — trying Together AI")
            ai_data = await _call_together(text)
            if ai_data is not None:
                ai_source = "together"

    if ai_data is None:
        logger.info("সব AI unavailable — '%s...' allowed", text[:30])
        return ALLOW

    is_violation = ai_data.get("is_violation", False)
    score = float(ai_data.get("score", 0.0))
    category = ai_data.get("category", "none")
    reason = ai_data.get("reason", "অনুপযুক্ত কন্টেন্ট")

    if is_violation and score >= config.GEMINI_BLOCK_THRESHOLD:
        logger.info(
            "[%s] blocked (score=%.2f, cat=%s): '%s...'",
            ai_source, score, category, text[:40],
        )
        return ModerationResult(
            Verdict.BLOCK,
            reason=f"AI মডারেশন ({ai_source}): {reason}",
            details={
                "score": score,
                "category": category,
                "tier": ai_source,
            },
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
