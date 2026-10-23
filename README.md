# 🛡️ Telegram Group Protection Bot

একটি সম্পূর্ণ Python-ভিত্তিক Telegram গ্রুপ মডারেশন বট যা Firebase-এ ডেটা সংরক্ষণ করে এবং Render-এ হোস্ট হয়।

---

## ✅ বৈশিষ্ট্যসমূহ

| বৈশিষ্ট্য | বিবরণ |
|---|---|
| 🔗 লিংক মডারেশন | নিষিদ্ধ, ফিশিং, স্ক্যাম ডোমেইন ব্লক করে |
| 📝 টেক্সট মডারেশন | বাংলা ও ইংরেজি অশ্লীল/ক্ষতিকর টেক্সট সনাক্ত করে |
| 🖼️ মিডিয়া মডারেশন | Sightengine দিয়ে ছবি, ভিডিও, GIF পরীক্ষা করে |
| ⚠️ সতর্কতা সিস্টেম | ৩টি সতর্কতার পর ২৪ ঘণ্টা mute করে |
| 🔒 Firebase সংরক্ষণ | সার্ভার রিস্টার্টেও সতর্কতা টিকে থাকে |
| 👮 অ্যাডমিন কমান্ড | warn, unwarn, resetwarn, domain ম্যানেজমেন্ট |

---

## 🚀 সেটআপ গাইড (Android ফোন থেকে)

### ধাপ ১: Telegram Bot তৈরি করুন

1. Telegram-এ [@BotFather](https://t.me/BotFather) খুলুন
2. `/newbot` লিখুন
3. বটের নাম দিন (যেমন: "আমার গ্রুপ বট")
4. username দিন (শেষে `bot` থাকতহবে, যেমন: `mygroup_bot`)
5. প্রাপ্ত **Token** টি কপি করে রাখুন

### ধাপ ২: Firebase সেটআপ

1. [Firebase Console](https://console.firebase.google.com) খুলুন
2. **Add project** → প্রজেক্টের নাম দিন → Continue
3. বাঁ পাশে **Build** → **Realtime Database** → **Create Database**
4. **Start in test mode** বেছে নিন → Enable
5. Database URL কপি করুন (যেমন: `https://myproject-default-rtdb.firebaseio.com`)
6. বাঁ পাশে ⚙️ **Project Settings** → **Service accounts**
7. **Generate new private key** → JSON ফাইল ডাউনলোড হবে
8. ফাইলটি খুলুন — পুরো কন্টেন্ট কপি করুন (এটাই `FIREBASE_SERVICE_ACCOUNT_JSON`)

### ধাপ ৩: Sightengine সেটআপ (ঐচ্ছিক)

1. [sightengine.com](https://sightengine.com) এ বিনামূল্যে অ্যাকাউন্ট খুলুন
2. Dashboard থেকে **API User** এবং **API Secret** কপি করুন
3. ছাড়া হলে মিডিয়া মডারেশন DEGRADED MODE-এ চলবে (শুধু টেক্সট/লিংক কাজ করবে)

### ধাপ ৪: GitHub রিপোজিটরি তৈরি করুন

1. [github.com](https://github.com) এ নতুন অ্যাকাউন্ট বা লগইন
2. **New repository** → নাম দিন → **Create repository**
3. Android-এ [GitHub Mobile](https://play.google.com/store/apps/details?id=com.github.android) অ্যাপ ব্যবহার করুন
4. এই প্রজেক্টের সব ফাইল আপলোড করুন (`.env` ফাইল **কখনো** আপলোড করবেন না)

### ধাপ ৫: Render-এ ডিপ্লয় করুন

1. [render.com](https://render.com) এ বিনামূল্যে অ্যাকাউন্ট খুলুন
2. **New** → **Web Service**
3. GitHub অ্যাকাউন্ট কানেক্ট করুন → রিপোজিটরি বেছে নিন
4. নিচের সেটিংস দিন:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Runtime:** Python 3
5. **Environment Variables** সেকশনে নিচের ভেরিয়েবলগুলো যোগ করুন:

```
BOT_TOKEN              = (BotFather থেকে পাওয়া টোকেন)
FIREBASE_DATABASE_URL  = (Firebase database URL)
FIREBASE_SERVICE_ACCOUNT_JSON = (Firebase JSON কন্টেন্ট, এক লাইনে)
SIGHTENGINE_API_USER   = (ঐচ্ছিক)
SIGHTENGINE_API_SECRET = (ঐচ্ছিক)
DEFAULT_MUTE_MINUTES   = 1440
LOG_LEVEL              = INFO
```

6. **Create Web Service** চাপুন

### ধাপ ৬: বটকে গ্রুপে যোগ করুন

1. Telegram গ্রুপে বটকে যোগ করুন
2. বটকে **Admin** করুন এবং নিচের permission দিন:
   - ✅ Delete messages
   - ✅ Ban users (mute করার জন্য)
   - ✅ Restrict members

---

## 📋 কমান্ড রেফারেন্স

### সাধারণ কমান্ড

| কমান্ড | বিবরণ |
|---|---|
| `/start` | বট পরিচিতি |
| `/help` | কমান্ড তালিকা |
| `/status` | বটের বর্তমান অবস্থা |
| `/warnings` | নিজের বা reply করা ইউজারের সতর্কতা দেখুন |

### অ্যাডমিন কমান্ড

| কমান্ড | বিবরণ |
|---|---|
| `/warn` | reply করা ইউজারকে সতর্ক করুন |
| `/unwarn` | reply করা ইউজারের একটি সতর্কতা কমান |
| `/resetwarn` | reply করা ইউজারের সব সতর্কতা রিসেট করুন |
| `/settings` | গ্রুপ সেটিংস দেখুন |
| `/allowdomain example.com` | ডোমেইন অনুমোদন করুন |
| `/blockdomain evil.com` | ডোমেইন ব্লক করুন |
| `/allowlist` | অনুমোদিত ডোমেইন তালিকা |
| `/blocklist` | নিষিদ্ধ ডোমেইন তালিকা |

---

## ⚠️ সতর্কতা নীতি

```
১ম লঙ্ঘন → বার্তা ডিলিট + ⚠️ সতর্কতা 1/3
২য় লঙ্ঘন → বার্তা ডিলিট + ⚠️ সতর্কতা 2/3
৩য় লঙ্ঘন → বার্তা ডিলিট + ⚠️ সতর্কতা 3/3 + 🔇 ২৪ ঘণ্টা mute
```

---

## 🔧 সমস্যা সমাধান

### বট কাজ করছে না?
- Render logs চেক করুন: **Dashboard → Logs**
- BOT_TOKEN সঠিক কিনা যাচাই করুন
- বটকে গ্রুপ admin করা হয়েছে কিনা চেক করুন

### Firebase connection error?
- `FIREBASE_DATABASE_URL` সঠিক format-এ আছে কিনা দেখুন
- `FIREBASE_SERVICE_ACCOUNT_JSON` পুরো JSON এক লাইনে আছে কিনা দেখুন

### মিডিয়া মডারেশন কাজ করছে না?
- Sightengine credentials যোগ করা হয়েছে কিনা দেখুন
- `/status` কমান্ড দিয়ে AI মডারেশন স্ট্যাটাস দেখুন

---

## 📁 প্রজেক্ট স্ট্রাকচার

```
group-protection-bot/
├── bot.py          — মূল বট, হ্যান্ডলার, কমান্ড
├── moderation.py   — URL, টেক্সট, মিডিয়া মডারেশন
├── warnings.py     — সতর্কতা সিস্টেম
├── database.py     — Firebase অপারেশন
├── config.py       — কনফিগারেশন লোডার
├── requirements.txt
├── render.yaml     — Render ডিপ্লয়মেন্ট কনফিগ
├── .env.example    — এনভায়রনমেন্ট ভেরিয়েবল টেমপ্লেট
├── .gitignore
└── tests/
    ├── test_moderation.py
    ├── test_warnings.py
    ├── test_database.py
    └── test_commands.py
```

---

## ⚖️ পরিচিত সীমাবদ্ধতা

1. **Render Free Tier** — ১৫ মিনিট নিষ্ক্রিয়তায় ঘুমিয়ে পড়ে। প্রথম বার্তায় ধীরে সাড়া দিতে পারে।
2. **ভিডিও সীমা** — ২০ MB-এর বড় ভিডিও AI দিয়ে পরীক্ষা করা হয় না।
3. **Sightengine ছাড়া** — মিডিয়া মডারেশন নিষ্ক্রিয় (টেক্সট/লিংক স্বাভাবিক কাজ করে)।
4. **Supergroup প্রয়োজন** — mute কাজ করতে গ্রুপকে supergroup হতে হবে।
5. **URL shortener** — bit.ly ইত্যাদির গন্তব্য ভেরিফাই করা হয় না (log হয়, block হয় না)।


### Media safety notes (Strict Mode)
- `MEDIA_FALLBACK_POLICY` defaults to `block` in both local config and Render.
- Photos are inspected as images; videos use Sightengine's synchronous video endpoint.
- GIFs are decoded and each frame is inspected. GIFs above `VIDEO_MAX_FRAMES` are treated as uninspected and blocked by the default fallback.
- Static WebP stickers are inspected as images. Animated TGS and video WebM stickers cannot currently be reliably inspected directly and are treated as uninspected; Strict Mode blocks them.
- If Firebase group settings/domain lists cannot be read, URL-containing messages are blocked and uninspected media uses the `block` fallback.
- Automated tests use mocks; they do not replace real integration testing with Telegram, Firebase, and Sightengine credentials.
