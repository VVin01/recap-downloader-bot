"""
voxcpm_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via VoxCPM.
Connects seamlessly to OpenBMB's VoxCPM-Demo on Hugging Face Spaces for free, human-like voice synthesis.
Supports real-time queue ETA & position tracking.

API Reference (verified 2026-10-20):
  endpoint: /generate
  params:   text_input, control_instruction, reference_wav_path_input,
            use_prompt_text, prompt_text_input, cfg_value_input, do_normalize, denoise
  returns:  filepath (local path to generated audio)
"""

from __future__ import annotations

import os
import time
import shutil
import hashlib
import logging
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

DEFAULT_VOXCPM_SPACE = "openbmb/VoxCPM-Demo"
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone "
    "and fluent pronunciation. Speak in Myanmar language with authentic accent."
)


def _resolve_result_path(result) -> Optional[str]:
    """
    Safely extract the local file path from a gradio_client result.
    gradio_client may return: str, dict with 'path' key, tuple, or None.
    """
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("path") or result.get("url")
    if isinstance(result, (list, tuple)) and len(result) > 0:
        first = result[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("path")
    path_attr = getattr(result, "path", None)
    if path_attr:
        return str(path_attr)
    return None


class VoxCPMEngine:
    """
    High-level client for VoxCPM speech synthesis and voice cloning.
    Runs free inference via Hugging Face Cloud Spaces with live Queue ETA monitoring.
    """
    _client_instance = None
    _last_init_time = 0

    def __init__(
        self,
        space_name: str = DEFAULT_VOXCPM_SPACE,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.space_name = space_name
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", ".")) / "voxcpm_cache"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_fn = log_fn

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                pass

    @classmethod
    def get_client(cls, space_name: str = DEFAULT_VOXCPM_SPACE):
        """Get or initialize cached Gradio client."""
        now = time.time()
        if cls._client_instance is None or (now - cls._last_init_time > 1800):
            try:
                from gradio_client import Client
                logger.info(f"Connecting to VoxCPM Space: {space_name}...")
                cls._client_instance = Client(space_name)
                cls._last_init_time = now
            except Exception as e:
                logger.error(f"Failed to connect to VoxCPM Space: {e}")
                cls._client_instance = None
                raise
        return cls._client_instance

    def synthesize(
        self,
        text: str,
        output_wav: Path | str,
        control_instruction: str = DEFAULT_BURMESE_CONTROL,
        reference_wav: Optional[Path | str] = None,
        prompt_text: str = "",
        cfg_value: float = 2.0,
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 2,
    ) -> Path:
        """
        Synthesize Burmese speech for given text using VoxCPM.
        Tracks real-time queue ETA and updates progress callback.

        Args:
            text: Input text to synthesize (Burmese or multilingual)
            output_wav: Destination path for output WAV file
            control_instruction: Natural language style description
            reference_wav: Optional reference audio for voice cloning
            prompt_text: Optional transcript of the reference audio (improves accuracy)
            cfg_value: Classifier-free guidance scale (1.0-3.0, default 2.0)
            progress_cb: Callback for live status updates (queue ETA, position)
            retries: Number of retry attempts on failure
        Returns:
            Path to the output WAV file
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("VoxCPM input text is empty.")

        # Check local cache (skip caching when reference_wav is used — unique per voice)
        cache_key = None
        cached_file = None
        if not reference_wav:
            cache_key = hashlib.sha256(
                f"{clean_text}_{control_instruction}_{prompt_text}_{cfg_value}".encode("utf-8")
            ).hexdigest()[:16]
            cached_file = self.work_dir / f"voxcpm_{cache_key}.wav"
            if cached_file.is_file() and cached_file.stat().st_size > 4000:
                shutil.copy2(cached_file, out_path)
                self._log(f"♻️ VoxCPM Cache Hit: {out_path.name}")
                return out_path

        from gradio_client import handle_file

        # Prepare reference audio file for upload
        ref_file = None
        if reference_wav:
            ref_path = Path(reference_wav)
            if ref_path.is_file():
                ref_file = handle_file(str(ref_path.resolve()))
                self._log(f"🎵 VoxCPM Reference Audio: {ref_path.name} ({ref_path.stat().st_size} bytes)")
            else:
                logger.warning("VoxCPM reference_wav path not found: %s — proceeding without it", reference_wav)

        last_err = None
        for attempt in range(1, retries + 2):
            try:
                self._log(
                    f"🌐 [VoxCPM Cloud] Synthesizing attempt {attempt}/{retries+1} "
                    f"— chars={len(clean_text)}, ref={'yes' if ref_file else 'no'}"
                )
                client = self.get_client(self.space_name)

                # Submit async job to Gradio Space queue
                job = client.submit(
                    text_input=clean_text,
                    control_instruction=control_instruction or DEFAULT_BURMESE_CONTROL,
                    reference_wav_path_input=ref_file,
                    use_prompt_text=bool(prompt_text),
                    prompt_text_input=prompt_text or "",
                    cfg_value_input=float(cfg_value or 2.0),
                    do_normalize=True,   # Always normalize for consistent output volume
                    denoise=False,
                    api_name="/generate",
                )

                # Monitor live queue ETA and position
                while not job.done():
                    try:
                        st = job.status()
                        eta_val = getattr(st, "eta", None)
                        pos_val = getattr(st, "queue_position", None) or getattr(st, "position", None)
                        if eta_val is not None and float(eta_val) > 0:
                            eta_sec = int(round(float(eta_val)))
                            pos_str = f" (အဆင့်: {pos_val})" if pos_val is not None else ""
                            msg = f"⏳ VoxCPM Queue စောင့်ဆိုင်းနေပါသည်{pos_str} — ခန့်မှန်းကြာချိန် ~{eta_sec}s"
                            self._log(msg)
                            if progress_cb:
                                try:
                                    progress_cb(msg)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    time.sleep(2)

                raw_result = job.result()
                result_path_str = _resolve_result_path(raw_result)

                if (
                    result_path_str
                    and os.path.isfile(result_path_str)
                    and os.path.getsize(result_path_str) > 1000
                ):
                    # Convert to genuine WAV PCM 16-bit mono 24000Hz via ffmpeg for maximum compatibility
                    converted_cleanly = False
                    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
                    if ffmpeg_bin:
                        try:
                            import subprocess
                            cmd = [
                                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                                "-i", str(result_path_str),
                                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                                str(out_path)
                            ]
                            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                            if out_path.is_file() and out_path.stat().st_size > 1000:
                                converted_cleanly = True
                        except Exception as e_ffmpeg:
                            logger.debug("ffmpeg conversion fallback to direct copy: %s", e_ffmpeg)

                    if not converted_cleanly:
                        shutil.copy2(result_path_str, out_path)

                    # Cache result only for no-reference generations
                    if cached_file and not ref_file:
                        try:
                            shutil.copy2(out_path, cached_file)
                        except Exception:
                            pass
                    self._log(
                        f"✅ VoxCPM Success: {out_path.name} ({os.path.getsize(out_path)} bytes)"
                    )
                    return out_path
                else:
                    raise RuntimeError(
                        f"VoxCPM returned empty or missing audio. raw_result={raw_result!r}, "
                        f"resolved_path={result_path_str!r}"
                    )

            except Exception as e:
                last_err = e
                self._log(f"⚠️ VoxCPM attempt {attempt} failed: {e}")
                VoxCPMEngine._client_instance = None
                if attempt <= retries:
                    time.sleep(2 * attempt)

        raise RuntimeError(
            f"VoxCPM speech synthesis failed after {retries + 1} attempts: {last_err}"
        )
