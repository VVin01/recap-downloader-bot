"""
gemini_recap.py - Generate Viral TikTok Recap Scripts and Myanmar SRT using Google Gemini API.
"""

import os
import re
import logging
from typing import Dict, Any, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def get_gemini_api_key() -> Optional[str]:
    """Retrieve Gemini API key from environment."""
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "YOUR_GEMINI_API_KEY_HERE":
        return None
    return key


def generate_tiktok_recap_and_srt(
    srt_content: str,
    title: str = "",
    platform: str = "",
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Takes an SRT transcript of a video and uses Google Gemini to generate:
    1. Viral TikTok Video Recap Script in Burmese (Hook, Engaging Voiceover, CTA, Hashtags).
    2. Timed Myanmar Subtitle (.srt) for CapCut / Video editing.
    """
    key = api_key or get_gemini_api_key()
    if not key:
        raise ValueError(
            "GEMINI_API_KEY ကို .env ဖိုင်ထဲတွင် ထည့်သွင်းထားခြင်း မရှိသေးပါ!\n"
            "Google AI Studio (https://aistudio.google.com/) မှ အခမဲ့ API Key ရယူပြီး .env ထဲတွင် GEMINI_API_KEY=... ဟု ထည့်ပေးပါခင်ဗျာ。"
        )

    # Clean / limit SRT content if extremely long (to stay well within prompt limits while keeping context)
    lines = srt_content.strip().splitlines()
    if len(lines) > 2000:
        truncated_srt = "\n".join(lines[:2000]) + "\n[Content truncated for length]"
    else:
        truncated_srt = srt_content

    prompt = f"""
You are an expert Burmese (မြန်မာ) TikTok Content Creator, Video Recap Specialist, and Professional Translator.
You specialize in creating viral Movie / Video / Story Recap scripts for TikTok & Facebook Reels in Burmese that capture viewers' attention from the first second.

Video Title: {title or 'Video'}
Platform: {platform or 'Web Media'}

Here is the original Subtitle (SRT) transcript of the video:
---
{truncated_srt}
---

Your task:
1. Create a captivating, viral TikTok Recap Video Voiceover Script in natural, engaging, and attractive Burmese (မြန်မာဘာသာ စကားပြောဟန်).
   - Hook (0-3s): Attention-grabbing opening line that prevents users from scrolling away.
   - Body Story / Recap: A gripping narrative recap of the video/story, keeping the audience hooked.
   - Key Highlight / Twist: The climax or takeaway of the video.
   - Call-To-Action (CTA): Invite viewers to like, share, and comment.
   - Viral Hashtags: Trending hashtags for TikTok (e.g., #recap #myanmar #tiktokmyanmar #foryou).

2. Translate the original SRT transcript into Burmese (.srt format), preserving the original timestamp numbers and indices so it can be imported into CapCut / Premiere Pro.

Format your output strictly using these two delimiters:

===TIKTOK_RECAP_START===
🔥 [VIRAL HOOK (0-3s)]
(Write an intense, curious hook here in Burmese)

🎬 [VOICEOVER RECAP SCRIPT (မြန်မာစကားပြော ဇာတ်ညွှန်း)]
(Write the full engaging recap script in natural spoken Burmese, ready to be read aloud)

💡 [HIGHLIGHT & QUESTION]
(Key message + question to spark comments)

📣 [CALL TO ACTION]
(Follow & Like prompt in Burmese)

🏷️ [VIRAL HASHTAGS]
(Relevant trending hashtags)
===TIKTOK_RECAP_END===

===MYANMAR_SRT_START===
(Provide the translated Burmese SRT content matching timestamps from the original)
===MYANMAR_SRT_END===
"""

    from google import genai
    client = genai.Client(api_key=key)

    # Use active Gemini models (gemini-3.8-flash, gemini-3.5-flash, gemini-3.5-flash-lite, gemini-3.6-flash)
    candidate_models = [m for m in [env_model, "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.8-flash"] if m]
    response = None
    last_err = None

    for model_name in candidate_models:
        try:
            logger.info(f"Generating TikTok recap using Gemini model '{model_name}'...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                break
        except Exception as e:
            logger.warning(f"Failed with model {model_name}: {e}")
            last_err = e

    if not response or not response.text:
        raise RuntimeError(f"Gemini API request failed: {last_err or 'No response'}")

    full_text = response.text.strip()

    # Extract TikTok Recap
    recap_match = re.search(
        r'===TIKTOK_RECAP_START===\s*(.*?)\s*===TIKTOK_RECAP_END===',
        full_text,
        re.DOTALL
    )
    if recap_match:
        recap_script = recap_match.group(1).strip()
    else:
        # Fallback if delimiter missed
        recap_script = full_text

    # Remove prompt echo instruction lines if model echoed them
    cleaned_lines = []
    for line in recap_script.splitlines():
        trimmed = line.strip()
        if (trimmed.startswith("(") and trimmed.endswith(")")) or (trimmed.startswith("<") and trimmed.endswith(">")):
            if any(k in trimmed.lower() for k in ["write", "provide", "intense", "engaging", "key message", "relevant", "prompt", "here"]):
                continue
        cleaned_lines.append(line)
    recap_script = "\n".join(cleaned_lines).strip()

    # Extract Myanmar SRT
    srt_match = re.search(
        r'===MYANMAR_SRT_START===\s*(.*?)\s*===MYANMAR_SRT_END===',
        full_text,
        re.DOTALL
    )
    if srt_match:
        myanmar_srt = srt_match.group(1).strip()
    else:
        myanmar_srt = ""

    return {
        "recap_script": recap_script,
        "myanmar_srt": myanmar_srt,
        "full_text": full_text
    }
