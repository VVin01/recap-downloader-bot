"""
auth_gate.py - VIP Authorization Gate for Voice Clone Pro Downloader.
Restricts Recap generation and media downloads to Purchased (VIP) users
or Admins registered in Voice Clone Pro Official database.
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Set
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, constants

logger = logging.getLogger(__name__)

OFFICIAL_BOT_USERNAME = os.getenv("OFFICIAL_BOT_USERNAME", "VoiceClonePro").lstrip("@")
OFFICIAL_BOT_LINK = f"https://t.me/{OFFICIAL_BOT_USERNAME}"


def get_admin_ids() -> Set[int]:
    """Parse ADMIN_IDS from environment."""
    raw = os.getenv("ADMIN_IDS", "1957002115")
    ids = set()
    for item in raw.split(","):
        clean = item.strip()
        if clean.isdigit():
            ids.add(int(clean))
    return ids


def check_user_purchased_status(user_id: int) -> bool:
    """
    Check if the user is an Admin or has 'PURCHASED' account_category
    in the Voice Clone Pro PostgreSQL database.
    """
    # 1. Admins always have unrestricted VIP access
    if user_id in get_admin_ids():
        return True

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        # If no database is configured yet, allow usage by default
        return True

    try:
        import psycopg2
        # Standardize connection string
        clean_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        if "sslmode=" not in clean_url and "postgres.render.com" in clean_url:
            clean_url += ("&" if "?" in clean_url else "?") + "sslmode=require"
        with psycopg2.connect(clean_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT account_category, status FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    category = str(row[0] or "").upper()
                    status = str(row[1] or "").upper()
                    if status != "BLOCKED" and category == "PURCHASED":
                        return True
        return False
    except Exception as e:
        logger.warning(f"Database purchase verification error for user {user_id}: {e}")
        # In case of temporary db network glitch, don't hard block admins
        return user_id in get_admin_ids()


async def send_vip_gate_required_message(update: Update) -> None:
    """Send an elegant, high-converting VIP paywall message directing to Official Bot."""
    text = (
        "👑 <b>Voice Clone Pro — VIP သီးသန့် ဝန်ဆောင်မှု</b>\n\n"
        "မင်္ဂလာပါခင်ဗျာ! ဤ <b>AI Recap & Video Downloader Bot</b> သည် "
        f"<a href=\"{OFFICIAL_BOT_LINK}\">Voice Clone Pro Official Bot</a> တွင် "
        "အသံသွင်းခွင့် ဝယ်ယူထားသော <b>Purchased (VIP) Users</b> များသာ အသုံးပြုခွင့်ရှိသော "
        "သီးသန့် ဝန်ဆောင်မှု ဖြစ်ပါသည်ခင်ဗျာ။\n\n"
        "✨ <b>ရရှိနိုင်မည့် အထူးအခွင့်အရေးများ:</b>\n"
        "• 🎬 Full HD Video & High-Quality Audio ဒေါင်းလုဒ် ရယူခြင်း\n"
        "• 🇲🇲 AI Recap Video (Smart Sync အသံနှင့် စာတန်းထိုး အပြီးသတ်)\n"
        "• 🔥 TikTok, Reels, Shorts ဗိုင်းရပ်စ် အယ်ဂိုရီသမ် စတိုင်လ်များ\n"
        "• 🎙️ VoxCPM Cloud AI Clone & Studio BGM Ducking စနစ်\n"
        "• 🛡️ Anti-Copyright (Flip, Zoom, Subtitle Blur) အပြည့်အစုံ\n\n"
        "<i>👉 အောက်ပါခလုတ်ကို နှိပ်၍ Voice Clone Pro တွင် စတင်ဝယ်ယူ အသုံးပြုနိုင်ပါသည်ခင်ဗျာ။</i>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💎 Voice Clone Pro တွင် ဝယ်ယူရန်", url=OFFICIAL_BOT_LINK),
        ],
        [
            InlineKeyboardButton("🔄 ခွင့်ပြုချက် ပြန်လည်စစ်ဆေးမည်", callback_data="gate:recheck"),
        ]
    ])

    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)
    elif update.callback_query:
        await update.callback_query.answer("⚠️ VIP အကောင့် လိုအပ်ပါသည်", show_alert=True)
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass
