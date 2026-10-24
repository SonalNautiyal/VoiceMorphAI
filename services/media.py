"""FFmpeg / FFprobe helpers — every shell/media command lives here."""
import json
import re
import shutil
import subprocess
from pathlib import Path


class MediaError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str], ctx: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise MediaError(
            "FFmpeg was not found on this machine. Install it (see README) and make sure "
            "'ffmpeg' and 'ffprobe' are on your PATH, then restart the server."
        ) from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"{ctx} timed out.") from None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise MediaError(f"{ctx} failed: {' | '.join(tail) or 'unknown error'}")
    return proc


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe(path: Path) -> dict:
    """Inspect a media file: {duration, has_video, has_audio}."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = _run(cmd, "Media inspection")
    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        info = {}
    return {
        "duration": _as_float(info.get("format", {}).get("duration")),
        "has_video": any(s.get("codec_type") == "video" for s in info.get("streams", [])),
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


def extract_audio(src: Path, dst: Path, sample_rate: int = 16000) -> None:
    """Decode the first audio track into mono WAV — the format transcribers like."""
    _run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate), str(dst)],
         "Audio extraction")


def stretch_audio(src: Path, dst: Path, factor: float) -> None:
    """Time-stretch audio (atempo) without changing pitch."""
    factor = max(0.5, min(2.0, factor))
    _run(["ffmpeg", "-y", "-i", str(src), "-filter:a", f"atempo={factor:.4f}", "-ar", "44100", str(dst)],
         "Audio timing adjustment")


def speech_onset(path: Path, threshold_db: float = -32.0, min_gap: float = 0.1) -> float | None:
    """When speech first starts in a file, in seconds (or None if undetectable).

    Uses ffmpeg's silencedetect filter: if the clip begins with silence, the
    first detected "silence_end" is the moment speech begins.
    """
    proc = _run(
        ["ffmpeg", "-i", str(path), "-af",
         f"silencedetect=noise={threshold_db}dB:d={min_gap}", "-f", "null", "-"],
        "Speech onset detection",
    )
    text = proc.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    if starts and starts[0] < 0.2 and ends:
        return ends[0]
    if starts and starts[0] < 0.2:
        # silence diagnosed but never ended: effectively no speech
        return None
    return 0.0


def fit_audio(src: Path, dst: Path, target_duration: float,
              trim_head: float = 0.0, delay_head: float = 0.0) -> None:
    """Make an audio file exactly `target_duration` long, optionally shifted.

    * `trim_head` — cut the first N seconds (generated speech started later
      than the original, so its leading silence must go).
    * `delay_head` — pad the front with N seconds of silence (generated speech
      started earlier than the original).
    * The output is padded to the exact target length with silence, or cut at
      the target length if it runs long — no half-second drifts.
    """
    filters = []
    if trim_head > 0:
        filters.append(f"atrim=start={trim_head:.3f},asetpts=PTS-STARTPTS")
    if delay_head > 0:
        ms = int(round(delay_head * 1000))
        filters.append(f"adelay={ms}")
    filters.append("apad")   # pad the tail with silence (no-op if already long enough)
    _run(["ffmpeg", "-y", "-i", str(src), "-af", ",".join(filters),
          "-ar", "44100", "-t", f"{target_duration:.3f}", str(dst)],
         "Audio alignment")


def remix_video(video: Path, audio: Path, dst: Path) -> None:
    """Rebuild the clip: original visuals untouched + new audio track.

    `-c:v copy` copies the video stream without re-encoding, so quality and
    frame timing of the visuals stay exactly as uploaded.
    """
    _run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
          "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
          "-shortest", str(dst)],
         "Video remix")
