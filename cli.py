"""
cli.py - Interactive CLI Downloader
Allows direct downloading of Video, Audio, and SRT from YouTube, TikTok, and Short URLs
without requiring Telegram Bot Token.
"""

import os
import sys
import io

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from downloader import (
    MediaDownloader,
    extract_first_url,
    unshorten_url,
    format_duration,
    format_filesize
)

def main():
    downloader = MediaDownloader()

    print("=" * 60)
    print("🎬 Video, Audio, Subtitle & Recap Tool (CLI Mode)")
    print("Supports: YouTube, TikTok, Short URLs & Local Video/Audio Files")
    print("=" * 60)

    # Allow passing URL or File Path as command-line argument
    if len(sys.argv) > 1:
        raw_input = sys.argv[1].strip('\'"')
    else:
        raw_input = input("\n🔗 Link သို့မဟုတ် Video ဖိုင်လမ်းကြောင်း ထည့်ပါ (URL or Local File Path): ").strip('\'"')

    url = extract_first_url(raw_input) or raw_input
    if not url.startswith("http") and not os.path.exists(url):
        print("❌ မှားယွင်းသော Link သို့မဟုတ် File Path ဖြစ်နေပါသည်။ (Invalid URL or File Path)")
        return

    print(f"\n🔍 စစ်ဆေးနေပါသည်: {url}")
    try:
        info = downloader.get_info(url)
    except Exception as e:
        print(f"❌ Error fetching media info: {e}")
        return

    is_local = info.get("is_local", False)
    print("\n" + "-" * 50)
    print(f"📌 Platform : {info.get('platform')}")
    print(f"🎬 Title    : {info.get('title')}")
    if is_local:
        print(f"📁 File Size: {info.get('filesize_formatted')}")
        if info.get('width'):
            print(f"📐 Resolution: {info.get('width')}x{info.get('height')} ({info.get('vcodec') or 'video'})")
    else:
        print(f"👤 Channel  : {info.get('uploader')}")
    print(f"⏱️ Duration : {info.get('duration_formatted')}")
    sub_count = len(info.get('available_subs', []))
    sub_text = f"{sub_count} available ({', '.join(info.get('available_subs', [])[:5])})" if sub_count > 0 else "AI Whisper Auto-Transcribe"
    print(f"📝 Subtitles: {sub_text}")
    print("-" * 50)

    print("\nရွေးချယ်စရာများ (Select Download Option):")
    print("1. 🎬 Video (MP4)")
    print("2. 🎵 Audio (MP3)")
    print("3. 📝 Subtitle (SRT)")
    print("4. 🌟 All (Video + Audio + SRT)")
    print("5. 🔥 TikTok Viral Recap (Gemini AI)")
    print("6. 🇲🇲 Recap Video (En ➔ Mm Smart Sync)")
    print("7. ❌ Cancel")

    choice = input("\nနံပါတ် ရွေးပါ [1-7]: ").strip()

    expanded_url = info.get("expanded_url")
    download_folder = os.path.join(os.getcwd(), "downloads")
    os.makedirs(download_folder, exist_ok=True)

    if choice == "1":
        print("\n⏳ Video (MP4) ဒေါင်းလုဒ်လုပ်နေပါသည်...")
        try:
            path, _ = downloader.download_video(expanded_url, download_folder)
            print(f"✅ Video Downloaded successfully: {path}")
            print(f"📁 Size: {format_filesize(os.path.getsize(path))}")
        except Exception as e:
            print(f"❌ Failed to download video: {e}")

    elif choice == "2":
        print("\n⏳ Audio (MP3) ဒေါင်းလုဒ်လုပ်ပြီး ပြောင်းလဲနေပါသည်...")
        try:
            path, _ = downloader.download_audio(expanded_url, download_folder)
            print(f"✅ Audio Downloaded successfully: {path}")
            print(f"📁 Size: {format_filesize(os.path.getsize(path))}")
        except Exception as e:
            print(f"❌ Failed to download audio: {e}")

    elif choice == "3":
        print("\n⏳ Subtitle (SRT) ဒေါင်းလုဒ်လုပ်နေပါသည်... (AI Auto-Transcribe ပါဝင်သည်)")
        try:
            paths, sinfo = downloader.download_subtitles(expanded_url, download_folder, "en")
            if paths:
                for p in paths:
                    tag = " (AI Whisper Transcribed)" if sinfo.get("is_ai_generated") else " (Official)"
                    print(f"✅ Subtitle Ready{tag}: {p}")
            else:
                print("⚠️ No subtitles could be extracted.")
        except Exception as e:
            print(f"❌ Failed to download subtitles: {e}")

    elif choice == "4":
        print("\n⏳ Downloading All (Video, Audio & Subtitles)...")
        try:
            vpath, _ = downloader.download_video(expanded_url, download_folder)
            print(f"✅ Video: {vpath}")
        except Exception as e:
            print(f"❌ Video failed: {e}")

        try:
            apath, _ = downloader.download_audio(expanded_url, download_folder)
            print(f"✅ Audio: {apath}")
        except Exception as e:
            print(f"❌ Audio failed: {e}")

        try:
            spaths, sinfo = downloader.download_subtitles(expanded_url, download_folder, "en")
            for p in spaths:
                tag = " (AI Whisper Transcribed)" if sinfo.get("is_ai_generated") else " (Official)"
                print(f"✅ Subtitle{tag}: {p}")
        except Exception as e:
            print(f"❌ Subtitles failed: {e}")

        print(f"\n🎉 အားလုံး ပြီးစီးပါပြီ! ဖိုင်များကို '{download_folder}' တွင် ကြည့်ရှုနိုင်ပါသည်။")

    elif choice == "5":
        print("\n⏳ Gemini AI ဖြင့် TikTok Viral Recap Script & မြန်မာစာတန်းထိုး ထုတ်ယူနေပါသည်...")
        try:
            recap_data = downloader.generate_viral_recap(expanded_url, download_folder)
            print("\n" + "=" * 60)
            print("🔥 TikTok Viral Recap Script (မြန်မာဘာသာ):")
            print("=" * 60)
            print(recap_data.get("recap_script", ""))
            print("=" * 60)
            print(f"📄 Voiceover Script File: {recap_data.get('script_path')}")
            if recap_data.get("myanmar_srt_path"):
                print(f"📝 Myanmar Subtitle (SRT) File: {recap_data.get('myanmar_srt_path')}")
        except Exception as e:
            print(f"❌ Failed to generate TikTok recap: {e}")

    elif choice == "6":
        print("\n🇲🇲 Recap Video (En ➔ Mm Smart Sync) Workflow")
        print("1. 🚀 1-Click Pro Recap (Auto Anti-Copyright + Sub Blur + Burn Synced Subs) [Recommended]")
        print("2. ⚙️ Standard Recap (No Flip/Blur, Natural Original)")
        mode_choice = input("ရွေးချယ်ပါ [1/2] (Default: 1): ").strip()
        is_pro = mode_choice != "2"

        print("\nတင်ဆက်မှု စတိုင် ရွေးချယ်ပါ (Presentation Style):")
        print("1. 🎬 Movie Recap Narrator (ဆွဲဆောင်မှုရှိသော ရုပ်ရှင်ဇာတ်လမ်းပြော စတိုင် - 100% Visual Sync) [Recommended]")
        print("2. 🎭 Character POV Dubbing (ဇာတ်ကောင်နေရာကနေ ပြောပြသော စတိုင် - ကျွန်တော်/ကျွန်မ)")
        print("3. 🔥 Suspense Dramatic (သည်းထိတ်ရင်ဖို ဒရာမာ စတိုင်)")
        style_sel = input("စတိုင် ရွေးပါ [1/2/3] (Default: 1): ").strip()
        if style_sel == "2":
            style_name = "pov"
        elif style_sel == "3":
            style_name = "dramatic"
        else:
            style_name = "narrator"

        voice_options = [
            ("my-MM-ThihaNeural", "👨 Thiha (Male - Microsoft Edge TTS)"),
            ("my-MM-NilarNeural", "👩 Nilar (Female - Microsoft Edge TTS)"),
        ]
        try:
            from recap_engine.voice_clone_bridge import get_available_clone_models, is_applio_available
            if is_applio_available():
                clones = get_available_clone_models()
                for ckey, cinfo in clones.items():
                    voice_options.append((ckey, f"🎭 {cinfo['name']} (Voice Clone Pro RVC)"))
        except Exception:
            pass

        print("\nရွေးချယ်ရန် အသံ (Choose Voice):")
        for i, (v_key, v_disp) in enumerate(voice_options, 1):
            print(f"{i}. {v_disp}")
        v_prompt = f"အသံ ရွေးပါ [1-{len(voice_options)}] (Default: 1): "
        voice_sel = input(v_prompt).strip()
        try:
            sel_idx = int(voice_sel) - 1
            if 0 <= sel_idx < len(voice_options):
                voice_name = voice_options[sel_idx][0]
            else:
                voice_name = voice_options[0][0]
        except Exception:
            voice_name = voice_options[0][0]

        print("\nအသံအမြန်နှုန်း ရွေးချယ်ပါ (Voice Speed):")
        print("1. 🎙️ မူရင်းပုံမှန် သဘာဝကျ (+0% Natural Normal) [Recommended - နားထောင်အကောင်းဆုံး]")
        print("2. ⚡ ခပ်သွက်သွက် (+8% Lively)")
        print("3. 🚀 အမြန်နှုန်း (+18% Fast)")
        print("4. ⚡⚡ အထူးအမြန်နှုန်း (+22% Super Fast)")
        speed_sel = input("အမြန်နှုန်း ရွေးပါ [1/2/3/4] (Default: 1): ").strip()
        if speed_sel == "2":
            speed_val = "+8%"
        elif speed_sel == "3":
            speed_val = "+18%"
        elif speed_sel == "4":
            speed_val = "+22%"
        else:
            speed_val = "+0%"

        ratio = "Original"
        blur_y = 0.81
        blur_h = 0.13
        blur_subs_enabled = is_pro
        sub_y = 0.85
        sub_lines = 1
        sub_font_size = 16

        if is_pro:
            print("\nAspect Ratio ရွေးချယ်ပါ:")
            print("1. Original (မူရင်းအတိုင်း)")
            print("2. 9:16 (TikTok / Reels / Shorts)")
            print("3. 16:9 (YouTube Landscape)")
            ratio_sel = input("Ratio ရွေးပါ [1/2/3] (Default: 1): ").strip()
            if ratio_sel == "2":
                ratio = "9:16"
            elif ratio_sel == "3":
                ratio = "16:9"

            print("\nမူရင်း Subtitle ဖုံးအုပ်မည့် Blur နေရာ (Blur Position):")
            print("1. 📍 အောက်ခြေအလယ် (Bottom Mid: Y=81%, H=13%) [Recommended]")
            print("2. 📍 အောက်ခြေကပ် (Bottom Low: Y=85%, H=12%)")
            print("3. 📍 အပေါ်ခပ်မြင့်မြင့် (Bottom High: Y=73%, H=14%)")
            print("4. ✏️ စိတ်ကြိုက် သတ်မှတ်မည် (Custom Y% and H%)")
            print("5. ❌ Blur မသုံးပါ (No Subtitle Blur)")
            blur_sel = input("Blur နေရာ ရွေးပါ [1/2/3/4/5] (Default: 1): ").strip()
            if blur_sel == "2":
                blur_y, blur_h = 0.85, 0.12
            elif blur_sel == "3":
                blur_y, blur_h = 0.73, 0.14
            elif blur_sel == "4":
                try:
                    c_y = float(input("Y အကွာအဝေး ရာခိုင်နှုန်း (e.g. 81): ").strip()) / 100.0
                    c_h = float(input("H အမြင့် ရာခိုင်နှုန်း (e.g. 13): ").strip()) / 100.0
                    blur_y, blur_h = c_y, c_h
                except Exception:
                    blur_y, blur_h = 0.81, 0.13
            elif blur_sel == "5":
                blur_subs_enabled = False

            print("\n📝 မြန်မာစာတန်းထိုး ပုံစံ ရွေးချယ်ပါ (Subtitle Lines):")
            print("1. 1️⃣ တစ်ကြောင်းတည်း သေသပ်သော စာတန်းထိုး (1-Line Compact) [Recommended]")
            print("2. 2️⃣ နှစ်ကြောင်း စာတန်းထိုး (2-Lines Dynamic)")
            line_sel = input("စာကြောင်း ရွေးပါ [1/2] (Default: 1): ").strip()
            sub_lines = 2 if line_sel == "2" else 1

            print("\n🔤 စာလုံးအရွယ်အစား ရွေးချယ်ပါ (Font Size):")
            print("1. ပုံမှန် (Normal - 16pt) [Recommended]")
            print("2. သေး (Small - 13pt)")
            print("3. ကြီး (Large - 19pt)")
            print("4. အလွန်ကြီး (Extra Large - 22pt)")
            font_sel = input("စာလုံးဆိုဒ် ရွေးပါ [1/2/3/4] (Default: 1): ").strip()
            if font_sel == "2":
                sub_font_size = 13
            elif font_sel == "3":
                sub_font_size = 19
            elif font_sel == "4":
                sub_font_size = 22
            else:
                sub_font_size = 16

            snap_y_calc = round(blur_y + blur_h * 0.55, 2)
            print("\n📐 စာတန်းထိုး နေရာ သတ်မှတ်ပါ (Subtitle Y Position):")
            print("1. 📍 ပုံမှန်နေရာ (Bottom Default: Y=85%) [Recommended]")
            print(f"2. 🔗 Blur အကွက်ထဲ ကပ်မည် (Snap inside Blur: Y={int(round(snap_y_calc*100))}%)")
            print("3. ✏️ စိတ်ကြိုက် Y% သတ်မှတ်မည် (Custom Y%)")
            sub_pos_sel = input("စာတန်းနေရာ ရွေးပါ [1/2/3] (Default: 1): ").strip()
            if sub_pos_sel == "2":
                sub_y = snap_y_calc
            elif sub_pos_sel == "3":
                try:
                    sub_y = float(input("စာတန်း Y အကွာအဝေး ရာခိုင်နှုန်း (e.g. 85): ").strip()) / 100.0
                except Exception:
                    sub_y = 0.85
            else:
                sub_y = 0.85

            preview_ask = input("\n🖼️ Overlay Preview ပုံ ကြည့်ရှုလိုပါသလား? (y/N): ").strip().lower()
            if preview_ask in ("y", "yes"):
                target_input = expanded_url if expanded_url else url
                print("⏳ Overlay Preview Frame ထုတ်ယူနေပါသည်...")
                try:
                    prev_path = downloader.generate_recap_preview(
                        url_or_path=target_input,
                        target_dir=download_folder,
                        blur_subtitles=blur_subs_enabled,
                        blur_y=blur_y,
                        blur_h=blur_h,
                        burn_subtitles=is_pro,
                        sub_y=sub_y,
                        sub_lines=sub_lines,
                        sub_font_size=sub_font_size,
                        output_ratio=ratio,
                        mirror_video=is_pro,
                        zoom_factor=1.06 if is_pro else 1.0,
                    )
                    print(f"✅ Preview Frame ပုံ ထွက်ရှိပါပြီ: {prev_path}")
                except Exception as p_err:
                    print(f"⚠️ Preview ထုတ်မရပါ: {p_err}")

        line_desc = f"{sub_lines}-Line"
        print(f"\n🎭 Style: {style_name}")
        print(f"🎙️ Voice: {voice_name} ({speed_val})")
        print(f"🛡️ Anti-Copyright: {'ON (Flip + Zoom 1.06x + Blur Y=' + str(int(blur_y*100)) + '%, H=' + str(int(blur_h*100)) + '% + ' + line_desc + ' Subs ' + str(sub_font_size) + 'pt, Y=' + str(int(round(sub_y*100))) + '%)' if is_pro else 'OFF'}")
        print(f"📐 Ratio: {ratio}\n")

        def cli_progress(current, total, msg):
            print(f"[{current}/{total}] {msg}")

        def cli_log(msg):
            print(f"  ➜ {msg}")

        try:
            target_input = expanded_url if expanded_url else url
            res = downloader.generate_recap_video(
                url_or_path=target_input,
                target_dir=download_folder,
                voice=voice_name,
                narrative_style=style_name,
                voice_speed=speed_val,
                mirror_video=is_pro,
                zoom_factor=1.06 if is_pro else 1.0,
                blur_subtitles=blur_subs_enabled,
                blur_y=blur_y,
                blur_h=blur_h,
                blur_watermark=False,
                burn_subtitles=is_pro,
                sub_y=sub_y,
                sub_lines=sub_lines,
                sub_font_size=sub_font_size,
                output_ratio=ratio,
                log_callback=cli_log,
                progress_callback=cli_progress,
            )
            print("\n" + "=" * 60)
            print("🎉 Recap Video အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ! (Recap Completed)")
            print("=" * 60)
            vpath = res.get("final_video") or res.get("final_video_path")
            if vpath and os.path.exists(vpath):
                print(f"🎬 Recap Video File : {vpath}")
                print(f"📁 Video File Size  : {format_filesize(os.path.getsize(vpath))}")
            apath = res.get("audio") or res.get("final_audio_path")
            if apath and os.path.exists(apath):
                print(f"🎵 Myanmar Audio    : {apath}")
            spath = res.get("subtitles_srt")
            if spath and os.path.exists(spath):
                print(f"📝 Synced SRT Subs  : {spath}")
            duration_val = res.get("duration", 0)
            seg_count = res.get("segments", 0)
            print(f"⏱️ Recap Duration   : {duration_val:.2f}s")
            print(f"📝 Segments Synced  : {seg_count}")
            print("=" * 60)
        except Exception as e:
            print(f"❌ Failed to generate recap video: {e}")

    else:
        print("မလုပ်ဆောင်တော့ပါ။ (Cancelled)")

if __name__ == "__main__":
    main()
