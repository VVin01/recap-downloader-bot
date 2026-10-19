"""
voxcpm_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via VoxCPM.
Connects seamlessly to OpenBMB's VoxCPM-Demo on Hugging Face Spaces for free, human-like voice synthesis.
Supports real-time queue ETA & position tracking.
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import hashlib
import logging
from pathlib import Path
from typing import Optional, Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_VOXCPM_SPACE = "openbmb/VoxCPM-Demo"
DEFAULT_BURMESE_CONTROL = "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone and fluent pronunciation"


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
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("VoxCPM input text is empty.")

        # Check local cache
        cache_key = hashlib.sha256(
            f"{clean_text}_{control_instruction}_{reference_wav}_{prompt_text}".encode("utf-8")
        ).hexdigest()[:16]
        cached_file = self.work_dir / f"voxcpm_{cache_key}.wav"

        if cached_file.is_file() and cached_file.stat().st_size > 4000:
            shutil.copy2(cached_file, out_path)
            self._log(f"♻️ VoxCPM Cache Hit: {out_path.name}")
            return out_path

        from gradio_client import handle_file

        ref_file = None
        if reference_wav and Path(reference_wav).is_file():
            ref_file = handle_file(str(Path(reference_wav).resolve()))

        last_err = None
        for attempt in range(1, retries + 2):
            try:
                self._log(f"🌐 [VoxCPM Cloud] Synthesizing ({attempt}/{retries+1}) — chars={len(clean_text)}")
                client = self.get_client(self.space_name)

                # Submit job for asynchronous queue tracking
                job = client.submit(
                    text_input=clean_text,
                    control_instruction=control_instruction or DEFAULT_BURMESE_CONTROL,
                    reference_wav_path_input=ref_file,
                    use_prompt_text=bool(prompt_text),
                    prompt_text_input=prompt_text or "",
                    cfg_value_input=float(cfg_value or 2.0),
                    do_normalize=True,
                    denoise=False,
                    api_name="/generate",
                )

                # Monitor live queue status and ETA
                while not job.done():
                    try:
                        st = job.status()
                        eta_val = getattr(st, "eta", None)
                        pos_val = getattr(st, "position", None)
                        if eta_val is not None and eta_val > 0:
                            eta_sec = int(round(eta_val))
                            pos_str = f" (အဆင့်: {pos_val})" if pos_val is not None else ""
                            msg = f"⏳ VoxCPM Queue စောင့်ဆိုင်းနေပါသည်{pos_str} — ခန့်မှန်းကြာချိန် ~{eta_sec}s"
                            self._log(msg)
                            if progress_cb:
                                progress_cb(msg)
                    except Exception:
                        pass
                    time.sleep(2)

                result_tmp = job.result()

                if result_tmp and os.path.isfile(result_tmp) and os.path.getsize(result_tmp) > 1000:
                    shutil.copy2(result_tmp, out_path)
                    try:
                        shutil.copy2(result_tmp, cached_file)
                    except Exception:
                        pass
                    self._log(f"✅ VoxCPM Success: {out_path.name} ({os.path.getsize(out_path)} bytes)")
                    return out_path
                else:
                    raise RuntimeError("VoxCPM returned empty or missing audio file.")

            except Exception as e:
                last_err = e
                self._log(f"⚠️ VoxCPM attempt {attempt} failed: {e}")
                VoxCPMEngine._client_instance = None
                if attempt <= retries:
                    time.sleep(2 * attempt)

        raise RuntimeError(f"VoxCPM speech synthesis failed after {retries + 1} attempts: {last_err}")
