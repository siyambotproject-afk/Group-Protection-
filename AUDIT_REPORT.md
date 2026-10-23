# 🔍 অডিট রিপোর্ট — Group Protection Bot v1.2

**অডিট তারিখ:** ২০২৬-১০-২৩
**সংশোধিত সংস্করণ:** v1.2 (এই ZIP)
**পূর্ববর্তী সংস্করণ:** v1.1-audited

---

## সংক্ষিপ্ত ফলাফল

| বাগ আইডি | গুরুত্ব | অবস্থা | টেস্ট |
|---|---|---|---|
| BUG-1 | 🔴 Critical | ✅ v1.1-এ সংশোধিত, v1.2-এ রিগ্রেশন টেস্ট বজায় | 15 টেস্ট |
| BUG-2 | 🔴 Critical | ✅ v1.1-এ সংশোধিত, v1.2-এ বজায় | 3 টেস্ট |
| BUG-3 | 🟠 High | ✅ v1.1-এ সংশোধিত, v1.2-এ বজায় | 3 টেস্ট |
| BUG-4 | 🔴 Critical | ✅ v1.2-এ পুনরায় সংশোধিত (v1.1-এর ভুল fix রিভার্ট) | 13 টেস্ট |
| BUG-4b | 🔴 Critical | ✅ v1.2-এ নতুন — video sticker DEGRADED fix | 1 টেস্ট |
| BUG-5 | 🟡 Medium | ✅ v1.1-এ সংশোধিত, v1.2-এ বজায় | 8 টেস্ট |
| BUG-6 | 🟡 Medium | ✅ v1.1-এ সংশোধিত, v1.2-এ আরও সংশোধিত | render.yaml |
| BUG-7 (Strict Mode) | 🔴 Critical | ✅ v1.2-এ নতুন — fallback default "allow"→"block" | 2 টেস্ট |
| BUG-8 (Sticker bypass) | 🔴 Critical | ✅ v1.2.1-এ static WebP inspect; animated/video sticker DEGRADED | regression tests |
| BUG-9 (Firebase fail-open) | 🔴 Critical | ✅ v1.2.1-এ strict reads + URL/media fail-closed | regression tests |
| BUG-10 (Render override) | 🔴 Critical | ✅ v1.2.1-এ Render fallback default block | config audit |

**v1.2 base test run:** 118 | **পাস:** 118 | **ফেল:** 0 (historical)

**v1.2.1:** final regression suite completed; actual result is recorded at the end of this report.

---

## v1.2-এ নতুন সংশোধন

---

### BUG-4 পুনরায় সংশোধন — Sightengine ভিডিও endpoint (Critical)

**v1.1-এর ভুল:**
v1.1-এ BUG-4 "fix" করতে গিয়ে একটি নতুন ভুল হয়েছিল:
```python
# v1.1-এর ভুল (আমাদের নিজস্ব অনুমান, verified নয়):
_SIGHTENGINE_VIDEO_CHECK_URL = "https://api.sightengine.com/1.0/check.json"
# এবং response parsing-এও image schema ব্যবহার হত
```

দাবি করা হয়েছিল যে "Sightengine `/check.json` video/mp4 accept করে" — কিন্তু এই দাবির কোনো official documentation ছিল না।

**v1.2-এর সঠিক সংশোধন:**
Official Sightengine documentation (https://sightengine.com/docs/moderate-stored-video) পড়ে নিশ্চিত করা হয়েছে:

```
Sync endpoint (< 60 s video): POST /video/check-sync.json
Fields: media (bytes), models, api_user, api_secret
Response: { status, data:{ frames:[{info:{id,position},
           nudity:{sexual_activity,...}, offensive:{prob}},...] } }
```

এই endpoint সব account tier-এ available — enterprise-only নয়।
Async endpoint (`/video/check.json`) আলাদা এবং callback_url প্রয়োজন।

**মূল পার্থক্য (Image vs Video):**

| বিষয় | Image `/check.json` | Video `/video/check-sync.json` |
|---|---|---|
| Method | POST multipart | POST multipart |
| Response | Top-level `nudity`, `offensive` | `data.frames[]` array |
| Parser | `_parse_image_response()` | `_parse_video_response()` |
| Timeout | 15 s | 90 s |

**নতুন টেস্ট:**
- `TestSightengineEndpointConstants` — URL constant verification
- `TestVideoResponseParser` — frame-by-frame response schema (8 টেস্ট)
- `TestVideoModeration` — end-to-end with mocked HTTP (10 টেস্ট)
- `test_image_and_video_endpoints_are_different` — cross-check
- `test_check_json_not_used_for_video_in_source` — source code assertion

---

### BUG-4b — Video sticker download failure silently safe (Critical)

**সমস্যা:**
```python
# v1.1-এর ভুল কোড:
if msg.sticker.is_video:
    data = await _get_media_bytes(...)
    if data:                           # ← data is None হলে?
        return await moderate_video_bytes(data)
return None                            # ← None মানে "media নেই" = safe!
```

Video sticker download ব্যর্থ হলে `None` return হত, যা `message_handler` "কোনো media নেই" হিসেবে treat করত — uninspected media silently pass হত।

**সংশোধন:**
```python
if data is None:
    return moderation.ModerationResult(
        Verdict.DEGRADED,
        "ভিডিও স্টিকার ডাউনলোড ব্যর্থ — পরীক্ষা করা যায়নি।",
    )
return await moderation.moderate_video_bytes(data)
```

একই pattern ছিল photo, video, GIF-এর জন্যও — সবগুলো verify করা হয়েছে।

---

### BUG-7 — Media fallback default "allow" → "block" (Critical, Strict Mode)

**সমস্যা:**
`MEDIA_FALLBACK_POLICY` default ছিল `"allow"` — Sightengine না থাকলে বা error হলে সব media silently pass হত। এটি "Strict Mode" requirement-এর বিরুদ্ধে।

**সংশোধন:**
Default `"block"` করা হয়েছে:
```python
MEDIA_FALLBACK_POLICY: str = os.environ.get("MEDIA_FALLBACK_POLICY", "block").lower()
```

Permissive mode চাইলে `.env`-এ `MEDIA_FALLBACK_POLICY=allow` সেট করতে হবে।
এই পরিবর্তন `.env.example` এবং `render.yaml`-এ documented।

---

## টেস্ট কভারেজ (v1.2)

### মক টেস্ট — এই session-এ চালানো হয়েছে ✅

| টেস্ট ফাইল | টেস্ট | নতুন (v1.2) |
|---|---|---|
| `test_moderation.py` | 66 | +28 (video endpoint, parsers, frame-by-frame GIF) |
| `test_warnings.py` | 22 | 0 |
| `test_database.py` | 18 | 0 |
| `test_commands.py` | 19 | +5 in v1.2.1 (fallback, stickers, Firebase failure) |
| **v1.2.1 মোট** | **125** | **+7 vs v1.2 base** |

**v1.2 historical ফলাফল:** `Ran 118 tests in 0.074s — OK`

---

### v1.2.1 follow-up fixes

- Render `MEDIA_FALLBACK_POLICY` now explicitly defaults to `block`.
- Static WebP stickers are sent to image moderation with WebP metadata.
- Animated TGS and video WebM stickers return `DEGRADED` rather than silently passing; strict fallback deletes them.
- GIF moderation decodes and checks each frame individually. If frame count exceeds `VIDEO_MAX_FRAMES`, decoding fails, or any frame cannot be inspected, it returns `DEGRADED` instead of trusting one still frame.
- Moderation path uses strict Firebase readers. On Firebase read failure it continues with local defaults, blocks URL-containing messages because custom domain policy is unavailable, and forces media fallback to `block`.
- Legacy database getters remain unchanged for commands and compatibility.

### Real Service Integration Tests — চালানো হয়নি ⚠️

নিচের কোনো real API call এই session-এ হয়নি:

| টেস্ট | কারণ |
|---|---|
| Sightengine `/check.json` (image) | Real API key প্রয়োজন |
| Sightengine `/video/check-sync.json` | Real API key + real video প্রয়োজন |
| Firebase read/write | Real credentials প্রয়োজন |
| Telegram Bot API | Real BOT_TOKEN + group প্রয়োজন |

**Real integration test procedure:** `tests/test_integration_manual.md` দেখুন।

---

## API Compatibility Disclosure

Sightengine endpoint documentation পড়া হয়েছে (2026-10-23):
- `https://sightengine.com/docs/getstarted` (image)
- `https://sightengine.com/docs/moderate-stored-video` (video sync)

Documented response schema মোট কোডে implement করা হয়েছে।
তবে **real API call ছাড়া 100% compatibility নিশ্চিত করা সম্ভব নয়।**
Sightengine-এর actual behavior ভবিষ্যতে বদলাতে পারে।

---

## পরিচিত সীমাবদ্ধতা

| সীমাবদ্ধতা | বিবরণ |
|---|---|
| Video > 60 s | Sync endpoint-এ কাজ করে না → DEGRADED → fallback policy প্রযোজ্য |
| Video > 20 MB | Download/upload করা হয় না → DEGRADED |
| Animated TGS/video WebM sticker | Sightengine-এ নির্ভরযোগ্যভাবে decode করা হয় না; DEGRADED → fallback policy |
| Render Free tier | 15 min inactivity-তে sleep → messages moderate হয় না |
| Sightengine real compatibility | Mocked test only; real API call করা হয়নি |

---

## নিরাপত্তা নিশ্চিতকরণ

- ✅ ZIP-এ কোনো real credential নেই
- ✅ `.gitignore`-এ `.env`, `*.json` credentials বাদ
- ✅ সব API keys শুধু environment variables থেকে


## v1.2.1 final validation

- Test suite: `python -m unittest discover -s tests -v` — 125 tests, all passed (mocked).
- Python compile check: `python -m compileall -q .` — passed.
- Render fallback: explicitly `block`.
- Live Telegram/Firebase/Sightengine integration: not run; requires user credentials and a test group.
