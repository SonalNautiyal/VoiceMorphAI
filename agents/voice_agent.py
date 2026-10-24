"""Voice Agent — coordinates speech generation with Drama 3.

Pipeline position:
  transcript (reviewed)  ->  build drama script  ->  Drama 3 synthesis  ->  WAV/MP3

Drama 3 reads `[bracketed]` text as a direction and does not speak it, so the
generated take performs the SAME words with the voice card's delivery style.

Multi-speaker: Fish Audio's transcribe-1-pro emits `<|speaker:N|>` markers —
the EXACT syntax Drama 3's multi-speaker mode consumes — so we can re-index
those markers, give every speaker their own voice, and generate the whole
scene in one request.
"""
import re
from pathlib import Path

import services.fish_api as fish_api
import services.media as media

from agents import dialogue_agent
from config import VERIFY_SIMILARITY_WARN, get_voice

# One short line, paid for once per voice and cached server-side.
PREVIEW_TEXT = "This is VoiceMorph. Your dialogue, reborn in a brand new voice."

_SPEAKER_MARK = re.compile(r"<\|speaker:(\d+)\|>")


class VoiceAgentError(Exception):
    pass


def require_voice(voice_id: str) -> dict:
    voice = get_voice(voice_id)
    if not voice:
        raise VoiceAgentError(f"Unknown voice '{voice_id}'.")
    return voice


def build_single_script(transcript: str, voice: dict) -> str:
    """Single speaker: one direction line + the (unchanged) dialogue."""
    return f"{voice['direction']}\n{transcript}"


def build_multi_script(text: str, voices_by_label: dict[int, dict]) -> tuple[str, list[str]]:
    """Rewrite STT speaker markers for Drama 3 multi-speaker syntheses.

    * Re-indexes each `<|speaker:N|>` marker to its first-appearance order.
    * Inserts that speaker's voice direction (bracketed = not spoken) once,
      right after the speaker's first marker.
    * Returns (script_text, [fish_id, ...]) — reference_id must line up with
      the re-indexed slots.
    """
    seen: list[int] = []      # original label -> slot index
    slot_voices: list[dict] = []
    parts: list[str] = []
    last = 0

    for m in _SPEAKER_MARK.finditer(text):
        old = int(m.group(1))
        if old not in seen:
            seen.append(old)
            fallback = next(iter(voices_by_label.values()), None)
            slot_voices.append(voices_by_label.get(old) or fallback)
            slot = len(seen) - 1
            parts.append(text[last:m.start()])
            parts.append(f"<|speaker:{slot}|>{slot_voices[slot]['direction']}")
        else:
            slot = seen.index(old)
            parts.append(text[last:m.start()])
            parts.append(f"<|speaker:{slot}|>")
        last = m.end()
    parts.append(text[last:])

    refs = [v["fish_id"] for v in slot_voices]
    return "".join(parts), refs


def preview_audio(voice_id: str) -> tuple[str, bytes]:
    """Small sample for the voice card. Returns (mime_type, audio_bytes)."""
    voice = require_voice(voice_id)
    data = fish_api.synthesize(PREVIEW_TEXT, voice["fish_id"], speed=voice["speed"], fmt="mp3")
    return "audio/mpeg", data


def generate_speech(script: str, refs: list[str], speed: float, out_path: Path) -> float:
    """Generate one take as WAV. Returns the audio duration in seconds."""
    audio = fish_api.synthesize(script, refs, speed=speed, fmt="wav")
    out_path.write_bytes(audio)
    return media.probe(out_path)["duration"]


def verify(generated_wav: Path, source_transcript: str) -> dict:
    """Re-transcribe the generated audio and score how well the words survived.

    This is the honest "did the dialogue stay the same?" check — nobody has to
    take our word for it.
    """
    result = dialogue_agent.transcribe(generated_wav)
    ratio = dialogue_agent.similarity(source_transcript, result["text"])
    return {
        "similarity": round(ratio, 3),
        "generated_text": result["text"],
        "needs_review": ratio < VERIFY_SIMILARITY_WARN,
    }
