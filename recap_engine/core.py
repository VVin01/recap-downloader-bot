from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import re
import unicodedata
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


LogFn = Callable[[str], None]
def _noop(_: str) -> None:
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def resolve_binary(explicit: str | None, executable: str, common: Iterable[str] = ()) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(common)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    found = shutil.which(executable)
    if found:
        return found
    raise FileNotFoundError(f"{executable} မတွေ့ပါ။ config.json / PATH ကိုစစ်ပါ။")


# A user's PC got "WSServerHandshakeError: 403, message='Invalid response status'" on every try (2026-10-12):
# Microsoft's voice service refused the connection. It refuses many VPN / proxy servers: here the direct
# route worked while the same request through a v2rayN server got 403. edge-tts already corrects a wrong clock
# once. The "proxy" tries go through the PC's proxy (aiohttp does not read the Windows proxy by itself) for
# networks where only a proxy works, and the error says what to try.
EDGE_403_HELP = (
    "Microsoft's Edge voice service refused the connection (403). It refuses many VPN / proxy servers.\n"
    "1) If a VPN is on (v2rayN, Psiphon, 1.1.1.1 …), turn it off — or switch to another server — then press START / RESUME.\n"
    "2) Windows clock: Settings → Time & language → Set time automatically + the right time zone.\n"
    "3) Run INSTALL_REQUIREMENTS.bat again (it updates edge-tts).\n"
    "VPN ဖွင့်ထားရင် ပိတ်ပြီး (သို့) server ပြောင်းပြီး START / RESUME ပြန်နှိပ်ပါ။ ကွန်ပျူတာ နာရီ/Time zone မှန်မမှန်လည်း စစ်ပါ။"
)


def edge_proxy() -> str | None:
    """Proxy for the Edge TTS "proxy" tries: EDGE_TTS_PROXY / GEMINI_PROXY, else the Windows proxy, else a local
    v2rayN on 127.0.0.1:10808 when it is running, else none."""
    value = str(os.getenv("EDGE_TTS_PROXY") or os.getenv("GEMINI_PROXY") or "").strip()
    if value:
        return None if value.lower() in {"none", "direct", "off"} else value
    try:
        import urllib.request
        found = urllib.request.getproxies()
        value = str(found.get("https") or found.get("http") or "").strip()
        if value:
            return value if "://" in value else "http://" + value
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", 10808), timeout=0.2):
            return "http://127.0.0.1:10808"
    except OSError:
        return None


def is_edge_refused(error: BaseException | str) -> bool:
    text = str(error)
    return "403" in text or "WSServerHandshakeError" in text


class EdgeTTSEngine:
    """Local Edge-TTS adapter with resilient direct/system-route retries.

    Edge TTS uses a WebSocket service. ``NoAudioReceived`` is often a
    transport/route failure rather than a Smart Sync failure. Generate the
    MP3 in-process so the adapter can classify errors, back off, and try both
    a proxy-bypassed route and the inherited system/proxy route.
    """

    _PROXY_KEYS = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "NO_PROXY", "no_proxy",
    )
    _env_lock = threading.Lock()

    def __init__(self, *, ffmpeg: str, work_dir: Path, rate: str = "+0%", pitch: str = "+0Hz", log: LogFn = _noop) -> None:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is None:
            raise RuntimeError("edge-tts မတွေ့ပါ။ pip install -U edge-tts လုပ်ပြီး ပြန်စမ်းပါ။")
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rate = str(rate or "+0%").strip()
        self.pitch = str(pitch or "+0Hz").strip()
        self.log = log
        self.VOICE_DATABASE: dict[str, dict[str, Any]] = {}
        self._counter = 0
        try:
            from importlib.metadata import version as _pkg_version
            edge_ver = _pkg_version("edge-tts")
        except Exception:
            edge_ver = "unknown"
        self.proxy = edge_proxy()
        self.log(f"✅ Edge TTS READY — version={edge_ver}, rate={self.rate}, pitch={self.pitch}"
                 + (f" | proxy tries via {self.proxy}" if self.proxy else ""))

    @staticmethod
    def _clean_text(text: str) -> str:
        value = unicodedata.normalize("NFC", str(text or ""))
        value = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in value)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            raise RuntimeError("Edge TTS text is empty after cleanup.")
        if not any(ch.isalnum() for ch in value):
            raise RuntimeError(
                f"Edge TTS text contains no speakable letters/numbers: {value!r}. "
                "This is a translation placeholder, not a network failure."
            )
        return value

    async def _save_mp3_async(self, text: str, voice: str, mp3: Path, proxy: str | None = None) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(
            text,
            voice=str(voice),
            rate=self.rate,
            pitch=self.pitch,
            proxy=proxy,
        )
        await communicate.save(str(mp3))

    def _synthesize_once(self, text: str, voice: str, mp3: Path, *, direct: bool) -> None:
        # edge_tts/aiohttp respects environment/system proxy settings. Some
        # setups need a proxy for Gemini while Edge TTS succeeds only direct.
        with self._env_lock:
            backup = {key: os.environ.get(key) for key in self._PROXY_KEYS}
            try:
                if direct:
                    for key in self._PROXY_KEYS:
                        os.environ.pop(key, None)
                    os.environ["NO_PROXY"] = "*"
                    os.environ["no_proxy"] = "*"
                asyncio.run(self._save_mp3_async(text, voice, mp3, None if direct else self.proxy))
            finally:
                for key, old_value in backup.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value

    def generate_channel99_voice(self, text: str, voice: str):
        clean_text = self._clean_text(text)
        self._counter += 1
        stem = f"edge_{self._counter:05d}_{hashlib.sha1(clean_text.encode('utf-8')).hexdigest()[:10]}"
        mp3 = self.work_dir / f"{stem}.mp3"
        wav = self.work_dir / f"{stem}.wav"
        mp3.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)

        # The previous 0.5-second retry was too short for transient WebSocket
        # failures. Try direct twice, then inherited system/proxy twice.
        routes = [
            (True, "direct", 0.0),
            (True, "direct", 1.5),
            (False, "system/proxy", 2.5),
            (False, "system/proxy", 5.0),
        ]
        errors: list[str] = []
        for attempt, (direct, route_name, delay) in enumerate(routes, start=1):
            if delay:
                time.sleep(delay)
            mp3.unlink(missing_ok=True)
            try:
                self.log(
                    f"🌐 Edge TTS request {attempt}/{len(routes)} — {route_name} route | "
                    f"voice={voice} | chars={len(clean_text)}"
                )
                self._synthesize_once(clean_text, voice, mp3, direct=direct)
                if not mp3.is_file() or mp3.stat().st_size < 256:
                    raise RuntimeError("Edge TTS returned no usable MP3 data.")
                if attempt > 1:
                    self.log(f"✅ Edge TTS recovered on {route_name} route (attempt {attempt}/{len(routes)})")
                break
            except Exception as exc:
                name = type(exc).__name__
                msg = str(exc).strip()
                errors.append(f"{route_name}#{attempt}: {name}: {msg}")
                self.log(f"⚠️ Edge TTS {route_name} attempt {attempt}/{len(routes)} failed: {name}: {msg}")
        else:
            preview = clean_text[:160].replace("\n", " ")
            if all(is_edge_refused(e) for e in errors):
                raise RuntimeError(EDGE_403_HELP + "\n\nLast error: " + errors[-1][:300])
            raise RuntimeError(
                "Edge TTS service returned no audio after direct + system/proxy retries. "
                f"voice={voice}, rate={self.rate}, pitch={self.pitch}, chars={len(clean_text)}, "
                f"text_preview={preview!r}. Last errors: " + " | ".join(errors[-4:])
            )

        conv = subprocess.run(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            mp3.unlink(missing_ok=True)
        except Exception:
            pass
        if conv.returncode != 0 or not wav.is_file():
            raise RuntimeError("Edge TTS WAV conversion failed: " + (conv.stderr or "")[-2000:])
        return str(wav)


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    for part in text.split(":"):
        total = total * 60.0 + float(part)
    return total


def normalize_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("segments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("smart_sync_plan.json ထဲမှာ timestamp-resolved 'segments' မရှိပါ။")
    out: list[dict[str, Any]] = []
    for source_index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("narration", item.get("text", ""))).strip()
        start = _seconds(item.get("video_start", item.get("start", 0.0)))
        end = _seconds(item.get("video_end", item.get("end", 0.0)))
        if not text or end <= start + 0.01:
            continue
        out.append({
            "segment_id": len(out) + 1,
            "source_index": source_index,
            "video_start": round(max(0.0, start), 3),
            "video_end": round(max(0.0, end), 3),
            "narration": text,
        })
    if not out:
        raise ValueError("အသုံးပြုလို့ရတဲ့ narration + timestamp segments မရှိပါ။")
    out.sort(key=lambda x: (x["video_start"], x["video_end"]))
    for i, seg in enumerate(out, 1):
        seg["segment_id"] = i
    return out


def _stamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000.0)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}h{m:02d}m{s:02d}s{ms:03d}"


def segment_basename(seg: dict[str, Any]) -> str:
    return f"S{int(seg['segment_id']):04d}_{_stamp(float(seg['video_start']))}__{_stamp(float(seg['video_end']))}"


# English glosses in brackets ("ဂူ (Gu)") are not read by the Burmese voice (2026-10-12).
_GLOSS_RE = re.compile(r"\s*\((?=[^)]*[A-Za-z])[^)]*\)")
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*")


def spoken_text(text: str) -> str:
    """The narration as the voice should say it: Latin-letter glosses in ( ) dropped."""
    value = str(text or "")
    bare = _GLOSS_RE.sub("", value)
    if bare != value and any(ch.isalpha() for ch in bare):
        return re.sub(r"\s+", " ", bare).strip()
    return value


def without_english(text: str) -> str:
    """Last try for a line Edge TTS gave no audio for: the English words left out."""
    return re.sub(r"\s+", " ", _LATIN_RUN_RE.sub(" ", str(text or ""))).strip()


def segment_fingerprint(seg: dict[str, Any], voice: str) -> str:
    payload = {
        "voice": voice,
        "video_start": seg["video_start"],
        "video_end": seg["video_end"],
        "narration": seg["narration"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def probe_duration(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    return float(result.stdout.strip())


def _concat_list_line(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace("'", "'\\''")
    return f"file '{p}'"


def trim_edge_silence(
    source: Path,
    target: Path,
    *,
    keep_lead: float = 0.12,
    keep_tail: float = 0.38,
    min_duration: float = 0.0,
    threshold_db: float = -50.0,
) -> tuple[Path, float]:
    """Short pauses: cut excessive Edge TTS silence while keeping a natural breath pause (~0.5s) between sentences.

    Joined clips pause keep_lead + keep_tail (~0.50 s, like a natural human breath/sentence break)
    instead of 0.25s frantic pacing or ~1.2s dead air.
    A clip is never made shorter than min_duration: below source window / max_video_speed Smart
    Sync would have to cut the picture, so there a longer pause is kept. Pauses inside the clip
    are untouched and the source WAV (the TTS cache) is never changed.
    Returns (file to use, its duration).
    """
    import wave

    import numpy as np

    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        raw = reader.readframes(params.nframes)
    rate = int(params.framerate or 1)
    if params.sampwidth != 2 or not params.nframes:
        return source, params.nframes / float(rate)
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, params.nchannels)
    duration = len(samples) / rate
    window = max(1, rate // 100)  # 10 ms
    count = len(samples) // window
    if not count:
        return source, duration
    blocks = samples[: count * window].astype(np.float32).reshape(count, window, params.nchannels)
    level = np.sqrt(np.mean(blocks ** 2, axis=(1, 2))) / 32768.0
    loud = np.nonzero(level > 10 ** (threshold_db / 20.0))[0]
    if not loud.size:
        return source, duration
    start = max(0.0, loud[0] * window / rate - keep_lead)
    end = min(duration, (loud[-1] + 1) * window / rate + keep_tail)
    if end - start < min_duration:
        end = min(duration, start + min_duration)
        start = max(0.0, end - min_duration)
    first, last = int(round(start * rate)), int(round(end * rate))
    if first <= 0 and last >= len(samples):
        return source, duration
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        try:
            with wave.open(str(target), "rb") as existing:
                if existing.getnframes() == last - first:
                    return target, (last - first) / rate
        except (wave.Error, EOFError):
            pass
    partial = target.with_name(target.stem + ".partial.wav")
    with wave.open(str(partial), "wb") as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples[first:last].tobytes())
    partial.replace(target)
    return target, (last - first) / rate


def concat_audio(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    if not parts:
        raise ValueError("Audio parts မရှိပါ။")
    list_file = work_dir / "audio_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Audio concat failed:\n{result.stderr[-4000:]}")


def _sync_filter(
    source_duration: float,
    target_duration: float,
    *,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
) -> tuple[str, dict[str, float]]:
    """Recap-style hybrid timing plan for one timestamp segment.

    - Normal case: exact setpts to the actual TTS duration.
    - Audio much shorter: cap speed-up at max_video_speed, then trim.
    - Audio much longer: cap slow-down at max_video_slow.  When the requested
      stretch exceeds that visual limit, the caller may distribute short
      freeze holds across the segment instead of making motion unnaturally slow.
    """
    if source_duration <= 0.01 or target_duration <= 0.01:
        raise ValueError("Smart Sync duration invalid")
    max_speed = max(1.0, float(max_video_speed))
    max_slow = max(1.0, float(max_video_slow))
    desired = target_duration / source_duration
    min_factor = 1.0 / max_speed

    if desired < min_factor:
        applied = min_factor
        adjusted = source_duration * applied
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),trim=duration={target_duration:.6f},setpts=PTS-STARTPTS,fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    if desired <= max_slow or not smooth_freeze_fallback:
        # With fallback disabled this deliberately behaves like V0.2.4 exact sync.
        applied = desired
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    applied = max_slow
    adjusted = source_duration * applied
    freeze_duration = max(0.0, target_duration - adjusted)
    # The filter string is not used for the hybrid case; render_video_part builds
    # a split/concat graph so the hold time is distributed across the segment.
    return (
        "",
        {
            "desired_factor": desired,
            "applied_factor": applied,
            "source_duration": source_duration,
            "target_duration": target_duration,
            "freeze_duration": freeze_duration,
            "freeze_count": 0.0,
            "hybrid_used": 1.0,
            "max_freeze_hold": max(0.15, float(max_freeze_hold)),
        },
    )


OUTPUT_RATIOS = ("16:9", "9:16", "1:1", "Original")
FRAME_FILLS = ("blur", "black", "crop")  # fit on a blurred copy / fit with black bars / crop to fill
OUTPUT_LONG_SIDE = 1920


def _even(value: float) -> int:
    return max(2, int(round(float(value) / 2.0)) * 2)


def probe_video_size(ffprobe: str, path: Path | str) -> tuple[int, int]:
    """Width/height of the first video stream as ffmpeg decodes it (phone rotation applied)."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-1200:]}")
    streams = (json.loads(result.stdout or "{}").get("streams") or [{}])
    video = streams[0] if streams else {}
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(round(float((video.get("tags") or {}).get("rotate", 0) or 0)))
        except (TypeError, ValueError):
            rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def output_canvas(ratio: str, source_w: int = 0, source_h: int = 0) -> tuple[int, int]:
    """Final frame size: 16:9 = 1920x1080, 9:16 = 1080x1920, 1:1 = 1080x1080, Original = the
    source shape (long side at most 1920)."""
    if ratio == "9:16":
        return 1080, 1920
    if ratio == "1:1":
        return 1080, 1080
    if ratio == "Original" and source_w > 0 and source_h > 0:
        scale = min(1.0, OUTPUT_LONG_SIDE / max(source_w, source_h))
        return _even(source_w * scale), _even(source_h * scale)
    return 1920, 1080


def frame_layout(ratio: str, fill: str, source_w: int, source_h: int,
                 canvas: tuple[int, int] | None = None) -> dict[str, Any]:
    """Everything _visual_filters needs; `canvas` overrides the size (the preview draws smaller)."""
    ratio = ratio if ratio in OUTPUT_RATIOS else "16:9"
    cw, ch = canvas or output_canvas(ratio, source_w, source_h)
    cw, ch = _even(cw), _even(ch)
    fill = fill if fill in FRAME_FILLS else "blur"
    sw, sh = int(source_w or 0) or cw, int(source_h or 0) or ch

    if fill == "crop" or abs(sw / sh - cw / ch) < 0.01:
        pw, ph = cw, ch
        ox, oy = 0, 0
    else:
        scale = min(cw / sw, ch / sh) if sw and sh else 1.0
        pw, ph = min(cw, _even(sw * scale)), min(ch, _even(sh * scale))
        ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0

    return {
        "ratio": ratio,
        "fill": fill,
        "cw": cw,
        "ch": ch,
        "sw": int(source_w or 0),
        "sh": int(source_h or 0),
        "pw": pw,
        "ph": ph,
        "ox": ox,
        "oy": oy,
    }


def _visual_filters(mirror_video: bool, zoom_factor: float,
                    layout: dict[str, Any] | None = None) -> tuple[str, float]:
    """Flip + zoom + placement on the output frame, as one filtergraph string.

    Crop (and any source already of the frame's shape) fills the frame and cuts the overflow —
    exactly the old 16:9 behaviour. Blur / black keep the whole picture visible, centred, with a
    blurred copy of it or black bars in the free room. Zoom always zooms the picture itself.
    """
    zoom = max(1.0, min(float(zoom_factor), 1.25))
    lay = layout or {"fill": "crop", "cw": 1920, "ch": 1080, "sw": 0, "sh": 0}
    cw, ch = int(lay["cw"]), int(lay["ch"])
    sw, sh = int(lay.get("sw") or 0) or cw, int(lay.get("sh") or 0) or ch
    head = "hflip," if mirror_video else ""
    color_grade = ",eq=contrast=1.03:brightness=-0.01:saturation=1.05" if mirror_video else ""
    if lay.get("fill") == "crop" or abs(sw / sh - cw / ch) < 0.01:
        fill_w, fill_h = max(cw, _even(cw * zoom)), max(ch, _even(ch * zoom))
        return (f"{head}scale={fill_w}:{fill_h}:force_original_aspect_ratio=increase,"
                f"crop={cw}:{ch},setsar=1{color_grade},format=yuv420p"), zoom
    if "pw" in lay and "ph" in lay and "ox" in lay and "oy" in lay:
        pw, ph = int(lay["pw"]), int(lay["ph"])
        ox, oy = int(lay["ox"]), int(lay["oy"])
    else:
        scale = min(cw / sw, ch / sh)
        pw, ph = min(cw, _even(sw * scale)), min(ch, _even(sh * scale))
        ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0
    zw, zh = max(pw, _even(pw * zoom)), max(ph, _even(ph * zoom))
    picture = f"scale={zw}:{zh},crop={pw}:{ph},setsar=1{color_grade}"
    if lay.get("fill") == "black":
        return f"{head}{picture},pad={cw}:{ch}:{ox}:{oy}:color=black,setsar=1,format=yuv420p", zoom
    bw, bh = _even(cw / 4.0), _even(ch / 4.0)  # the blurred copy is made small (fast), then scaled up
    luma = max(1, min(14, min(bw, bh) // 2 - 1))
    chroma = max(1, min(7, min(bw, bh) // 4 - 1))
    return (f"{head}split=2[vffg0][vfbg0];"
            f"[vfbg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=luma_radius={luma}:luma_power=2:chroma_radius={chroma}:chroma_power=1,"
            f"scale={cw}:{ch},eq=brightness=-0.06:saturation=0.85,setsar=1[vfbg];"
            f"[vffg0]{picture}[vffg];"
            f"[vfbg][vffg]overlay={ox}:{oy},format=yuv420p"), zoom


def _distributed_freeze_graph(
    source_duration: float,
    target_duration: float,
    slow_factor: float,
    freeze_duration: float,
    mirror_video: bool,
    zoom_factor: float,
    max_freeze_hold: float,
    layout: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, float]]:
    """Build a smooth-ish hybrid graph: moderate slow motion + distributed holds.

    Rather than one long frozen tail, the remaining hold time is spread over
    several positions inside the source segment.  Each hold clones the nearby
    frame for a short period, then motion continues.
    """
    import math

    max_hold = max(0.15, min(float(max_freeze_hold), 1.50))
    # Aim for <= max_hold per pause, but avoid pathological graph sizes.
    hold_count = max(1, int(math.ceil(freeze_duration / max_hold)))
    hold_count = min(24, hold_count)
    hold_each = freeze_duration / hold_count if hold_count else 0.0

    # Split source into equal pieces; hold after each piece (including the last).
    # This makes long extra duration less noticeable than one multi-second tail freeze.
    chunk_count = hold_count
    chunk_source = source_duration / chunk_count
    filters: list[str] = []
    split_labels = "".join(f"[src{i}]" for i in range(chunk_count))
    filters.append(f"[0:v]split={chunk_count}{split_labels}")
    out_labels: list[str] = []
    for i in range(chunk_count):
        a = i * chunk_source
        b = source_duration if i == chunk_count - 1 else (i + 1) * chunk_source
        src_len = b - a
        slowed_len = src_len * slow_factor
        target_len = slowed_len + hold_each
        label = f"[h{i}]"
        filters.append(
            f"[src{i}]trim=start={a:.6f}:end={b:.6f},"
            f"setpts={slow_factor:.9f}*(PTS-STARTPTS),fps=30,"
            f"tpad=stop_mode=clone:stop_duration={hold_each:.6f},"
            f"trim=duration={target_len:.6f},setpts=PTS-STARTPTS{label}"
        )
        out_labels.append(label)
    filters.append(f"{''.join(out_labels)}concat=n={chunk_count}:v=1:a=0[vhyb]")
    visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
    filters.append(f"[vhyb]{visual}[vout]")
    metrics = {
        "desired_factor": target_duration / source_duration,
        "applied_factor": slow_factor,
        "source_duration": source_duration,
        "target_duration": target_duration,
        "freeze_duration": freeze_duration,
        "freeze_count": float(hold_count),
        "freeze_each": hold_each,
        "hybrid_used": 1.0,
        "zoom_factor": zoom,
        "mirror_video": 1.0 if mirror_video else 0.0,
    }
    return ";".join(filters), "[vout]", metrics

def ffmpeg_encoders(ffmpeg: str) -> set[str]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("V"):
            encoders.add(fields[1])
    return encoders


def render_video_part(
    ffmpeg: str,
    source: Path,
    output: Path,
    seg: dict[str, Any],
    target_duration: float,
    encoder: str,
    mirror_video: bool = True,
    zoom_factor: float = 1.05,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
    frame_count: int | None = None,
    layout: dict[str, Any] | None = None,
) -> dict[str, float]:
    """frame_count (when given) locks the part to exactly that many 30 fps frames.
    layout (frame_layout) is the output ratio / fill; None = the original 16:9 fill.

    Without it each part rounds up to the next whole frame; over ~270 parts that
    added ~6 s, so the picture drifted behind the narration and -shortest cut the end.
    """
    source_duration = float(seg["video_end"]) - float(seg["video_start"])
    frame_lock = ["-frames:v", str(int(frame_count))] if frame_count and frame_count > 0 else []
    sync_vf, metrics = _sync_filter(
        source_duration,
        target_duration,
        max_video_speed=max_video_speed,
        max_video_slow=max_video_slow,
        smooth_freeze_fallback=smooth_freeze_fallback,
        max_freeze_hold=max_freeze_hold,
    )

    common_input = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{float(seg['video_start']):.6f}",
        "-t", f"{source_duration:.6f}",
        "-i", str(source), "-an",
    ]

    if metrics.get("hybrid_used", 0.0) >= 0.5:
        graph, out_label, metrics = _distributed_freeze_graph(
            source_duration=source_duration,
            target_duration=target_duration,
            slow_factor=float(metrics["applied_factor"]),
            freeze_duration=float(metrics["freeze_duration"]),
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            max_freeze_hold=max_freeze_hold,
            layout=layout,
        )
        cmd = [
            *common_input,
            "-filter_complex", graph,
            "-map", out_label,
            *frame_lock,
            *_encoder_args(encoder),
            "-movflags", "+faststart", str(output),
        ]
    else:
        visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
        vf = sync_vf + "," + visual
        if frame_lock:
            # Clone the last frame briefly so -frames:v can always be satisfied.
            vf += ",tpad=stop_mode=clone:stop_duration=0.5"
            metrics["frame_count"] = float(frame_count)
        metrics["zoom_factor"] = zoom
        metrics["mirror_video"] = 1.0 if mirror_video else 0.0
        cmd = [
            *common_input,
            "-vf", vf,
            *frame_lock,
            *_encoder_args(encoder),
            "-movflags", "+faststart", str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video segment render failed ({encoder}):\n{result.stderr[-4000:]}")
    return metrics

def concat_video_parts(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    list_file = work_dir / "video_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video concat failed:\n{result.stderr[-4000:]}")


def mux_audio(
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    volume_percent: int = 100,
    bgm_path: Optional[Path] = None,
    bgm_volume: float = 0.18,
) -> None:
    # Warm broadcast mastering chain:
    # 1. highpass=60Hz: cuts low-frequency hum/rumble
    # 2. lowpass=12kHz: softens harsh digital sibilance
    # 3. volume scaling: user configured volume
    # 4. alimiter: soft-knee limiter to prevent clipping and ensure comfortable listening
    vol_scale = float(volume_percent or 100) / 100.0

    if bgm_path and Path(bgm_path).is_file():
        # Studio ducking: Voice triggers sidechain compressor on BGM
        # When voice is active, BGM ducks smoothly; when voice pauses, BGM rises gently.
        filter_complex = (
            f"[1:a]highpass=f=60,lowpass=f=12000,volume={vol_scale:.3f}[v_master];"
            f"[v_master]asplit=2[v_out][v_sc];"
            f"[2:a]volume={bgm_volume:.3f},aloop=loop=-1:size=2e+09[bgm_loop];"
            f"[bgm_loop][v_sc]sidechaincompress=threshold=0.08:ratio=4:attack=50:release=350[bgm_ducked];"
            f"[v_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.95:level=disabled[a_final]"
        )
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio), "-i", str(bgm_path),
            "-map", "0:v:0", "-map", "[a_final]", "-map_metadata", "-1", "-c:v", "copy",
            "-filter_complex", filter_complex, "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
    else:
        af_filters = [
            "highpass=f=60",
            "lowpass=f=12000",
            f"volume={vol_scale:.3f}",
            "alimiter=limit=0.95:level=disabled",
        ]
        af_str = ",".join(af_filters)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1", "-c:v", "copy",
            "-af", af_str, "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Final mux failed:\n{result.stderr[-4000:]}")



def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _render_text_with_pillow(
    text: str,
    output: Path,
    font_size: int = 46,
    max_width: int = 1500,
    bg_alpha: float = 0.75,
    border_color: tuple = (255, 215, 0, 180),
    font_path: str = ""
) -> Path:
    """Render Myanmar text onto a transparent PNG badge with dark pill and gold/white styling."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow is required for rendering title text. Install with: pip install pillow")

    cand_fonts = [
        str(font_path or "").strip(),
        os.getenv("RECAP_MM_FONT", ""),
        r"C:\Windows\Fonts\mmrtext.ttf",
        r"C:\Windows\Fonts\MyanmarText.ttf",
        r"C:\Windows\Fonts\Pyidaungsu.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    font = None
    for cf in cand_fonts:
        if cf and os.path.exists(cf):
            try:
                font = ImageFont.truetype(cf, font_size)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        lines = [text.strip()]

    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)

    # Wrap long lines
    wrapped_lines = []
    for line in lines:
        words = line.split(" ")
        curr = ""
        for w in words:
            test = (curr + " " + w).strip()
            bbox = d.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width and curr:
                wrapped_lines.append(curr)
                curr = w
            else:
                curr = test
        if curr:
            wrapped_lines.append(curr)

    if not wrapped_lines:
        wrapped_lines = [text]

    line_bboxes = [d.textbbox((0, 0), l, font=font) for l in wrapped_lines]
    line_widths = [max(1, b[2] - b[0]) for b in line_bboxes]
    line_heights = [max(font_size, b[3] - b[1]) for b in line_bboxes]
    max_tw = max(line_widths) if line_widths else 200
    line_h = max(line_heights) if line_heights else font_size
    spacing = int(font_size * 0.25)
    total_th = len(wrapped_lines) * line_h + (len(wrapped_lines) - 1) * spacing

    pad_x = int(font_size * 0.8)
    pad_y = int(font_size * 0.35)
    w = max_tw + pad_x * 2
    h = total_th + pad_y * 2

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    alpha_int = int(round(bg_alpha * 255))
    if alpha_int > 0:
        draw.rounded_rectangle([0, 0, w, h], radius=16, fill=(15, 15, 20, alpha_int), outline=border_color, width=2)

    curr_y = pad_y
    for i, l in enumerate(wrapped_lines):
        lw = line_widths[i]
        curr_x = (w - lw) // 2
        # Drop shadow
        draw.text((curr_x + 2, curr_y + 2), l, font=font, fill=(0, 0, 0, 220))
        # Main text
        draw.text((curr_x, curr_y), l, font=font, fill=(255, 255, 255, 255))
        curr_y += line_h + spacing

    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output), "PNG")
    return output


def _render_title_png(text: str, output: Path, font_path: str, font_size: int, max_width: int = 1500) -> Path:
    """Render Myanmar title through Chromium renderer with Pillow fallback."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty title")

    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
        cfg = {
            "font_path": str(font_path or ""),
            "font_size": int(font_size),
            "max_width": int(max_width),
        }
        return make_video_title_png(text, output, cfg)
    except Exception:
        # Seamless Pillow fallback
        return _render_text_with_pillow(
            text=text,
            output=output,
            font_size=max(24, int(font_size)),
            max_width=int(max_width),
            bg_alpha=0.82,
            border_color=(255, 215, 0, 200),
            font_path=font_path
        )


def _render_extra_text_png(
    text: str,
    output: Path,
    font_size: int,
    background_alpha: float = 0.18,
    max_width: int = 1500,
) -> Path:
    """Render extra-text overlay with Chromium renderer and Pillow fallback."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty extra text")

    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
        size = max(18, min(int(font_size), 84))
        cfg = {
            "font_size": size,
            "min_title_font_size": max(16, min(size, int(round(size * 0.62)))),
            "panel_width": max(320, min(int(max_width), 1800)),
            "video_title_panel_height": max(110, min(360, int(round(size * 4.2)))),
            "max_title_lines": 3,
            "panel_bg_alpha": max(0.0, min(1.0, float(background_alpha))),
            "panel_radius": 22,
            "title_horizontal_padding": 34,
            "title_vertical_padding": 14,
            "stroke_px": 1.4,
            "font_weight": 700,
        }
        return make_video_title_png(text, output, cfg)
    except Exception:
        # Seamless Pillow fallback
        return _render_text_with_pillow(
            text=text,
            output=output,
            font_size=max(18, int(font_size)),
            max_width=int(max_width),
            bg_alpha=max(0.1, float(background_alpha)),
            border_color=(100, 150, 255, 180)
        )

def _encoder_args(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


BURMESE_SYLLABLE_BREAK = re.compile(
    r'(?<![္])(?=[က-အ\u1023-\u102A\u104E](?![\u1037\u1038]?[်္]))'
)

PARTICLE_SYLS = {
    'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'ပြီး', 'တဲ့အခါ', 'အခါ', 'သော်လည်း',
    'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်', 'ဖြစ်ပြီး', 'လျက်',
    'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတယ်', 'နေတာ', 'ပါပဲ', 'ဖြစ်တယ်', 'ရှိတယ်', 'သွားတယ်',
    'လိုက်တယ်', 'လိုက်တာ', 'ကို', 'မှာ', 'က', 'နဲ့', 'နှင့်', 'သို့', 'မှ', 'လည်း', 'တော့',
    'ကနေ', 'နေ', 'ထဲ', 'တုန်း'
}


BURMESE_COMPOUNDS = [
    "ကျွန်တော်", "ကျွန်မ", "သူမ", "သူတို့", "သူက",
    "ကောင်မလေး", "ကောင်လေး", "လူငယ်လေး", "အမျိုးသမီး", "အမျိုးသား",
    "ကမန်းကတန်း", "အလောတကြီး", "တခြား", "စိတ်ပျက်လက်ပျက်", "စိတ်ပျက်", "မထင်မှတ်ဘဲ", "မထင်မှတ်ထား",
    "တစ်ယောက်ဆီကနေ", "တစ်ယောက်ဆီက", "တစ်ယောက်တည်း", "တစ်ယောက်", "တစ်ပွဲ", "တစ်ခု", "ခေါက်ဆွဲတစ်ပွဲ", "ခေါက်ဆွဲ",
    "ဆိုင်ကယ်ပေါ်", "ဆိုင်ကယ်", "ပစ္စည်းတွေကို", "ပစ္စည်းတွေ", "ပစ္စည်း",
    "လမ်းဘေးဈေးသည်တွေနဲ့", "လမ်းဘေးဈေးသည်", "ဈေးသည်",
    "ဆွဲလုယူပြီး", "ဆွဲလုယူ", "ဆွဲယူသွား", "ဆွဲယူ", "မောင်းပြေးသွား", "မောင်းပြေး", "ထွက်ပြေးသွား", "ထွက်ပြေး",
    "ထိတ်လန့်တကြား", "ကြောင်အမ်းအမ်း", "အဖက်မလုပ်ဘဲ", "လက်ထဲက", "အဲ့ဒီတော့မှပဲ", "အဲ့ဒီတော့မှ", "အဲ့ဒီနောက်",
    "ဘာဖြစ်သွားမှန်းမသိ", "ဘာဖြစ်", "ဘာကြောင့်လဲ", "လတ်လတ်ဆတ်ဆတ်", "ထပ်ရောက်လာ", "ထပ်ရောက်", "ပြန်ရောက်လာ", "ပြန်ရောက်",
    "ဖိတ်စင်ကုန်", "ဖိတ်စင်", "ဝင်ထိုင်လိုက်တာနဲ့", "ထိုင်လိုက်တာနဲ့", "ထိုင်လိုက်ရုံပဲ", "ထိုင်လိုက်", "လိုက်ပါသေးတယ်", "သွားပါတော့တယ်",
    "လာစစ်နေတာမှန်း", "လာစစ်နေတာ", "ရိပ်မိလိုက်ရတာပါ", "ရိပ်မိလိုက်တာ", "စိတ်တိုသွား",
    "စည်ပင်တွေ", "စည်ပင်", "တာနဲ့", "တာမို့", "တာကြောင့်", "တဲ့အတွက်", "ပြီးတော့", "ပြီးနောက်", "ပြီးရင်", "တဲ့အခါ", "ဒါပေမဲ့",
    "ဒါပေမယ့်", "ကြောင့်", "အတွက်", "ဖြစ်ပြီး"
]

PARTICLE_SYLS = {
    'ပြီးတော့', 'ပြီးနောက်', 'ပြီးရင်', 'ပြီး', 'တဲ့အခါ', 'အခါ', 'သော်လည်း',
    'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်', 'ဖြစ်ပြီး', 'လျက်',
    'ပါတယ်', 'ခဲ့တယ်', 'တယ်', 'နေတယ်', 'နေတာ', 'ပါပဲ', 'ဖြစ်တယ်', 'ရှိတယ်', 'သွားတယ်',
    'လိုက်တယ်', 'လိုက်တာ', 'တာနဲ့', 'တာမို့', 'ကို', 'မှာ', 'က', 'နဲ့', 'နှင့်', 'သို့', 'မှ', 'လည်း', 'တော့',
    'ကနေ', 'နေ', 'ထဲ', 'တုန်း'
}

SUBTITLE_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "yellow": {
        "name": "Classic Gold",
        "label": "အဝါရောင် (Classic Gold)",
        "short": "အဝါရောင်",
        "primary": "&H0000FFFF",  # BGR: Yellow (0x00, 0xFF, 0xFF)
        "outline_col": "&H00000000",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.4,
        "shadow": 1.4,
    },
    "white": {
        "name": "Clean White",
        "label": "အဖြူရောင် (Clean White)",
        "short": "အဖြူရောင်",
        "primary": "&H00FFFFFF",  # BGR: White
        "outline_col": "&H00000000",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.2,
        "shadow": 1.2,
    },
    "boxed": {
        "name": "Dark Box",
        "label": "နောက်ခံဘောက် (Dark Box)",
        "short": "နောက်ခံဘောက်",
        "primary": "&H00FFFFFF",
        "outline_col": "&H00000000",
        "back_col": "&H70000000",  # Semi-transparent black box
        "border_style": 3,
        "outline": 1.0,
        "shadow": 0.0,
    },
    "neon": {
        "name": "Neon Cyan",
        "label": "နီယွန်စိမ်းပြာ (Neon Cyan)",
        "short": "နီယွန်",
        "primary": "&H00FFFF00",  # BGR: Cyan (0x00, 0xFF, 0xFF)
        "outline_col": "&H00151515",
        "back_col": "&H80000000",
        "border_style": 1,
        "outline": 2.2,
        "shadow": 1.2,
    },
}


def split_into_burmese_syllables(text: str) -> list[str]:
    """Splits Burmese text into indivisible atomic syllables, merging compound connectives so they stay together."""
    if not text:
        return []
    raw_syls = [s for s in BURMESE_SYLLABLE_BREAK.split(text) if s]
    merged = []
    i = 0
    while i < len(raw_syls):
        matched = False
        for comp in BURMESE_COMPOUNDS:
            joined = ""
            j = i
            while j < len(raw_syls) and len(joined.rstrip()) < len(comp):
                joined += raw_syls[j]
                j += 1
            if joined.rstrip() == comp:
                merged.append(joined)
                i = j
                matched = True
                break
        if not matched:
            merged.append(raw_syls[i])
            i += 1
    return merged


def split_burmese_subtitle_cues(
    start_sec: float,
    end_sec: float,
    text: str,
    max_cue_chars: int = 28,
    lines_per_cue: int = 1,
) -> list[tuple[float, float, str]]:
    """
    Splits long segment narration into short, balanced, and readable subtitle cues.
    - Preserves whole thoughts and sentences without fragmented micro-cues.
    - Prevents single lines from overflowing beyond the screen borders on 9:16 or 16:9.
    - Uses strict Burmese compound & syllable tokenization so words (e.g. 'လိုက်', 'ကောင်မလေး')
      are NEVER sliced across lines or cues.
    """
    text = str(text or "").strip()
    if not text:
        return []

    lines_cnt = 2 if int(lines_per_cue) == 2 else 1
    total_dur = max(0.4, float(end_sec) - float(start_sec))
    cue_limit = int(max_cue_chars or (54 if lines_cnt == 2 else 28))

    # If the segment text fits on screen, keep as single cue
    if len(text) <= cue_limit:
        clean_text = text
        if lines_cnt == 1:
            clean_text = re.sub(r'\s+', ' ', clean_text.replace("\n", " ")).strip()
        elif lines_cnt == 2 and "\n" not in clean_text and len(clean_text) > 28:
            syls = split_into_burmese_syllables(clean_text)
            if len(syls) > 1:
                half = len(clean_text) // 2
                best_idx = 1
                best_diff = 9999
                cand = []
                for i in range(1, len(syls)):
                    left_len = sum(len(s) for s in syls[:i])
                    diff = abs(left_len - half)
                    curr = syls[i - 1].strip()
                    if curr in PARTICLE_SYLS or any(curr.endswith(p) for p in ['ပြီး', 'တော့', 'က', 'နဲ့', 'မှာ', 'ကို', 'တယ်', 'တာ', '၊', ' ']):
                        cand.append((diff, i))
                if cand:
                    cand.sort(key=lambda x: x[0])
                    best_idx = cand[0][1]
                else:
                    for i in range(1, len(syls)):
                        left_len = sum(len(s) for s in syls[:i])
                        diff = abs(left_len - half)
                        if diff < best_diff:
                            best_diff = diff
                            best_idx = i
                l1 = "".join(syls[:best_idx]).strip()
                l2 = "".join(syls[best_idx:]).strip()
                if l1 and l2:
                    clean_text = f"{l1}\n{l2}"
        return [(round(start_sec, 3), round(end_sec, 3), clean_text)]

    # 1. Break into natural sentences or clauses (။, \n)
    initial_chunks = [s.strip() for s in re.split(r'(?<=[။\n])\s*', text) if s.strip()]

    def _split_chunk(chunk: str) -> list[str]:
        chunk = chunk.strip()
        if len(chunk) <= cue_limit:
            return [chunk]

        # Try splitting at comma ၊
        if "၊" in chunk:
            parts = [p.strip() for p in re.split(r'(?<=[၊])\s*', chunk) if p.strip()]
            if len(parts) > 1:
                res: list[str] = []
                for p in parts:
                    res.extend(_split_chunk(p))
                return res

        syls = split_into_burmese_syllables(chunk)
        if len(syls) <= 1:
            return [chunk]

        half_chars = len(chunk) // 2
        best_idx = 1
        best_diff = 9999

        candidate_splits = []
        for i in range(1, len(syls)):
            left_len = sum(len(s) for s in syls[:i])
            diff = abs(left_len - half_chars)
            curr_syl = syls[i - 1].strip()
            # Prioritize major clause connectives for natural grammatical breaks
            if any(curr_syl == c or curr_syl.endswith(c) for c in ['တာနဲ့', 'တာမို့', 'တာကြောင့်', 'တဲ့အခါ', 'တဲ့အတွက်', 'ပြီးတော့', 'ပြီးနောက်', 'ပြီး', 'ဒါပေမဲ့', 'ဒါပေမယ့်', 'ကြောင့်', 'အတွက်']):
                score = diff - 25
                candidate_splits.append((score, i))
            elif curr_syl in PARTICLE_SYLS or any(curr_syl.endswith(p) for p in ['တော့', 'က', 'နဲ့', 'မှာ', 'ကို', 'တယ်', 'တာ', '၊', ' ']):
                score = diff
                candidate_splits.append((score, i))

        if candidate_splits:
            candidate_splits.sort(key=lambda x: x[0])
            best_idx = candidate_splits[0][1]
        else:
            for i in range(1, len(syls)):
                left_len = sum(len(s) for s in syls[:i])
                diff = abs(left_len - half_chars)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i

        p1 = "".join(syls[:best_idx]).strip()
        p2 = "".join(syls[best_idx:]).strip()
        if not p1 or not p2:
            return [chunk]
        return _split_chunk(p1) + _split_chunk(p2)

    raw_cues: list[str] = []
    for s in initial_chunks:
        raw_cues.extend(_split_chunk(s))

    # Merge very short cues (< 12 characters) into previous cue if fits
    merged_cues: list[str] = []
    for c in raw_cues:
        if merged_cues and len(c) < 12 and (len(merged_cues[-1]) + len(c) <= cue_limit + 4):
            merged_cues[-1] = (merged_cues[-1] + " " + c).strip()
        else:
            merged_cues.append(c)

    # 3. Format cues according to lines_per_cue
    formatted: list[tuple[float, float, str]] = []
    total_len = sum(max(1, len(c)) for c in merged_cues) or 1
    cur_t = start_sec

    for i, c in enumerate(merged_cues):
        c_dur = (len(c) / total_len) * total_dur
        end_t = cur_t + c_dur if i < len(merged_cues) - 1 else end_sec
        c_clean = c.strip()

        if lines_cnt == 1:
            c_clean = re.sub(r'\s+', ' ', c_clean.replace("\n", " ")).strip()
        else:
            c_clean = c_clean.replace("\r", "")
            if "\n" not in c_clean and len(c_clean) > 35:
                syls = split_into_burmese_syllables(c_clean)
                if len(syls) > 1:
                    half_chars = len(c_clean) // 2
                    best_idx = 1
                    best_diff = 9999
                    candidates = []
                    for k in range(1, len(syls)):
                        left_len = sum(len(s) for s in syls[:k])
                        diff = abs(left_len - half_chars)
                        curr_syl = syls[k - 1].strip()
                        if curr_syl in PARTICLE_SYLS or any(curr_syl.endswith(p) for p in ['ပြီး', 'တော့', 'က', 'နဲ့', 'မှာ', 'ကို', 'တယ်', 'တာ', '၊', ' ']):
                            candidates.append((diff, k))
                    if candidates:
                        candidates.sort(key=lambda x: x[0])
                        best_idx = candidates[0][1]
                    else:
                        for k in range(1, len(syls)):
                            left_len = sum(len(s) for s in syls[:k])
                            diff = abs(left_len - half_chars)
                            if diff < best_diff:
                                best_diff = diff
                                best_idx = k
                    line1 = "".join(syls[:best_idx]).strip()
                    line2 = "".join(syls[best_idx:]).strip()
                    if line1 and line2:
                        c_clean = f"{line1}\n{line2}"

        formatted.append((round(cur_t, 3), round(end_t, 3), c_clean))
        cur_t = end_t

    return formatted


def apply_production_overlays(
    ffmpeg: str,
    input_video: Path,
    output_video: Path,
    overlays: dict[str, Any] | None,
    encoder: str,
    work_dir: Path,
    log: LogFn = _noop,
    frame_size: tuple[int, int] = (1920, 1080),
    layout: dict[str, Any] | None = None,
) -> Path:
    overlays = dict(overlays or {})
    cw, ch = int(frame_size[0]), int(frame_size[1])
    lay = layout or {}
    if "pw" in lay and "ph" in lay and "ox" in lay and "oy" in lay:
        pw, ph = int(lay["pw"]), int(lay["ph"])
        ox, oy = int(lay["ox"]), int(lay["oy"])
    else:
        sw, sh = int(lay.get("sw") or 0) or cw, int(lay.get("sh") or 0) or ch
        if lay.get("fill") == "crop" or abs(sw / sh - cw / ch) < 0.01:
            pw, ph = cw, ch
            ox, oy = 0, 0
        else:
            scale = min(cw / sw, ch / sh) if sw and sh else 1.0
            pw, ph = min(cw, _even(sw * scale)), min(ch, _even(sh * scale))
            ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0

    text_width = int(pw * 0.78)  # Scaled to actual picture width
    boxes = [x for x in (overlays.get("blur_boxes") or []) if isinstance(x, dict)]
    title_text = str(overlays.get("title_text") or "").strip()
    logo_enabled = bool(overlays.get("logo_enabled", True))
    logo_path = str(overlays.get("logo_path") or "").strip() if logo_enabled else ""
    logo_file = Path(logo_path) if logo_path else None
    extra_text = str(overlays.get("extra_text") or "").strip()
    subtitles_file = str(overlays.get("subtitles_file") or "").strip()
    subtitles_path = Path(subtitles_file) if subtitles_file else None
    if not boxes and not title_text and not extra_text and not (logo_file and logo_file.is_file()) and not (subtitles_path and subtitles_path.is_file()):
        return input_video

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_video)]
    input_index = 1
    title_file: Path | None = None
    title_idx: int | None = None
    logo_idx: int | None = None
    extra_file: Path | None = None
    extra_idx: int | None = None
    if title_text:
        title_file = _render_title_png(
            title_text, work_dir / "title_overlay.png",
            str(overlays.get("title_font_path") or ""), int(overlays.get("title_font_size", 54)),
            max_width=text_width,
        )
        title_idx = input_index
        cmd += ["-i", str(title_file)]
        input_index += 1
    if logo_file and logo_file.is_file():
        logo_idx = input_index
        cmd += ["-i", str(logo_file)]
        input_index += 1
    if extra_text:
        extra_file = _render_extra_text_png(
            extra_text,
            work_dir / "extra_text_overlay.png",
            int(overlays.get("extra_text_font_size", 42)),
            0.18,
            max_width=text_width,
        )
        extra_idx = input_index
        cmd += ["-i", str(extra_file)]
        input_index += 1

    graph: list[str] = []
    current = "[0:v]"
    blur_strength = max(4, min(40, int(overlays.get("blur_strength", 18))))
    for i, b in enumerate(boxes):
        # Precise coordinates mapped directly onto the video picture
        x = ox + min(pw - 8, int(round(_clamp01(b.get("x")) * pw)))
        y = oy + min(ph - 8, int(round(_clamp01(b.get("y")) * ph)))
        w = int(round(_clamp01(b.get("w"), 0.1) * pw))
        h = int(round(_clamp01(b.get("h"), 0.1) * ph))
        w = max(8, min(w, (ox + pw) - x))
        h = max(8, min(h, (oy + ph) - y))
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1
        # Prevent boxblur crashes by strictly clamping luma radius < min(w, h) / 2
        safe_luma = max(2, min(blur_strength, (min(w, h) // 2) - 1))
        safe_chroma = max(1, min(safe_luma // 2, 9))
        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        graph.append(f"{current}split=2[{base}][{crop}]")
        graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={safe_luma}:luma_power=2:"
            f"chroma_radius={safe_chroma}:chroma_power=1[{blur}]"
        )
        graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        current = f"[{out}]"

    if title_idx is not None and title_file is not None:
        pos = overlays.get("title_position") or [0.5, 0.12]
        tx = _clamp01(pos[0] if len(pos) > 0 else 0.5, 0.5)
        ty = _clamp01(pos[1] if len(pos) > 1 else 0.12, 0.12)
        out = "vtitle"
        graph.append(f"{current}[{title_idx}:v]overlay=x='{ox}+({pw}*{tx:.6f}-w/2)':y='{oy}+({ph}*{ty:.6f}-h/2)':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if logo_idx is not None:
        pos = overlays.get("logo_position") or [0.80, 0.06]
        lx = _clamp01(pos[0] if len(pos) > 0 else 0.80, 0.80)
        ly = _clamp01(pos[1] if len(pos) > 1 else 0.06, 0.06)
        lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
        lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.65))))
        graph.append(f"[{logo_idx}:v]scale={max(24, int(round(pw * lw)))}:-1[lg0]")
        if lo < 0.999:
            graph.append(f"[lg0]format=rgba,colorchannelmixer=aa={lo:.3f}[lg]")
        else:
            graph.append(f"[lg0]copy[lg]")
        out = "vlogo"
        graph.append(f"{current}[lg]overlay=x='{ox}+({pw}*{lx:.6f})':y='{oy}+({ph}*{ly:.6f})':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if extra_idx is not None and extra_file is not None:
        pos = overlays.get("extra_text_position") or [0.50, 0.84]
        ex = _clamp01(pos[0] if len(pos) > 0 else 0.50, 0.50)
        ey = _clamp01(pos[1] if len(pos) > 1 else 0.84, 0.84)
        eo = max(0.10, min(1.00, float(overlays.get("extra_text_opacity", 0.65))))
        if eo < 0.999:
            graph.append(f"[{extra_idx}:v]format=rgba,colorchannelmixer=aa={eo:.3f}[etxt]")
        else:
            graph.append(f"[{extra_idx}:v]copy[etxt]")
        out = "vextra"
        graph.append(f"{current}[etxt]overlay=x='{ox}+({pw}*{ex:.6f}-w/2)':y='{oy}+({ph}*{ey:.6f}-h/2)':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if subtitles_path and subtitles_path.is_file():
        sub_esc = subtitles_path.resolve().as_posix().replace(":", r"\:")
        sub_font = str(overlays.get("subtitles_font", "Myanmar Text"))
        sub_size = int(overlays.get("subtitles_font_size", 16))
        # Distance from bottom of canvas to bottom of picture
        bottom_pad = ch - (oy + ph)
        if "sub_y" in overlays and overlays["sub_y"] is not None:
            sy = _clamp01(float(overlays["sub_y"]), 0.85)
            pixels_from_bottom = ph * (1.0 - sy) + bottom_pad
        elif "subtitles_margin_v" in overlays:
            mv_val = float(overlays["subtitles_margin_v"])
            pixels_from_bottom = mv_val if mv_val > 100 else (mv_val / 288.0) * ch
        elif boxes:
            b0 = boxes[0]
            by = _clamp01(b0.get("y", 0.81))
            bh = _clamp01(b0.get("h", 0.13))
            sub_center = max(0.02, min(0.92, 1.0 - (by + bh * 0.65)))
            pixels_from_bottom = ph * sub_center + bottom_pad
        else:
            pixels_from_bottom = max(14, int(round(ph * 0.045))) + bottom_pad

        # FFmpeg's subtitles filter (libass on SRT) uses a 288 reference scale for MarginV
        sub_margin = max(4, min(280, int(round((pixels_from_bottom / max(1, ch)) * 288))))
        st_key = str(overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow").lower()
        st = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
        p_col = st["primary"]
        o_col = st["outline_col"]
        b_col = st["back_col"]
        b_style = st["border_style"]
        outl = st["outline"]
        shad = st["shadow"]
        out = "vsub"
        graph.append(
            f"{current}subtitles='{sub_esc}':force_style='Fontname={sub_font},FontSize={sub_size},"
            f"PrimaryColour={p_col},OutlineColour={o_col},BackColour={b_col},BorderStyle={b_style},Outline={outl},Shadow={shad},MarginV={sub_margin},MarginL=16,MarginR=16,Alignment=2,WrapStyle=0'[{out}]"
        )
        current = f"[{out}]"

    graph.append(f"{current}format=yuv420p[vout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map_metadata", "-1", "-an"] + _encoder_args(encoder) + ["-movflags", "+faststart", str(output_video)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and encoder == "h264_nvenc":
        log("⚠️ Overlay NVENC failed — libx264 fallback")
        cmd2 = cmd[:]
        # Rebuild tail safely rather than trying to mutate codec options in-place.
        map_pos = cmd2.index("-map")
        prefix = cmd2[:map_pos] + ["-map", "[vout]", "-map_metadata", "-1", "-an"]
        cmd2 = prefix + _encoder_args("libx264") + ["-movflags", "+faststart", str(output_video)]
        p = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
        raise RuntimeError("Production overlay render failed: " + p.stderr[-3000:])
    return output_video


def extract_preview_with_overlays(
    ffmpeg: str,
    source_video: Path | str,
    output_image: Path | str,
    timestamp: float = 3.0,
    overlays: dict[str, Any] | None = None,
    output_ratio: str = "Original",
    frame_fill: str = "blur",
    mirror_video: bool = True,
    zoom_factor: float = 1.06,
    sample_text: str = "ဒီဇာတ်လမ်းမှာတော့ အဓိကဇာတ်ကောင်ဟာ မမျှော်လင့်ဘဲ...",
    work_dir: Path | str | None = None,
    ffprobe: str | None = None,
) -> Path:
    """Extracts a single representative frame with layout, blur box, and sample 1-line subtitle applied."""
    source = Path(source_video).resolve()
    out_img = Path(output_image).resolve()
    out_img.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(work_dir).resolve() if work_dir else out_img.parent
    temp_dir.mkdir(parents=True, exist_ok=True)

    ffprobe_bin = resolve_binary(ffprobe, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
    try:
        sw, sh = probe_video_size(ffprobe_bin, source)
    except Exception:
        sw, sh = 1920, 1080

    try:
        vid_dur = probe_duration(ffprobe_bin, source)
    except Exception:
        vid_dur = 10.0

    if vid_dur > 1.2:
        seek_sec = min(max(0.3, float(timestamp)), max(0.3, vid_dur - 0.4))
    else:
        seek_sec = max(0.05, vid_dur * 0.3)

    layout = frame_layout(output_ratio, frame_fill, sw, sh)
    cw, ch = int(layout["cw"]), int(layout["ch"])
    pw, ph = int(layout.get("pw", cw)), int(layout.get("ph", ch))
    ox, oy = int(layout.get("ox", 0)), int(layout.get("oy", 0))

    vf, _ = _visual_filters(mirror_video, zoom_factor, layout)
    overlays = dict(overlays or {})
    boxes = [x for x in (overlays.get("blur_boxes") or []) if isinstance(x, dict)]

    graph: list[str] = [f"[0:v]{vf}[base]"]
    current = "[base]"
    blur_strength = max(4, min(40, int(overlays.get("blur_strength", 18))))

    for i, b in enumerate(boxes):
        x = ox + min(pw - 8, int(round(_clamp01(b.get("x")) * pw)))
        y = oy + min(ph - 8, int(round(_clamp01(b.get("y")) * ph)))
        w = int(round(_clamp01(b.get("w"), 0.1) * pw))
        h = int(round(_clamp01(b.get("h"), 0.1) * ph))
        w = max(8, min(w, (ox + pw) - x))
        h = max(8, min(h, (oy + ph) - y))
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1
        safe_luma = max(2, min(blur_strength, (min(w, h) // 2) - 1))
        safe_chroma = max(1, min(safe_luma // 2, 9))

        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        graph.append(f"{current}split=2[{base}][{crop}]")
        graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={safe_luma}:luma_power=2:"
            f"chroma_radius={safe_chroma}:chroma_power=1[{blur}]"
        )
        graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        current = f"[{out}]"

    # Add Logo overlay if enabled
    logo_file = None
    if overlays.get("logo_enabled", False):
        lp = str(overlays.get("logo_path") or "").strip()
        if lp and Path(lp).is_file():
            logo_file = Path(lp)

    logo_idx: int | None = None
    cmd_inputs: list[str] = ["-ss", f"{seek_sec:.2f}", "-i", str(source)]
    if logo_file:
        logo_idx = 1
        cmd_inputs += ["-i", str(logo_file)]

    if logo_idx is not None:
        pos = overlays.get("logo_position") or [0.80, 0.05]
        px = _clamp01(pos[0]) * pw
        py = _clamp01(pos[1]) * ph
        lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
        lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.70))))
        graph.append(f"[{logo_idx}:v]scale={max(24, int(round(pw * lw)))}:-1[lg0]")
        if lo < 0.98:
            graph.append(f"[lg0]format=rgba,colorchannelmixer=aa={lo:.2f}[lg]")
            lg_in = "[lg]"
        else:
            lg_in = "[lg0]"
        out = "vlogo"
        graph.append(f"{current}{lg_in}overlay={int(round(ox + px))}:{int(round(oy + py))}[{out}]")
        current = f"[{out}]"

    # Add sample subtitle if burn_subtitles is enabled
    srt_temp = None
    if overlays.get("burn_subtitles", True):
        sub_lines = int(overlays.get("sub_lines", 1))
        sample = str(sample_text or "").strip()
        if not sample:
            if sub_lines == 2:
                sample = "မူရင်းဇာတ်လမ်းကို မြန်မာဘာသာဖြင့်\nအချိန်ကိုက် ပြန်လည်တင်ဆက်ထားပါသည်"
            else:
                sample = "မြန်မာဘာသာဖြင့် ပြန်လည်တင်ဆက်ထားပါသည်"
        elif sub_lines == 2 and "\n" not in sample and len(sample) > 20:
            syls = split_into_burmese_syllables(sample)
            if len(syls) > 1:
                half = len(sample) // 2
                best_idx = 1
                best_diff = 9999
                cand = []
                for i in range(1, len(syls)):
                    left_len = sum(len(s) for s in syls[:i])
                    diff = abs(left_len - half)
                    curr = syls[i - 1].strip()
                    if curr in PARTICLE_SYLS or any(curr.endswith(p) for p in ['ပြီး', 'တော့', 'က', 'နဲ့', 'မှာ', 'ကို', 'တယ်', 'တာ', '၊', ' ']):
                        cand.append((diff, i))
                if cand:
                    cand.sort(key=lambda x: x[0])
                    best_idx = cand[0][1]
                else:
                    for i in range(1, len(syls)):
                        left_len = sum(len(s) for s in syls[:i])
                        diff = abs(left_len - half)
                        if diff < best_diff:
                            best_diff = diff
                            best_idx = i
                l1 = "".join(syls[:best_idx]).strip()
                l2 = "".join(syls[best_idx:]).strip()
                if l1 and l2:
                    sample = f"{l1}\n{l2}"
        elif sub_lines == 1:
            sample = sample.replace("\n", " ").strip()

        srt_temp = temp_dir / "preview_sample.srt"
        srt_temp.write_text(f"1\n00:00:00,000 --> 00:00:10,000\n{sample}\n", encoding="utf-8")
        sub_esc = srt_temp.resolve().as_posix().replace(":", r"\:")
        sub_font = str(overlays.get("subtitles_font", "Myanmar Text"))
        sub_size = int(overlays.get("subtitles_font_size", 16))
        bottom_pad = ch - (oy + ph)

        if "sub_y" in overlays and overlays["sub_y"] is not None:
            sy = _clamp01(float(overlays["sub_y"]), 0.85)
            pixels_from_bottom = ph * (1.0 - sy) + bottom_pad
        elif "subtitles_margin_v" in overlays:
            mv_val = float(overlays["subtitles_margin_v"])
            pixels_from_bottom = mv_val if mv_val > 100 else (mv_val / 288.0) * ch
        elif boxes:
            b0 = boxes[0]
            by = _clamp01(b0.get("y", 0.81))
            bh = _clamp01(b0.get("h", 0.13))
            sub_center = max(0.02, min(0.92, 1.0 - (by + bh * 0.65)))
            pixels_from_bottom = ph * sub_center + bottom_pad
        else:
            pixels_from_bottom = max(14, int(round(ph * 0.045))) + bottom_pad

        # FFmpeg's subtitles filter (libass on SRT) uses a 288 reference scale for MarginV
        sub_margin = max(4, min(280, int(round((pixels_from_bottom / max(1, ch)) * 288))))

        st_key = str(overlays.get("sub_style") or overlays.get("subtitles_style") or "yellow").lower()
        st = SUBTITLE_STYLE_PRESETS.get(st_key, SUBTITLE_STYLE_PRESETS["yellow"])
        p_col = st["primary"]
        o_col = st["outline_col"]
        b_col = st["back_col"]
        b_style = st["border_style"]
        outl = st["outline"]
        shad = st["shadow"]
        out = "vsub"
        graph.append(
            f"{current}subtitles='{sub_esc}':force_style='Fontname={sub_font},FontSize={sub_size},"
            f"PrimaryColour={p_col},OutlineColour={o_col},BackColour={b_col},BorderStyle={b_style},Outline={outl},Shadow={shad},MarginV={sub_margin},MarginL=16,MarginR=16,Alignment=2,WrapStyle=0'[{out}]"
        )
        current = f"[{out}]"

    graph.append(f"{current}format=yuv420p[vout]")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
    ] + cmd_inputs + [
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-frames:v", "1",
        "-q:v", "2",
        "-strict", "-2",
        "-update", "1",
        str(out_img),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if srt_temp and srt_temp.is_file():
        srt_temp.unlink(missing_ok=True)
    if p.returncode != 0 or not out_img.is_file():
        raise RuntimeError("Preview extraction failed: " + p.stderr[-1500:])
    return out_img


@dataclass
class PipelinePaths:
    root: Path
    tts_parts: Path
    video_parts: Path
    work: Path
    checkpoint: Path
    voice: Path
    synced_video_noaudio: Path
    decorated_video_noaudio: Path
    plan: Path
    final: Path


def make_paths(job_dir: Path) -> PipelinePaths:
    root = job_dir / "edge_tts_smart_sync"
    tts = root / "tts_parts"
    video = root / "video_parts"
    work = root / "work"
    for p in (root, tts, video, work):
        p.mkdir(parents=True, exist_ok=True)
    return PipelinePaths(
        root=root,
        tts_parts=tts,
        video_parts=video,
        work=work,
        checkpoint=root / "checkpoint.json",
        voice=root / "voice_edge_tts.wav",
        synced_video_noaudio=work / "video_synced_noaudio.mp4",
        decorated_video_noaudio=work / "video_synced_decorated_noaudio.mp4",
        plan=root / "smart_sync_plan_edge_tts.json",
        final=root / "final_edge_tts_smart_sync.mp4",
    )


class EdgeSmartSync:
    def __init__(
        self,
        *,
        job_dir: str = "",
        source_path: str | None = None,
        plan_path: str | None = None,
        voice: str,
        edge_tts_rate: str = "+0%",
        edge_tts_pitch: str = "+0Hz",
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        max_video_speed: float = 1.25,
        max_video_slow: float = 1.35,
        mirror_video: bool = True,
        zoom_factor: float = 1.05,
        smooth_freeze_fallback: bool = False,
        max_freeze_hold: float = 0.75,
        render_final_video: bool = True,
        output_ratio: str = "16:9",
        frame_fill: str = "blur",
        overlays: dict[str, Any] | None = None,
        log: LogFn = _noop,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.source_path = Path(source_path).resolve() if source_path else (self.job_dir / "source.mp4")
        self.plan_path = Path(plan_path).resolve() if plan_path else (self.job_dir / "smart_sync_plan.json")
        self.voice = voice
        self.edge_tts_rate = str(edge_tts_rate or "+0%")
        self.edge_tts_pitch = str(edge_tts_pitch or "+0Hz")
        self.ffmpeg = resolve_binary(ffmpeg_path, "ffmpeg", [r"C:\ffmpeg\bin\ffmpeg.exe"])
        self.ffprobe = resolve_binary(ffprobe_path, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
        # Kept for backward config compatibility; exact Recap-style sync does
        # not clamp segment speed/slow when Auto Freeze is OFF.
        self.max_video_speed = max(1.0, float(max_video_speed))
        self.max_video_slow = max(1.0, float(max_video_slow))
        self.mirror_video = bool(mirror_video)
        self.zoom_factor = max(1.0, min(float(zoom_factor), 1.25))
        self.output_ratio = output_ratio if output_ratio in OUTPUT_RATIOS else "16:9"
        self.frame_fill = frame_fill if frame_fill in FRAME_FILLS else "blur"
        self.smooth_freeze_fallback = bool(smooth_freeze_fallback)
        self.max_freeze_hold = max(0.15, min(float(max_freeze_hold), 1.50))
        self.render_final_video = bool(render_final_video)
        # Short pauses: see trim_edge_silence. Set by render_worker from the UI box.
        self.short_pauses = False
        self.overlays = dict(overlays or {})
        self.log = log
        self.progress = progress or (lambda a, b, c: None)
        self.should_stop = should_stop or (lambda: False)

    def _checkpoint_load(self, paths: PipelinePaths) -> dict[str, Any]:
        if paths.checkpoint.is_file():
            try:
                data = load_json(paths.checkpoint)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"version": 1, "audio": {}, "video": {}}

    def _log_sync_check(self, paths: "PipelinePaths", segments: list[dict[str, Any]], checkpoint: dict[str, Any]) -> None:
        """Automatic, language-independent sync report written to the run log.

        Narration is the clock; every video part is frame-locked to it. So if the
        concatenated picture and the narration track have the same length, every
        segment starts on its own narration line. Speed-capped/trimmed segments are
        listed only for information: trimming shortens footage, not the timeline.
        """
        try:
            video_len = probe_duration(self.ffprobe, paths.synced_video_noaudio)
            audio_len = probe_duration(self.ffprobe, paths.voice)
            diff = video_len - audio_len
            trimmed = 0
            for seg in segments:
                sync = ((checkpoint.get("video") or {}).get(segment_basename(seg)) or {}).get("sync") or {}
                if float(sync.get("applied_factor", 0.0) or 0.0) > float(sync.get("desired_factor", 0.0) or 0.0) + 1e-6:
                    trimmed += 1
            status = "✅ SYNC OK" if abs(diff) <= 0.1 else "⚠️ SYNC DRIFT"
            self.log(
                f"{status}: picture={video_len:.2f}s · narration={audio_len:.2f}s · diff={diff:+.3f}s "
                f"| {len(segments)} segments, {trimmed} speed-capped ({self.max_video_speed:g}x) + footage trimmed"
            )
        except Exception as exc:
            self.log(f"⚠️ Sync check skipped: {exc}")

    def run(self) -> dict[str, Any]:
        source = self.source_path
        plan_file = self.plan_path
        if not source.is_file():
            raise FileNotFoundError(f"Source video မတွေ့ပါ: {source}")
        if not plan_file.is_file():
            raise FileNotFoundError(f"smart_sync_plan.json မတွေ့ပါ: {plan_file}")

        plan = load_json(plan_file)
        segments = normalize_segments(plan)
        paths = make_paths(self.job_dir)
        checkpoint = self._checkpoint_load(paths)
        checkpoint.setdefault("audio", {})
        checkpoint.setdefault("video", {})
        checkpoint["engine"] = "edge_tts"
        checkpoint["voice"] = self.voice

        self.log(f"📁 Job: {self.job_dir}")
        self.log(f"🧩 Timestamp segments: {len(segments)}")
        self.log(f"🎙️ TTS: Edge TTS | Voice: {self.voice}")
        self.log(f"🎞️ Smart Sync: Exact Recap timing — distributed freeze fallback={'ON' if self.smooth_freeze_fallback else 'OFF'}")
        try:
            source_w, source_h = probe_video_size(self.ffprobe, source)
        except Exception as exc:  # only the frame shape depends on it: Original falls back to 16:9
            source_w, source_h = 0, 0
            self.log(f"⚠️ Video size not readable ({exc}); Original ratio falls back to 16:9")
        layout = frame_layout(self.output_ratio, self.frame_fill, source_w, source_h)
        self.log(f"🪞 Mirror={'ON' if self.mirror_video else 'OFF'} | Ratio={layout['ratio']} "
                 f"{layout['cw']}x{layout['ch']} ({layout['fill']}) from {source_w}x{source_h} | Zoom={self.zoom_factor:.3f}x")

        # Method 2: Optimize default Edge TTS speed (+12% to eliminate dragging robotic tone)
        effective_rate = self.edge_tts_rate
        if not effective_rate or effective_rate in ("+0%", "0%", "+0Hz"):
            effective_rate = "+12%"
        effective_pitch = self.edge_tts_pitch or "+0Hz"

        engine = EdgeTTSEngine(
            ffmpeg=self.ffmpeg,
            work_dir=paths.work / "edge_tts_temp",
            rate=effective_rate,
            pitch=effective_pitch,
            log=self.log,
        )

        # Method 1: Check if VoxCPM Cloud Engine is requested
        use_voxcpm = (
            str(self.voice).lower().startswith(("voxcpm", "vox_"))
            or str(self.overlays.get("tts_engine") or "").lower() == "voxcpm"
        )
        vox_engine = None
        if use_voxcpm:
            try:
                from recap_engine.voxcpm_engine import VoxCPMEngine
                vox_engine = VoxCPMEngine(work_dir=paths.work / "voxcpm_temp", log_fn=self.log)
                self.log("🎙️ Initialized VoxCPM Cloud Neural Voice Engine (Zero-shot human audio)")
            except Exception as vox_init_err:
                self.log(f"⚠️ VoxCPM init error ({vox_init_err}) — will use Edge TTS")
                use_voxcpm = False

        base_edge_voice = self.voice
        if str(self.voice).startswith(("clone_", "clone:")):
            try:
                from recap_engine.voice_clone_bridge import resolve_clone_model
                minfo = resolve_clone_model(self.voice)
                base_edge_voice = minfo.get("edge_base", "my-MM-ThihaNeural") if minfo else "my-MM-ThihaNeural"
            except Exception:
                base_edge_voice = "my-MM-ThihaNeural"

        engine_provider = "voxcpm" if use_voxcpm else "edge_tts"
        fingerprint_voice = (
            f"{engine_provider}|{base_edge_voice}|rate={effective_rate}|pitch={effective_pitch}"
        )
        checkpoint["tts_provider"] = engine_provider

        timing: list[dict[str, Any]] = []
        audio_parts: list[Path] = []
        cursor = 0.0
        short_pauses = bool(self.short_pauses)
        tts_total = 0.0

        # AUDIO: timestamp segment = master save unit. The user's engine may split
        # sentences internally, but only one deterministic WAV is kept per timestamp segment.
        for idx, seg in enumerate(segments, 1):
            if self.should_stop():
                raise InterruptedError("STOP requested — လက်ရှိပြီးထားတာတွေ checkpoint ထဲသိမ်းထားပါတယ်။")
            base = segment_basename(seg)
            dst = paths.tts_parts / f"{base}.wav"
            fp = segment_fingerprint(seg, fingerprint_voice)
            rec = checkpoint["audio"].get(base, {})
            reusable = rec.get("fingerprint") == fp and dst.is_file() and dst.stat().st_size > 1000
            if reusable:
                duration = probe_duration(self.ffprobe, dst)
                self.log(f"♻️ [{idx}/{len(segments)}] {base}.wav reuse ({duration:.2f}s)")
            else:
                speak = spoken_text(seg["narration"])
                produced = None

                # 1. Try VoxCPM if selected
                if use_voxcpm and vox_engine:
                    self.progress(idx - 1, len(segments), f"VoxCPM Cloud {idx}/{len(segments)}")
                    self.log(f"🎙️ [{idx}/{len(segments)}] {base} generate via VoxCPM Cloud")
                    try:
                        ref_wav = self.overlays.get("voxcpm_ref_audio")
                        ctrl_prompt = (
                            self.overlays.get("voxcpm_control")
                            or "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone and fluent pronunciation"
                        )
                        produced = vox_engine.synthesize(
                            text=speak,
                            output_wav=dst,
                            control_instruction=ctrl_prompt,
                            reference_wav=ref_wav,
                        )
                    except Exception as vox_exc:
                        self.log(f"⚠️ VoxCPM failed ({vox_exc}) — falling back to Edge TTS for line {idx}")
                        produced = None

                # 2. Edge TTS (either default or fallback)
                if not produced or not Path(produced).is_file():
                    self.progress(idx - 1, len(segments), f"Edge TTS {idx}/{len(segments)}")
                    self.log(f"🗣️ [{idx}/{len(segments)}] {base} generate via Edge TTS")
                    try:
                        try:
                            produced = engine.generate_channel99_voice(speak, base_edge_voice)
                        except Exception as first:
                            plain = without_english(speak)
                            if is_edge_refused(first) or plain == speak or not any(ch.isalpha() for ch in plain):
                                raise
                            # A line mixing English and Burmese sometimes gets no audio at all
                            self.log(f"⚠️ [{idx}/{len(segments)}] no audio — trying again without the English words "
                                     "(Edit Text can rewrite the line)")
                            produced = engine.generate_channel99_voice(plain, base_edge_voice)
                        if not produced or not Path(produced).is_file():
                            raise RuntimeError("TTS output path မရပါ။")
                    except Exception as exc:
                        if is_edge_refused(exc):
                            raise RuntimeError(f"Edge TTS stopped at line {idx}/{len(segments)}.\n\n{exc}") from exc
                        raise RuntimeError(f"TTS FAILED at segment {idx} ({base}): {exc}\n\n"
                                           f"Text: {seg['narration'][:200]}") from exc
                produced_path = Path(produced).resolve()
                shutil.copy2(produced_path, dst)
                produced_path.unlink(missing_ok=True)
                duration = probe_duration(self.ffprobe, dst)
                if duration <= 0.05:
                    raise RuntimeError(f"Generated audio duration invalid: {dst}")
                checkpoint["audio"][base] = {
                    "fingerprint": fp,
                    "file": str(dst),
                    "duration": round(duration, 3),
                    "video_start": seg["video_start"],
                    "video_end": seg["video_end"],
                    "text": seg["narration"],
                }
                atomic_write_json(paths.checkpoint, checkpoint)
                self.log(f"✅ {base}.wav = {duration:.2f}s")

            part = dst
            tts_total += duration
            if short_pauses:
                window = float(seg["video_end"]) - float(seg["video_start"])
                part, duration = trim_edge_silence(dst, paths.tts_parts.parent / "tts_parts_short" / dst.name,
                                                   min_duration=window / self.max_video_speed)
            start = cursor
            end = cursor + duration
            timing.append({
                "segment_id": seg["segment_id"],
                "source_index": seg["source_index"],
                "video_start": seg["video_start"],
                "video_end": seg["video_end"],
                "audio_start": round(start, 3),
                "audio_end": round(end, 3),
                "audio_duration": round(duration, 3),
                "text": seg["narration"],
                "audio_file": str(part),
            })
            cursor = end
            audio_parts.append(part)
            self.progress(idx, len(segments), f"Audio {idx}/{len(segments)}")

        if short_pauses:
            self.log(f"✂️ Short pauses: Edge TTS silence trimmed · voice {tts_total / 60:.1f} → {cursor / 60:.1f} min "
                     f"(~0.25 s between lines; picture never cut for it)")
        self.log("🔗 Timestamp audio parts concat...")
        concat_audio(self.ffmpeg, audio_parts, paths.voice, paths.work)
        final_audio_duration = probe_duration(self.ffprobe, paths.voice)
        self.log(f"✅ {paths.voice.name} = {final_audio_duration:.2f}s")

        # Voice Clone Pro (Applio RVC) voice conversion
        if str(self.voice).startswith(("clone_", "clone:")) or self.overlays.get("clone_model"):
            clone_tag = str(self.overlays.get("clone_model") or self.voice)
            cloned_audio_path = paths.root / "recap_voice_cloned.wav"
            try:
                from recap_engine.voice_clone_bridge import convert_voice_rvc
                self.log(f"🎭 Voice Clone Pro (Applio RVC) — Converting audio with model '{clone_tag}'...")
                self.progress(len(segments), len(segments), f"🎭 RVC Voice Cloning: {clone_tag}")
                cloned_res = convert_voice_rvc(
                    input_wav=paths.voice,
                    output_wav=cloned_audio_path,
                    model_tag=clone_tag,
                    pitch=int(self.overlays.get("clone_pitch", 0)),
                    f0_method=str(self.overlays.get("clone_f0_method", "rmvpe")),
                    log_fn=self.log,
                )
                if cloned_res.is_file() and cloned_res.stat().st_size > 1000:
                    paths.voice = cloned_res
                    final_audio_duration = probe_duration(self.ffprobe, paths.voice)
                    self.log(f"✅ Voice Clone Pro RVC success! Using cloned audio: {paths.voice.name}")
            except Exception as vc_err:
                self.log(f"⚠️ Voice Clone Pro conversion error (fallback to Edge TTS): {vc_err}")

        sync_segments = [
            {
                "video_start": item["video_start"],
                "video_end": item["video_end"],
                "audio_start": item["audio_start"],
                "audio_end": item["audio_end"],
            }
            for item in timing
        ]
        out_plan = dict(plan)
        out_plan["segments"] = [dict(seg) for seg in plan.get("segments", [])]
        out_plan["tts_provider"] = "edge_tts"
        out_plan["tts_voice"] = self.voice
        out_plan["actual_audio_duration"] = round(final_audio_duration, 3)
        out_plan["tts_timing_segments"] = timing
        out_plan["sync_segments"] = sync_segments
        out_plan["subtitle_segments"] = [
            {"start": item["audio_start"], "end": item["audio_end"], "text": item["text"]}
            for item in timing
        ]
        out_plan["production_overlays"] = self.overlays
        atomic_write_json(paths.plan, out_plan)

        # Generate synchronized Myanmar SRT subtitle file (split into 1-2 line balanced cues)
        srt_file = paths.root / "recap_myanmar_synced.srt"
        try:
            srt_lines = []
            cue_counter = 1
            for s_item in timing:
                raw_st = float(s_item.get("audio_start", 0.0))
                raw_en = float(s_item.get("audio_end", 0.0))
                txt = str(s_item.get("text", "")).strip()
                if not txt:
                    continue
                lines_per_cue = int(self.overlays.get("sub_lines", 1))
                cw_val = int(layout.get("cw", 1920))
                ch_val = int(layout.get("ch", 1080))
                is_vert = (ch_val > cw_val) or (self.output_ratio == "9:16")
                if lines_per_cue == 1:
                    max_chars = 28 if is_vert else 36
                else:
                    max_chars = 54 if is_vert else 72
                sub_cues = split_burmese_subtitle_cues(
                    raw_st, raw_en, txt,
                    max_cue_chars=max_chars,
                    lines_per_cue=lines_per_cue
                )
                if not sub_cues:
                    sub_cues = [(raw_st, raw_en, txt)]
                for c_st, c_en, c_txt in sub_cues:
                    st_ms = max(0, int(round(c_st * 1000)))
                    en_ms = max(0, int(round(c_en * 1000)))
                    st_str = f"{st_ms // 3600000:02d}:{(st_ms % 3600000) // 60000:02d}:{(st_ms % 60000) // 1000:02d},{st_ms % 1000:03d}"
                    en_str = f"{en_ms // 3600000:02d}:{(en_ms % 3600000) // 60000:02d}:{(en_ms % 60000) // 1000:02d},{en_ms % 1000:03d}"
                    srt_lines.append(f"{cue_counter}\n{st_str} --> {en_str}\n{c_txt}\n")
                    cue_counter += 1
            srt_file.write_text("\n".join(srt_lines) + "\n", encoding="utf-8")
            self.log(f"📝 Synced Myanmar SRT saved: {srt_file.name} ({cue_counter - 1} cues)")
        except Exception as srt_err:
            self.log(f"⚠️ Failed to write synced SRT: {srt_err}")

        if self.overlays.get("burn_subtitles") and srt_file.is_file():
            self.overlays["subtitles_file"] = str(srt_file)

        if not self.render_final_video:
            self.log("✅ Audio + Smart Sync plan READY (final video render skipped).")
            return {
                "audio": str(paths.voice),
                "plan": str(paths.plan),
                "final": None,
                "segments": len(timing),
            }

        self.log("🎬 Smart Sync video parts render စမယ်...")
        encoders = ffmpeg_encoders(self.ffmpeg)
        preferred = "h264_nvenc" if "h264_nvenc" in encoders else "libx264"
        remembered = str(checkpoint.get("selected_encoder") or "")
        encoder = remembered if remembered in {"h264_nvenc", "libx264"} else preferred
        video_parts: list[Path] = []
        # Cumulative frame lock: each part gets round(audio_end*30) - frames_so_far
        # frames, so video never drifts more than half a frame from the narration.
        audio_cursor = 0.0
        frames_done = 0

        for idx, (seg, item) in enumerate(zip(segments, timing), 1):
            if self.should_stop():
                raise InterruptedError("STOP requested — video parts checkpoint ထဲကနေ Resume လုပ်နိုင်ပါတယ်။")
            base = segment_basename(seg)
            dst = paths.video_parts / f"{base}.mp4"
            target = float(item["audio_duration"])
            audio_cursor += target
            frame_count = max(1, int(round(audio_cursor * 30)) - frames_done)
            frames_done += frame_count
            vfp_raw = json.dumps({
                "frame_count": frame_count,
                "source": str(source.resolve()),
                "source_mtime": source.stat().st_mtime_ns,
                "start": seg["video_start"], "end": seg["video_end"],
                "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
            }, sort_keys=True).encode()
            vfp = hashlib.sha256(vfp_raw).hexdigest()
            rec = checkpoint["video"].get(base, {})
            reusable = rec.get("fingerprint") == vfp and dst.is_file() and dst.stat().st_size > 5000
            if reusable:
                self.log(f"♻️ VIDEO [{idx}/{len(segments)}] {base}.mp4 reuse")
            else:
                self.progress(idx - 1, len(segments), f"Smart Sync video {idx}/{len(segments)}")
                self.log(f"🎞️ VIDEO [{idx}/{len(segments)}] {base} → {target:.2f}s")
                try:
                    metrics = render_video_part(
                        self.ffmpeg, source, dst, seg, target, encoder,
                        self.mirror_video, self.zoom_factor,
                        self.max_video_speed, self.max_video_slow,
                        self.smooth_freeze_fallback, self.max_freeze_hold,
                        frame_count=frame_count, layout=layout,
                    )
                except Exception:
                    # Only on the first non-reused part, allow global NVENC -> CPU fallback.
                    if encoder == "h264_nvenc" and not video_parts:
                        self.log("⚠️ NVENC မရပါ — libx264 CPU encoder ကို fallback လုပ်မယ်။")
                        encoder = "libx264"
                        # Encoder changed, invalidate any video checkpoint rows to avoid mixed codecs.
                        checkpoint["video"] = {}
                        for old in paths.video_parts.glob("*.mp4"):
                            old.unlink(missing_ok=True)
                        metrics = render_video_part(
                            self.ffmpeg, source, dst, seg, target, encoder,
                            self.mirror_video, self.zoom_factor,
                            self.max_video_speed, self.max_video_slow,
                            self.smooth_freeze_fallback, self.max_freeze_hold,
                            frame_count=frame_count, layout=layout,
                        )
                        # Recompute fingerprint with CPU encoder.
                        vfp_raw = json.dumps({
                            "frame_count": frame_count,
                            "source": str(source.resolve()),
                            "source_mtime": source.stat().st_mtime_ns,
                            "start": seg["video_start"], "end": seg["video_end"],
                            "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                            "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                            "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                            "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                            "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
                        }, sort_keys=True).encode()
                        vfp = hashlib.sha256(vfp_raw).hexdigest()
                    else:
                        raise
                if metrics.get("hybrid_used", 0.0) >= 0.5:
                    self.log(
                        f"   ↳ Hybrid: slow {metrics['applied_factor']:.3f}x + "
                        f"{int(metrics.get('freeze_count', 0))} short holds "
                        f"({metrics.get('freeze_duration', 0.0):.2f}s total)"
                    )
                checkpoint["video"][base] = {
                    "fingerprint": vfp,
                    "file": str(dst),
                    "target_duration": round(target, 3),
                    "encoder": encoder,
                    "sync": {k: round(v, 6) for k, v in metrics.items()},
                }
                checkpoint["selected_encoder"] = encoder
                atomic_write_json(paths.checkpoint, checkpoint)
            video_parts.append(dst)
            self.progress(idx, len(segments), f"Video {idx}/{len(segments)}")

        self.log("🔗 Smart Sync video parts concat...")
        concat_video_parts(self.ffmpeg, video_parts, paths.synced_video_noaudio, paths.work)
        self._log_sync_check(paths, segments, checkpoint)
        video_for_mux = paths.synced_video_noaudio
        if (
            self.overlays.get("blur_boxes")
            or str(self.overlays.get("title_text") or "").strip()
            or str(self.overlays.get("logo_path") or "").strip()
            or str(self.overlays.get("extra_text") or "").strip()
            or str(self.overlays.get("subtitles_file") or "").strip()
        ):
            self.log("🎨 Production overlay pass — Blur / Title PNG / Logo / Subtitles...")
            video_for_mux = apply_production_overlays(
                self.ffmpeg, paths.synced_video_noaudio, paths.decorated_video_noaudio,
                self.overlays, encoder, paths.work, self.log, frame_size=(layout["cw"], layout["ch"]),
                layout=layout,
            )
        self.log("🎧 Final audio mux (with limiter & mastering chain)...")
        volume_percent = int(getattr(self, "voice_volume_percent", 100) or 100)
        if volume_percent != 100:
            self.log(f"🔊 Voice volume {volume_percent}% applied at final mux (limiter on)")
        bgm_file = self.overlays.get("bgm_path")
        bgm_vol = float(self.overlays.get("bgm_volume", 0.18))
        if bgm_file and Path(bgm_file).is_file():
            self.log(f"🎵 Studio BGM sidechain ducking applied ({Path(bgm_file).name}, vol={bgm_vol:.2f})")
        mux_audio(
            self.ffmpeg,
            video_for_mux,
            paths.voice,
            paths.final,
            volume_percent,
            bgm_path=Path(bgm_file) if bgm_file else None,
            bgm_volume=bgm_vol,
        )
        final_duration = probe_duration(self.ffprobe, paths.final)

        # Copy synced SRT next to final video
        final_srt = paths.final.with_suffix(".srt")
        if srt_file.is_file():
            try:
                shutil.copy2(srt_file, final_srt)
            except Exception:
                pass

        # Export standalone MP3 of the voiceover audio for separate delivery
        final_mp3 = paths.final.with_suffix(".mp3")
        try:
            cmd_mp3 = [
                self.ffmpeg, "-y", "-i", str(paths.voice),
                "-vn", "-af", "highpass=f=60,lowpass=f=12000,alimiter=limit=0.95:level=disabled",
                "-c:a", "libmp3lame", "-b:a", "192k",
                "-map_metadata", "-1",
                str(final_mp3)
            ]
            subprocess.run(cmd_mp3, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e_mp3:
            self.log(f"⚠️ Standalone MP3 export warning: {e_mp3}")
            final_mp3 = paths.voice

        self.log(f"✅ FINAL READY: {paths.final}")
        self.log(f"⏱️ Final duration: {final_duration:.2f}s")
        return {
            "audio": str(final_mp3 if (isinstance(final_mp3, Path) and final_mp3.is_file()) else paths.voice),
            "voice_wav": str(paths.voice),
            "plan": str(paths.plan),
            "final": str(paths.final),
            "subtitles_srt": str(final_srt) if final_srt.is_file() else (str(srt_file) if srt_file.is_file() else None),
            "segments": len(timing),
            "duration": final_duration,
        }
