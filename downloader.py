"""
downloader.py - Core Downloader Engine
Supports:
- YouTube (Videos, Shorts, Music)
- TikTok (Videos, Audio, Watermark-free where available)
- Short URLs (bit.ly, tinyurl, youtu.be, vt.tiktok.com, vm.tiktok.com, etc.)
- Outputs: Video (MP4), Audio (MP3), Subtitles (SRT)
"""

import os
import re
import shutil
import tempfile
import logging
import subprocess
import json
import uuid
import requests
import yt_dlp
from typing import Dict, Any, Optional, List, Tuple, Callable

logger = logging.getLogger(__name__)

MEDIA_VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.wmv', '.m4v', '.ts'}
MEDIA_AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.opus', '.wma'}
MEDIA_EXTS = MEDIA_VIDEO_EXTS | MEDIA_AUDIO_EXTS

def is_local_media(path_or_url: str) -> bool:
    """Check if the provided string is a path to an existing local media file or media filename."""
    if not path_or_url:
        return False
    clean = str(path_or_url).strip('\'"')
    if clean.startswith(("http://", "https://")):
        return False
    if os.path.exists(clean):
        return True
    ext = os.path.splitext(clean)[1].lower()
    return ext in MEDIA_EXTS

def probe_local_media(file_path: str) -> Dict[str, Any]:
    """Inspect duration, streams, codec, resolution, and filesize via ffprobe."""
    file_path = str(file_path).strip('\'"')
    info: Dict[str, Any] = {
        "duration": 0.0,
        "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
        "width": None,
        "height": None,
        "vcodec": None,
        "acodec": None,
        "has_video": False,
        "has_audio": False,
    }
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            try:
                info["duration"] = float(fmt.get("duration", 0.0))
            except (ValueError, TypeError):
                info["duration"] = 0.0
            try:
                info["size"] = int(fmt.get("size", info["size"]))
            except (ValueError, TypeError):
                pass
            streams = data.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
            if video_stream:
                info["has_video"] = True
                info["width"] = video_stream.get("width")
                info["height"] = video_stream.get("height")
                info["vcodec"] = video_stream.get("codec_name")
                if not info["duration"] and "duration" in video_stream:
                    try:
                        info["duration"] = float(video_stream["duration"])
                    except Exception:
                        pass
            if audio_stream:
                info["has_audio"] = True
                info["acodec"] = audio_stream.get("codec_name")
                if not info["duration"] and "duration" in audio_stream:
                    try:
                        info["duration"] = float(audio_stream["duration"])
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"ffprobe error on {file_path}: {e}")
    return info

def extract_thumbnail_from_media(file_path: str, output_path: str, timestamp_sec: float = 1.0) -> Optional[str]:
    """Capture a snapshot image from a video file using ffmpeg."""
    file_path = str(file_path).strip('\'"')
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0.1, timestamp_sec)),
            "-i", file_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as e:
        logger.warning(f"Thumbnail extraction failed for {file_path}: {e}")
    return None

try:
    from transcriber import transcribe_to_srt
except ImportError:
    transcribe_to_srt = None

try:
    from gemini_recap import generate_tiktok_recap_and_srt, get_gemini_api_key
except ImportError:
    generate_tiktok_recap_and_srt = None
    get_gemini_api_key = lambda: None

try:
    from recap_engine.recap_pipeline import run_recap_pipeline, generate_recap_preview
except ImportError:
    run_recap_pipeline = None
    generate_recap_preview = None

# Regex to detect URLs in a message
URL_REGEX = re.compile(
    r'(https?:\/\/(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'www\.[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?:\/\/[a-zA-Z0-9]+\.[^\s]{2,})'
)

def extract_first_url(text: str) -> Optional[str]:
    """Find and return the first valid HTTP/HTTPS URL from a string."""
    match = URL_REGEX.search(text)
    if match:
        url = match.group(0)
        # Strip trailing punctuation often caught in chat messages
        url = url.rstrip('.,!?:;)]}')
        return url
    return None

def unshorten_url(url: str) -> str:
    """
    Expands shortened URLs (bit.ly, tinyurl, vt.tiktok.com, youtu.be, etc.)
    to their canonical target URL.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        # Follow redirects with HEAD request first
        session = requests.Session()
        resp = session.head(url, allow_redirects=True, headers=headers, timeout=10)
        final_url = resp.url
        if final_url and final_url != url:
            return final_url
            
        # Fallback to GET with stream=True (does not download body)
        resp = session.get(url, allow_redirects=True, headers=headers, stream=True, timeout=10)
        return resp.url
    except Exception:
        # If redirect resolution fails, return original URL (yt-dlp will try to resolve it anyway)
        return url

def detect_platform(url: str) -> str:
    """Identify the platform name from URL or local media for display."""
    if is_local_media(url):
        clean = str(url).strip('\'"')
        ext = os.path.splitext(clean)[1].lower()
        if ext in MEDIA_AUDIO_EXTS:
            return "Uploaded Audio"
        return "Uploaded Video"
    url_lower = url.lower()
    if "tiktok.com" in url_lower:
        return "TikTok"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "YouTube"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return "Facebook"
    elif "instagram.com" in url_lower:
        return "Instagram"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "X (Twitter)"
    else:
        return "Web Media"

def format_duration(seconds: Optional[int]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_filesize(bytes_val: Optional[int]) -> str:
    """Format bytes into MB or KB string."""
    if not bytes_val:
        return "N/A"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB"
    kb = bytes_val / 1024
    return f"{kb:.1f} KB"

def find_best_preview_timestamp(video_path: str, target_dir: Optional[str] = None) -> float:
    """
    Find the most optimal timestamp (in seconds) containing original subtitles/dialogue for preview frame.
    Checks:
    1. Existing .srt / .vtt / transcript.json in target_dir or video directory.
    2. Embedded subtitle tracks in the video file via ffprobe.
    3. Safe dialogue start heuristic based on video duration (avoiding opening logos/black frames).
    """
    video_path = str(video_path).strip('\'"')
    search_dirs = []
    if target_dir and os.path.isdir(target_dir):
        search_dirs.append(target_dir)
        ws = os.path.join(target_dir, "workspace")
        if os.path.isdir(ws):
            search_dirs.append(ws)
    if os.path.isfile(video_path):
        search_dirs.append(os.path.dirname(os.path.abspath(video_path)))

    # 1. Check existing subtitle/transcript files on disk
    for d in search_dirs:
        try:
            for fname in os.listdir(d):
                full_p = os.path.join(d, fname)
                if not os.path.isfile(full_p):
                    continue
                lower_name = fname.lower()
                if lower_name.endswith(('.srt', '.vtt')) and not lower_name.startswith('preview'):
                    try:
                        with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read(50000)
                        pattern = r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})'
                        matches = re.findall(pattern, content)
                        for m in matches:
                            sh, sm, ss, sms, eh, em, es, ems = map(int, m)
                            st = sh * 3600 + sm * 60 + ss + sms / 1000.0
                            et = eh * 3600 + em * 60 + es + ems / 1000.0
                            if 4.0 <= st <= 90.0:
                                return round((st + et) / 2.0, 2)
                        if matches:
                            sh, sm, ss, sms, eh, em, es, ems = map(int, matches[0])
                            st = sh * 3600 + sm * 60 + ss + sms / 1000.0
                            et = eh * 3600 + em * 60 + es + ems / 1000.0
                            if st > 0.5:
                                return round((st + et) / 2.0, 2)
                    except Exception as e:
                        logger.debug(f"Error reading sub file {full_p}: {e}")

                elif lower_name in ('whisper_transcript.json', 'atomic_units.json', 'plan.json', 'transcript.json'):
                    try:
                        with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                            data = json.load(f)
                        items = []
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            items = data.get('segments') or data.get('items') or data.get('atomic_units') or []
                        for it in items:
                            if isinstance(it, dict) and 'start' in it and 'end' in it:
                                st = float(it['start'])
                                et = float(it['end'])
                                if 4.0 <= st <= 90.0:
                                    return round((st + et) / 2.0, 2)
                        if items and isinstance(items[0], dict) and 'start' in items[0]:
                            st = float(items[0]['start'])
                            et = float(items[0].get('end', st + 2.0))
                            if st > 0.5:
                                return round((st + et) / 2.0, 2)
                    except Exception as e:
                        logger.debug(f"Error reading json transcript {full_p}: {e}")
        except Exception:
            pass

    # 2. Check embedded subtitle stream in the video container via ffprobe
    if os.path.isfile(video_path):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "s",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                cmd2 = [
                    "ffprobe", "-v", "error",
                    "-select_streams", "s:0",
                    "-show_entries", "packet=pts_time",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    "-read_intervals", "0%+120",
                    video_path
                ]
                res2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
                if res2.returncode == 0 and res2.stdout.strip():
                    for line in res2.stdout.strip().splitlines():
                        try:
                            t_val = float(line.strip())
                            if 3.0 <= t_val <= 120.0:
                                return round(t_val + 0.5, 2)
                        except ValueError:
                            continue
        except Exception as e:
            logger.debug(f"ffprobe subtitle check error: {e}")

    # 3. Intelligent heuristic based on duration (skips intro logos)
    dur = 0.0
    if os.path.isfile(video_path):
        try:
            dur = probe_local_media(video_path).get("duration", 0.0)
        except Exception:
            dur = 0.0

    if dur >= 35.0:
        return 14.5
    elif dur >= 15.0:
        return round(dur * 0.35, 2)
    elif dur > 4.0:
        return round(dur * 0.30, 2)
    return 3.5


class MediaDownloader:
    find_best_preview_timestamp = staticmethod(find_best_preview_timestamp)
    def __init__(self, download_dir: Optional[str] = None):
        self.download_dir = download_dir or os.path.join(os.getcwd(), "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

    def _get_base_opts(self) -> Dict[str, Any]:
        """Common yt-dlp options for reliable extraction, JS challenge solving, and 403 bypass."""
        opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'nocheckcertificate': True,
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 5,
            'remote_components': ['ejs:github'],
        }
        # Enable Node.js for solving YouTube JS / n challenges if installed
        if shutil.which("node"):
            opts['js_runtimes'] = {'node': {}}
        return opts

    def get_local_info(self, file_path: str) -> Dict[str, Any]:
        """Inspect and return metadata dictionary for a local or uploaded video/audio file."""
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local media file not found: {file_path}")

        meta = probe_local_media(file_path)
        base_name = os.path.basename(file_path)
        title, _ = os.path.splitext(base_name)
        file_id = f"local_{uuid.uuid4().hex[:8]}"

        # Generate thumbnail if it has a video stream
        thumbnail_path = None
        if meta.get("has_video"):
            thumb_dest = os.path.join(self.download_dir, "thumbnails", f"{file_id}.jpg")
            seek_time = min(2.0, meta.get("duration", 0.0) / 2) if meta.get("duration") else 1.0
            thumbnail_path = extract_thumbnail_from_media(file_path, thumb_dest, seek_time)

        is_audio_only = meta.get("has_audio") and not meta.get("has_video")
        platform = "Uploaded Audio" if is_audio_only else "Uploaded Video"

        duration = int(round(meta.get("duration", 0.0)))
        filesize = meta.get("size", os.path.getsize(file_path))

        return {
            "id": file_id,
            "title": title,
            "duration": duration,
            "duration_formatted": format_duration(duration),
            "uploader": "Uploaded Media",
            "thumbnail": thumbnail_path,
            "platform": platform,
            "original_url": file_path,
            "expanded_url": file_path,
            "filesize": filesize,
            "filesize_formatted": format_filesize(filesize),
            "available_subs": [],
            "has_subtitles": False,
            "is_local": True,
            "local_path": file_path,
            "width": meta.get("width"),
            "height": meta.get("height"),
            "vcodec": meta.get("vcodec"),
            "acodec": meta.get("acodec"),
            "has_video": meta.get("has_video"),
            "has_audio": meta.get("has_audio"),
            "raw_info": meta
        }

    def get_info(self, url: str) -> Dict[str, Any]:
        """
        Extract video/media metadata without downloading content.
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.get_local_info(url)
        expanded_url = unshorten_url(url)
        opts = self._get_base_opts()
        opts['extract_flat'] = False

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(expanded_url, download=False)
            if not info:
                raise ValueError("Could not extract media info from the provided URL.")

            # Subtitles inspection
            subtitles = info.get('subtitles', {})
            auto_captions = info.get('automatic_captions', {})
            available_subs = list(set(list(subtitles.keys()) + list(auto_captions.keys())))

            duration = info.get('duration')
            title = info.get('title') or "media"
            uploader = info.get('uploader') or info.get('channel') or info.get('creator') or "Unknown"
            thumbnail = info.get('thumbnail')
            filesize = info.get('filesize') or info.get('filesize_approx')
            platform = detect_platform(expanded_url)

            return {
                "id": info.get("id"),
                "title": title,
                "duration": duration,
                "duration_formatted": format_duration(duration),
                "uploader": uploader,
                "thumbnail": thumbnail,
                "platform": platform,
                "original_url": url,
                "expanded_url": expanded_url,
                "filesize": filesize,
                "filesize_formatted": format_filesize(filesize),
                "available_subs": available_subs,
                "has_subtitles": len(available_subs) > 0,
                "raw_info": info
            }

    def download_video(self, url: str, target_dir: Optional[str] = None, max_height: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Download video in MP4 (H.264/AAC for best Telegram compatibility).
        Returns (file_path, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.process_local_video(url, target_dir=target_dir, max_height=max_height)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        # Output template
        outtmpl = os.path.join(dest_dir, "%(id)s_%(title).50s.%(ext)s")

        # Select compatible format: MP4 with H.264 video and AAC audio
        # Telegram native video player plays best with h264 + aac
        if max_height:
            fmt = f"bestvideo[height<={max_height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        else:
            fmt = "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

        opts = self._get_base_opts()
        opts.update({
            'format': fmt,
            'outtmpl': outtmpl,
            'merge_output_format': 'mp4',
            'writethumbnail': True,
            'postprocessors': [
                {
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }
            ]
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(expanded_url, download=True)
            # Find the downloaded mp4 file
            file_path = ydl.prepare_filename(info)
            if not file_path.endswith('.mp4'):
                base, _ = os.path.splitext(file_path)
                file_path = f"{base}.mp4"

            if not os.path.exists(file_path):
                # Search directory for file with id
                media_id = info.get("id")
                for fname in os.listdir(dest_dir):
                    if media_id in fname and fname.endswith(('.mp4', '.mkv', '.webm')):
                        file_path = os.path.join(dest_dir, fname)
                        break

            return file_path, info

    def download_audio(self, url: str, target_dir: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Download audio and convert to MP3 (192kbps) with metadata & album art.
        Returns (file_path, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.extract_local_audio(url, target_dir=target_dir)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        outtmpl = os.path.join(dest_dir, "%(id)s_%(title).50s.%(ext)s")

        opts = self._get_base_opts()
        opts.update({
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'writethumbnail': True,
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ]
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(expanded_url, download=True)
            # Filename after audio extraction is .mp3
            base_filename = ydl.prepare_filename(info)
            base_name, _ = os.path.splitext(base_filename)
            mp3_path = f"{base_name}.mp3"

            if not os.path.exists(mp3_path):
                media_id = info.get("id")
                for fname in os.listdir(dest_dir):
                    if media_id in fname and fname.endswith('.mp3'):
                        mp3_path = os.path.join(dest_dir, fname)
                        break

            return mp3_path, info

    def download_audio_for_transcription(self, url: str, target_dir: Optional[str] = None) -> str:
        """
        Fast audio download specifically for Whisper transcription.
        Downloads lightweight raw audio without thumbnail or MP3 conversion.
        """
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        outtmpl = os.path.join(dest_dir, "transcribe_%(id)s.%(ext)s")
        opts = self._get_base_opts()
        opts.update({
            'format': 'ba/b',
            'outtmpl': outtmpl,
            'writethumbnail': False,
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(expanded_url, download=True)
            fname = ydl.prepare_filename(info)
            if os.path.exists(fname):
                return fname

            media_id = str(info.get("id", ""))
            for f in os.listdir(dest_dir):
                if media_id in f and f.startswith("transcribe_"):
                    return os.path.join(dest_dir, f)

            raise FileNotFoundError(f"Downloaded audio for transcription not found: {fname}")

    def download_subtitles(
        self,
        url: str,
        target_dir: Optional[str] = None,
        lang: str = 'en',
        allow_ai_fallback: bool = True
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Download subtitles and convert to .srt.
        If the media lacks subtitles, automatically falls back to AI Speech-to-Text (Whisper).
        Returns (list_of_srt_file_paths, info_dict).
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.transcribe_local_media(url, target_dir=target_dir)

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        expanded_url = unshorten_url(url)

        # Check available languages first
        info = self.get_info(expanded_url)
        available_subs = info.get("available_subs", [])

        # Target language selection:
        # If requested lang in available_subs, use it. Otherwise use the first available.
        target_lang = lang
        if target_lang not in available_subs:
            # Check prefix match (e.g. 'en' matches 'en-US' or 'en-orig')
            prefix_matches = [s for s in available_subs if s.startswith(lang)]
            if prefix_matches:
                target_lang = prefix_matches[0]
            elif 'my' in available_subs: # Myanmar subtitle priority if available
                target_lang = 'my'
            elif available_subs:
                target_lang = available_subs[0]
            else:
                target_lang = 'en'

        outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")

        opts = self._get_base_opts()
        opts.update({
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [target_lang],
            'subtitlesformat': 'srt',
            'ignoreerrors': True,
            'outtmpl': outtmpl,
            'postprocessors': [{
                'key': 'FFmpegSubtitlesConvertor',
                'format': 'srt',
            }]
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([expanded_url])

        # Collect generated .srt files in destination
        media_id = str(info.get("id", ""))
        srt_files = []
        for fname in os.listdir(dest_dir):
            if fname.endswith('.srt'):
                if not media_id or media_id in fname:
                    srt_files.append(os.path.join(dest_dir, fname))

        if srt_files:
            info["is_ai_generated"] = False
            return srt_files, info

        # AI Fallback: When no native subtitles exist, transcribe audio via Whisper
        if allow_ai_fallback and transcribe_to_srt is not None:
            logger.info(f"No native subtitles found for {url}. Generating SRT with AI Whisper...")
            audio_path = None
            try:
                try:
                    audio_path = self.download_audio_for_transcription(expanded_url, target_dir=dest_dir)
                except Exception as dl_err:
                    logger.warning(f"Fast transcription download failed ({dl_err}), falling back to standard audio download...")
                    audio_path, _ = self.download_audio(expanded_url, target_dir=dest_dir)

                if audio_path and os.path.exists(audio_path):
                    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', media_id) or "media"
                    ai_srt_path = os.path.join(dest_dir, f"{safe_id}_ai.srt")
                    out_srt, detected_lang, prob = transcribe_to_srt(
                        audio_file_path=audio_path,
                        output_srt_path=ai_srt_path
                    )
                    if os.path.exists(out_srt):
                        srt_files.append(out_srt)
                        info["is_ai_generated"] = True
                        info["detected_lang"] = detected_lang
                        info["lang_probability"] = prob
            except Exception as e:
                logger.error(f"AI Subtitle transcription error: {e}")
            finally:
                if audio_path:
                    self.cleanup_files(audio_path)

        return srt_files, info

    def generate_viral_recap(
        self,
        url: str,
        target_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Extracts subtitle for the media (using native subtitles or AI transcription),
        and prompts Google Gemini to create a viral TikTok recap script and Myanmar SRT.
        Supports both Online URLs and Local/Uploaded files.
        """
        if is_local_media(url):
            return self.generate_viral_recap_for_local(url, target_dir=target_dir)

        if generate_tiktok_recap_and_srt is None:
            raise ImportError("gemini_recap module is not available.")

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)

        srt_files, info = self.download_subtitles(url, target_dir=dest_dir)
        if not srt_files:
            raise ValueError("Could not extract or transcribe subtitles for this media.")

        primary_srt = srt_files[0]
        with open(primary_srt, "r", encoding="utf-8", errors="replace") as f:
            srt_content = f.read()

        title = info.get("title", "Video")
        platform = info.get("platform", "Web Media")

        recap_result = generate_tiktok_recap_and_srt(
            srt_content=srt_content,
            title=title,
            platform=platform
        )

        media_id = str(info.get("id") or "media")
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', media_id)

        # Save TikTok recap script to a .txt file
        script_path = os.path.join(dest_dir, f"{safe_id}_tiktok_recap.txt")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(recap_result["recap_script"])

        # Save Myanmar SRT if generated
        myanmar_srt_path = None
        if recap_result.get("myanmar_srt"):
            myanmar_srt_path = os.path.join(dest_dir, f"{safe_id}_myanmar.srt")
            with open(myanmar_srt_path, "w", encoding="utf-8") as f:
                f.write(recap_result["myanmar_srt"])

        return {
            "recap_script": recap_result["recap_script"],
            "script_path": script_path,
            "myanmar_srt_path": myanmar_srt_path,
            "original_srt_path": primary_srt,
            "info": info
        }

    def process_local_video(
        self,
        file_path: str,
        target_dir: Optional[str] = None,
        max_height: Optional[int] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Ensure local video is standard H.264/AAC MP4 compatible with Telegram and players."""
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        meta = probe_local_media(file_path)
        cur_height = meta.get("height") or 0
        needs_scale = bool(max_height and cur_height > max_height)
        is_mp4 = file_path.lower().endswith(".mp4") and meta.get("vcodec") in {"h264", "avc1"}

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "video"))[:50]
        out_mp4 = os.path.join(dest_dir, f"{info['id']}_{safe_title}.mp4")

        # If already mp4 h264 and no resize needed, simply copy or reuse
        if is_mp4 and not needs_scale:
            if os.path.abspath(file_path) != os.path.abspath(out_mp4):
                shutil.copy2(file_path, out_mp4)
            return out_mp4, info

        vf_args = []
        if needs_scale:
            vf_args = ["-vf", f"scale=-2:'min({max_height},ih)'"]

        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            *vf_args,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out_mp4
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if res.returncode != 0 or not os.path.exists(out_mp4):
            shutil.copy2(file_path, out_mp4)

        return out_mp4, info

    def extract_local_audio(
        self,
        file_path: str,
        target_dir: Optional[str] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Extract high-quality 192kbps MP3 audio from a local video or audio file."""
        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "audio"))[:50]
        out_mp3 = os.path.join(dest_dir, f"{info['id']}_{safe_title}.mp3")

        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            out_mp3
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if res.returncode != 0 or not os.path.exists(out_mp3):
            raise RuntimeError(f"Audio extraction failed for {file_path}")

        return out_mp3, info

    def transcribe_local_media(
        self,
        file_path: str,
        target_dir: Optional[str] = None
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Transcribe local video or audio directly into .srt subtitle with Whisper."""
        if transcribe_to_srt is None:
            raise ImportError("transcriber module (faster-whisper) is not available.")

        file_path = os.path.abspath(str(file_path).strip('\'"'))
        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)
        info = self.get_local_info(file_path)

        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', info.get("title", "media"))[:50]
        out_srt = os.path.join(dest_dir, f"{info['id']}_{safe_title}_ai.srt")

        srt_path, detected_lang, prob = transcribe_to_srt(
            audio_file_path=file_path,
            output_srt_path=out_srt
        )
        info["is_ai_generated"] = True
        info["detected_lang"] = detected_lang
        info["lang_probability"] = prob

        return [srt_path], info

    def generate_viral_recap_for_local(
        self,
        file_path: str,
        target_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transcribe local media with Whisper and generate viral TikTok Recap + Myanmar SRT with Gemini."""
        if generate_tiktok_recap_and_srt is None:
            raise ImportError("gemini_recap module is not available.")

        dest_dir = target_dir or self.download_dir
        os.makedirs(dest_dir, exist_ok=True)

        srt_files, info = self.transcribe_local_media(file_path, target_dir=dest_dir)
        if not srt_files:
            raise ValueError("Could not extract subtitles from local media.")

        primary_srt = srt_files[0]
        with open(primary_srt, "r", encoding="utf-8", errors="replace") as f:
            srt_content = f.read()

        title = info.get("title", "Uploaded Video")
        platform = info.get("platform", "Uploaded Media")

        recap_result = generate_tiktok_recap_and_srt(
            srt_content=srt_content,
            title=title,
            platform=platform
        )

        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', str(info.get("id", "media")))
        script_path = os.path.join(dest_dir, f"{safe_id}_tiktok_recap.txt")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(recap_result["recap_script"])

        myanmar_srt_path = None
        if recap_result.get("myanmar_srt"):
            myanmar_srt_path = os.path.join(dest_dir, f"{safe_id}_myanmar.srt")
            with open(myanmar_srt_path, "w", encoding="utf-8") as f:
                f.write(recap_result["myanmar_srt"])

        return {
            "recap_script": recap_result["recap_script"],
            "script_path": script_path,
            "myanmar_srt_path": myanmar_srt_path,
            "original_srt_path": primary_srt,
            "info": info
        }

    def generate_recap_video(
        self,
        url_or_path: str,
        target_dir: Optional[str] = None,
        voice: str = "my-MM-ThihaNeural",
        narrative_style: str = "narrator",
        voice_speed: str = "+0%",
        mirror_video: bool = True,
        zoom_factor: float = 1.06,
        blur_subtitles: bool = True,
        blur_y: float = 0.81,
        blur_h: float = 0.13,
        blur_x: float = 0.05,
        blur_w: float = 0.90,
        blur_watermark: bool = False,
        burn_subtitles: bool = True,
        sub_y: Optional[float] = None,
        sub_lines: int = 1,
        sub_font_size: int = 16,
        sub_style: str = "yellow",
        title_text: Optional[str] = None,
        logo_path: Optional[str] = None,
        logo_position: Optional[list[float]] = None,
        logo_scale: float = 0.16,
        logo_opacity: float = 0.70,
        extra_text: Optional[str] = None,
        output_ratio: str = "Original",
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Executes the full Recap English -> Myanmar workflow:
        If given a URL, downloads the source video first.
        Then runs Whisper -> Gemini Translation -> Edge TTS Myanmar -> Smart Sync -> Final Video.
        Applies Anti-Copyright enhancements: Flip/Mirror, Zoom, Blur Subtitles/Watermark, Burn Synced Myanmar Subs.
        """
        if run_recap_pipeline is None:
            raise ImportError("recap_engine is not available.")

        dest_dir = target_dir or os.path.join(self.download_dir, "recap_output")
        os.makedirs(dest_dir, exist_ok=True)

        # 1. Resolve source video file
        if is_local_media(url_or_path):
            source_video_path = os.path.abspath(str(url_or_path).strip('\'"'))
            if not os.path.exists(source_video_path):
                raise FileNotFoundError(f"Source video not found: {source_video_path}")
        else:
            if log_callback:
                log_callback("📥 Downloading source video from URL for Recap processing...")
            if progress_callback:
                progress_callback(0, 4, "📥 Downloading source video from URL...")
            source_video_path, _ = self.download_video(url_or_path, target_dir=dest_dir)

        # 2. Run the full recap engine pipeline with Anti-Copyright & Branding
        recap_res = run_recap_pipeline(
            source_video_path=source_video_path,
            job_dir=os.path.join(dest_dir, "workspace"),
            voice=voice,
            narrative_style=narrative_style,
            voice_speed=voice_speed,
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            blur_subtitles=blur_subtitles,
            blur_y=blur_y,
            blur_h=blur_h,
            blur_x=blur_x,
            blur_w=blur_w,
            blur_watermark=blur_watermark,
            burn_subtitles=burn_subtitles,
            sub_y=sub_y,
            sub_lines=sub_lines,
            sub_font_size=sub_font_size,
            sub_style=sub_style,
            title_text=title_text,
            logo_path=logo_path,
            logo_position=logo_position,
            logo_scale=logo_scale,
            logo_opacity=logo_opacity,
            extra_text=extra_text,
            output_ratio=output_ratio,
            log_fn=log_callback,
            progress_fn=progress_callback,
            should_stop=should_stop,
        )

        return recap_res

    def generate_recap_preview(
        self,
        url_or_path: str,
        target_dir: Optional[str] = None,
        blur_subtitles: bool = True,
        blur_y: float = 0.81,
        blur_h: float = 0.13,
        blur_x: float = 0.05,
        blur_w: float = 0.90,
        burn_subtitles: bool = True,
        sub_y: Optional[float] = None,
        sub_lines: int = 1,
        sub_font_size: int = 16,
        sub_style: str = "yellow",
        sample_text: str = "",
        output_ratio: str = "Original",
        mirror_video: bool = True,
        zoom_factor: float = 1.06,
        at_seconds: Optional[float] = None,
        logo_enabled: bool = False,
        logo_path: Optional[str] = None,
        logo_position: Optional[list[float]] = None,
        logo_scale: float = 0.16,
        logo_opacity: float = 0.70,
    ) -> str:
        """
        Extracts a single frame snapshot with blur box and sample subtitle for preview.
        """
        if generate_recap_preview is None:
            raise ImportError("recap_engine.recap_pipeline.generate_recap_preview is not available.")

        dest_dir = target_dir or os.path.join(self.download_dir, "recap_output")
        os.makedirs(dest_dir, exist_ok=True)

        if is_local_media(url_or_path):
            source_video_path = os.path.abspath(str(url_or_path).strip('\'"'))
            if not os.path.exists(source_video_path):
                raise FileNotFoundError(f"Source video not found: {source_video_path}")
        else:
            source_video_path, _ = self.download_video(url_or_path, target_dir=dest_dir)

        if at_seconds is None or float(at_seconds) <= 0:
            at_seconds = find_best_preview_timestamp(source_video_path, target_dir=dest_dir)

        out_img = os.path.join(dest_dir, f"preview_{uuid.uuid4().hex[:8]}.jpg")
        return generate_recap_preview(
            source_video_path=source_video_path,
            output_image_path=out_img,
            blur_subtitles=blur_subtitles,
            blur_y=blur_y,
            blur_h=blur_h,
            blur_x=blur_x,
            blur_w=blur_w,
            burn_subtitles=burn_subtitles,
            sub_y=sub_y,
            sub_lines=sub_lines,
            sub_font_size=sub_font_size,
            sub_style=sub_style,
            sample_text=sample_text,
            output_ratio=output_ratio,
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            at_seconds=at_seconds,
            logo_enabled=logo_enabled,
            logo_path=logo_path,
            logo_position=logo_position,
            logo_scale=logo_scale,
            logo_opacity=logo_opacity,
        )

    @staticmethod
    def cleanup_files(*file_paths: str):
        """Remove temporary files and directories safely and thoroughly."""
        import gc, stat
        gc.collect()
        for path in file_paths:
            if not path or not os.path.exists(path):
                continue
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    try:
                        os.chmod(path, stat.S_IWRITE)
                    except Exception:
                        pass
                    os.remove(path)
                elif os.path.isdir(path):
                    def _on_rm_error(func, p, exc_info):
                        try:
                            os.chmod(p, stat.S_IWRITE)
                            func(p)
                        except Exception:
                            pass
                    shutil.rmtree(path, onerror=_on_rm_error)
            except Exception as e:
                logger.debug(f"cleanup_files warning: {e}")
