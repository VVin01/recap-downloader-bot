# 🎬 Media Downloader & Recap Bot (URL + Video Upload)
**Video (MP4), Audio (MP3), Subtitle (SRT) နှင့် TikTok Viral Recap ရယူနိုင်သော Telegram Bot & CLI**

---

## ✨ အဓိက လုပ်ဆောင်ချက်များ (Features)

1. **ထောက်ပံ့ပေးထားသော နည်းလမ်းများ (Input Methods):**
   - **URL Download:** YouTube (Videos, Shorts, Music), TikTok (Videos, Audio), Short URLs (youtu.be, vt.tiktok.com, bit.ly, tinyurl စသည်)
   - **Direct Video / Audio Upload:** ကွန်ပျူတာ သို့မဟုတ် ဖုန်းထဲရှိ Video / Audio ဖိုင်များကို Bot ထံသို့ တိုက်ရိုက် Send / Upload တင်ပို့ခြင်း သို့မဟုတ် CLI တွင် File path ထည့်သွင်းခြင်း

2. **ဒေါင်းလုဒ် / ထုတ်ယူ ရယူနိုင်သော ဖော်မက်များ (Features & Outputs):**
   - 🎬 **Video (MP4):** Telegram player နှင့် ကိုက်ညီသော H.264/AAC MP4 အရည်အသွေးမြင့် ဗီဒီယို
   - 🎵 **Audio (MP3):** 192kbps High Quality MP3 ဖိုင် (Artist, Title အချက်အလက်များ အလိုအလျောက် ထည့်သွင်းပေးထားပါသည်)
   - 📝 **Subtitle (SRT):** တရားဝင်စာတန်းထိုး (Official/Auto-captions) များ ရယူနိုင်သည့်အပြင်၊ **စာတန်းထိုး မပါရှိသော Video များကိုလည်း အသံမှတစ်ဆင့် AI (faster-whisper) ဖြင့် အလိုအလျောက် စာတန်းထိုး (Auto-Transcribe) ပြုလုပ်ပြီး `.srt` အဖြစ် ထုတ်ယူပေးနိုင်ပါသည်!**
   - 🔥 **TikTok Viral Recap (Gemini AI):** Video/Audio မှ ရရှိသော SRT ဖိုင်ကို Google Gemini AI သို့ ပေးပို့ကာ **TikTok / Reels တွင် ဗိုင်းရပ်စ်ဖြစ်စေမည့် မြန်မာစကားပြော Voiceover Recap Script (Hook, Body, CTA, Hashtags)** နှင့် CapCut / Premiere Pro တွင် တိုက်ရိုက်သုံးနိုင်သော **မြန်မာဘာသာပြန် SRT ဖိုင်** အဖြစ် အလိုအလျောက် ပြန်ဆိုရေးသားပေးပါသည်။
   - 🇲🇲 **Recap Video (English ➔ Myanmar Smart Sync):** English စကားပြော ဗီဒီယိုများကို အပြည့်အစုံ မြန်မာစကားပြော Recap Video အဖြစ် တစ်ခါတည်း အပြီးအစီး ပြုလုပ်ပေးသည့် Feature အသစ်!
     - **Whisper Speech-to-Text:** အင်္ဂလိပ်စကားပြော အသံကို စာကြောင်းအလိုက် တိကျစွာ ခွဲခြမ်းမှတ်သားခြင်း
     - **Gemini Story Context Memory:** ဇာတ်လမ်းနောက်ခံအခြေအနေကို နားလည်၍ သဘာဝကျသော မြန်မာဇာတ်လမ်းပြော Recap အဖြစ် ဘာသာပြန်ဆိုခြင်း
     - **Edge TTS Myanmar Voiceover:** သဘာဝကျသော မြန်မာအသံ (`my-MM-ThihaNeural` သို့မဟုတ် `my-MM-NilarNeural`) ဖြင့် အသံထွက်ဖန်တီးခြင်း
     - **Smart Sync 30fps Video Retiming:** မြန်မာစကားပြောကြာချိန်နှင့် မူရင်းဗီဒီယိုမြင်ကွင်းများကို အံကိုက်ဖြစ်စေရန် အလိုအလျောက် speed adjust လုပ်၍ ချိန်ညှိပေးခြင်း
     - **Final Video Muxing:** မြန်မာအသံနောက်ခံပါဝင်သော အပြီးသတ် MP4 ဗီဒီယိုကို Telegram သို့မဟုတ် ကွန်ပျူတာထဲသို့ တိုက်ရိုက် ထုတ်ပေးခြင်း!

3. **Telegram Bot UX:**
   - Link ပို့လိုက်ရုံ သို့မဟုတ် Video ဖိုင် Upload တင်လိုက်ရုံဖြင့် Title, Channel/Sender, Duration, Resolution, Subtitle အချက်အလက်များနှင့် Thumbnail ကို အစမ်းပြသခြင်း
   - Inline Keyboard ခလုတ်များဖြင့် Video, Audio, SRT, TikTok Recap, **🇲🇲 Recap Video** ကို လွယ်ကူစွာ တစ်ချက်နှိပ်ရွေးချယ်နိုင်ခြင်း
   - ဒေါင်းလုဒ်ပြီးပါက ယာယီဖိုင်များကို Storage သက်သာစေရန် အလိုအလျောက် ရှင်းလင်းပေးခြင်း (Auto-cleanup)

4. **Standalone CLI Mode:**
   - Telegram Bot Token မထည့်သွင်းမီ သို့မဟုတ် Terminal မှ တိုက်ရိုက်ဒေါင်းလုဒ်ဆွဲလိုပါက အသုံးပြုနိုင်သော Local CLI tool ပါဝင်ပါသည်။ URL သာမက စက်ထဲရှိ Video ဖိုင်ကို Drag & Drop ဆွဲထည့်ပြီး Video, Audio, SRT, TikTok Recap နှင့် **Recap Video (En ➔ Mm Smart Sync)** လုပ်ဆောင်ချက်များ အားလုံးကို run နိုင်ပါသည်။

---

## 📁 ဖိုင်ဖွဲ့စည်းပုံ (Project Structure)

```
VOICE_CLONE_PRO_Downloader/
├── bot.py             # Telegram Bot ပရိုဂရမ် အဓိကဖိုင် (URL & Upload + Recap Support)
├── downloader.py      # Core Media Downloader & Recap Pipeline Wrapper
├── transcriber.py     # AI Speech-to-Text Transcription Engine (faster-whisper)
├── gemini_recap.py    # TikTok Viral Recap & Myanmar Subtitle Engine (Google Gemini)
├── recap_engine/      # English to Myanmar Smart Sync Recap Core Engine
│   ├── recap_pipeline.py  # Orchestrator (Whisper -> Gemini -> TTS -> Retime)
│   ├── recap_planner.py   # Context-aware segmentation & narration planning
│   ├── core.py            # Segment-level retiming, video speed adjustment & muxing
│   └── services/          # AI Service (Gemini 3.8) & Whisper Service
├── cli.py             # Terminal / Command Line ဒေါင်းလုဒ်ဆွဲသည့် ကိရိယာ
├── requirements.txt   # လိုအပ်သော Python Packages များ
├── .env.example       # Token & Gemini API Key ထည့်သွင်းရန် နမူနာဖိုင်
├── .env               # မိမိ၏ Bot Token နှင့် Gemini API Key ထည့်သွင်းရမည့်ဖိုင်
├── run_bot.bat        # Telegram Bot ကို တစ်ချက်နှိပ် ဖွင့်လှစ်သည့်ဖိုင် (Windows)
├── test_cli.bat       # CLI Downloader ကို တစ်ချက်နှိပ် ဖွင့်လှစ်သည့်ဖိုင် (Windows)
└── README.md          # အသုံးပြုနည်း လမ်းညွှန်
```

---

## 🚀 စတင်အသုံးပြုပုံ (Setup & Usage)

### အဆင့် ၁ - Telegram Bot Token ရယူခြင်း
1. Telegram တွင် [@BotFather](https://t.me/BotFather) သို့ သွားပါ။
2. `/newbot` ဟု ရိုက်ပို့ပါ။
3. Bot ၏ အမည် (Name) နှင့် Username (နောက်ဆုံးတွင် `bot` ဖြင့် ဆုံးရမည်၊ ဥပမာ `my_video_dl_bot`) ကို သတ်မှတ်ပေးပါ။
4. BotFather မှ ပေးပို့သော **HTTP API Token** ကို ကော်ပီ (Copy) ယူပါ။

### အဆင့် ၂ - Token ထည့်သွင်းခြင်း
`.env` ဖိုင်ကို Notepad ဖြင့် ဖွင့်ပြီး ကော်ပီယူထားသော Token ကို ထည့်သွင်းပါ -

```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
```

### အဆင့် ၃ - Bot ကို စတင်ဖွင့်လှစ်ခြင်း
- Windows တွင် **`run_bot.bat`** ဖိုင်ကို Double-Click နှိပ်၍ ဖွင့်ပါ။
- သို့မဟုတ် Command Prompt / Terminal တွင်:
  ```bash
  python bot.py
  ```

### အဆင့် ၄ - Telegram တွင် စမ်းသပ်အသုံးပြုခြင်း
1. မိမိ ဖန်တီးထားသော Bot ဆီသို့ သွား၍ `/start` ဟု ပို့ပါ။
2. မည်သည့် YouTube, TikTok သို့မဟုတ် Short Link ကိုမဆို Bot ထံ ပို့လိုက်ပါ။
3. ပေါ်လာသော ခလုတ်များမှ မိမိ လိုချင်သည့် `Video (MP4)`, `Audio (MP3)` သို့မဟုတ် `Subtitle (SRT)` ကို ရွေးချယ် ဒေါင်းလုဒ် ရယူနိုင်ပါပြီ!

---

## 💻 CLI Mode ဖြင့် တိုက်ရိုက် စမ်းသပ်ခြင်း (Offline / Terminal)

Telegram Bot Token မရှိသေးသော်လည်း ကွန်ပျူတာပေါ်တွင် တိုက်ရိုက်ဒေါင်းလုဒ်ဆွဲလိုပါက:

- Windows တွင် **`test_cli.bat`** ကို Double-Click နှိပ်ပါ။
- သို့မဟုတ် Terminal တွင် အောက်ပါအတိုင်း အသုံးပြုနိုင်ပါသည် -
  ```bash
  python cli.py
  ```
  သို့မဟုတ် Link ကို တိုက်ရိုက်ထည့်ပါ:
  ```bash
  python cli.py "https://youtu.be/dQw4w9WgXcQ"
  ```

---

## 🐳 Local Telegram Bot API (Docker) ဖြင့် File Size Limit (2000MB / 2GB) အထိ တိုးမြှင့်ခြင်း

Telegram Bot API ၏ တရားဝင် Cloud စည်းမျဉ်းအရ Bot များသည် အများဆုံး **50MB** အထိသာ ဖိုင်တစ်ခုချင်း ပို့ပေးနိုင်ပြီး **20MB** အထိသာ ဒေါင်းလုဒ် ဆွဲယူနိုင်ပါသည်။

သို့သော် ဤပရောဂျက်တွင် **Local Telegram Bot API Server (Docker)** ချိတ်ဆက်မှုကို အလိုအလျောက် (Auto-detect) ထောက်ပံ့ပေးထားပါသည်!

1. Docker တွင် `telegram-bot-api` container ကို ဖွင့်ထားပါက:
   ```bash
   docker start telegram-bot-api
   ```
2. Bot စတင် run သည့်အခါ Local API (`http://127.0.0.1:8081`) ကို အလိုအလျောက် စစ်ဆေးပြီး:
   - **Upload File Limit:** **2000 MB (2 GB)** အထိ အလိုအလျောက် တိုးမြှင့်ပေးပါသည်! (1080p Full HD Video များနှင့် Recap Video ရှည်များကို Telegram ထဲသို့ တိုက်ရိုက် ပေးပို့နိုင်သည်)
   - **Download File Limit:** **2000 MB (2 GB)** အထိ Video/Audio Upload ဖိုင်များကို လက်ခံဒေါင်းလုဒ် ပြုလုပ်နိုင်ပါသည်!
3. အကယ်၍ Docker မဖွင့်ထားပါက Bot မှ Cloud API (50MB/20MB) သို့ အလိုအလျောက် သက်တောင့်သက်သာ ပြောင်းလဲ အလုပ်လုပ်ပေးမည် ဖြစ်ပါသည်။

`.env` ဖိုင်တွင် ပြင်ဆင်နိုင်သော အချက်များ:
```env
USE_LOCAL_BOT_API=auto
LOCAL_BOT_API_URL=http://127.0.0.1:8081
```
