"""
features.py — Feature toggle system.
প্রতিটা feature on/off করা যাবে per-group ভিত্তিতে।
"""

# সব feature-এর default value (True = চালু)
FEATURE_DEFAULTS = {
    # মডারেশন
    "url_filter":        True,   # URL block/allow
    "text_moderation":   True,   # regex + AI text check
    "media_moderation":  True,   # ছবি/ভিডিও check
    "spam_filter":       True,   # spam/flood control
    "phone_block":       True,   # ফোন নম্বর block
    "forward_block":     False,  # forward message block
    "arabic_block":      False,  # Arabic/Hindi spam
    "sticker_block":     False,  # sticker block
    "voice_block":       False,  # voice message block
    # Welcome সিস্টেম
    "welcome":           True,   # নতুন মেম্বার welcome
    "goodbye":           True,   # মেম্বার চলে গেলে goodbye
    "captcha":           False,  # নতুন মেম্বার captcha
    # Auto সিস্টেম
    "anti_raid":         True,   # anti-raid protection
    "anti_bot":          True,   # নতুন/bot account kick
    "night_mode":        False,  # রাতে গ্রুপ lock
    "warn_expire":       True,   # warning auto expire (৭দিন)
    "auto_delete":       False,  # নির্দিষ্ট সময়ে মেসেজ delete
    # Log সিস্টেম
    "log_channel":       False,  # violation log channel
}

FEATURE_LABELS = {
    "url_filter":        "🔗 URL ফিল্টার",
    "text_moderation":   "🤖 AI টেক্সট মডারেশন",
    "media_moderation":  "🖼️ মিডিয়া মডারেশন",
    "spam_filter":       "🚫 স্প্যাম ফিল্টার",
    "phone_block":       "📵 ফোন নম্বর ব্লক",
    "forward_block":     "↩️ ফরওয়ার্ড ব্লক",
    "arabic_block":      "🌐 Arabic/Hindi ব্লক",
    "sticker_block":     "😀 স্টিকার ব্লক",
    "voice_block":       "🎙️ ভয়েস ব্লক",
    "welcome":           "👋 ওয়েলকাম মেসেজ",
    "goodbye":           "👋 গুডবাই মেসেজ",
    "captcha":           "🔐 ক্যাপচা",
    "anti_raid":         "🛡️ অ্যান্টি-রেইড",
    "anti_bot":          "🤖 অ্যান্টি-বট",
    "night_mode":        "🌙 নাইট মোড",
    "warn_expire":       "⏰ ওয়ার্নিং এক্সপায়ার",
    "auto_delete":       "🗑️ অটো ডিলিট",
    "log_channel":       "📋 লগ চ্যানেল",
}
