"""Thin wrapper around the Fish Audio REST API (transcription + Drama 3 TTS).

Only this file talks to fish.audio. Keeping every network call in one place
makes the project easy to explain, test, or swap providers later.

API cheat-sheet used here:
  POST /v1/asr  — multipart upload, `model` header selects the STT model.
  POST /v1/tts  — JSON body, `model` header must be `drama-3` for Drama 3.
                  `reference_id` selects the voice; returns raw audio bytes.
"""
import json
import os

import httpx

from config import FISH_API_KEY, STT_MODEL, TTS_MODEL

BASE_URL = "https://api.fish.audio"
# Transcription of long clips can take minutes, so allow a generous timeout.
TIMEOUT = float(os.getenv("FISH_TIMEOUT_S", "900"))


class FishAPIError(Exception):
    """Raised for every failure while talking to the Fish Audio API."""

    def __init__(self, status: int, message: str, code: str | None = None):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(message)

    @property
    def friendly(self) -> str:
        """A clear, human explanation for the UI instead of a raw API error."""
        hints = {
            401: "The FISH_API_KEY in your .env file is missing or invalid.",
            402: "Your Fish Audio account is out of credit — top it up at fish.audio, then try again.",
            413: "The audio clip was too large for the API. Try a shorter or more compressed clip.",
            429: "Fish Audio is rate-limiting us right now. Wait a few seconds and try again.",
        }
        base = hints.get(self.status)
        if base:
            return base
        if self.status >= 500:
            return "The Fish Audio service is temporarily unavailable. Please try again shortly."
        return self.message


def _check_key() -> None:
    if not FISH_API_KEY:
        raise FishAPIError(
            500,
            "FISH_API_KEY is not configured. Copy .env.example to .env and paste your Fish Audio key.",
        )


def _error_from_response(status: int, body: bytes, content_type: str) -> FishAPIError:
    try:
        if "json" in content_type:
            data = json.loads(body.decode("utf-8"))
            return FishAPIError(status, data.get("message", "Unknown API error"), data.get("code"))
        return FishAPIError(status, body.decode("utf-8", errors="replace")[:300] or "Unknown API error")
    except Exception:
        return FishAPIError(status, "Unknown API error")


def transcribe(audio: bytes, filename: str = "audio.wav", language: str | None = None) -> dict:
    """Speech-to-text via Fish Audio.

    `ignore_timestamps=false` gives us word timestamps plus styled
    `speaker_turns`, so we can know how many speakers the clip contains.
    """
    _check_key()
    headers = {"Authorization": f"Bearer {FISH_API_KEY}", "model": STT_MODEL}
    data = {"ignore_timestamps": "false"}  # timestamps ON: we want word-level detail
    files = {"audio": (filename, audio, "audio/wav")}
    if language:
        data["language"] = language

    try:
        r = httpx.post(f"{BASE_URL}/v1/asr", headers=headers, data=data, files=files, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise FishAPIError(503, f"Could not reach the Fish Audio transcription service: {exc}") from exc

    if r.status_code != 200:
        raise _error_from_response(r.status_code, r.content, r.headers.get("content-type", ""))
    return r.json()


def synthesize(text: str, voice_id: str | list[str], speed: float = 1.0, fmt: str = "mp3") -> bytes:
    """Text-to-speech with Drama 3. Returns raw audio bytes (mp3 or wav).

    `voice_id` may be a single voice model ID, or a list of IDs for
    multi-speaker dialogue (text then contains <|speaker:N|> tags).
    """
    _check_key()
    headers = {
        "Authorization": f"Bearer {FISH_API_KEY}",
        "Content-Type": "application/json",
        # If this header is missing or misspelled the API silently falls back to
        # the older s2.1-pro model, so it must be exact.
        "model": TTS_MODEL,
    }
    payload = {
        "text": text,
        "reference_id": voice_id,
        "format": fmt,
        "temperature": 0.7,
        "top_p": 0.7,
        "normalize": True,
        "prosody": {"speed": speed, "volume": 0, "normalize_loudness": True},
        "mp3_bitrate": 128,
    }

    try:
        r = httpx.post(f"{BASE_URL}/v1/tts", headers=headers, json=payload, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise FishAPIError(503, f"Could not reach the Fish Audio TTS service: {exc}") from exc

    if r.status_code != 200:
        raise _error_from_response(r.status_code, r.content, r.headers.get("content-type", ""))
    return r.content
