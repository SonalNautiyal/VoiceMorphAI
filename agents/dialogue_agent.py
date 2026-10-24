"""Dialogue Agent — transcribes the original clip and checks spoken words later.

Two jobs:
1. Turn the prepared mono WAV into text (via Fish Audio's `transcribe-1-pro`).
2. Re-transcribe *generated* audio and score how closely it matches the
   original words, so we can honestly report dialogue preservation.
"""
import re
from difflib import SequenceMatcher
from pathlib import Path

import services.fish_api as fish_api

# Speaker markers returned inside `text`, e.g. `<|speaker:0|>`
_SPEAKER_TAG = re.compile(r"<\|speaker:\d+\|>")


def clean_text(text: str) -> str:
    """Remove internal speaker markers; keep emotion cues like [laughter].

    That matters here: Drama 3 treats [bracketed] text as a performance
    direction, so a cue such as [laughter] improves the delivery without
    being spoken — the dialogue itself stays untouched.
    """
    text = _SPEAKER_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def transcribe(wav_path: Path) -> dict:
    """Transcribe one WAV file. Returns {text, raw_text, language, speaker_count, duration}."""
    audio = wav_path.read_bytes()
    data = fish_api.transcribe(audio, filename=wav_path.name)

    raw_text = data.get("text", "")
    tags = set(_SPEAKER_TAG.findall(raw_text))
    turns = data.get("speaker_turns") or []
    speakers = {t.get("speaker") for t in turns if t.get("speaker")} or tags
    speaker_count = len(speakers) if speakers else 1

    return {
        "text": clean_text(raw_text),
        "raw_text": raw_text,
        "language": data.get("language") or "Unknown",
        "language_code": data.get("language_code"),
        "duration": data.get("duration"),
        "speaker_count": speaker_count,
    }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def similarity(a: str, b: str) -> float:
    """Token-level similarity between two transcripts (0..1)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, ta, tb).ratio()
