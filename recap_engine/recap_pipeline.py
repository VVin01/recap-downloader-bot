"""
recap_pipeline.py - High-level execution pipeline for Recap English -> Myanmar
Combines RecapPlanBuilder (Whisper + Gemini Story Memory + Translation)
and EdgeSmartSync (Edge TTS Myanmar + Smart Sync video setpts + Mux).
"""

from __future__ import annotations

import os
import sys
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recap_planner import RecapPlanBuilder
from core import EdgeSmartSync, probe_duration, extract_preview_with_overlays

logger = logging.getLogger(__name__)


def find_binary(name: str) -> str:
    """Find ffmpeg or ffprobe executable on the system."""
    for cand in (
        shutil.which(name),
        rf"C:\ffmpeg\bin\{name}.exe",
        str(HERE.parents[1] / "ffmpeg" / f"{name}.exe"),
        str(HERE.parents[1] / "ffmpeg" / "bin" / f"{name}.exe"),
    ):
        if cand and Path(cand).is_file():
            return str(Path(cand).resolve())
    return name


def run_recap_pipeline(
    source_video_path: str | Path,
    job_dir: Optional[str | Path] = None,
    voice: str = "my-MM-ThihaNeural",
    narrative_style: str = "narrator",
    voice_speed: str = "+12%",
    tts_engine: str = "edge_tts",
    bgm_path: Optional[str] = None,
    bgm_volume: float = 0.18,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    env_path: Optional[str | Path] = None,
    output_ratio: str = "Original",
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
) -> Dict[str, Any]:
    """
    Executes the full Recap English -> Myanmar workflow:
    1. Whisper Word-level Transcription
    2. Whole-sentence segmentation
    3. Gemini Story Context Memory
    4. Gemini English -> Myanmar translation + audit
    5. Microsoft Edge TTS Myanmar narration (Thiha or Nilar)
    6. Smart Sync re-timing (30 fps frame-lock, max 1.25x speed, distributed freeze)
    7. Anti-Copyright transformations (Mirror horizontal, Zoom crop, Ratio)
    8. Original subtitle & watermark blur boxes
    9. Custom branding (Title badge, Logo) & Synced Myanmar subtitles burn
    10. Final Muxing to MP4

    Returns dictionary with final_video path, audio path, subtitles_srt, plan, and metrics.
    """
    source = Path(source_video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source video not found: {source}")

    def _log(msg: str) -> None:
        logger.info(str(msg))
        if log_fn:
            try:
                log_fn(str(msg))
            except Exception:
                pass

    def _progress(current: int, total: int, label: str) -> None:
        if progress_fn:
            try:
                progress_fn(current, total, label)
            except Exception:
                pass

    stop_check = should_stop or (lambda: False)

    ffmpeg_bin = find_binary("ffmpeg")
    ffprobe_bin = find_binary("ffprobe")

    root_dir = HERE.parent
    if not job_dir:
        import hashlib
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in source.stem)[:40] or "recap_job"
        sig = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        target_job_dir = root_dir / "downloads" / f"recap_{safe_name}_{sig}"
    else:
        target_job_dir = Path(job_dir).resolve()

    target_job_dir.mkdir(parents=True, exist_ok=True)
    _log(f"🎬 Starting Recap English → Myanmar: {source.name}")
    _log(f"📁 Job Workspace: {target_job_dir}")
    _log(f"🎙️ Voice: {voice}")

    gemini_env = Path(env_path) if env_path else root_dir / ".env"

    # Step 1: PLANNER (Whisper -> Gemini Context -> Translation -> Smart Sync Plan)
    _progress(1, 4, "🎙️ [1/4] Whisper English Audio Transcription...")
    planner = RecapPlanBuilder(
        runtime_root=HERE,
        gemini_env_path=gemini_env if gemini_env.is_file() else None,
        ffprobe_path=ffprobe_bin,
        log=_log,
        progress=lambda cur, tot, lbl: _progress(1 + int(cur * 2 / max(1, tot)), 4, lbl),
        should_stop=stop_check,
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite"),
        gemini_request_timeout_seconds=120,
        narrative_style=narrative_style,
    )

    plan_path, plan = planner.build(
        source=source,
        job_dir=target_job_dir,
        source_title=source.stem,
        reuse=True,
    )

    if stop_check():
        raise InterruptedError("Recap stopped after planning stage.")

    # Build production overlays (Blur boxes, Title badge, Logo, Synced subtitles)
    overlays: dict[str, Any] = {}
    blur_boxes = []
    if blur_subtitles:
        # User customized or preset bottom subtitle area
        blur_boxes.append({"x": float(blur_x), "y": float(blur_y), "w": float(blur_w), "h": float(blur_h)})
    if blur_watermark:
        # Standard top-right watermark
        blur_boxes.append({"x": 0.74, "y": 0.03, "w": 0.24, "h": 0.12})
    if blur_boxes:
        overlays["blur_boxes"] = blur_boxes
        overlays["blur_strength"] = 18

    if title_text and str(title_text).strip():
        overlays["title_text"] = str(title_text).strip()
        overlays["title_position"] = [0.5, 0.12]

    if logo_path and Path(logo_path).is_file():
        overlays["logo_path"] = str(logo_path)
        overlays["logo_enabled"] = True
        overlays["logo_position"] = logo_position or [0.82, 0.06]
        overlays["logo_width_fraction"] = float(logo_scale or 0.16)
        overlays["logo_opacity"] = float(logo_opacity or 0.70)

    if extra_text and str(extra_text).strip():
        overlays["extra_text"] = str(extra_text).strip()
        overlays["extra_text_position"] = [0.5, 0.84]

    overlays["burn_subtitles"] = bool(burn_subtitles)
    if sub_y is not None:
        overlays["sub_y"] = float(sub_y)
    overlays["sub_lines"] = int(sub_lines)
    overlays["subtitles_font_size"] = int(sub_font_size)
    overlays["sub_style"] = str(sub_style or "yellow")
    overlays["tts_engine"] = str(tts_engine or "edge_tts")
    if bgm_path:
        overlays["bgm_path"] = str(bgm_path)
        overlays["bgm_volume"] = float(bgm_volume or 0.18)

    # Step 2: RENDER (Edge TTS + Smart Sync Video)
    _progress(3, 4, "🗣️ [3/4] Microsoft Edge TTS & Smart Sync Render...")
    runner = EdgeSmartSync(
        job_dir=target_job_dir,
        source_path=source,
        plan_path=plan_path,
        voice=voice,
        edge_tts_rate=voice_speed,
        edge_tts_pitch="+0Hz",
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        max_video_speed=1.25,
        max_video_slow=1.35,
        mirror_video=bool(mirror_video),
        zoom_factor=float(zoom_factor),
        smooth_freeze_fallback=False,
        max_freeze_hold=0.75,
        render_final_video=True,
        output_ratio=output_ratio,
        frame_fill="blur",
        overlays=overlays,
        log=_log,
        progress=lambda cur, tot, lbl: _progress(3, 4, f"Smart Sync: {lbl}"),
        should_stop=stop_check,
    )
    runner.voice_volume_percent = 100
    runner.short_pauses = True

    result = runner.run()
    _progress(4, 4, "✅ Final Recap Video Ready!")
    _log(f"🎉 Final Video: {result.get('final')}")

    return {
        "final_video": result.get("final"),
        "audio": result.get("audio"),
        "subtitles_srt": result.get("subtitles_srt"),
        "plan_path": str(plan_path),
        "job_dir": str(target_job_dir),
        "segments": result.get("segments", len(plan.get("segments", []))),
        "duration": result.get("duration", 0.0),
    }


def generate_recap_preview(
    source_video_path: str | Path,
    output_image_path: Optional[str | Path] = None,
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
    at_seconds: float = 3.5,
    logo_enabled: bool = False,
    logo_path: Optional[str] = None,
    logo_position: Optional[list[float]] = None,
    logo_scale: float = 0.16,
    logo_opacity: float = 0.70,
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
) -> str:
    """Generate a single preview frame with mirror/zoom, blur box, logo, and sample subtitle applied."""
    src = Path(source_video_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source video not found: {src}")

    ffmpeg_bin = ffmpeg_path or find_binary("ffmpeg")
    ffprobe_bin = ffprobe_path or find_binary("ffprobe")

    if not output_image_path:
        output_image_path = src.parent / f"preview_{src.stem}.jpg"
    out_path = Path(output_image_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    overlays: dict[str, Any] = {}
    blur_boxes = []
    if blur_subtitles:
        blur_boxes.append({"x": float(blur_x), "y": float(blur_y), "w": float(blur_w), "h": float(blur_h)})
    if blur_boxes:
        overlays["blur_boxes"] = blur_boxes
        overlays["blur_strength"] = 18
    overlays["burn_subtitles"] = bool(burn_subtitles)
    if sub_y is not None:
        overlays["sub_y"] = float(sub_y)
    overlays["sub_lines"] = int(sub_lines)
    overlays["subtitles_font_size"] = int(sub_font_size)
    overlays["sub_style"] = str(sub_style or "yellow")

    if logo_enabled and logo_path:
        overlays["logo_enabled"] = True
        overlays["logo_path"] = str(logo_path)
        overlays["logo_position"] = logo_position or [0.80, 0.05]
        overlays["logo_width_fraction"] = float(logo_scale or 0.16)
        overlays["logo_opacity"] = float(logo_opacity or 0.70)

    res = extract_preview_with_overlays(
        ffmpeg=ffmpeg_bin,
        source_video=src,
        output_image=out_path,
        timestamp=at_seconds,
        overlays=overlays,
        output_ratio=output_ratio,
        mirror_video=mirror_video,
        zoom_factor=zoom_factor,
        sample_text=sample_text,
        ffprobe=ffprobe_bin,
    )
    return str(res)

