"""Media Agent — validates the uploaded media and prepares the audio.

In this MVP the "agents" are ordinary, well-separated functions that the
coordinator calls in a fixed order. They are tools, not autonomous bots.
"""
from pathlib import Path

import services.media as media

from config import AUDIO_EXTS, MAX_DURATION_S, MAX_UPLOAD_MB, VIDEO_EXTS


class MediaAgentError(Exception):
    pass


def validate_file(filename: str, size_bytes: int) -> str:
    """Check the filename extension and file size. Returns the normalized extension."""
    ext = Path(filename or "").suffix.lower()
    if ext not in (AUDIO_EXTS | VIDEO_EXTS):
        raise MediaAgentError(
            "Unsupported file type. Upload an audio clip (wav, mp3, m4a, aac, flac, ogg) "
            "or a video clip (mp4, mov, mkv, webm)."
        )
    if size_bytes <= 0:
        raise MediaAgentError("The uploaded file is empty.")
    if size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        raise MediaAgentError(
            f"That file is {size_bytes / (1024 * 1024):.0f} MB — the limit is {MAX_UPLOAD_MB} MB. "
            "Trim or compress the clip and try again."
        )
    return ext


def prepare(job_dir: Path) -> dict:
    """Probe the uploaded file and extract mono WAV audio for transcription.

    Returns {kind, duration, media_path, mono_path}.
    """
    sources = list(job_dir.glob("upload.*"))
    if not sources:
        raise MediaAgentError("The uploaded file could not be stored. Try uploading again.")

    src = sources[0]
    if not media.ffmpeg_available():
        raise MediaAgentError(
            "FFmpeg is not installed on the server. See README for a one-line install, "
            "then restart the server."
        )

    info = media.probe(src)
    if not info["has_audio"]:
        raise MediaAgentError("No audio track was found in this file. Upload a clip that contains dialogue.")
    kind = "video" if info["has_video"] else "audio"
    if info["duration"] > MAX_DURATION_S:
        raise MediaAgentError(
            f"Your clip is {info['duration']:.0f}s but the limit is {MAX_DURATION_S}s. "
            "Trim it down and try again."
        )

    mono = job_dir / "original_mono.wav"
    media.extract_audio(src, mono)
    return {
        "kind": kind,
        "duration": info["duration"],
        "media_path": src,
        "mono_path": mono,
        "ext": src.suffix,
    }
