"""Central configuration for VoiceMorph AI.

Secrets come from environment variables (or a `.env` file) — nothing sensitive
is hardcoded, and nothing is ever exposed to the frontend.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------- directories
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = BASE_DIR / "static"

# --------------------------------------------------------------------- limits
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
MAX_DURATION_S = int(os.getenv("MAX_DURATION_S", "300"))  # keep hackathon clips short

# ---------------------------------------------------------------- Fish Audio
# Required. Get a key at https://fish.audio
FISH_API_KEY = os.getenv("FISH_API_KEY", "").strip()
TTS_MODEL = os.getenv("TTS_MODEL", "drama-3")       # Drama 3  — directed TTS
STT_MODEL = os.getenv("STT_MODEL", "transcribe-1-pro")  # transcription

# ------------------------------------------------------------------- tuning
# When the generated take is noticeably shorter/longer than the original clip,
# gently time-stretch the generated audio (ffmpeg `atempo`) so it fits the video.
STRETCH_ENABLED = os.getenv("STRETCH_ENABLED", "1") == "1"
STRETCH_FACTOR_MIN = 0.5
STRETCH_FACTOR_MAX = 2.0
# Only stretch if the mismatch is bigger than 0.5% (≈50ms on a 10s clip).
# Small differences are corrected anyway by padding/trimming in fit_audio;
# anything above this gets a proper speed change so words stay right about
# where the original mouth moves.
STRETCH_THRESHOLD = float(os.getenv("STRETCH_THRESHOLD", "0.005"))
# Below this transcript-match score the UI warns the user to review the words.
VERIFY_SIMILARITY_WARN = float(os.getenv("VERIFY_SIMILARITY_WARN", "0.85"))

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# ---------------------------------------------------------------- voice cast
# Every voice is a Fish Audio *voice model ID* passed to Drama 3 as
# `reference_id`. Only consenting public/synthetic profiles are used — no real
# actor's voice without an authorized model.
#
# `direction` is Drama 3's plain-language performance direction. It goes into
# the script in [square brackets]; Drama 3 treats bracketed text as direction,
# so it shapes the delivery WITHOUT changing the spoken words.
#
# Add more cards by grabbing model IDs from https://fish.audio (Explore page).
VOICES = [
    {
        "id": "atlas",
        "name": "Atlas",
        "persona": "Deep Cinematic",
        "description": "A deep, resonant male voice with the calm gravity of a movie-trailer narrator.",
        "fish_id": "bf322df2096a46f18c579d0baa36f41d",  # "Adrian" — official Drama 3 cast voice (EN-US, male)
        "direction": "[deep, rich, cinematic delivery — slow, confident, in control]",
        "speed": 0.95,
        "accent": "v-atlas",
        "tags": ["EN-US", "Male"],
    },
    {
        "id": "nova",
        "name": "Nova",
        "persona": "Calm & Clear",
        "description": "A warm, composed female voice built for storytelling and gentle explanation.",
        "fish_id": "c70cfb98867a4bfb9ae20d2401257b92",  # "Delia" — official Drama 3 cast voice (EN-US, female)
        "direction": "[calm, warm, steady narrator voice — relaxed and reassuring]",
        "speed": 1.0,
        "accent": "v-nova",
        "tags": ["EN-US", "Female"],
    },
    {
        "id": "vera",
        "name": "Vera",
        "persona": "Dramatic Actor",
        "description": "An expressive female voice that leans into emotion — tension, whispers, and pauses.",
        "fish_id": "f3a2b90078d54a65af4b95f63bc7798e",  # "Sheila" — official Drama 3 cast voice (EN-US, female)
        "direction": "[emotional dramatic performance — intense, expressive, moving]",
        "speed": 1.0,
        "accent": "v-vera",
        "tags": ["EN-US", "Female"],
    },
    {
        "id": "ignis",
        "name": "Ignis",
        "persona": "Energetic Announcer",
        "description": "A booming, high-energy announcer voice straight out of a fight-night broadcast.",
        "fish_id": "90e65eaaf50e4470b8e6d43ee6afd7d5",  # community voice — deep, dramatic announcer (EN)
        "direction": "[energetic, hyped announcer — punchy and powerful]",
        "speed": 1.05,
        "accent": "v-ignis",
        "tags": ["EN", "Male"],
    },
]


def get_voice(voice_id: str):
    for v in VOICES:
        if v["id"] == voice_id:
            return v
    return None


def public_voices():
    """Voices safe to send to the frontend (no internal fields)."""
    return [
        {k: v[k] for k in ("id", "name", "persona", "description", "accent", "tags")}
        for v in VOICES
    ]
