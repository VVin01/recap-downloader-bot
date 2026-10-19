"""
bot.py - Telegram Bot for downloading Video, Audio, and SRT subtitles
from YouTube, TikTok, and Short URLs.
"""

import os
import sys
import re
import json
import io
import html
import logging
import uuid
import asyncio
import functools
from datetime import datetime
import glob
from typing import Any, Optional, Dict, List, Tuple
from dotenv import load_dotenv

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    constants
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from downloader import (
    MediaDownloader,
    extract_first_url,
    unshorten_url,
    detect_platform,
    format_duration,
    format_filesize
)
from gemini_recap import get_gemini_api_key
from auth_gate import check_user_purchased_status, send_vip_gate_required_message

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Cache for active user requests (Maps session_id -> {url, info, timestamp})
# Kept in memory to stay safely within Telegram's 64-byte callback_data limit
SESSIONS = {}
USER_ACTIVE_SESSION: dict[int, str] = {}
RECAP_DELIVERY_OUTPUTS: dict[str, dict[str, Any]] = {}

# Telegram Bot API limits (Default Cloud: 50MB upload, 20MB download. Local API: 2000MB)
MAX_TELEGRAM_FILESIZE_BYTES = 50 * 1024 * 1024
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024
IS_LOCAL_BOT_API = False

LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "http://127.0.0.1:8081").strip()
USE_LOCAL_BOT_API = os.getenv("USE_LOCAL_BOT_API", "auto").strip().lower()

downloader = MediaDownloader()
DOWNLOAD_DIR = downloader.download_dir

def detect_and_configure_bot_api(token: str):
    """Detect if Local Telegram Bot API (Docker) is active and configure limits."""
    global MAX_TELEGRAM_FILESIZE_BYTES, MAX_TELEGRAM_DOWNLOAD_BYTES, IS_LOCAL_BOT_API

    clean_url = LOCAL_BOT_API_URL.rstrip("/")
    should_use_local = False

    if USE_LOCAL_BOT_API in ("true", "1", "yes"):
        should_use_local = True
    elif USE_LOCAL_BOT_API in ("auto", ""):
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{clean_url}/bot{token}/getMe",
                headers={"User-Agent": "DownloaderBotChecker"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.getcode() == 200:
                    should_use_local = True
        except Exception:
            should_use_local = False

    if should_use_local:
        MAX_TELEGRAM_FILESIZE_BYTES = 2000 * 1024 * 1024  # 2000 MB (2 GB)
        MAX_TELEGRAM_DOWNLOAD_BYTES = 2000 * 1024 * 1024  # 2000 MB (2 GB)
        IS_LOCAL_BOT_API = True
        logger.info(f"🌐 [Local Bot API] Connected to Docker telegram-bot-api at {clean_url} (2000MB limit unlocked!)")
    else:
        MAX_TELEGRAM_FILESIZE_BYTES = 50 * 1024 * 1024
        MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024
        IS_LOCAL_BOT_API = False
        logger.info("☁️ [Cloud Bot API] Using official Telegram API (50MB upload / 20MB download limit)")

    return should_use_local, clean_url

def clean_old_sessions():
    """Remove expired sessions and delete stale temporary directories to prevent storage bloat."""
    now = datetime.now().timestamp()
    expired = [k for k, v in SESSIONS.items() if now - v.get("created_at", now) > 1800]
    for k in expired:
        SESSIONS.pop(k, None)
        job_dir = os.path.join(downloader.download_dir, f"job_{k}")
        if os.path.exists(job_dir):
            downloader.cleanup_files(job_dir)

    to_del = [uid for uid, sid in USER_ACTIVE_SESSION.items() if sid not in SESSIONS]
    for uid in to_del:
        USER_ACTIVE_SESSION.pop(uid, None)

    # Clean expired recap on-demand delivery files (> 2 hours)
    expired_deliv = [sid for sid, d in RECAP_DELIVERY_OUTPUTS.items() if now - d.get("created_at", now) > 7200]
    for sid in expired_deliv:
        RECAP_DELIVERY_OUTPUTS.pop(sid, None)

    # Clean orphaned job_* directories or recap_output/workspace or recap_finished older than expiry
    try:
        dl_dir = downloader.download_dir
        if os.path.exists(dl_dir):
            for item in os.listdir(dl_dir):
                item_path = os.path.join(dl_dir, item)
                if os.path.isdir(item_path):
                    if item.startswith("job_"):
                        if now - os.path.getmtime(item_path) > 1200:
                            downloader.cleanup_files(item_path)
                    elif item == "recap_output":
                        ws = os.path.join(item_path, "workspace")
                        if os.path.exists(ws) and (now - os.path.getmtime(ws) > 1800):
                            downloader.cleanup_files(ws)
                    elif item == "recap_finished":
                        for fin_sub in os.listdir(item_path):
                            fin_sub_path = os.path.join(item_path, fin_sub)
                            if os.path.isdir(fin_sub_path) and (now - os.path.getmtime(fin_sub_path) > 7200):
                                downloader.cleanup_files(fin_sub_path)
    except Exception as e:
        logger.debug(f"clean_old_sessions disk cleaner: {e}")


USER_SETTINGS_FILE = os.path.join(downloader.download_dir, "user_settings.json")


def load_user_settings(user_id: int | str | None) -> dict:
    if not user_id:
        return {}
    uid_str = str(user_id)
    if not os.path.exists(USER_SETTINGS_FILE):
        return {}
    try:
        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(uid_str, {})
    except Exception as e:
        logger.warning(f"Error loading user settings: {e}")
        return {}


def save_user_setting(user_id: int | str | None, key: str, value: Any):
    if not user_id:
        return
    uid_str = str(user_id)
    data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    u_dict = data.setdefault(uid_str, {})
    u_dict[key] = value
    try:
        os.makedirs(os.path.dirname(USER_SETTINGS_FILE), exist_ok=True)
        with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Error saving user setting {key}: {e}")


def save_all_user_settings(user_id: int | str | None, opts: dict):
    if not user_id:
        return
    uid_str = str(user_id)
    data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    keys_to_save = [
        "voice", "style", "speed", "mirror", "zoom", "blur_subs",
        "blur_y", "blur_h", "blur_x", "blur_w", "blur_preset", "burn_subs", "sub_y",
        "sub_lines", "sub_font_size", "sub_style", "ratio", "logo_enabled",
        "logo_path", "logo_pos", "logo_scale", "logo_opacity"
    ]
    u_dict = data.setdefault(uid_str, {})
    for k in keys_to_save:
        if k in opts:
            u_dict[k] = opts[k]
    try:
        os.makedirs(os.path.dirname(USER_SETTINGS_FILE), exist_ok=True)
        with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Error saving all user settings: {e}")


def _get_logo_coords(pos_name: str) -> list[float]:
    mapping = {
        "top-right": [0.82, 0.04],
        "top-left": [0.04, 0.04],
        "bottom-right": [0.82, 0.88],
        "bottom-left": [0.04, 0.88],
        "top-center": [0.42, 0.04],
    }
    return mapping.get(pos_name, [0.82, 0.04])


def _get_sub_style_info(s_key: str) -> dict:
    from recap_engine.core import SUBTITLE_STYLE_PRESETS
    return SUBTITLE_STYLE_PRESETS.get(str(s_key).lower(), SUBTITLE_STYLE_PRESETS["yellow"])


def _get_recap_opts(session: dict, user_id: int | str | None = None) -> dict:
    if "recap_opts" in session:
        return session["recap_opts"]

    uid = user_id or session.get("user_id") or session.get("chat_id")
    saved = load_user_settings(uid) if uid else {}

    default_logo_path = os.path.join(DOWNLOAD_DIR, "logos", f"logo_{uid}.png") if uid else ""
    has_logo = os.path.isfile(default_logo_path)
    custom_logo_path = str(saved.get("logo_path") or default_logo_path)
    final_logo_path = custom_logo_path if (custom_logo_path and os.path.isfile(custom_logo_path)) else (default_logo_path if has_logo else "")

    opts = {
        "voice": saved.get("voice", "thiha"),
        "style": saved.get("style", "viral"),
        "speed": saved.get("speed", "+0%"),
        "mirror": saved.get("mirror", True),
        "zoom": saved.get("zoom", 1.06),
        "blur_subs": saved.get("blur_subs", True),
        "blur_y": saved.get("blur_y", 0.81),
        "blur_h": saved.get("blur_h", 0.13),
        "blur_x": saved.get("blur_x", 0.05),
        "blur_w": saved.get("blur_w", 0.90),
        "blur_preset": saved.get("blur_preset", "mid"),
        "burn_subs": saved.get("burn_subs", True),
        "sub_y": saved.get("sub_y", 0.85),
        "sub_lines": saved.get("sub_lines", 1),
        "sub_font_size": saved.get("sub_font_size", 16),
        "sub_style": saved.get("sub_style", "yellow"),
        "ratio": saved.get("ratio", "Original"),
        "logo_enabled": saved.get("logo_enabled", bool(final_logo_path)),
        "logo_path": final_logo_path,
        "logo_pos": saved.get("logo_pos", "top-right"),
        "logo_scale": float(saved.get("logo_scale", 0.16)),
        "logo_opacity": float(saved.get("logo_opacity", 0.70)),
        "user_id": uid,
    }
    session["recap_opts"] = opts
    return opts


def _get_voice_info(v_tag: str) -> dict:
    if v_tag == "voxcpm":
        return {
            "name": "VoxCPM",
            "label": "🎙️ VoxCPM (AI Clone Cloud)",
            "button": "🎙️ VoxCPM (AI Clone)",
            "is_clone": True,
        }
    elif v_tag == "thiha":
        return {
            "name": "Thiha",
            "label": "Thiha (ယောက်ျားသံ)",
            "button": "👨 Thiha (ကျား)",
            "is_clone": False,
        }
    elif v_tag == "nilar":
        return {
            "name": "Nilar",
            "label": "Nilar (မိန်းမသံ)",
            "button": "👩 Nilar (မ)",
            "is_clone": False,
        }
    elif str(v_tag).startswith("clone_"):
        try:
            from recap_engine.voice_clone_bridge import resolve_clone_model
            minfo = resolve_clone_model(v_tag)
            if minfo:
                name = minfo.get("name", v_tag)
                return {
                    "name": name,
                    "label": f"🎭 {name} (Voice Clone Pro)",
                    "button": f"🎭 {name} (Clone)",
                    "is_clone": True,
                }
        except Exception:
            pass
        clean_name = str(v_tag).replace("clone_sys_", "").replace("clone_usr_", "")
        return {
            "name": clean_name,
            "label": f"🎭 {clean_name} (Voice Clone Pro)",
            "button": f"🎭 {clean_name} (Clone)",
            "is_clone": True,
        }
    return {
        "name": str(v_tag),
        "label": str(v_tag),
        "button": f"🎙️ {v_tag}",
        "is_clone": False,
    }


def _get_recap_voice_cycle() -> list[str]:
    cycle = ["voxcpm", "thiha", "nilar"]
    try:
        from recap_engine.voice_clone_bridge import get_available_clone_models, is_applio_available
        if is_applio_available():
            clones = list(get_available_clone_models().keys())
            cycle.extend(clones)
    except Exception:
        pass
    return cycle


def _make_main_action_markup(sess_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✨ 🇲🇲 AI Recap Video (Smart Sync) ✨", callback_data=f"rec:{sess_id}"),
        ],
        [
            InlineKeyboardButton("🔥 TikTok Recap Script", callback_data=f"rcp:{sess_id}"),
        ],
        [
            InlineKeyboardButton("🎬 Video (MP4)", callback_data=f"vid:{sess_id}"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"aud:{sess_id}"),
        ],
        [
            InlineKeyboardButton("📝 Subtitle (SRT)", callback_data=f"srt:{sess_id}"),
            InlineKeyboardButton("ℹ️ Video Info", callback_data=f"nfo:{sess_id}"),
        ],
    ])


def _make_main_action_caption(info: dict, is_upload: bool = False, sender_name: str = "") -> str:
    raw_title = info.get("title", "Media")
    raw_platform = info.get("platform", "Media")
    duration = info.get("duration_formatted", "00:00")
    sub_count = len(info.get("available_subs", []))
    sub_status = f"✅ ရရှိနိုင်သည် ({sub_count} ခု)" if sub_count > 0 else "🎙️ AI Auto-Gen (Whisper)"

    title_esc = html.escape(str(raw_title)[:70])
    platform_esc = html.escape(str(raw_platform))

    if is_upload:
        sender_esc = html.escape(str(sender_name or "Unknown")[:50])
        fsize = info.get("filesize_formatted", "N/A")
        return (
            f"🎬 <b>AI Myanmar Recap Studio Pro</b>\n\n"
            f"📌 <b>ဖိုင်အမည်:</b> {title_esc}\n"
            f"👤 <b>Upload တင်သူ:</b> {sender_esc} · 📁 <code>{fsize}</code>\n"
            f"⏱️ <b>ကြာချိန်:</b> <code>{duration}</code> · 📝 <b>Original Sub:</b> {sub_status}\n\n"
            f"👇 <b>ဆောင်ရွက်လိုသည့် လုပ်ငန်းစဉ်ကို ရွေးချယ်ပါ -</b>"
        )
    else:
        raw_uploader = info.get("uploader", "Unknown")
        uploader_esc = html.escape(str(raw_uploader)[:50])
        return (
            f"🎬 <b>AI Myanmar Recap Studio Pro</b>\n\n"
            f"🌐 <b>ပလက်ဖောင်း:</b> {platform_esc}\n"
            f"📌 <b>ခေါင်းစဉ်:</b> {title_esc}\n"
            f"👤 <b>Channel:</b> {uploader_esc} · ⏱️ <code>{duration}</code>\n"
            f"📝 <b>Original Sub:</b> {sub_status}\n\n"
            f"👇 <b>ဆောင်ရွက်လိုသည့် လုပ်ငန်းစဉ်ကို ရွေးချယ်ပါ -</b>"
        )


def _make_recap_markup(sess_id: str, opts: dict) -> InlineKeyboardMarkup:
    v_info = _get_voice_info(opts.get("voice", "thiha"))
    v_icon = v_info["button"]
    rat = opts.get("ratio", "Original")
    ratio_icon = f"📐 {rat}"
    m_icon = "🪞 Mirror: ON ✅" if opts.get("mirror", True) else "🪞 Mirror: OFF ❌"
    b_icon = "🌫️ Blur: ON ✅" if opts.get("blur_subs", True) else "🌫️ Blur: OFF ❌"
    s_icon = "📝 Sub: ON ✅" if opts.get("burn_subs", True) else "📝 Sub: OFF ❌"
    logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
    l_icon = "🖼️ Logo: ON ✅" if logo_on else "🖼️ Logo: OFF ❌"

    style = opts.get("style", "viral")
    if style == "viral":
        style_label = "🔥 ဗိုင်းရပ်စ် Hook"
    elif style == "funny":
        style_label = "😂 ဟာသရွဲ့စောင်း"
    elif style == "pov":
        style_label = "🎭 ဇာတ်ကောင် POV"
    elif style == "dramatic":
        style_label = "⚡ သည်းထိတ်ရင်ဖို"
    else:
        style_label = "🎬 ဇာတ်လမ်းပြော"

    speed = opts.get("speed", "+0%")
    if speed == "+8%":
        speed_label = "သွက် (+8%)"
    elif speed == "+18%":
        speed_label = "မြန် (+18%)"
    elif speed == "+22%":
        speed_label = "အထူးမြန် (+22%)"
    else:
        speed_label = "ပုံမှန် (+0%)"

    by = int(round(opts.get("blur_y", 0.81) * 100))
    preset = opts.get("blur_preset", "mid")
    preset_name = "အလယ်" if preset == "mid" else ("အောက်" if preset == "low" else ("အပေါ်" if preset == "high" else "စိတ်ကြိုက်"))

    s_style_key = opts.get("sub_style", "yellow")
    s_style_info = _get_sub_style_info(s_style_key)
    s_style_label = s_style_info.get("short", "အဝါရောင်")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 1-Click Pro Recap စတင်မည်", callback_data=f"rco:{sess_id}:quick"),
        ],
        [
            InlineKeyboardButton("🖼️ စတိုင် & Preview (မျက်မြင်ပြင်ရန်)", callback_data=f"rcp_prev:{sess_id}"),
        ],
        [
            InlineKeyboardButton(f"🎭 စတိုင်: {style_label}", callback_data=f"rct:{sess_id}:style"),
        ],
        [
            InlineKeyboardButton(f"🎙️ {v_icon}", callback_data=f"rct:{sess_id}:voice"),
            InlineKeyboardButton(f"⚡ {speed_label}", callback_data=f"rct:{sess_id}:speed"),
        ],
        [
            InlineKeyboardButton(f"📍 Blur: {preset_name} ({by}%)", callback_data=f"rct:{sess_id}:blur_pos"),
            InlineKeyboardButton(f"{b_icon}", callback_data=f"rct:{sess_id}:blur_subs"),
        ],
        [
            InlineKeyboardButton(f"🎨 Sub: {s_style_label}", callback_data=f"rct:{sess_id}:sub_style"),
            InlineKeyboardButton(f"{s_icon}", callback_data=f"rct:{sess_id}:burn_subs"),
        ],
        [
            InlineKeyboardButton(f"{ratio_icon}", callback_data=f"rct:{sess_id}:ratio"),
            InlineKeyboardButton(f"{l_icon}", callback_data=f"rct:{sess_id}:logo"),
        ],
        [
            InlineKeyboardButton("🔙 Back (မူလစာမျက်နှာ)", callback_data=f"bck:{sess_id}"),
        ]
    ])


def _make_recap_caption(media_info: dict, opts: dict) -> str:
    title_esc = html.escape(str(media_info.get('title', 'Video'))[:60])
    dur = media_info.get("duration_formatted", "N/A")
    v_info = _get_voice_info(opts.get("voice", "thiha"))
    v_name = v_info["label"]
    style = opts.get("style", "viral")
    if style == "viral":
        style_desc = "🔥 ဗိုင်းရပ်စ် Hook စတိုင် (TikTok / Reels Views တက်စေရန် 3-Second Hook & Retention Pacing) [အကြံပြု]"
    elif style == "funny":
        style_desc = "😂 ဟာသရွဲ့စောင်း စတိုင် (Humorous Roast & Sarcasm - ကြည့်ရှုသူ Comment & Share များစေသည်)"
    elif style == "pov":
        style_desc = "🎭 ဇာတ်ကောင်နေရာကနေ ပြောပြသော စတိုင် (Character POV Dubbing)"
    elif style == "dramatic":
        style_desc = "⚡ သည်းထိတ်ရင်ဖို ဒရာမာ စတိုင် (Suspense / Thriller Twists)"
    else:
        style_desc = "🎬 ရုပ်ရှင်ဇာတ်လမ်းပြော စတိုင် (Cinematic Movie Narrator - 100% Visual Sync)"

    speed = opts.get("speed", "+0%")
    if speed == "+8%":
        speed_desc = "သွက်လက် (+8%)"
    elif speed == "+18%":
        speed_desc = "အမြန်နှုန်း (+18%)"
    elif speed == "+22%":
        speed_desc = "အထူးအမြန်နှုန်း (+22%)"
    else:
        speed_desc = "သဘာဝကျကျ အေးဆေးပုံမှန် (+0%) [နားထောင်အကောင်းဆုံး]"

    by = int(round(opts.get("blur_y", 0.81) * 100))
    bh = int(round(opts.get("blur_h", 0.13) * 100))
    preset = opts.get("blur_preset", "mid")
    preset_name = "အလယ် (Mid)" if preset == "mid" else ("အောက်ကပ် (Low)" if preset == "low" else ("အပေါ်မြင့် (High)" if preset == "high" else "စိတ်ကြိုက်"))

    sy = int(round(opts.get("sub_y", 0.85) * 100))
    sub_lines = int(opts.get("sub_lines", 1))
    fsize = int(opts.get("sub_font_size", 16))
    line_label = "၁ ကြောင်း" if sub_lines == 1 else "၂ ကြောင်း"

    s_style_key = opts.get("sub_style", "yellow")
    s_style_info = _get_sub_style_info(s_style_key)
    s_style_name = s_style_info.get("label", "အဝါရောင် (Classic Gold)")

    logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
    pos_key = opts.get("logo_pos", "top-right")
    pos_label = "အပေါ် ညာ" if pos_key == "top-right" else ("အပေါ် ဘယ်" if pos_key == "top-left" else ("အောက် ညာ" if pos_key == "bottom-right" else "အောက် ဘယ်"))
    scale_val = float(opts.get("logo_scale", 0.16))
    scale_label = "သေး" if scale_val <= 0.12 else ("လတ်" if scale_val <= 0.16 else "ကြီး")

    return (
        f"🎬 <b>AI Myanmar Recap Studio Pro (Smart Sync)</b>\n"
        f"📌 <b>{title_esc}</b>\n"
        f"⏱️ <b>ကြာချိန်:</b> <code>{dur}</code>\n\n"
        f"🎭 <b>တင်ဆက်မှု:</b> {style_desc}\n"
        f"🎙️ <b>အသံ & နှုန်း:</b> {v_name} · <code>{speed_desc}</code>\n"
        f"📐 <b>Ratio:</b> {opts.get('ratio', 'Original')}\n\n"
        f"🛡️ <b>Anti-Copyright & Branding System:</b>\n"
        f"• <b>Horizontal Flip:</b> {'ဖွင့်ထားပါသည် (ON) ✅' if opts.get('mirror') else 'ပိတ်ထားပါသည် (OFF) ❌'}\n"
        f"• <b>Dynamic Zoom:</b> {'1.06x Zoom (ဘောင်ဖြတ်ခြင်း)' if opts.get('mirror') else '1.0x (မူရင်း)'}\n"
        f"• <b>Subtitle Blur:</b> {f'{preset_name} (Y={by}%, H={bh}%) ✅' if opts.get('blur_subs') else 'မဖုံးပါ ❌'}\n"
        f"• <b>Burn Myanmar Subs:</b> {f'အချိန်ကိုက် မြန်မာစာတန်းထိုး ({line_label} · {fsize}pt · Y={sy}%) ✅' if opts.get('burn_subs') else 'မထည့်ပါ ❌'}\n"
        f"• <b>Subtitle Style:</b> <code>{s_style_name}</code>\n"
        f"• <b>Channel Logo:</b> {f'ထည့်သွင်းထားသည် ({pos_label} · {scale_label}) ✅' if logo_on else 'မထည့်ပါ ❌'}\n\n"
        f"🖼️ <i>Blur၊ စာတန်းထိုး နှင့် Logo များကို <b>'စတိုင် & Preview'</b> တွင် မျက်မြင်ပြင်ဆင်နိုင်ပါသည် -</i>"
    )


def _make_preview_markup(sess_id: str, opts: dict) -> InlineKeyboardMarkup:
    tab = opts.get("preview_tab", "blur")
    by = int(round(opts.get("blur_y", 0.81) * 100))
    bh = int(round(opts.get("blur_h", 0.13) * 100))
    bx = int(round(opts.get("blur_x", 0.05) * 100))
    bw = int(round(opts.get("blur_w", 0.90) * 100))
    sy = int(round(opts.get("sub_y", 0.85) * 100))
    sub_lines = int(opts.get("sub_lines", 1))
    fsize = int(opts.get("sub_font_size", 16))

    preset = opts.get("blur_preset", "mid")
    preset_name = "အလယ်" if preset == "mid" else ("အောက်" if preset == "low" else ("အပေါ်" if preset == "high" else "စိတ်ကြိုက်"))
    line_label = "၁ ကြောင်း" if sub_lines == 1 else "၂ ကြောင်း"
    size_label = f"{fsize}pt"

    s_style_key = opts.get("sub_style", "yellow")
    s_style_info = _get_sub_style_info(s_style_key)
    s_style_short = s_style_info.get("short", "အဝါရောင်")

    logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
    pos_key = opts.get("logo_pos", "top-right")
    pos_label = "ညာပေါ်" if pos_key == "top-right" else ("ဘယ်ပေါ်" if pos_key == "top-left" else ("ညာအောက်" if pos_key == "bottom-right" else "ဘယ်အောက်"))
    scale_val = float(opts.get("logo_scale", 0.16))
    scale_label = "သေး" if scale_val <= 0.12 else ("လတ်" if scale_val <= 0.16 else "ကြီး")

    tab_blur_btn = "🔘 🌫️ Blur" if tab == "blur" else "🌫️ Blur Box"
    tab_sub_btn = "🔘 📝 စာတန်း" if tab == "sub" else "📝 စာတန်းထိုး"
    tab_logo_btn = "🔘 🖼️ Logo" if tab == "logo" else "🖼️ Logo & More"

    rows = [
        [
            InlineKeyboardButton(tab_blur_btn, callback_data=f"rcadj:{sess_id}:tab_blur"),
            InlineKeyboardButton(tab_sub_btn, callback_data=f"rcadj:{sess_id}:tab_sub"),
            InlineKeyboardButton(tab_logo_btn, callback_data=f"rcadj:{sess_id}:tab_logo"),
        ]
    ]

    cur_t = float(opts.get("preview_time", 14.5))
    m = int(cur_t // 60)
    s = int(cur_t % 60)
    time_str = f"{m:02d}:{s:02d}"

    if tab == "blur":
        blur_on = opts.get("blur_subs", True)
        rows.extend([
            [
                InlineKeyboardButton(f"📍 {preset_name} ({by}%)", callback_data=f"rcadj:{sess_id}:p_cycle"),
                InlineKeyboardButton(f"🌫️ {'ON ✅' if blur_on else 'OFF ❌'}", callback_data=f"rcadj:{sess_id}:t_blur"),
                InlineKeyboardButton("🔄 Reset", callback_data=f"rcadj:{sess_id}:b_reset"),
            ],
            [
                InlineKeyboardButton("⬅️ ဘယ်", callback_data=f"rcadj:{sess_id}:bx_left"),
                InlineKeyboardButton("⬆️ အပေါ်", callback_data=f"rcadj:{sess_id}:by_up"),
                InlineKeyboardButton("⬇️ အောက်", callback_data=f"rcadj:{sess_id}:by_down"),
                InlineKeyboardButton("➡️ ညာ", callback_data=f"rcadj:{sess_id}:bx_right"),
            ],
            [
                InlineKeyboardButton("↕️ မြင့် (+2%)", callback_data=f"rcadj:{sess_id}:bh_up"),
                InlineKeyboardButton("↕️ နိမ့် (-2%)", callback_data=f"rcadj:{sess_id}:bh_down"),
                InlineKeyboardButton("↔️ ကျယ် (+4%)", callback_data=f"rcadj:{sess_id}:bw_up"),
                InlineKeyboardButton("↔️ ကျဉ်း (-4%)", callback_data=f"rcadj:{sess_id}:bw_down"),
            ],
            [
                InlineKeyboardButton("⏪ -10s", callback_data=f"rcadj:{sess_id}:t_prev10"),
                InlineKeyboardButton(f"⏱️ {time_str}", callback_data=f"rcadj:{sess_id}:t_refresh"),
                InlineKeyboardButton("⏩ +10s", callback_data=f"rcadj:{sess_id}:t_next10"),
            ],
            [
                InlineKeyboardButton("🔗 Sub ကို Blur ထဲကပ်မည် (Snap Sub)", callback_data=f"rcadj:{sess_id}:snap_sub"),
            ],
        ])
    elif tab == "sub":
        sub_on = opts.get("burn_subs", True)
        rows.extend([
            [
                InlineKeyboardButton("⬆️ စာတန်း အပေါ်", callback_data=f"rcadj:{sess_id}:sy_up"),
                InlineKeyboardButton("⬇️ စာတန်း အောက်", callback_data=f"rcadj:{sess_id}:sy_down"),
                InlineKeyboardButton("🔗 Blur ထဲကပ်မည်", callback_data=f"rcadj:{sess_id}:snap_sub"),
            ],
            [
                InlineKeyboardButton(f"📝 {line_label}", callback_data=f"rcadj:{sess_id}:t_lines"),
                InlineKeyboardButton(f"🔤 {size_label}", callback_data=f"rcadj:{sess_id}:t_fsize"),
                InlineKeyboardButton(f"🎨 {s_style_short}", callback_data=f"rcadj:{sess_id}:t_sstyle"),
            ],
            [
                InlineKeyboardButton("⏪ -10s", callback_data=f"rcadj:{sess_id}:t_prev10"),
                InlineKeyboardButton(f"⏱️ {time_str}", callback_data=f"rcadj:{sess_id}:t_refresh"),
                InlineKeyboardButton("⏩ +10s", callback_data=f"rcadj:{sess_id}:t_next10"),
            ],
            [
                InlineKeyboardButton(f"📝 စာတန်းထိုး: {'ON ✅' if sub_on else 'OFF ❌'}", callback_data=f"rcadj:{sess_id}:t_sub"),
            ],
        ])
    else:  # logo & more
        mirror_on = opts.get("mirror", True)
        rat = opts.get("ratio", "Original")
        rows.extend([
            [
                InlineKeyboardButton(f"🖼️ Logo: {'ON ✅' if logo_on else 'OFF ❌'}", callback_data=f"rcadj:{sess_id}:t_logo"),
                InlineKeyboardButton(f"📍 {pos_label}", callback_data=f"rcadj:{sess_id}:p_logo"),
                InlineKeyboardButton(f"📐 {scale_label}", callback_data=f"rcadj:{sess_id}:s_logo"),
            ],
            [
                InlineKeyboardButton(f"🪞 Mirror: {'ON ✅' if mirror_on else 'OFF ❌'}", callback_data=f"rcadj:{sess_id}:t_mirror"),
                InlineKeyboardButton(f"📐 Ratio: {rat}", callback_data=f"rcadj:{sess_id}:t_ratio"),
            ],
        ])

    rows.append([
        InlineKeyboardButton("🚀 စတင်ဖန်တီးမည် (Start)", callback_data=f"rco:{sess_id}:quick"),
        InlineKeyboardButton("🔙 Recap Menu", callback_data=f"rec:{sess_id}"),
    ])

    return InlineKeyboardMarkup(rows)


def _make_recap_delivery_markup(sess_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎙️ Voiceover အသံ", callback_data=f"rc_dl:{sess_id}:aud"),
            InlineKeyboardButton("📝 စာတန်းထိုး (SRT)", callback_data=f"rc_dl:{sess_id}:srt"),
        ],
        [
            InlineKeyboardButton("📄 ဇာတ်ညွှန်း (Script)", callback_data=f"rc_dl:{sess_id}:scr"),
            InlineKeyboardButton("🎬 Recap Menu", callback_data=f"rec:{sess_id}"),
        ]
    ])


def _make_preview_caption(opts: dict) -> str:
    tab = opts.get("preview_tab", "blur")
    by = int(round(opts.get("blur_y", 0.81) * 100))
    bh = int(round(opts.get("blur_h", 0.13) * 100))
    bx = int(round(opts.get("blur_x", 0.05) * 100))
    bw = int(round(opts.get("blur_w", 0.90) * 100))
    sy = int(round(opts.get("sub_y", 0.85) * 100))
    sub_lines = int(opts.get("sub_lines", 1))
    fsize = int(opts.get("sub_font_size", 16))

    preset = opts.get("blur_preset", "mid")
    preset_name = "အလယ် (Mid)" if preset == "mid" else ("အောက် (Low)" if preset == "low" else ("အပေါ် (High)" if preset == "high" else "စိတ်ကြိုက် (Custom)"))
    line_label = "၁ ကြောင်း သီးသန့်" if sub_lines == 1 else "၂ ကြောင်း အချိုးညီ"
    size_label = f"{fsize}pt"

    s_style_key = opts.get("sub_style", "yellow")
    s_style_info = _get_sub_style_info(s_style_key)
    s_style_name = s_style_info.get("label", "အဝါရောင် (Classic Gold)")

    logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
    pos_key = opts.get("logo_pos", "top-right")
    pos_label = "အပေါ် ညာ" if pos_key == "top-right" else ("အပေါ် ဘယ်" if pos_key == "top-left" else ("အောက် ညာ" if pos_key == "bottom-right" else "အောက် ဘယ်"))
    scale_val = float(opts.get("logo_scale", 0.16))
    scale_label = "သေး (12%)" if scale_val <= 0.12 else ("လတ် (16%)" if scale_val <= 0.16 else "ကြီး (22%)")
    logo_status = f"{pos_label} · {scale_label} ✅" if logo_on else "မထည့်ပါ (OFF) ❌"

    cur_t = float(opts.get("preview_time", 14.5))
    m = int(cur_t // 60)
    s = int(cur_t % 60)
    time_str = f"{m:02d}:{s:02d}"

    if tab == "blur":
        return (
            f"🖼️ <b>Settings & Preview</b>\n"
            f"👉 <b>[ 🌫️ Subtitle Blur Box ပြင်ဆင်ခြင်း ]</b>\n\n"
            f"⏱️ <b>အခန်းချိန်:</b> <code>{time_str}</code> (⏪ / ⏩ ဖြင့် အခန်းရွှေ့နိုင်ပါသည်)\n"
            f"📍 <b>Preset:</b> <code>{preset_name}</code>\n"
            f"↔️ <b>ဘေးဘယ်ညာ:</b> X=<code>{bx}%</code> · အကျယ် (W)=<code>{bw}%</code>\n"
            f"↕️ <b>အပေါ်အောက်:</b> Y=<code>{by}%</code> · အမြင့် (H)=<code>{bh}%</code>\n"
            f"🛡️ <b>အခြေအနေ:</b> {'ဖွင့်ထားပါသည် (ON) ✅' if opts.get('blur_subs', True) else 'ပိတ်ထားပါသည် (OFF) ❌'}\n\n"
            f"💡 <i>စာတန်းပါသော အခန်းသို့ ⏪ / ⏩ ဖြင့် ရွှေ့ပြီး Blur ကို အတိအကျ ချိန်ညှိပါ -</i>"
        )
    elif tab == "sub":
        return (
            f"🖼️ <b>Settings & Preview</b>\n"
            f"👉 <b>[ 📝 စာတန်းထိုး (Myanmar Subtitles) ပြင်ဆင်ခြင်း ]</b>\n\n"
            f"⏱️ <b>အခန်းချိန်:</b> <code>{time_str}</code> (⏪ / ⏩ ဖြင့် အခန်းရွှေ့နိုင်ပါသည်)\n"
            f"↕️ <b>စာတန်းနေရာ (Y):</b> <code>{sy}%</code>\n"
            f"📝 <b>စာကြောင်း:</b> <code>{line_label}</code> · <b>ဆိုဒ်:</b> <code>{size_label}</code>\n"
            f"🎨 <b>ဒီဇိုင်းစတိုင်:</b> <code>{s_style_name}</code>\n"
            f"✨ <b>အခြေအနေ:</b> {'ထည့်သွင်းမည် (ON) ✅' if opts.get('burn_subs', True) else 'မထည့်ပါ (OFF) ❌'}\n\n"
            f"💡 <i>အောက်ပါ ခလုတ်များဖြင့် စာတန်းထိုး နေရာနှင့် ဒီဇိုင်းကို စိတ်ကြိုက် ပြင်နိုင်ပါသည် -</i>"
        )
    else:  # logo / more
        return (
            f"🖼️ <b>Settings & Preview</b>\n"
            f"👉 <b>[ 🖼️ Logo & Video Options ပြင်ဆင်ခြင်း ]</b>\n\n"
            f"🖼️ <b>Channel Logo:</b> <code>{logo_status}</code>\n"
            f"📐 <b>Aspect Ratio:</b> <code>{opts.get('ratio', 'Original')}</code>\n"
            f"🪞 <b>Mirror Flip:</b> {'ON ✅' if opts.get('mirror') else 'OFF ❌'}\n"
            f"🔍 <b>Dynamic Zoom:</b> {'1.06x ✅' if opts.get('mirror') else '1.0x'}\n\n"
            f"💡 <i>Logo ဓာတ်ပုံ ထည့်လိုပါက Chat ထဲသို့ PNG/JPG ပို့ပေးပါခင်ဗျာ -</i>"
        )



async def send_or_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
    parse_mode=constants.ParseMode.HTML
):
    """Safely reply to a message, falling back to direct send if reply fails."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return None

    if update.message:
        try:
            return await update.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except BadRequest as e:
            logger.warning(f"reply_text failed ({e}), falling back to direct send_message")
        except Exception as e:
            logger.warning(f"Unexpected error in reply_text: {e}")

    return await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )


def _make_welcome_text(user_id: Optional[int] = None) -> str:
    mode_badge = "⚡ <b>Local API Mode:</b> 2000MB (2GB) အပြည့်အဝ အသုံးပြုနိုင်ပါသည် 🔥\n\n" if IS_LOCAL_BOT_API else ""
    vip_badge = ""
    if user_id:
        try:
            if check_user_purchased_status(user_id):
                vip_badge = "👑 <b>VIP Account:</b> <i>အတည်ပြုပြီး (Active VIP)</i> ✅\n\n"
            else:
                vip_badge = "👑 <b>VIP Status:</b> <i>Voice Clone Pro ဝယ်ယူသူများ သီးသန့် အသုံးပြုနိုင်ပါသည်</i> 🔒\n\n"
        except Exception:
            pass
    return (
        "🎬 <b>AI Myanmar Recap Studio Pro</b> 🇲🇲\n"
        "<i>Professional Video Dubbing & Movie Recap Creator</i>\n\n"
        f"{vip_badge}"
        f"{mode_badge}"
        "English ဗီဒီယိုနှင့် ရုပ်ရှင်ကားများကို <b>မြန်မာဘာသာစကားပြော AI Recap Video</b> အဖြစ် Voice Clone သဘာဝအသံ၊ အချိန်ကိုက် မြန်မာစာတန်းထိုး၊ မူရင်းစာတန်းဖျက် (Blur) နှင့် Channel Logo များဖြင့် 1-Click အပြီးသတ် ဖန်တီးပေးနိုင်ပါသည်။\n\n"
        "🚀 <b>စတင်အသုံးပြုရန်:</b>\n"
        "YouTube / TikTok Link ပို့ပေးပါ သို့မဟုတ် မိမိ Video ဖိုင်ကို Upload တင်ပေးလိုက်ပါခင်ဗျာ!"
    )


def _make_welcome_markup() -> InlineKeyboardMarkup:
    web_app_url = os.getenv("MINI_APP_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    rows = []
    if web_app_url:
        from telegram import WebAppInfo
        rows.append([
            InlineKeyboardButton("📱 Open Studio (Mini App) ✨", web_app=WebAppInfo(url=web_app_url))
        ])
    rows.append([
        InlineKeyboardButton("📖 အသုံးပြုနည်း လမ်းညွှန် (Help)", callback_data="menu:help"),
        InlineKeyboardButton("🎙️ Voice Clone အသံများ", callback_data="menu:voices"),
    ])
    return InlineKeyboardMarkup(rows)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id if update.effective_user else None
    await send_or_reply(
        update,
        context,
        _make_welcome_text(user_id),
        reply_markup=_make_welcome_markup(),
        parse_mode=constants.ParseMode.HTML
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = (
        "📖 <b>AI Myanmar Recap Studio Pro — အသုံးပြုနည်း လမ်းညွှန်</b>\n\n"
        "🎬 <b>Recap Video ဖန်တီးနည်း အဆင့်ဆင့်:</b>\n"
        "1️⃣ YouTube/TikTok Link ပို့ပါ သို့မဟုတ် ဗီဒီယိုဖိုင်ကို Chat ထဲသို့ Upload တင်ပါ။\n"
        "2️⃣ <b>'✨ AI Recap Video'</b> ခလုတ်ကို နှိပ်ပါ။\n"
        "3️⃣ <b>'🖼️ စတိုင် & Preview'</b> တွင် Blur Box၊ စာတန်းထိုးနှင့် Logo များကို စိတ်ကြိုက် ချိန်ညှိပါ။\n"
        "4️⃣ <b>'🚀 1-Click Pro Recap'</b> ကို နှိပ်လိုက်ရုံဖြင့် ပြီးပြည့်စုံသော Recap Video ရရှိပါမည်။\n\n"
        "🎙️ <b>အသံရွေးချယ်မှု (Voice):</b>\n"
        "• Thiha (ယောက်ျားသံ) / Nilar (မိန်းမသံ)\n"
        "• Voice Clone Pro မှ မိမိ Train ထားသော Clone အသံမော်ဒယ်များ (🎭 Clone)\n\n"
        "🖼️ <b>Channel Logo ထည့်သွင်းနည်း:</b>\n"
        "• မိမိ Logo (PNG/JPG) ဓာတ်ပုံကို Chat ထဲသို့ ပို့ပေးလိုက်ရုံဖြင့် အလိုအလျောက် သတ်မှတ်ပေးပါမည်။\n\n"
        "⌨️ <b>စာရိုက်၍ ချိန်ညှိနိုင်သော အမိန့်များ:</b>\n"
        "• <code>/time 25</code> သို့မဟုတ် <code>/time 01:15</code> - စာတန်းပါသော အခန်းချိန်သို့ သွားရန်\n"
        "• <code>/blur Y H [X W]</code> - ဥပမာ <code>/blur 81 13</code> သို့မဟုတ် <code>/blur 81 13 5 90</code>\n"
        "• <code>/sub Y</code> - စာတန်းထိုး နေရာ အနိမ့်အမြင့် (ဥပမာ <code>/sub 85</code>)\n"
        "• <code>/lines 1</code> သို့မဟုတ် <code>/lines 2</code> - စာကြောင်း အရေအတွက်\n"
        "• <code>/font 16</code> - စာလုံးအရွယ်အစား (13, 16, 19, 22pt)"
    )
    await send_or_reply(update, context, help_text, parse_mode=constants.ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages and extract URLs."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id and not check_user_purchased_status(user_id):
        await send_vip_gate_required_message(update)
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else None

    # Check if user is typing custom blur, subtitle or time settings
    time_match = re.match(r'^(?:/(?:time|sec)\s+|(?:time|sec)\s+)(?:(\d{1,2}):)?(\d{1,4})$', text, re.IGNORECASE)
    blur_match = re.match(r'^(?:/blur\s+|blur\s+)?(\d{1,2})\s+(\d{1,2})(?:\s+(\d{1,2})\s+(\d{1,2}))?$', text, re.IGNORECASE)
    sub_match = re.match(r'^(?:/sub\s+|sub\s+)(\d{1,2})$', text, re.IGNORECASE)
    lines_match = re.match(r'^(?:/lines?\s+|lines?\s+)([12])$', text, re.IGNORECASE)
    font_match = re.match(r'^(?:/fonts?\s+|fonts?\s+)(\d{1,2})$', text, re.IGNORECASE)
    style_match = re.match(r'^(?:/(?:substyle|style)\s+|(?:substyle|style)\s+)(yellow|white|boxed|neon|gold|cyan)$', text, re.IGNORECASE)

    is_custom_setting = time_match or blur_match or sub_match or lines_match or font_match or style_match
    if is_custom_setting:
        sess_id = USER_ACTIVE_SESSION.get(chat_id)
        if sess_id and sess_id in SESSIONS:
            sess = SESSIONS[sess_id]
            opts = _get_recap_opts(sess)
            change_desc = ""

            if time_match:
                m_str, s_str = time_match.groups()
                sec_val = int(m_str) * 60 + int(s_str) if m_str else int(s_str)
                opts["preview_time"] = float(max(1, sec_val))
                change_desc = f"Preview အခန်းချိန် ({sec_val}s)"
            elif blur_match:
                y_pct = int(blur_match.group(1))
                h_pct = int(blur_match.group(2))
                opts["blur_y"] = max(0.05, min(0.95, y_pct / 100.0))
                opts["blur_h"] = max(0.04, min(0.40, h_pct / 100.0))
                if blur_match.group(3) and blur_match.group(4):
                    x_pct = int(blur_match.group(3))
                    w_pct = int(blur_match.group(4))
                    opts["blur_x"] = max(0.0, min(0.95, x_pct / 100.0))
                    opts["blur_w"] = max(0.05, min(1.0, w_pct / 100.0))
                    change_desc = f"Blur နေရာ (Y={y_pct}%, H={h_pct}%, X={x_pct}%, W={w_pct}%)"
                else:
                    change_desc = f"Blur နေရာ (Y={y_pct}%, H={h_pct}%)"
                opts["blur_preset"] = "custom"
            elif sub_match:
                sy_pct = int(sub_match.group(1))
                opts["sub_y"] = max(0.05, min(0.95, sy_pct / 100.0))
                change_desc = f"စာတန်းထိုး နေရာ (Y={sy_pct}%)"
            elif lines_match:
                n_lines = int(lines_match.group(1))
                opts["sub_lines"] = n_lines
                change_desc = f"စာကြောင်းအရေအတွက် ({n_lines} ကြောင်း)"
            elif font_match:
                f_size = max(10, min(36, int(font_match.group(1))))
                opts["sub_font_size"] = f_size
                change_desc = f"စာလုံးအရွယ်အစား ({f_size}pt)"
            elif style_match:
                s_val = style_match.group(1).lower()
                if s_val == "gold":
                    s_val = "yellow"
                elif s_val == "cyan":
                    s_val = "neon"
                opts["sub_style"] = s_val
                s_info = _get_sub_style_info(s_val)
                change_desc = f"စာတန်းစတိုင် ({s_info['short']})"

            save_all_user_settings(opts.get("user_id"), opts)

            status_prev = await send_or_reply(
                update, context,
                f"⏳ <b>{change_desc} ဖြင့် Preview Frame ထုတ်ယူနေပါသည်...</b>",
                parse_mode=constants.ParseMode.HTML
            )
            try:
                loop = asyncio.get_running_loop()
                job_dir = os.path.join(downloader.download_dir, f"job_{sess_id}")
                logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
                logo_path_val = str(opts.get("logo_path")) if logo_on else None
                logo_coords_val = _get_logo_coords(opts.get("logo_pos", "top-right")) if logo_on else None

                if not opts.get("preview_time"):
                    try:
                        from downloader import find_best_preview_timestamp
                        opts["preview_time"] = find_best_preview_timestamp(sess["expanded_url"], job_dir)
                    except Exception:
                        opts["preview_time"] = 14.5

                prev_img = await loop.run_in_executor(
                    None,
                    functools.partial(
                        downloader.generate_recap_preview,
                        url_or_path=sess["expanded_url"],
                        target_dir=job_dir,
                        blur_subtitles=opts.get("blur_subs", True),
                        blur_y=opts.get("blur_y", 0.81),
                        blur_h=opts.get("blur_h", 0.13),
                        blur_x=opts.get("blur_x", 0.05),
                        blur_w=opts.get("blur_w", 0.90),
                        burn_subtitles=opts.get("burn_subs", True),
                        sub_y=opts.get("sub_y", 0.85),
                        sub_lines=int(opts.get("sub_lines", 1)),
                        sub_font_size=int(opts.get("sub_font_size", 16)),
                        sub_style=opts.get("sub_style", "yellow"),
                        output_ratio=opts.get("ratio", "Original"),
                        mirror_video=opts.get("mirror", True),
                        zoom_factor=opts.get("zoom", 1.06),
                        at_seconds=float(opts.get("preview_time", 14.5)),
                        logo_enabled=logo_on,
                        logo_path=logo_path_val,
                        logo_position=logo_coords_val,
                        logo_scale=float(opts.get("logo_scale", 0.16)),
                        logo_opacity=float(opts.get("logo_opacity", 0.70)),
                    )
                )
                with open(prev_img, "rb") as pf:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=pf,
                        caption=_make_preview_caption(opts),
                        reply_markup=_make_preview_markup(sess_id, opts),
                        parse_mode=constants.ParseMode.HTML,
                    )
                if status_prev:
                    try:
                        await status_prev.delete()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error generating preview: {e}")
                if status_prev:
                    await status_prev.edit_text(f"❌ Preview ထုတ်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: {e}")
            return
        else:
            await send_or_reply(
                update, context,
                "⚠️ <b>လက်ရှိ အသုံးပြုနေသော Video မရှိသေးပါ။</b> ကျေးဇူးပြု၍ Video Link သို့မဟုတ် Video ဖိုင်ကို အရင် ပို့ပေးပါခင်ဗျာ。",
                parse_mode=constants.ParseMode.HTML
            )
            return

    url = extract_first_url(text)

    if not url:
        await send_or_reply(
            update,
            context,
            "⚠️ မှန်ကန်သော YouTube သို့မဟုတ် TikTok Link ကို မတွေ့ရှိပါ။ ကျေးဇူးပြု၍ Link အပြည့်အစုံကို ပို့ပေးပါခင်ဗျာ。\n"
            "ဥပမာ: <code>https://youtu.be/...</code> သို့မဟုတ် <code>https://vt.tiktok.com/...</code>\n"
            "💡 <i>(ချိန်ညှိလိုပါက: <code>/blur 81 13</code>, <code>/sub 85</code>, <code>/lines 1</code> သို့မဟုတ် <code>/font 16</code> ဟု ပို့နိုင်ပါသည်)</i>",
            parse_mode=constants.ParseMode.HTML
        )
        return

    # Inform user that the link is being processed
    status_msg = await send_or_reply(
        update,
        context,
        "🔍 <b>Link ကို စစ်ဆေးနေပါသည်... ခေတ္တစောင့်ဆိုင်းပါ...</b>\n"
        "<i>(Resolving media details & subtitles...)</i>",
        parse_mode=constants.ParseMode.HTML
    )

    clean_old_sessions()

    # Extract metadata using asyncio to avoid blocking Telegram's event loop
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, downloader.get_info, url)
    except Exception as e:
        logger.error(f"Error extracting info for {url}: {e}")
        err_msg = html.escape(str(e)[:200])
        err_text = (
            f"❌ <b>အချက်အလက် ရယူရာတွင် အမှားတစ်ခု ဖြစ်ပေါ်ခဲ့သည် -</b>\n<code>{err_msg}</code>\n\n"
            "ကျေးဇူးပြု၍ Link မှန်ကန်မှု ရှိမရှိ သို့မဟုတ် Public video ဟုတ်မဟုတ် စစ်ဆေးပေးပါခင်ဗျာ。"
        )
        if status_msg:
            try:
                await status_msg.edit_text(err_text, parse_mode=constants.ParseMode.HTML)
                return
            except Exception:
                pass
        await send_or_reply(update, context, err_text, parse_mode=constants.ParseMode.HTML)
        return

    # Create session id for inline buttons
    session_id = uuid.uuid4().hex[:10]
    user_id = update.effective_user.id if update.effective_user else None
    SESSIONS[session_id] = {
        "url": url,
        "expanded_url": info.get("expanded_url"),
        "info": info,
        "user_id": user_id,
        "chat_id": chat_id,
        "created_at": datetime.now().timestamp()
    }
    if chat_id:
        USER_ACTIVE_SESSION[chat_id] = session_id

    caption = _make_main_action_caption(info, is_upload=False)
    reply_markup = _make_main_action_markup(session_id)

    # Delete the temporary status message
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    chat_id = update.effective_chat.id
    thumbnail_url = info.get("thumbnail")
    sent = False

    if thumbnail_url:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.HTML
            )
            sent = True
        except Exception as e:
            logger.warning(f"Failed to send thumbnail photo: {e}. Falling back to text message.")

    if not sent:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.HTML
        )


async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming video/audio/document uploads."""
    message = update.message
    if not message:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id and not check_user_purchased_status(user_id):
        await send_vip_gate_required_message(update)
        return

    media = None
    media_type = "video"
    raw_filename = "uploaded_media"

    # Check if user sent an image/photo as custom Logo/Watermark
    is_image_doc = False
    if message.document:
        doc_name = message.document.file_name or ""
        doc_ext = os.path.splitext(doc_name)[1].lower()
        doc_mime = (message.document.mime_type or "").lower()
        if doc_ext in {'.png', '.jpg', '.jpeg', '.webp'} or doc_mime.startswith("image/"):
            is_image_doc = True

    if message.photo or is_image_doc:
        uid = update.effective_user.id if update.effective_user else "default"
        chat_id = update.effective_chat.id if update.effective_chat else None

        status_logo = await send_or_reply(
            update, context,
            "🖼️ <b>Channel Logo / Watermark ဓာတ်ပုံကို ရယူသိမ်းဆည်းနေပါသည်...</b>",
            parse_mode=constants.ParseMode.HTML
        )
        try:
            if message.photo:
                photo_obj = message.photo[-1]
                photo_file = await photo_obj.get_file()
            else:
                photo_file = await message.document.get_file()

            logo_dir = os.path.join(DOWNLOAD_DIR, "logos")
            os.makedirs(logo_dir, exist_ok=True)
            logo_save_path = os.path.join(logo_dir, f"logo_{uid}.png")

            await photo_file.download_to_drive(custom_path=logo_save_path)

            save_user_setting(uid, "logo_enabled", True)
            save_user_setting(uid, "logo_path", logo_save_path)

            sess_id = USER_ACTIVE_SESSION.get(chat_id)
            if sess_id and sess_id in SESSIONS:
                opts = _get_recap_opts(SESSIONS[sess_id], uid)
                opts["logo_enabled"] = True
                opts["logo_path"] = logo_save_path
                save_all_user_settings(uid, opts)

            success_text = (
                f"✅ <b>Channel Logo / Watermark ကို အောင်မြင်စွာ သိမ်းဆည်းလိုက်ပါပြီ!</b>\n\n"
                f"• <b>Logo ဖိုင်:</b> <code>logo_{uid}.png</code>\n"
                f"• <b>အခြေအနေ:</b> အသုံးပြုရန် ဖွင့်ထားပါသည် (ON) ✅\n\n"
                f"💡 <i>ဗီဒီယို Recap လုပ်သည့်အခါ <b>'Settings & Preview'</b> တွင် Logo နေရာ (အပေါ် ညာ/အပေါ် ဘယ်/အောက် ညာ/အောက် ဘယ်) နှင့် အရွယ်အစားကို စိတ်ကြိုက် မျက်မြင်ချိန်ညှိနိုင်ပါသည်။</i>"
            )
            if status_logo:
                await status_logo.edit_text(success_text, parse_mode=constants.ParseMode.HTML)
            else:
                await send_or_reply(update, context, success_text, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error saving user logo: {e}")
            err_text = f"❌ <b>Logo သိမ်းဆည်းရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်:</b>\n<code>{html.escape(str(e)[:200])}</code>"
            if status_logo:
                await status_logo.edit_text(err_text, parse_mode=constants.ParseMode.HTML)
            else:
                await send_or_reply(update, context, err_text, parse_mode=constants.ParseMode.HTML)
        return

    if message.video:
        media = message.video
        media_type = "video"
        raw_filename = getattr(media, "file_name", None) or f"video_{media.file_unique_id}.mp4"
    elif message.document:
        media = message.document
        raw_filename = media.file_name or f"file_{media.file_unique_id}"
        ext = os.path.splitext(raw_filename)[1].lower()
        mime = (media.mime_type or "").lower()
        if ext in {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.wmv', '.m4v', '.ts'} or mime.startswith("video/"):
            media_type = "video"
        elif ext in {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.opus', '.wma'} or mime.startswith("audio/"):
            media_type = "audio"
        else:
            await send_or_reply(
                update,
                context,
                "⚠️ <b>ကျေးဇူးပြု၍ Video သို့မဟုတ် Audio ဖိုင်များကိုသာ ပို့ပေးပါခင်ဗျာ။</b>\n"
                "<i>(Supported: MP4, MKV, MOV, AVI, WEBM, MP3, WAV, M4A စသည်...)</i>",
                parse_mode=constants.ParseMode.HTML
            )
            return
    elif message.audio:
        media = message.audio
        media_type = "audio"
        raw_filename = getattr(media, "file_name", None) or f"audio_{media.file_unique_id}.mp3"
    elif message.voice:
        media = message.voice
        media_type = "audio"
        raw_filename = f"voice_{media.file_unique_id}.ogg"
    elif message.video_note:
        media = message.video_note
        media_type = "video"
        raw_filename = f"videonote_{media.file_unique_id}.mp4"
    else:
        return

    # Check file size limit imposed by Telegram Bot API (20MB on Cloud, 2000MB on Local Docker API)
    if media.file_size and media.file_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
        size_str = format_filesize(media.file_size)
        limit_str = format_filesize(MAX_TELEGRAM_DOWNLOAD_BYTES)
        await send_or_reply(
            update,
            context,
            f"⚠️ <b>ဖိုင်ဆိုဒ် ကြီးမားနေပါသည် ({size_str})</b>\n\n"
            f"လက်ရှိ Telegram Bot API စည်းမျဉ်းအရ {limit_str} အထက် ဖိုင်များကို တိုက်ရိုက် Download ဆွဲယူခွင့် မရှိပါ။\n\n"
            "👉 <b>အကြံပြုချက်များ:</b>\n"
            f"1. {limit_str} အောက် Video Clip ပြုလုပ်၍ ပို့ပေးပါ (သို့မဟုတ်)\n"
            "2. YouTube / TikTok သို့ Upload လုပ်ပြီး Link ကို ပို့ပေးပါ (သို့မဟုတ်)\n"
            "3. ကွန်ပျူတာပေါ်ရှိ <code>test_cli.bat</code> (CLI Mode) တွင် မည်သည့် ဖိုင်ဆိုဒ်မဆို ကန့်သတ်ချက်မရှိ တိုက်ရိုက် run နိုင်ပါသည်ခင်ဗျာ。",
            parse_mode=constants.ParseMode.HTML
        )
        return

    status_msg = await send_or_reply(
        update,
        context,
        "📥 <b>ဗီဒီယို / အသံဖိုင်ကို လက်ခံရယူနေပါသည်... ခေတ္တစောင့်ဆိုင်းပါ...</b>\n"
        "<i>(Downloading and analyzing uploaded media...)</i>",
        parse_mode=constants.ParseMode.HTML
    )

    clean_old_sessions()
    session_id = uuid.uuid4().hex[:10]

    uploads_dir = os.path.join(downloader.download_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    clean_name = re.sub(r'[^a-zA-Z0-9._-]', '_', raw_filename)
    dest_path = os.path.join(uploads_dir, f"{session_id}_{clean_name}")

    try:
        tg_file = await context.bot.get_file(media.file_id, read_timeout=600, write_timeout=600)
        await tg_file.download_to_drive(custom_path=dest_path, read_timeout=600, write_timeout=600)
    except Exception as e:
        logger.error(f"Error downloading uploaded file from Telegram: {e}")
        err_text = f"❌ <b>ဖိုင်ဒေါင်းလုဒ် ရယူရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်:</b>\n<code>{html.escape(str(e)[:150])}</code>"
        if status_msg:
            try:
                await status_msg.edit_text(err_text, parse_mode=constants.ParseMode.HTML)
                return
            except Exception:
                pass
        await send_or_reply(update, context, err_text, parse_mode=constants.ParseMode.HTML)
        return

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, downloader.get_info, dest_path)
    except Exception as e:
        logger.error(f"Error extracting local media info for {dest_path}: {e}")
        err_text = f"❌ <b>ဖိုင်အချက်အလက် စစ်ဆေးရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်:</b>\n<code>{html.escape(str(e)[:150])}</code>"
        if status_msg:
            try:
                await status_msg.edit_text(err_text, parse_mode=constants.ParseMode.HTML)
                return
            except Exception:
                pass
        await send_or_reply(update, context, err_text, parse_mode=constants.ParseMode.HTML)
        return

    sender_name = update.effective_user.full_name if update.effective_user else "User"
    info["uploader"] = sender_name

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    SESSIONS[session_id] = {
        "url": dest_path,
        "expanded_url": dest_path,
        "is_local": True,
        "is_upload": True,
        "sender_name": sender_name,
        "info": info,
        "user_id": user_id,
        "chat_id": chat_id,
        "created_at": datetime.now().timestamp()
    }
    if chat_id:
        USER_ACTIVE_SESSION[chat_id] = session_id

    caption = _make_main_action_caption(info, is_upload=True, sender_name=sender_name)
    reply_markup = _make_main_action_markup(session_id)

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    chat_id = update.effective_chat.id
    thumbnail_path = info.get("thumbnail")
    sent = False

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            with open(thumbnail_path, "rb") as thumb_f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=thumb_f,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.HTML
                )
                sent = True
        except Exception as e:
            logger.warning(f"Failed to send local thumbnail: {e}")

    if not sent:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.HTML
        )


async def safe_edit_menu(query, text_or_caption: str, reply_markup=None):
    """Safely update an inline message whether it contains a photo or pure text."""
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=text_or_caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.HTML
            )
        else:
            await query.edit_message_text(
                text=text_or_caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.HTML
            )
    except BadRequest as e:
        err_str = str(e).lower()
        if "not modified" in err_str:
            return
        logger.warning(f"safe_edit_menu initial try failed ({e}), attempting fallback...")
        try:
            if query.message and query.message.photo:
                await query.edit_message_text(
                    text=text_or_caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.HTML
                )
            else:
                await query.edit_message_caption(
                    caption=text_or_caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.HTML
                )
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"safe_edit_menu unexpected error: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Inline Keyboard button clicks."""
    query = update.callback_query
    if not query:
        return

    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Could not answer callback query: {e}")

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return

    action, session_id = parts[0], parts[1]

    # VIP GATE ACTIONS
    if action == "gate":
        if session_id == "recheck":
            user_id = update.effective_user.id if update.effective_user else None
            is_vip = check_user_purchased_status(user_id) if user_id else False
            if is_vip:
                try:
                    await query.answer("🎉 ဂုဏ်ယူပါသည်! သင်သည် VIP ဝယ်ယူထားသူဖြစ်၍ အသုံးပြုခွင့် ရရှိပါပြီခင်ဗျာ။", show_alert=True)
                except Exception:
                    pass
                await query.edit_message_text(
                    "✅ <b>VIP အတည်ပြုခြင်း အောင်မြင်ပါပြီခင်ဗျာ!</b>\n\nYouTube သို့မဟုတ် TikTok Link ပေးပို့၍ AI Recap နှင့် Video ဒေါင်းလုဒ်များကို စတင် အသုံးပြုနိုင်ပါပြီ။",
                    parse_mode=constants.ParseMode.HTML
                )
            else:
                try:
                    await query.answer("❌ VIP ဝယ်ယူထားခြင်း မရှိသေးပါခင်ဗျာ။ Voice Clone Pro Official Bot တွင် အရင်ဆုံး ဝယ်ယူပေးပါရန် မေတ္တာရပ်ခံအပ်ပါသည်။", show_alert=True)
                except Exception:
                    pass
        return

    # Check VIP gate for all operational actions (downloads, recap generation)
    user_id = update.effective_user.id if update.effective_user else None
    if action != "menu" and user_id and not check_user_purchased_status(user_id):
        await send_vip_gate_required_message(update)
        return

    # TOP-LEVEL MENU ACTIONS (HELP, VOICES, WELCOME)
    if action == "menu":
        if session_id == "help":
            help_text = (
                "📖 <b>AI Myanmar Recap Studio Pro — အသုံးပြုနည်း လမ်းညွှန်</b>\n\n"
                "🎬 <b>Recap Video ဖန်တီးနည်း အဆင့်ဆင့်:</b>\n"
                "1️⃣ YouTube/TikTok Link ပို့ပါ သို့မဟုတ် ဗီဒီယိုဖိုင်ကို Chat ထဲသို့ Upload တင်ပါ။\n"
                "2️⃣ <b>'✨ AI Recap Video'</b> ခလုတ်ကို နှိပ်ပါ။\n"
                "3️⃣ <b>'🖼️ စတိုင် & Preview'</b> တွင် Blur Box၊ စာတန်းထိုးနှင့် Logo များကို စိတ်ကြိုက် ချိန်ညှိပါ။\n"
                "4️⃣ <b>'🚀 1-Click Pro Recap'</b> ကို နှိပ်လိုက်ရုံဖြင့် ပြီးပြည့်စုံသော Recap Video ရရှိပါမည်။\n\n"
                "🎙️ <b>အသံရွေးချယ်မှု (Voice):</b>\n"
                "• Thiha (ယောက်ျားသံ) / Nilar (မိန်းမသံ)\n"
                "• Voice Clone Pro မှ မိမိ Train ထားသော Clone အသံမော်ဒယ်များ (🎭 Clone)\n\n"
                "🖼️ <b>Channel Logo ထည့်သွင်းနည်း:</b>\n"
                "• မိမိ Logo (PNG/JPG) ဓာတ်ပုံကို Chat ထဲသို့ ပို့ပေးလိုက်ရုံဖြင့် အလိုအလျောက် သတ်မှတ်ပေးပါမည်။\n\n"
                "⌨️ <b>စာရိုက်၍ ချိန်ညှိနိုင်သော အမိန့်များ:</b>\n"
                "• <code>/blur Y H [X W]</code> - ဥပမာ <code>/blur 81 13</code> သို့မဟုတ် <code>/blur 81 13 5 90</code>\n"
                "• <code>/sub Y</code> - စာတန်းထိုး နေရာ အနိမ့်အမြင့် (ဥပမာ <code>/sub 85</code>)\n"
                "• <code>/lines 1</code> သို့မဟုတ် <code>/lines 2</code> - စာကြောင်း အရေအတွက်\n"
                "• <code>/font 16</code> - စာလုံးအရွယ်အစား (13, 16, 19, 22pt)"
            )
            help_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔙 မူလစာမျက်နှာ (Back)", callback_data="menu:start"),
                    InlineKeyboardButton("🎙️ Voice Clone အသံများ", callback_data="menu:voices"),
                ]
            ])
            await safe_edit_menu(query, help_text, help_kb)
            return
        elif session_id == "voices":
            voice_text = (
                "🎙️ <b>ရရှိနိုင်သော အသံမော်ဒယ်များ (Available Voices)</b>\n\n"
                "<b>1️⃣ Microsoft Edge Natural TTS:</b>\n"
                "• 👨 <b>Thiha:</b> သဘာဝကျသော မြန်မာအမျိုးသားအသံ (စံသတ်မှတ်ချက်)\n"
                "• 👩 <b>Nilar:</b> သဘာဝကျသော မြန်မာအမျိုးသမီးအသံ\n\n"
                "<b>2️⃣ Voice Clone Pro (Applio RVC Engine):</b>\n"
                "• 🎭 Voice Clone Pro Official တွင် Train ထားရှိသော မော်ဒယ်များအားလုံးကို အလိုအလျောက် ချိတ်ဆက်ပေးထားပါသည်\n"
                "• 🎬 Recap ပြုလုပ်ရာတွင် <b>'🎙️ အသံ'</b> ခလုတ်ကို နှိပ်၍ မိမိစိတ်ကြိုက် Clone မော်ဒယ်သို့ အလွယ်တကူ ပြောင်းလဲနိုင်ပါသည်!"
            )
            voice_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔙 မူလစာမျက်နှာ (Back)", callback_data="menu:start"),
                    InlineKeyboardButton("📖 အသုံးပြုနည်း လမ်းညွှန်", callback_data="menu:help"),
                ]
            ])
            await safe_edit_menu(query, voice_text, voice_kb)
            return
        elif session_id == "start":
            user_id = update.effective_user.id if update.effective_user else None
            await safe_edit_menu(query, _make_welcome_text(user_id), _make_welcome_markup())
            return

    # ON-DEMAND RECAP ASSETS DELIVERY (VOICEOVER / SRT / SCRIPT)
    if action == "rc_dl":
        dl_type = parts[2] if len(parts) > 2 else ""
        deliv = RECAP_DELIVERY_OUTPUTS.get(session_id)
        if not deliv:
            finished_dir = os.path.join(DOWNLOAD_DIR, "recap_finished", session_id)
            if os.path.isdir(finished_dir):
                deliv = {"session_id": session_id, "title": "Recap Video", "voice_label": "AI Voice"}
                for f in os.listdir(finished_dir):
                    fp = os.path.join(finished_dir, f)
                    if f.endswith((".mp3", ".wav", ".aac")):
                        deliv["audio"] = fp
                    elif f.endswith(".srt"):
                        deliv["srt"] = fp
                    elif f.endswith(".txt"):
                        deliv["script"] = fp
                RECAP_DELIVERY_OUTPUTS[session_id] = deliv

        if not deliv:
            try:
                await query.answer("⚠️ ဖိုင်သက်တမ်း ကုန်ဆုံးသွားပါပြီ (သို့မဟုတ်) ဖိုင်ရှာမတွေ့ပါခင်ဗျာ။", show_alert=True)
            except Exception:
                pass
            return

        title_safe = deliv.get("title", "Recap Video")
        v_name = deliv.get("voice_label", "AI Voice")

        if dl_type == "aud":
            aud_path = deliv.get("audio")
            if aud_path and os.path.exists(aud_path):
                try:
                    await query.answer("🎙️ Voiceover အသံဖိုင် ပေးပို့နေပါသည်...")
                except Exception:
                    pass
                with open(aud_path, "rb") as af:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=af,
                        title=f"{title_safe} (Voiceover)",
                        performer=v_name,
                        caption=(
                            f"🎙️ <b>Recap Voiceover Audio</b>\n"
                            f"🎬 <b>{html.escape(title_safe)}</b>\n"
                            f"🗣️ <b>အသံ:</b> {v_name}\n"
                            f"💡 <i>CapCut / Video Editor များတွင် သီးခြားအသုံးပြုနိုင်ပါသည်</i>"
                        ),
                        parse_mode=constants.ParseMode.HTML
                    )
            else:
                try:
                    await query.answer("⚠️ Voiceover အသံဖိုင် ရှာမတွေ့ပါခင်ဗျာ။", show_alert=True)
                except Exception:
                    pass
            return

        if dl_type == "srt":
            srt_path = deliv.get("srt")
            if srt_path and os.path.exists(srt_path):
                try:
                    await query.answer("📝 Subtitle (SRT) ပေးပို့နေပါသည်...")
                except Exception:
                    pass
                with open(srt_path, "rb") as sf:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=sf,
                        filename=os.path.basename(srt_path),
                        caption=(
                            f"📝 <b>Myanmar Subtitles (SRT):</b> <code>{html.escape(os.path.basename(srt_path))}</code>\n"
                            f"🎬 <b>{html.escape(title_safe)}</b>\n"
                            f"💡 <i>CapCut / Premiere Pro တွင် စာတန်းထိုးအဖြစ် တိုက်ရိုက် ထည့်သွင်းနိုင်ပါသည်</i>"
                        ),
                        parse_mode=constants.ParseMode.HTML
                    )
            else:
                try:
                    await query.answer("⚠️ Subtitle (SRT) ဖိုင် ရှာမတွေ့ပါခင်ဗျာ။", show_alert=True)
                except Exception:
                    pass
            return

        if dl_type == "scr":
            scr_path = deliv.get("script")
            if scr_path and os.path.exists(scr_path):
                try:
                    await query.answer("📄 ဇာတ်ညွှန်း Script ပေးပို့နေပါသည်...")
                except Exception:
                    pass
                with open(scr_path, "rb") as scf:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=scf,
                        filename=os.path.basename(scr_path),
                        caption=(
                            f"📄 <b>Myanmar Recap Script:</b> <code>{html.escape(os.path.basename(scr_path))}</code>\n"
                            f"🎬 <b>{html.escape(title_safe)}</b>\n"
                            f"🗣️ <b>အသံ:</b> {v_name}"
                        ),
                        parse_mode=constants.ParseMode.HTML
                    )
            else:
                try:
                    await query.answer("⚠️ ဇာတ်ညွှန်း Script ဖိုင် ရှာမတွေ့ပါခင်ဗျာ။", show_alert=True)
                except Exception:
                    pass
            return

        return

    if chat_id:
        USER_ACTIVE_SESSION[chat_id] = session_id
    session = SESSIONS.get(session_id)

    if not session:
        expired_text = "⚠️ <b>ဤ Request သည် သက်တမ်းကုန်သွားပါပြီ။</b> ကျေးဇူးပြု၍ Link သို့မဟုတ် Video ကို နောက်တစ်ကြိမ် ပြန်ပို့ပေးပါခင်ဗျာ。"
        await safe_edit_menu(query, expired_text, None)
        return

    url = session["expanded_url"]
    info = session["info"]
    is_local = session.get("is_local", False)
    loop = asyncio.get_running_loop()

    # Unique job directory to avoid file collisions
    job_dir = os.path.join(downloader.download_dir, f"job_{session_id}")

    # 1. ACTION: INFO
    if action == "nfo":
        info_title = html.escape(str(info.get('title', 'Unknown')))
        info_uploader = html.escape(str(info.get('uploader', 'Unknown')))
        info_platform = html.escape(str(info.get('platform', 'Unknown')))
        fsize_str = html.escape(str(info.get('filesize_formatted', 'N/A')))

        if is_local:
            res_str = f"{info.get('width', 'N/A')}x{info.get('height', 'N/A')}" if info.get('width') else "Audio Only"
            vcodec_str = html.escape(str(info.get('vcodec') or "None"))
            acodec_str = html.escape(str(info.get('acodec') or "None"))
            info_text = (
                f"ℹ️ <b>Media Details (Uploaded File)</b>\n\n"
                f"📌 <b>ခေါင်းစဉ်:</b> {info_title}\n"
                f"👤 <b>Sender:</b> {info_uploader}\n"
                f"🌐 <b>Platform:</b> {info_platform}\n"
                f"⏱️ <b>Duration:</b> <code>{info.get('duration_formatted', '00:00')}</code>\n"
                f"📁 <b>File Size:</b> <code>{fsize_str}</code>\n"
                f"📐 <b>Resolution:</b> <code>{res_str}</code>\n"
                f"🎞️ <b>Video Codec:</b> <code>{vcodec_str}</code>\n"
                f"🔊 <b>Audio Codec:</b> <code>{acodec_str}</code>\n"
                f"📝 <b>Subtitles:</b> <code>AI Whisper Transcribe Support</code>\n"
            )
        else:
            sub_list = ", ".join(info.get("available_subs", [])[:10]) or "None"
            info_orig = html.escape(str(info.get('original_url', '')))
            sub_list_esc = html.escape(sub_list)
            info_text = (
                f"ℹ️ <b>Media Details</b>\n\n"
                f"📌 <b>ခေါင်းစဉ်:</b> {info_title}\n"
                f"👤 <b>Channel:</b> {info_uploader}\n"
                f"🌐 <b>Platform:</b> {info_platform}\n"
                f"⏱️ <b>Duration:</b> <code>{info.get('duration_formatted', '00:00')}</code>\n"
                f"📁 <b>File Size:</b> <code>{fsize_str}</code>\n"
                f"📝 <b>Available Subtitles:</b> <code>{sub_list_esc}</code>\n"
                f"🔗 <b>Original URL:</b> <a href=\"{info_orig}\">Watch Link</a>\n"
            )
        await context.bot.send_message(
            chat_id=chat_id,
            text=info_text,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    # 2. ACTION: SUBTITLES (SRT)
    if action == "srt":
        has_native = len(info.get("available_subs", [])) > 0
        if has_native:
            status_text = (
                "⏳ <b>Subtitle (SRT) ဖိုင်များကို ဒေါင်းလုဒ်လုပ်နေပါသည်...</b>\n"
                "<i>(Downloading available subtitles...)</i>"
            )
        else:
            status_text = (
                "🎙️ <b>ဗီဒီယိုတွင် မူလ Subtitle မပါရှိသဖြင့် AI (Whisper) ဖြင့် စာတန်းထိုး ထုတ်ယူနေပါသည်...</b>\n"
                "<i>(Transcribing audio to SRT subtitles... ခေတ္တစောင့်ဆိုင်းပါ)</i>"
            )

        status_update = await context.bot.send_message(
            chat_id=chat_id,
            text=status_text,
            parse_mode=constants.ParseMode.HTML
        )

        try:
            srt_files, sub_info = await loop.run_in_executor(
                None,
                downloader.download_subtitles,
                url,
                job_dir,
                "en"
            )

            if not srt_files:
                await status_update.edit_text(
                    "⚠️ <b>ဤ Video တွင် Subtitle ထုတ်ယူနိုင်ခြင်း မရှိပါခင်ဗျာ။</b>",
                    parse_mode=constants.ParseMode.HTML
                )
                return

            await status_update.edit_text(
                "📤 <b>SRT Subtitle ဖိုင်ကို Telegram သို့ ပို့ဆောင်နေပါသည်...</b>",
                parse_mode=constants.ParseMode.HTML
            )

            is_ai = sub_info.get("is_ai_generated", False)
            detected_lang = str(sub_info.get("detected_lang", "")).upper()

            for srt_path in srt_files:
                filename = os.path.basename(srt_path)
                with open(srt_path, "rb") as f:
                    if is_ai:
                        caption_text = (
                            f"📝 <b>AI Generated Subtitle (SRT):</b> <code>{html.escape(filename)}</code>\n"
                            f"🌐 <b>Detected Speech Language:</b> <code>{html.escape(detected_lang or 'Auto')}</code>\n"
                            f"🎬 {html.escape(info.get('title', '')[:50])}"
                        )
                    else:
                        caption_text = (
                            f"📝 <b>Subtitle File:</b> <code>{html.escape(filename)}</code>\n"
                            f"🎬 {html.escape(info.get('title', '')[:50])}"
                        )

                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        caption=caption_text,
                        parse_mode=constants.ParseMode.HTML
                    )

            try:
                await status_update.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Subtitle download error: {e}")
            await status_update.edit_text(
                f"❌ Subtitle ဒေါင်းလုဒ်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: <code>{html.escape(str(e)[:150])}</code>",
                parse_mode=constants.ParseMode.HTML
            )
        finally:
            downloader.cleanup_files(job_dir)
        return

    # 3. ACTION: AUDIO (MP3)
    if action == "aud":
        status_update = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ <b>Audio (MP3) ကို ဒေါင်းလုဒ်ရယူပြီး ဖော်မက်ပြောင်းလဲနေပါသည်...</b>\n"
                 "<i>(Converting to High Quality MP3...)</i>",
            parse_mode=constants.ParseMode.HTML
        )

        try:
            mp3_path, meta = await loop.run_in_executor(
                None,
                downloader.download_audio,
                url,
                job_dir
            )

            if not os.path.exists(mp3_path):
                raise FileNotFoundError("Audio file was not generated.")

            file_size = os.path.getsize(mp3_path)
            if file_size > MAX_TELEGRAM_FILESIZE_BYTES:
                await status_update.edit_text(
                    f"⚠️ <b>Audio ဖိုင်ဆိုဒ် ({format_filesize(file_size)}) သည် Telegram Bot 50MB ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်။</b>",
                    parse_mode=constants.ParseMode.HTML
                )
                return

            await status_update.edit_text("📤 <b>MP3 သီချင်းဖိုင်ကို Telegram သို့ တင်ပို့နေပါသည်...</b>", parse_mode=constants.ParseMode.HTML)

            title_safe = info.get("title", "Audio")[:64]
            uploader_safe = info.get("uploader", "Unknown Artist")[:64]

            with open(mp3_path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    title=title_safe,
                    performer=uploader_safe,
                    duration=info.get("duration"),
                    caption=f"🎵 <b>{html.escape(title_safe)}</b>\n👤 {html.escape(uploader_safe)}",
                    parse_mode=constants.ParseMode.HTML
                )

            try:
                await status_update.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Audio download error: {e}")
            await status_update.edit_text(
                f"❌ Audio ဒေါင်းလုဒ်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: <code>{html.escape(str(e)[:150])}</code>",
                parse_mode=constants.ParseMode.HTML
            )
        finally:
            downloader.cleanup_files(job_dir)
        return

    # 4. ACTION: VIDEO (MP4)
    if action == "vid":
        status_update = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ <b>Video (MP4) ကို ဒေါင်းလုဒ်လုပ်နေပါသည်...</b>\n"
                 "<i>(Downloading best MP4 format...)</i>",
            parse_mode=constants.ParseMode.HTML
        )

        try:
            # Quality resolution: 1080p if Local Bot API active, otherwise 720p for Cloud API 50MB safety
            initial_max_h = 1080 if IS_LOCAL_BOT_API else 720
            video_path, meta = await loop.run_in_executor(
                None,
                downloader.download_video,
                url,
                job_dir,
                initial_max_h
            )

            if not os.path.exists(video_path):
                raise FileNotFoundError("Video file was not generated.")

            file_size = os.path.getsize(video_path)
            if file_size > MAX_TELEGRAM_FILESIZE_BYTES and not IS_LOCAL_BOT_API:
                # If still over limit on Cloud API, try 480p fallback
                await status_update.edit_text(
                    "⚠️ ဖိုင်ဆိုဒ် 50MB ကျော်လွန်နေသဖြင့် 480p ဖြင့် ထပ်မံ ကြိုးစားနေပါသည်...",
                    parse_mode=constants.ParseMode.HTML
                )
                downloader.cleanup_files(job_dir)
                video_path, meta = await loop.run_in_executor(
                    None,
                    downloader.download_video,
                    url,
                    job_dir,
                    480
                )
                file_size = os.path.getsize(video_path)

            if file_size > MAX_TELEGRAM_FILESIZE_BYTES:
                limit_str = format_filesize(MAX_TELEGRAM_FILESIZE_BYTES)
                await status_update.edit_text(
                    f"⚠️ <b>Video ဖိုင်အရွယ်အစား ({format_filesize(file_size)}) သည် Telegram Bot ၏ {limit_str} ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်။</b>\n\n"
                    "👉 Video ကြာချိန်ရှည်လျားလွန်းသဖြင့် <b>Audio (MP3)</b> ကိုသာ ရယူရန် အကြံပြုအပ်ပါသည်။",
                    parse_mode=constants.ParseMode.HTML
                )
                return

            await status_update.edit_text("📤 <b>Video ဖိုင်ကို Telegram သို့ တင်ပို့နေပါသည်...</b>", parse_mode=constants.ParseMode.HTML)

            video_title_safe = info.get('title', 'Video')[:60]
            video_uploader_safe = info.get('uploader', '')[:50]

            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"🎬 <b>{html.escape(video_title_safe)}</b>\n👤 {html.escape(video_uploader_safe)}",
                    duration=info.get("duration"),
                    supports_streaming=True,
                    parse_mode=constants.ParseMode.HTML
                )

            try:
                await status_update.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Video download error: {e}")
            await status_update.edit_text(
                f"❌ Video ဒေါင်းလုဒ်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: <code>{html.escape(str(e)[:150])}</code>",
                parse_mode=constants.ParseMode.HTML
            )
        finally:
            downloader.cleanup_files(job_dir)
        return

    # 5. ACTION: TIKTOK VIRAL RECAP (GEMINI AI)
    if action == "rcp":
        api_key = get_gemini_api_key()
        if not api_key:
            guide_text = (
                "⚠️ <b>Google Gemini API Key ထည့်သွင်းရန် လိုအပ်ပါသည်!</b>\n\n"
                "SRT ဖိုင်ကို မြန်မာဘာသာပြန်ပြီး TikTok တွင် Viral ဖြစ်စေမည့် Recap Script ရေးသားရန်အတွက် Google Gemini API Key လိုအပ်ပါသည်။\n\n"
                "🔑 <b>API Key ရယူနည်း (အခမဲ့ Free):</b>\n"
                "1. <a href=\"https://aistudio.google.com/\">Google AI Studio</a> သို့ ဝင်ရောက်ပါ။\n"
                "2. <b>Get API key</b> နှိပ်ပြီး Create API key ပြုလုပ်ပါ။\n"
                "3. <code>.env</code> ဖိုင်ထဲတွင် အောက်ပါအတိုင်း ထည့်သွင်းပါ -\n"
                "   <code>GEMINI_API_KEY=AIzaSy...</code>\n"
                "4. Bot ကို Restart ပြန်လုပ်ပြီး ချက်ချင်း အသုံးပြုနိုင်ပါပြီခင်ဗျာ。"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=guide_text,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True
            )
            return

        status_update = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ <b>Gemini AI ဖြင့် TikTok Viral Recap Script & မြန်မာစာတန်းထိုး ဖန်တီးနေပါသည်...</b>\n"
                 "<i>(1. Subtitle အချက်အလက် ရယူခြင်း ➔ 2. Gemini AI ဖြင့် ဗိုင်းရပ်စ် ဇာတ်ညွှန်း ရေးသားခြင်း...)</i>",
            parse_mode=constants.ParseMode.HTML
        )

        try:
            recap_data = await loop.run_in_executor(
                None,
                downloader.generate_viral_recap,
                url,
                job_dir
            )

            script_text = recap_data.get("recap_script", "")
            script_path = recap_data.get("script_path")
            myanmar_srt_path = recap_data.get("myanmar_srt_path")

            # Telegram has a 4096 char limit for messages, so truncate preview safely
            display_script = script_text[:3400]
            if len(script_text) > 3400:
                display_script += "\n\n<i>(...ကျန်ရှိသော ဇာတ်ညွှန်း အပြည့်အစုံကို အောက်ပါ Text ဖိုင်တွင် ဖတ်ရှုနိုင်ပါသည်)</i>"

            chat_reply = (
                f"🔥 <b>TikTok Viral Recap Script (မြန်မာဘာသာ)</b>\n"
                f"🎬 <b>{html.escape(info.get('title', 'Video')[:50])}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{html.escape(display_script)}"
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text=chat_reply,
                parse_mode=constants.ParseMode.HTML
            )

            # Send script as text document
            if script_path and os.path.exists(script_path):
                with open(script_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(script_path),
                        caption=f"📄 <b>TikTok Voiceover Script:</b> <code>{html.escape(os.path.basename(script_path))}</code>\n"
                                f"🎙️ <i>TikTok / Reels အတွက် အသံဖတ်ရန် ဇာတ်ညွှန်း</i>",
                        parse_mode=constants.ParseMode.HTML
                    )

            # Send Myanmar SRT if generated
            if myanmar_srt_path and os.path.exists(myanmar_srt_path):
                with open(myanmar_srt_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(myanmar_srt_path),
                        caption=f"📝 <b>Myanmar Subtitles (SRT):</b> <code>{html.escape(os.path.basename(myanmar_srt_path))}</code>\n"
                                f"💡 <i>CapCut / Premiere Pro တွင် စာတန်းထိုးအဖြစ် တိုက်ရိုက် ထည့်သွင်းနိုင်ပါသည်</i>",
                        parse_mode=constants.ParseMode.HTML
                    )

            try:
                await status_update.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"TikTok recap generation error: {e}")
            await status_update.edit_text(
                f"❌ TikTok Recap ထုတ်ယူရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: <code>{html.escape(str(e)[:180])}</code>",
                parse_mode=constants.ParseMode.HTML
            )
        finally:
            downloader.cleanup_files(job_dir)
        return

    # 6. ACTION: BACK TO MAIN OPTIONS
    if action == "bck":
        is_up = bool(session.get("is_upload") or session.get("is_local"))
        sname = session.get("sender_name", "")
        caption = _make_main_action_caption(info, is_upload=is_up, sender_name=sname)
        reply_markup = _make_main_action_markup(session_id)
        await safe_edit_menu(query, caption, reply_markup)
        return

    # 7. ACTION: RECAP MENU
    if action == "rec":
        api_key = get_gemini_api_key()
        if not api_key:
            guide_text = (
                "⚠️ <b>Google Gemini API Key ထည့်သွင်းရန် လိုအပ်ပါသည်!</b>\n\n"
                "Recap Video ပြုလုပ်ရန်အတွက် Google Gemini API Key လိုအပ်ပါသည်။\n"
                "<code>.env</code> ဖိုင်ထဲတွင် <code>GEMINI_API_KEY=...</code> ထည့်သွင်းပေးပါခင်ဗျာ。"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=guide_text,
                parse_mode=constants.ParseMode.HTML
            )
            return

        opts = _get_recap_opts(session)
        recap_caption = _make_recap_caption(info, opts)
        recap_kb = _make_recap_markup(session_id, opts)
        await safe_edit_menu(query, recap_caption, recap_kb)
        return

    # ACTION: RECAP PREVIEW FRAME
    if action == "rcp_prev":
        opts = _get_recap_opts(session)
        try:
            await query.answer("⏳ Preview Frame ထုတ်ယူနေပါသည်...")
        except Exception:
            pass
        try:
            logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
            logo_path_val = str(opts.get("logo_path")) if logo_on else None
            logo_coords_val = _get_logo_coords(opts.get("logo_pos", "top-right")) if logo_on else None

            if not opts.get("preview_time"):
                try:
                    from downloader import find_best_preview_timestamp
                    opts["preview_time"] = find_best_preview_timestamp(url, job_dir)
                except Exception:
                    opts["preview_time"] = 14.5

            prev_img = await loop.run_in_executor(
                None,
                functools.partial(
                    downloader.generate_recap_preview,
                    url_or_path=url,
                    target_dir=job_dir,
                    blur_subtitles=opts.get("blur_subs", True),
                    blur_y=opts.get("blur_y", 0.81),
                    blur_h=opts.get("blur_h", 0.13),
                    blur_x=opts.get("blur_x", 0.05),
                    blur_w=opts.get("blur_w", 0.90),
                    burn_subtitles=opts.get("burn_subs", True),
                    sub_y=opts.get("sub_y", 0.85),
                    sub_lines=int(opts.get("sub_lines", 1)),
                    sub_font_size=int(opts.get("sub_font_size", 16)),
                    sub_style=opts.get("sub_style", "yellow"),
                    output_ratio=opts.get("ratio", "Original"),
                    mirror_video=opts.get("mirror", True),
                    zoom_factor=opts.get("zoom", 1.06),
                    at_seconds=float(opts.get("preview_time", 14.5)),
                    logo_enabled=logo_on,
                    logo_path=logo_path_val,
                    logo_position=logo_coords_val,
                    logo_scale=float(opts.get("logo_scale", 0.16)),
                    logo_opacity=float(opts.get("logo_opacity", 0.70)),
                )
            )
            with open(prev_img, "rb") as pf:
                if query.message and query.message.photo:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=pf, caption=_make_preview_caption(opts), parse_mode=constants.ParseMode.HTML),
                        reply_markup=_make_preview_markup(session_id, opts)
                    )
                else:
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=pf,
                        caption=_make_preview_caption(opts),
                        reply_markup=_make_preview_markup(session_id, opts),
                        parse_mode=constants.ParseMode.HTML,
                    )
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logger.error(f"Error displaying recap preview: {e}")
            try:
                await query.answer(f"❌ Preview Error: {str(e)[:50]}", show_alert=True)
            except Exception:
                pass
        return

    # ACTION: RECAP PREVIEW INTERACTIVE ADJUSTMENT
    if action == "rcadj":
        parts = data.split(":")
        adj_cmd = parts[2] if len(parts) > 2 else "refresh"
        opts = _get_recap_opts(session)

        # 0. Tab switching (Instant - no re-render)
        if adj_cmd in ("tab_blur", "tab_sub", "tab_logo"):
            target_tab = adj_cmd.replace("tab_", "")
            opts["preview_tab"] = target_tab
            try:
                await query.edit_message_caption(
                    caption=_make_preview_caption(opts),
                    reply_markup=_make_preview_markup(session_id, opts),
                    parse_mode=constants.ParseMode.HTML
                )
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logger.warning(f"Error switching preview tab: {e}")
            return

        # 1. Blur Controls
        if adj_cmd == "bx_left":
            opts["blur_x"] = max(0.00, round(opts.get("blur_x", 0.05) - 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd == "bx_right":
            opts["blur_x"] = min(0.90, round(opts.get("blur_x", 0.05) + 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd in ("by_up", "y_up"):
            opts["blur_y"] = max(0.05, round(opts.get("blur_y", 0.81) - 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd in ("by_down", "y_down"):
            opts["blur_y"] = min(0.95, round(opts.get("blur_y", 0.81) + 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd in ("bh_up", "h_up"):
            opts["blur_h"] = min(0.40, round(opts.get("blur_h", 0.13) + 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd in ("bh_down", "h_down"):
            opts["blur_h"] = max(0.04, round(opts.get("blur_h", 0.13) - 0.02, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd == "bw_up":
            opts["blur_w"] = min(1.00, round(opts.get("blur_w", 0.90) + 0.04, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd == "bw_down":
            opts["blur_w"] = max(0.10, round(opts.get("blur_w", 0.90) - 0.04, 2))
            opts["blur_preset"] = "custom"
        elif adj_cmd == "b_reset":
            opts["blur_x"] = 0.05
            opts["blur_w"] = 0.90
            opts["blur_y"] = 0.81
            opts["blur_h"] = 0.13
            opts["blur_preset"] = "mid"
        elif adj_cmd == "t_blur":
            opts["blur_subs"] = not opts.get("blur_subs", True)

        # 2. Subtitle Controls
        elif adj_cmd == "sy_up":
            opts["sub_y"] = max(0.05, round(opts.get("sub_y", 0.85) - 0.02, 2))
        elif adj_cmd == "sy_down":
            opts["sub_y"] = min(0.98, round(opts.get("sub_y", 0.85) + 0.02, 2))
        elif adj_cmd == "t_lines":
            opts["sub_lines"] = 2 if int(opts.get("sub_lines", 1)) == 1 else 1
        elif adj_cmd == "t_fsize":
            f_cycle = [13, 16, 19, 22]
            cur_f = int(opts.get("sub_font_size", 16))
            idx = f_cycle.index(cur_f) if cur_f in f_cycle else 1
            opts["sub_font_size"] = f_cycle[(idx + 1) % len(f_cycle)]
        elif adj_cmd == "t_sstyle":
            s_cycle = ["yellow", "white", "boxed", "neon"]
            cur_s = opts.get("sub_style", "yellow")
            idx = s_cycle.index(cur_s) if cur_s in s_cycle else 0
            opts["sub_style"] = s_cycle[(idx + 1) % len(s_cycle)]
        elif adj_cmd == "t_sub":
            opts["burn_subs"] = not opts.get("burn_subs", True)
        elif adj_cmd == "snap_sub":
            opts["sub_y"] = max(0.05, min(0.98, round(opts.get("blur_y", 0.81) + opts.get("blur_h", 0.13) * 0.55, 2)))

        # 3. Blur Presets
        elif adj_cmd == "p_cycle":
            p_list = ["mid", "low", "high"]
            cur_p = opts.get("blur_preset", "mid")
            idx = p_list.index(cur_p) if cur_p in p_list else 0
            nxt_p = p_list[(idx + 1) % len(p_list)]
            opts["blur_preset"] = nxt_p
            if nxt_p == "low":
                opts["blur_y"] = 0.85
                opts["blur_h"] = 0.12
            elif nxt_p == "high":
                opts["blur_y"] = 0.73
                opts["blur_h"] = 0.14
            else:
                opts["blur_y"] = 0.81
                opts["blur_h"] = 0.13
        elif adj_cmd == "p_low":
            opts["blur_y"] = 0.85
            opts["blur_h"] = 0.12
            opts["blur_preset"] = "low"
        elif adj_cmd == "p_mid":
            opts["blur_y"] = 0.81
            opts["blur_h"] = 0.13
            opts["blur_preset"] = "mid"
        elif adj_cmd == "p_high":
            opts["blur_y"] = 0.73
            opts["blur_h"] = 0.14
            opts["blur_preset"] = "high"

        # 4. Logo & Video Controls
        elif adj_cmd == "t_logo":
            opts["logo_enabled"] = not opts.get("logo_enabled", False)
        elif adj_cmd == "p_logo":
            pos_cycle = ["top-right", "top-left", "bottom-right", "bottom-left"]
            cur_p = opts.get("logo_pos", "top-right")
            idx = pos_cycle.index(cur_p) if cur_p in pos_cycle else 0
            opts["logo_pos"] = pos_cycle[(idx + 1) % len(pos_cycle)]
        elif adj_cmd == "s_logo":
            scale_cycle = [0.12, 0.16, 0.22]
            cur_s = float(opts.get("logo_scale", 0.16))
            idx = scale_cycle.index(cur_s) if cur_s in scale_cycle else 1
            opts["logo_scale"] = scale_cycle[(idx + 1) % len(scale_cycle)]
        elif adj_cmd == "t_mirror":
            opts["mirror"] = not opts.get("mirror", True)
        elif adj_cmd == "t_ratio":
            r_list = ["Original", "16:9", "9:16"]
            cur_r = opts.get("ratio", "Original")
            idx = r_list.index(cur_r) if cur_r in r_list else 0
            opts["ratio"] = r_list[(idx + 1) % len(r_list)]

        # 5. Scene Time Navigation
        elif adj_cmd == "t_prev10":
            cur_t = float(opts.get("preview_time", 14.5))
            opts["preview_time"] = max(1.0, round(cur_t - 10.0, 1))
        elif adj_cmd == "t_next10":
            cur_t = float(opts.get("preview_time", 14.5))
            media_dur = session.get("info", {}).get("duration", 0.0) or 600.0
            opts["preview_time"] = min(max(1.0, media_dur - 1.0), round(cur_t + 10.0, 1))
        elif adj_cmd == "t_refresh":
            pass

        save_all_user_settings(opts.get("user_id"), opts)

        try:
            logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
            logo_path_val = str(opts.get("logo_path")) if logo_on else None
            logo_coords_val = _get_logo_coords(opts.get("logo_pos", "top-right")) if logo_on else None

            if not opts.get("preview_time"):
                try:
                    from downloader import find_best_preview_timestamp
                    opts["preview_time"] = find_best_preview_timestamp(url, job_dir)
                except Exception:
                    opts["preview_time"] = 14.5

            prev_img = await loop.run_in_executor(
                None,
                functools.partial(
                    downloader.generate_recap_preview,
                    url_or_path=url,
                    target_dir=job_dir,
                    blur_subtitles=opts.get("blur_subs", True),
                    blur_y=opts.get("blur_y", 0.81),
                    blur_h=opts.get("blur_h", 0.13),
                    blur_x=opts.get("blur_x", 0.05),
                    blur_w=opts.get("blur_w", 0.90),
                    burn_subtitles=opts.get("burn_subs", True),
                    sub_y=opts.get("sub_y", 0.85),
                    sub_lines=int(opts.get("sub_lines", 1)),
                    sub_font_size=int(opts.get("sub_font_size", 16)),
                    sub_style=opts.get("sub_style", "yellow"),
                    output_ratio=opts.get("ratio", "Original"),
                    mirror_video=opts.get("mirror", True),
                    zoom_factor=opts.get("zoom", 1.06),
                    at_seconds=float(opts.get("preview_time", 14.5)),
                    logo_enabled=logo_on,
                    logo_path=logo_path_val,
                    logo_position=logo_coords_val,
                    logo_scale=float(opts.get("logo_scale", 0.16)),
                    logo_opacity=float(opts.get("logo_opacity", 0.70)),
                )
            )
            with open(prev_img, "rb") as pf:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=pf, caption=_make_preview_caption(opts), parse_mode=constants.ParseMode.HTML),
                    reply_markup=_make_preview_markup(session_id, opts)
                )
            for old_p in glob.glob(os.path.join(job_dir, "preview_*.jpg")):
                if os.path.abspath(old_p) != os.path.abspath(prev_img):
                    try:
                        os.remove(old_p)
                    except Exception:
                        pass
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logger.warning(f"Failed to edit preview message media: {e}")
            try:
                await query.edit_message_caption(
                    caption=_make_preview_caption(opts),
                    reply_markup=_make_preview_markup(session_id, opts),
                    parse_mode=constants.ParseMode.HTML
                )
            except Exception as cap_e:
                if "not modified" not in str(cap_e).lower():
                    logger.warning(f"Failed to edit preview caption fallback: {cap_e}")
        return

    # 8. ACTION: RECAP OPTION TOGGLE
    if action == "rct":
        parts = data.split(":")
        opt_key = parts[2] if len(parts) > 2 else ""
        opts = _get_recap_opts(session)

        if opt_key == "voice":
            cycle = _get_recap_voice_cycle()
            cur = opts.get("voice", "thiha")
            idx = cycle.index(cur) if cur in cycle else 0
            opts["voice"] = cycle[(idx + 1) % len(cycle)]
        elif opt_key == "style":
            cycle = ["viral", "funny", "narrator", "dramatic", "pov"]
            cur = opts.get("style", "viral")
            idx = cycle.index(cur) if cur in cycle else 0
            opts["style"] = cycle[(idx + 1) % len(cycle)]
        elif opt_key == "speed":
            cycle = ["+0%", "+8%", "+18%", "+22%"]
            cur = opts.get("speed", "+0%")
            idx = cycle.index(cur) if cur in cycle else 0
            opts["speed"] = cycle[(idx + 1) % len(cycle)]
        elif opt_key == "ratio":
            current_r = opts.get("ratio", "Original")
            ratio_cycle = ["Original", "9:16", "16:9"]
            idx = ratio_cycle.index(current_r) if current_r in ratio_cycle else 0
            opts["ratio"] = ratio_cycle[(idx + 1) % len(ratio_cycle)]
        elif opt_key == "blur_pos":
            presets = ["mid", "low", "high"]
            cur = opts.get("blur_preset", "mid")
            idx = presets.index(cur) if cur in presets else 0
            next_p = presets[(idx + 1) % len(presets)]
            opts["blur_preset"] = next_p
            if next_p == "low":
                opts["blur_y"] = 0.85
                opts["blur_h"] = 0.12
            elif next_p == "high":
                opts["blur_y"] = 0.73
                opts["blur_h"] = 0.14
            else:
                opts["blur_y"] = 0.81
                opts["blur_h"] = 0.13
        elif opt_key == "mirror":
            opts["mirror"] = not opts.get("mirror", True)
            opts["zoom"] = 1.06 if opts["mirror"] else 1.0
        elif opt_key == "blur_subs":
            opts["blur_subs"] = not opts.get("blur_subs", True)
        elif opt_key == "burn_subs":
            opts["burn_subs"] = not opts.get("burn_subs", True)
        elif opt_key == "sub_style":
            s_cycle = ["yellow", "white", "boxed", "neon"]
            cur_s = opts.get("sub_style", "yellow")
            idx = s_cycle.index(cur_s) if cur_s in s_cycle else 0
            opts["sub_style"] = s_cycle[(idx + 1) % len(s_cycle)]
        elif opt_key == "logo":
            opts["logo_enabled"] = not opts.get("logo_enabled", False)

        save_all_user_settings(opts.get("user_id"), opts)
        recap_caption = _make_recap_caption(info, opts)
        recap_kb = _make_recap_markup(session_id, opts)
        await safe_edit_menu(query, recap_caption, recap_kb)
        return

    # 9. ACTION: RUN RECAP PIPELINE (1-Click Pro or Custom)
    if action in ("rco", "rcv"):
        api_key = get_gemini_api_key()
        if not api_key:
            guide_text = (
                "⚠️ <b>Google Gemini API Key ထည့်သွင်းရန် လိုအပ်ပါသည်!</b>\n\n"
                "Recap Video ပြုလုပ်ရန်အတွက် Google Gemini API Key လိုအပ်ပါသည်။\n"
                "<code>.env</code> ဖိုင်ထဲတွင် <code>GEMINI_API_KEY=...</code> ထည့်သွင်းပေးပါခင်ဗျာ。"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=guide_text,
                parse_mode=constants.ParseMode.HTML
            )
            return

        parts = data.split(":")
        mode = parts[2] if len(parts) > 2 else "quick"
        opts = _get_recap_opts(session)
        style_val = opts.get("style", "narrator")
        speed_val = opts.get("speed", "+0%")

        if speed_val in ("+0%", "0%", ""):
            speed_val = "+12%"

        if action == "rcv":
            # Direct legacy callback
            voice_tag = mode
            if voice_tag == "nilar":
                selected_voice = "my-MM-NilarNeural"
            elif voice_tag == "thiha":
                selected_voice = "my-MM-ThihaNeural"
            elif voice_tag == "voxcpm":
                selected_voice = "voxcpm"
            else:
                selected_voice = voice_tag
            mirror_flag = True
            zoom_val = 1.06
            blur_subs_flag = True
            burn_subs_flag = True
            ratio_val = "Original"
        elif mode == "quick":
            # 1-Click Pro Recap preset
            voice_tag = opts.get("voice", "voxcpm")
            if voice_tag == "nilar":
                selected_voice = "my-MM-NilarNeural"
            elif voice_tag == "thiha":
                selected_voice = "my-MM-ThihaNeural"
            elif voice_tag == "voxcpm":
                selected_voice = "voxcpm"
            else:
                selected_voice = voice_tag
            mirror_flag = True
            zoom_val = 1.06
            blur_subs_flag = True
            burn_subs_flag = True
            ratio_val = opts.get("ratio", "Original")
        else:
            # Custom preset from toggles
            voice_tag = opts.get("voice", "voxcpm")
            if voice_tag == "nilar":
                selected_voice = "my-MM-NilarNeural"
            elif voice_tag == "thiha":
                selected_voice = "my-MM-ThihaNeural"
            elif voice_tag == "voxcpm":
                selected_voice = "voxcpm"
            else:
                selected_voice = voice_tag
            mirror_flag = bool(opts.get("mirror", True))
            zoom_val = float(opts.get("zoom", 1.06)) if mirror_flag else 1.0
            blur_subs_flag = bool(opts.get("blur_subs", True))
            burn_subs_flag = bool(opts.get("burn_subs", True))
            ratio_val = str(opts.get("ratio", "Original"))

        v_info = _get_voice_info(voice_tag)
        voice_label = v_info["label"]
        voice_is_clone = v_info["is_clone"]
        if style_val == "viral":
            style_title = "🔥 ဗိုင်းရပ်စ် Hook (Viral Algorithm)"
        elif style_val == "funny":
            style_title = "😂 ဟာသရွဲ့စောင်း (Humorous Roast)"
        elif style_val == "pov":
            style_title = "🎭 ဇာတ်ကောင် POV (Character)"
        elif style_val == "dramatic":
            style_title = "⚡ သည်းထိတ်ရင်ဖို (Dramatic)"
        else:
            style_title = "🎬 ဇာတ်လမ်းပြော (Narrator)"

        blur_y_val = float(opts.get("blur_y", 0.81))
        blur_h_val = float(opts.get("blur_h", 0.13))
        blur_x_val = float(opts.get("blur_x", 0.05))
        blur_w_val = float(opts.get("blur_w", 0.90))

        sub_y_val = float(opts.get("sub_y", 0.85))
        sub_lines_val = int(opts.get("sub_lines", 1))
        sub_font_size_val = int(opts.get("sub_font_size", 16))

        logo_on = bool(opts.get("logo_enabled") and opts.get("logo_path") and os.path.isfile(str(opts.get("logo_path"))))
        logo_path_val = str(opts.get("logo_path")) if logo_on else None
        logo_coords_val = _get_logo_coords(opts.get("logo_pos", "top-right")) if logo_on else None
        logo_scale_val = float(opts.get("logo_scale", 0.16))
        logo_opacity_val = float(opts.get("logo_opacity", 0.70))

        features = []
        if mirror_flag:
            features.append("🪞 Flip/Mirror")
        if zoom_val > 1.0:
            features.append(f"🔍 Zoom {zoom_val:.2f}x")
        if blur_subs_flag:
            features.append(f"🌫️ Sub Blur ({int(round(blur_y_val*100))}%, {int(round(blur_h_val*100))}%)")
        if burn_subs_flag:
            line_txt = "1-Line" if sub_lines_val == 1 else "2-Lines"
            features.append(f"📝 {line_txt} Subs ({sub_font_size_val}pt, Y={int(round(sub_y_val*100))}%)")
        if logo_on:
            features.append(f"🏷️ Logo ({opts.get('logo_pos', 'top-right')})")
        features_str = " · ".join(features) if features else "None"

        if voice_is_clone:
            voice_tech_desc = f"Voice Clone Pro (Applio RVC · {v_info['name']})"
            step4_desc = (
                f"4️⃣ Microsoft Edge TTS သန့်ရှင်းသော အခြေခံအသံ ထုတ်ယူခြင်း\n"
                f"5️⃣ Voice Clone Pro (Applio RVC) ဖြင့် '{v_info['name']}' အသံသို့ ပြောင်းလဲခြင်း"
            )
            step6_num = "6️⃣"
            step7_num = "7️⃣"
        else:
            voice_tech_desc = f"Microsoft Edge TTS ({voice_label})"
            step4_desc = f"4️⃣ Microsoft Edge TTS ({voice_label} · {speed_val}) ထုတ်ယူခြင်း"
            step6_num = "5️⃣"
            step7_num = "6️⃣"

        status_update = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏳ <b>Recap English → Myanmar Video စတင်ဖန်တီးနေပါသည်...</b>\n"
                f"🎭 <b>တင်ဆက်မှုပုံစံ:</b> {style_title}\n"
                f"🎙️ <b>အသံ & နှုန်း:</b> {voice_label} (<code>{speed_val}</code>)\n"
                f"⚙️ <b>အသံနည်းပညာ:</b> {voice_tech_desc}\n"
                f"🛡️ <b>Anti-Copyright:</b> {features_str}\n"
                f"📐 <b>Ratio:</b> {ratio_val}\n\n"
                f"🔄 <i>လုပ်ဆောင်ချက် အဆင့်များ:</i>\n"
                f"1️⃣ English စကားသံကို အချိန်ကိုက် စာသားပြောင်းခြင်း (Whisper AI)\n"
                f"2️⃣ Gemini ဖြင့် ဇာတ်လမ်းနောက်ခံ မှတ်သားခြင်း\n"
                f"3️⃣ {style_title} ပုံစံဖြင့် မြန်မာလို အပိုဒ်လိုက် ဘာသာပြန်ဆိုခြင်း\n"
                f"{step4_desc}\n"
                f"{step6_num} Smart Sync (ရုပ်နှင့်အသံ 30fps အချိန်ကိုက် Video Render)\n"
                f"{step7_num} Anti-Copyright Transform (Flip, Zoom, Subtitle Blur, {sub_lines_val}-Line Subs)\n\n"
                f"<i>ခေတ္တစောင့်ဆိုင်းပေးပါခင်ဗျာ... (ဗီဒီယိုအရှည်ပေါ်မူတည်၍ အနည်းငယ် ကြာနိုင်ပါသည်)</i>"
            ),
            parse_mode=constants.ParseMode.HTML
        )

        last_progress_text = [""]
        def progress_cb(cur: int, tot: int, label: str):
            text = f"⏳ <b>Recap Process ({cur}/{tot}):</b>\n<code>{html.escape(str(label))}</code>"
            if text != last_progress_text[0]:
                last_progress_text[0] = text
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_update.edit_text(text, parse_mode=constants.ParseMode.HTML),
                        loop
                    )
                except Exception:
                    pass

        try:
            recap_fn = functools.partial(
                downloader.generate_recap_video,
                url_or_path=url,
                target_dir=job_dir,
                voice=selected_voice,
                narrative_style=style_val,
                voice_speed=speed_val,
                mirror_video=mirror_flag,
                zoom_factor=zoom_val,
                blur_subtitles=blur_subs_flag,
                blur_y=blur_y_val,
                blur_h=blur_h_val,
                blur_x=blur_x_val,
                blur_w=blur_w_val,
                blur_watermark=False,
                burn_subtitles=burn_subs_flag,
                sub_y=sub_y_val,
                sub_lines=sub_lines_val,
                sub_font_size=sub_font_size_val,
                sub_style=opts.get("sub_style", "yellow"),
                title_text=None,
                logo_path=logo_path_val,
                logo_position=logo_coords_val,
                logo_scale=logo_scale_val,
                logo_opacity=logo_opacity_val,
                extra_text=None,
                output_ratio=ratio_val,
                progress_callback=progress_cb,
            )
            recap_res = await loop.run_in_executor(None, recap_fn)

            final_video_path = recap_res.get("final_video") or recap_res.get("final_video_path")
            final_audio_path = recap_res.get("audio") or recap_res.get("final_audio_path")
            final_srt_path = recap_res.get("subtitles_srt")
            plan_path = recap_res.get("plan_path") or recap_res.get("plan")

            if not final_video_path or not os.path.exists(final_video_path):
                raise FileNotFoundError("Recap final video was not generated.")

            file_size = os.path.getsize(final_video_path)
            title_safe = info.get("title", "Recap Video")[:60]
            dur_sec = recap_res.get("duration", info.get("duration", 0))

            # Prepare safe copy of assets in recap_finished/{session_id} for on-demand downloads
            finished_dir = os.path.join(downloader.download_dir, "recap_finished", session_id)
            os.makedirs(finished_dir, exist_ok=True)

            deliv_data = {
                "session_id": session_id,
                "title": title_safe,
                "voice_label": voice_label,
                "created_at": datetime.now().timestamp(),
            }

            if final_audio_path and os.path.exists(final_audio_path):
                saved_audio = os.path.join(finished_dir, os.path.basename(final_audio_path))
                try:
                    shutil.copy2(final_audio_path, saved_audio)
                    deliv_data["audio"] = saved_audio
                except Exception as e_a:
                    logger.warning(f"Failed to copy final audio: {e_a}")
                    deliv_data["audio"] = final_audio_path

            if final_srt_path and os.path.exists(final_srt_path):
                saved_srt = os.path.join(finished_dir, os.path.basename(final_srt_path))
                try:
                    shutil.copy2(final_srt_path, saved_srt)
                    deliv_data["srt"] = saved_srt
                except Exception as e_s:
                    logger.warning(f"Failed to copy final srt: {e_s}")
                    deliv_data["srt"] = final_srt_path

            # Generate and save clean Burmese recap script text
            script_lines = []
            if final_srt_path and os.path.exists(final_srt_path):
                try:
                    with open(final_srt_path, "r", encoding="utf-8", errors="replace") as srt_f:
                        for s_line in srt_f:
                            s_str = s_line.strip()
                            if not s_str or s_str.isdigit() or "-->" in s_str:
                                continue
                            if s_str not in script_lines:
                                script_lines.append(s_str)
                except Exception as e_srt:
                    logger.warning(f"Failed to parse SRT for script: {e_srt}")

            if not script_lines and plan_path and os.path.exists(plan_path):
                try:
                    with open(plan_path, "r", encoding="utf-8") as pf:
                        plan_obj = json.load(pf)
                        for seg in plan_obj.get("segments", []):
                            txt = seg.get("translated_text") or seg.get("myanmar") or seg.get("narration") or ""
                            if txt:
                                script_lines.append(str(txt).strip())
                except Exception as e_p:
                    logger.warning(f"Failed to read plan for script: {e_p}")

            if script_lines:
                saved_script = os.path.join(finished_dir, f"{session_id}_myanmar_script.txt")
                try:
                    with open(saved_script, "w", encoding="utf-8") as sc_f:
                        sc_f.write(f"🎬 AI Myanmar Movie Recap Script: {title_safe}\n")
                        sc_f.write(f"🎙️ အသံ: {voice_label} | စတိုင်: {style_title}\n")
                        sc_f.write("=" * 50 + "\n\n")
                        sc_f.write("\n\n".join(script_lines))
                    deliv_data["script"] = saved_script
                except Exception as e_sc:
                    logger.warning(f"Failed to write script file: {e_sc}")

            RECAP_DELIVERY_OUTPUTS[session_id] = deliv_data

            # 1. Send Recap Video ONLY (with on-demand buttons for Audio, Subtitles, Script)
            delivery_kb = _make_recap_delivery_markup(session_id)
            if file_size <= MAX_TELEGRAM_FILESIZE_BYTES:
                await status_update.edit_text(
                    "📤 <b>မြန်မာ Recap Video ကို Telegram သို့ တင်ပို့နေပါသည်...</b>",
                    parse_mode=constants.ParseMode.HTML
                )
                with open(final_video_path, "rb") as vf:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=vf,
                        caption=(
                            f"🇲🇲 <b>Recap English → Myanmar Video (Smart Sync)</b>\n"
                            f"🎬 <b>{html.escape(title_safe)}</b>\n"
                            f"🎭 <b>စတိုင်:</b> {style_title}\n"
                            f"🎙️ <b>အသံ:</b> {voice_label} (<code>{speed_val}</code>)\n"
                            f"🛡️ <b>Anti-Copyright:</b> {features_str}\n"
                            f"⏱️ <b>ကြာချိန်:</b> <code>{format_duration(dur_sec)}</code>\n"
                            f"📁 <b>ဖိုင်ဆိုဒ်:</b> <code>{format_filesize(file_size)}</code>\n\n"
                            f"💡 <i>သီးခြား Voiceover အသံ၊ စာတန်းထိုး (SRT) သို့မဟုတ် ဇာတ်ညွှန်း (Script) လိုအပ်ပါက အောက်ပါ ခလုတ်များဖြင့် ရယူနိုင်ပါသည် -</i>"
                        ),
                        reply_markup=delivery_kb,
                        duration=int(dur_sec) if dur_sec else None,
                        supports_streaming=True,
                        read_timeout=600,
                        write_timeout=600,
                        parse_mode=constants.ParseMode.HTML
                    )
            else:
                size_str = format_filesize(file_size)
                limit_str = format_filesize(MAX_TELEGRAM_FILESIZE_BYTES)
                saved_video_path = os.path.join(finished_dir, os.path.basename(final_video_path))
                try:
                    shutil.copy2(final_video_path, saved_video_path)
                except Exception:
                    saved_video_path = final_video_path

                await status_update.edit_text(
                    f"⚠️ <b>Recap Video ဖိုင်အရွယ်အစား ({size_str}) သည် Telegram Bot {limit_str} ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်!</b>\n\n"
                    f"📁 <b>ကွန်ပျူတာပေါ်ရှိ အောက်ပါဖိုင်လမ်းကြောင်းတွင် အသင့်ရယူနိုင်ပါသည်:</b>\n"
                    f"<code>{html.escape(saved_video_path)}</code>\n\n"
                    f"💡 <i>သီးခြား Voiceover အသံ၊ စာတန်းထိုး (SRT) သို့မဟုတ် ဇာတ်ညွှန်း (Script) လိုအပ်ပါက အောက်ပါ ခလုတ်များဖြင့် ရယူနိုင်ပါသည် -</i>",
                    reply_markup=delivery_kb,
                    parse_mode=constants.ParseMode.HTML
                )

            try:
                await status_update.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Recap video generation error: {e}", exc_info=True)
            err_msg = html.escape(str(e)[:250])
            await status_update.edit_text(
                f"❌ <b>Recap Video ဖန်တီးရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်:</b>\n<code>{err_msg}</code>",
                parse_mode=constants.ParseMode.HTML
            )
        finally:
            downloader.cleanup_files(job_dir)
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates and notify user gracefully."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ စနစ်တွင် မမျှော်လင့်ထားသော အမှားတစ်ခု ဖြစ်ပေါ်ခဲ့ပါသည်။ ကျေးဇူးပြု၍ ခေတ္တစောင့်ပြီး Link အသစ်ဖြင့် ထပ်မံ ကြိုးစားပေးပါခင်ဗျာ。",
                parse_mode=constants.ParseMode.HTML
            )
        except Exception:
            pass


def main():
    """Start the Telegram Bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("=" * 60)
        print("⚠️ [သတိပေးချက် / WARNING]")
        print("TELEGRAM_BOT_TOKEN ကို .env ဖိုင်ထဲတွင် ထည့်သွင်းထားခြင်း မရှိသေးပါ!")
        print("-" * 60)
        print("Telegram Bot Token ရယူရန် အဆင့်များ -")
        print("1. Telegram တွင် @BotFather ကို ရှာဖွေပါ (https://t.me/BotFather)")
        print("2. /newbot ဟု ပို့ပြီး Bot အမည်နှင့် username သတ်မှတ်ပါ")
        print("3. ရရှိလာသော HTTP API Token ကို ကော်ပီယူပါ")
        print("4. .env ဖိုင်ထဲတွင် ထည့်သွင်းပါ:")
        print("   TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ")
        print("=" * 60)
        sys.exit(1)

    should_use_local, local_url = detect_and_configure_bot_api(token)

    from telegram.request import HTTPXRequest
    request_client = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=600.0,
        write_timeout=600.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )
    builder = ApplicationBuilder().token(token).request(request_client).concurrent_updates(True)

    if should_use_local:
        builder = (
            builder
            .base_url(f"{local_url}/bot")
            .base_file_url(f"{local_url}/file/bot")
        )
        print("🌐 Local Telegram Bot API (Docker) ကို အသုံးပြုနေပါသည်: 2000MB (2GB) အထိ တင်ပို့/ဒေါင်းလုဒ် ရရှိနိုင်ပါသည်။")
    else:
        print("☁️ Official Telegram Cloud API ကို အသုံးပြုနေပါသည်: 50MB အထိ တင်ပို့နိုင်ပါသည်။")

    print("🚀 Telegram Downloader Bot စတင် လည်ပတ်နေပါပြီ...")
    print("Bot ကို ရပ်တန့်ရန် Ctrl + C ကို နှိပ်နိုင်ပါသည်။")

    app = builder.build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.PHOTO,
            handle_media_upload
        )
    )
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    # Run bot with dropping old pending updates to avoid backlog crashes
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
