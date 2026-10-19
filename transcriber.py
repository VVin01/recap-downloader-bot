"""
transcriber.py - AI Audio/Video Transcription to SRT Subtitles
Uses faster-whisper (CTranslate2) for fast, lightweight, and offline transcription.
"""

import os
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_whisper_model = None
_current_model_size = None


def format_srt_timestamp(seconds: float) -> str:
    """Convert float seconds to standard SRT timestamp format: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    secs, ms = divmod(millis, 1000)
    mins, s = divmod(secs, 60)
    hrs, m = divmod(mins, 60)
    return f"{hrs:02d}:{m:02d}:{s:02d},{ms:03d}"


def get_whisper_model(model_size: Optional[str] = None):
    """
    Lazy-load and cache the WhisperModel singleton.
    Default model is 'base' (can be overridden by WHISPER_MODEL in .env).
    """
    global _whisper_model, _current_model_size

    target_size = model_size or os.getenv("WHISPER_MODEL", "base").strip()

    if _whisper_model is None or _current_model_size != target_size:
        logger.info(f"Loading faster-whisper model '{target_size}' on CPU (int8)...")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(
            target_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=os.cpu_count() or 4
        )
        _current_model_size = target_size
        logger.info(f"faster-whisper model '{target_size}' loaded successfully.")

    return _whisper_model


def transcribe_to_srt(
    audio_file_path: str,
    output_srt_path: Optional[str] = None,
    model_size: Optional[str] = None,
    language: Optional[str] = None
) -> Tuple[str, str, float]:
    """
    Transcribes an audio or video file and saves as a standard .srt subtitle file.
    
    Returns:
        Tuple[output_srt_path, detected_language, language_probability]
    """
    if not os.path.exists(audio_file_path):
        raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

    if not output_srt_path:
        base, _ = os.path.splitext(audio_file_path)
        output_srt_path = f"{base}_ai.srt"

    model = get_whisper_model(model_size)

    beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "2"))

    # Transcribe audio with speech-to-text
    # beam_size=2 balances speed and quality on CPU
    segments, info = model.transcribe(
        audio_file_path,
        language=language if language and language != "auto" else None,
        beam_size=beam_size,
        vad_filter=True, # Filter out silent portions
        vad_parameters=dict(min_silence_duration_ms=500)
    )

    detected_lang = info.language or "unknown"
    prob = info.language_probability or 0.0
    logger.info(f"Transcribing audio. Detected language: {detected_lang} (prob: {prob:.2f})")

    srt_entries = []
    index = 1

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        start_ts = format_srt_timestamp(segment.start)
        end_ts = format_srt_timestamp(segment.end)
        srt_entries.append(f"{index}\n{start_ts} --> {end_ts}\n{text}\n")
        index += 1

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_srt_path)), exist_ok=True)

    with open(output_srt_path, "w", encoding="utf-8") as f:
        if srt_entries:
            f.write("\n".join(srt_entries) + "\n")
        else:
            # Fallback if no speech detected
            f.write("1\n00:00:00,000 --> 00:00:05,000\n[No audible dialogue detected]\n")

    return output_srt_path, detected_lang, prob
