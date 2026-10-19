"""
voice_clone_bridge.py - Bridge between Recap Engine and Voice Clone Pro Official (Applio RVC).
Provides automatic 1-click voice conversion from Edge TTS base speech into custom cloned voices.
"""

from __future__ import annotations

import os
import sys
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Standard locations on user's system
DEFAULT_APPLIO_WORKDIR = r"C:\Applio"
DEFAULT_APPLIO_PYTHON = r"C:\Applio\env\python.exe"
DEFAULT_VOICE_CLONE_PROJECT = r"C:\Users\awint\VoiceCloneProOfficial"
DEFAULT_RUNNER_PATH = os.path.join(DEFAULT_VOICE_CLONE_PROJECT, "worker", "applio_runner.py")
DEFAULT_MODELS_ROOT = os.path.join(DEFAULT_VOICE_CLONE_PROJECT, "storage", "models")


def is_applio_available() -> bool:
    """Check if Applio environment and inference bridge are available."""
    py_ok = os.path.isfile(DEFAULT_APPLIO_PYTHON)
    runner_ok = os.path.isfile(DEFAULT_RUNNER_PATH)
    workdir_ok = os.path.isdir(DEFAULT_APPLIO_WORKDIR)
    return bool(py_ok and runner_ok and workdir_ok)


def get_available_clone_models() -> Dict[str, Dict[str, Any]]:
    """
    Scans Voice Clone Pro Official system and personal models.
    Returns a dictionary mapping model_key -> {name, pth, index, label, gender_base}
    """
    models: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(DEFAULT_MODELS_ROOT):
        return models

    # Scan system models
    sys_dir = os.path.join(DEFAULT_MODELS_ROOT, "system")
    if os.path.isdir(sys_dir):
        for folder_name in os.listdir(sys_dir):
            f_path = os.path.join(sys_dir, folder_name)
            if not os.path.isdir(f_path):
                continue
            pth_file = None
            index_file = None
            for fname in os.listdir(f_path):
                if fname.endswith(".pth"):
                    pth_file = os.path.join(f_path, fname)
                elif fname.endswith(".index"):
                    index_file = os.path.join(f_path, fname)

            if pth_file:
                # Create a normalized identifier
                key = f"clone_sys_{folder_name}"
                display_label = f"🎭 Clone: {folder_name}"
                # Guess gender base for Edge TTS (most are male character models)
                gender_base = "my-MM-ThihaNeural"
                models[key] = {
                    "id": key,
                    "name": folder_name,
                    "label": display_label,
                    "pth": pth_file,
                    "index": index_file or "",
                    "edge_base": gender_base,
                    "is_clone": True,
                }

    # Scan personal models
    personal_dir = os.path.join(DEFAULT_MODELS_ROOT, "personal")
    if os.path.isdir(personal_dir):
        for root, dirs, files in os.walk(personal_dir):
            for fname in files:
                if fname.endswith(".pth"):
                    pth_file = os.path.join(root, fname)
                    base_stem = os.path.splitext(fname)[0]
                    # Check if matching .index exists in same directory
                    index_file = ""
                    for ofile in files:
                        if ofile.endswith(".index"):
                            index_file = os.path.join(root, ofile)
                            break
                    key = f"clone_usr_{base_stem}"
                    models[key] = {
                        "id": key,
                        "name": base_stem,
                        "label": f"👤 Clone: {base_stem[:15]}",
                        "pth": pth_file,
                        "index": index_file,
                        "edge_base": "my-MM-ThihaNeural",
                        "is_clone": True,
                    }

    return models


def resolve_clone_model(model_tag: str) -> Optional[Dict[str, Any]]:
    """Resolve a voice tag (e.g. 'clone_sys_တွတ်ပီ' or 'တွတ်ပီ') to model info."""
    if not model_tag:
        return None
    models = get_available_clone_models()
    if model_tag in models:
        return models[model_tag]

    # Try matching by name or folder
    clean_tag = model_tag.lower().replace("clone:", "").replace("clone_", "").strip()
    for k, v in models.items():
        if v["name"].lower() == clean_tag or clean_tag in k.lower():
            return v
    return None


def convert_voice_rvc(
    input_wav: Path | str,
    output_wav: Path | str,
    model_tag: str,
    pitch: int = 0,
    f0_method: str = "rmvpe",
    timeout_seconds: int = 600,
    log_fn: Optional[Any] = None,
) -> Path:
    """
    Executes Applio RVC inference using Voice Clone Pro Official bridge.
    Input WAV -> Cloned Voice Output WAV.
    """
    def _log(msg: str):
        if log_fn:
            log_fn(msg)
        else:
            logger.info(msg)

    if not is_applio_available():
        raise RuntimeError("Applio RVC environment is not available at C:\\Applio")

    model_info = resolve_clone_model(model_tag)
    if not model_info:
        raise ValueError(f"Clone model not found for tag: {model_tag}")

    model_pth = Path(model_info["pth"]).resolve()
    index_pth = Path(model_info["index"]).resolve() if model_info["index"] else None
    in_wav = Path(input_wav).resolve()
    out_wav = Path(output_wav).resolve()

    if not in_wav.is_file():
        raise FileNotFoundError(f"Input audio for voice cloning not found: {in_wav}")

    out_wav.parent.mkdir(parents=True, exist_ok=True)

    _log(f"🎙️ [Voice Clone Pro] Converting voice with RVC model '{model_info['name']}' ({f0_method}, pitch={pitch})...")

    # Pre-process to mono 40kHz for maximum RVC & RMVPE pitch-tracking fidelity (eliminates stereo phasing/metallic artifacts)
    mono_in = in_wav.with_name(f"{in_wav.stem}_rvc_mono.wav")
    clean_target = in_wav
    try:
        ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        prep_cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(in_wav), "-ac", "1", "-ar", "40000", "-c:a", "pcm_s16le", str(mono_in)
        ]
        subprocess.run(prep_cmd, check=True, capture_output=True)
        if mono_in.is_file() and mono_in.stat().st_size > 1000:
            clean_target = mono_in
    except Exception as e_prep:
        logger.debug(f"Pre-RVC mono conversion skipped: {e_prep}")
        clean_target = in_wav

    # Calculate dynamic timeout based on audio length (CPU RVC inference on long recaps takes ~2.0-3.0x realtime)
    audio_dur = 0.0
    try:
        from recap_engine.core import probe_duration, resolve_binary
        ffprobe_bin = resolve_binary(None, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
        audio_dur = probe_duration(ffprobe_bin, in_wav)
    except Exception:
        audio_dur = 300.0
    calc_timeout = max(timeout_seconds, int(audio_dur * 5.0) + 600)

    try:
        cmd = [
            DEFAULT_APPLIO_PYTHON,
            DEFAULT_RUNNER_PATH,
            "--model", str(model_pth),
            "--index", str(index_pth) if index_pth and index_pth.is_file() else "",
            "--input", str(clean_target),
            "--output", str(out_wav),
            "--pitch", str(pitch),
            "--f0-method", str(f0_method),
            "--split-audio",
        ]

        res = subprocess.run(
            cmd,
            cwd=DEFAULT_APPLIO_WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=calc_timeout,
        )

        if res.returncode != 0 or not out_wav.is_file() or out_wav.stat().st_size < 1000:
            err_detail = res.stderr[-2000:] if res.stderr else res.stdout[-2000:]
            raise RuntimeError(f"Voice Clone Pro RVC conversion failed (code {res.returncode}):\n{err_detail}")
    finally:
        if mono_in.is_file():
            try:
                mono_in.unlink(missing_ok=True)
            except Exception:
                pass

    _log(f"✅ [Voice Clone Pro] Voice conversion completed successfully! Output: {out_wav.name} ({out_wav.stat().st_size / 1024:.1f} KB)")
    return out_wav
