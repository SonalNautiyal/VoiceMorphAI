# 🎙 VoiceMorph AI

**Same dialogue. A brand-new voice.**

Upload a short video or audio clip with dialogue, pick a voice profile (or one voice **per speaker**),
and VoiceMorph re-performs the *exact same words* in that new voice — then syncs the generated audio
back onto your untouched video. It's voice re-casting: not translation, not rewriting, not deepfake
generation of real actors.

Built for a hackathon MVP with **FastAPI + vanilla HTML/CSS/JS + FFmpeg**, powered by
**Fish Audio Drama 3**. Minimalist dark/light UI — one restrained accent, hairline borders,
theme toggle persisted in `localStorage`.

### Features

* **Media upload** — audio + video, type/size validation drag-and-drop, preview player, upload progress.
* **Voice cards** — four authorized Drama 3 voice profiles with live samples (generated once, cached).
* **Transcript review** — editable; STT speaker markers (`<|speaker:N|>`) and emotion cues
  (`[whisper]`, `[laughter]`) are preserved intentionally — Drama 3 consumes them as directions.
* **Multi-speaker re-casting** — the Dialogue Agent detects speakers; the UI lets you assign a
  different voice to each, and the Voice Agent performs the whole scene in one Drama 3 request.
* **Multiple takes** — run 1–3 Drama 3 takes per transform, get a word-by-word match score on each,
  auto-export the best take, and re-export any other take *for free* (`/select-take`, no new TTS).
* **Speed control** — pacing multiplier (0.8–1.25×) applied on top of each voice's preset.
* **Honest verification** — each take is re-transcribed and scored against the source transcript
  (0–100%); a low score shows a review warning, never a fake success.
* **Timing fit** — three-stage Post-Production: if a take's performance runs >10% off the clip's
  length, one **auto-paced** Drama 3 re-take is generated at an adjusted prosody speed (sounds natural,
  avoids heavy compression); then FFmpeg `atempo` finishes the pacing, the **speech onset** is aligned
  to the original mouth movement, and the tail is padded/trimmed so the output matches the source
  length exactly. Visuals are always stream-copied, never re-encoded.
* **Before/after preview** — waveform comparison, side-by-side transcripts, video preview, and
  audio-only + final-clip downloads.
* **Job history** — reopen any recent job's result without re-uploading (server-memory lifetime).

---

## How it works

```
 ┌────────────┐   ┌────────────────┐   ┌────────────────┐   ┌──────────────────┐
 │  Upload    │──▶│  Media Agent   │──▶│ Dialogue Agent │──▶│   Voice Agent    │
 │ (browser)  │   │ validate, probe│   │ Fish Audio STT │   │ Fish Audio TTS   │
 └────────────┘   │ FFmpeg: mono   │   │ transcript +   │   │ model: drama-3   │
                  │ WAV for ASR    │   │ word timing    │   │ reference voice  │
                  └────────────────┘   └────────────────┘   └────────┬─────────┘
                                              ▲                      │
                                    review & edit                      ▼
                                    the transcript          ┌──────────────────┐
                                                            │ Post-Production  │
                        ┌────────────────────────┐          │ FFmpeg atempo +  │
                        │   Dialogue Checker     │◀─────────│ mux video        │
                        │ re-transcribe the take │          └──────────────────┘
                        │ similarity % vs source │
                        └────────────────────────┘
```

The **Workflow Coordinator** (`agents/coordinator.py`) runs these stages in a fixed order in a
background thread, records a note after each stage, catches every failure, and exposes honest
progress over a polling API. The "agents" are ordinary, well-separated functions — a deterministic
workflow, not an autonomous LLM loop. That's on purpose: it's easy to explain at the demo and
impossible to wander off.

### What we learned about Drama 3 (investigation summary)

Drama 3 (Fish Audio, released Oct 2026) is a **text-to-speech** model — it does **not** accept an
audio file and return a re-voiced version. So a direct audio→audio conversion isn't possible, and
the honest pipeline above is the correct design. Key facts that shaped the implementation:

| Fact | How VoiceMorph uses it |
|---|---|
| `POST /v1/tts` with header `model: drama-3` (missing header silently falls back to s2.1-pro!) | We set the header exactly and verify output. |
| `reference_id` selects a voice model (a consenting public/synthetic profile) | Voice cards map to fixed Fish Audio voice IDs. |
| `[bracketed]` text is read as a **performance direction and is not spoken** | The words from the original clip are passed through untouched; a one-line delivery direction sits above them. Bracketed emotion cues from STT (`[laughter]`) also enrich delivery without altering words. |
| `prosody.speed` (0.5–2.0) adjusts pacing | Per-voice speed presets. |
| Output formats: mp3 / wav, mono | We request WAV for the pipeline, MP3 for previews. |
| `POST /v1/asr` with header `transcribe-1-pro`, multipart upload | Transcription with word timestamps and speaker turns. |
| Pricing is per characters of input text (launch promo $10/M chars until Nov 12 2026) | Short clips cost fractions of a cent per transformation. |

### Honest limitations (also visible in the UI)

* Drama 3 is a TTS model, so the performance (pauses, emotion, timing) is *re-performed*, not
  transferred. The Post-Production stage re-paces the take to the source duration (atempo), aligns
  the **speech onset** to the original, then pads/trims so the output matches the source length
  exactly — but pauses *between* phrases are the model's, so mid-clip word positions can drift.
* **Lip-sync is approximate by design.** The video's lip movements come from the original footage:
  the generated audio is timed so speech starts where the original mouth starts moving, but the mouth
  won't morph to the new voice's articulation. Pixel-perfect lipsync would require face re-animation
  (deepfake territory) — deliberately out of scope.
* Multi-speaker clips: each detected speaker is re-voiced with the voice you assign to them, but
  speaker *diarization* comes straight from the STT — mis-detected speaker boundaries will carry
  through to the output.
* Background music/noise in the source will not carry into the output (original video audio is
  replaced). Use clean-dialogue clips.
* The final "Dialogue Checker" re-transcribes the generated audio and reports a **match %**; below
  the threshold it tells you to review — it never fakes success.
* Voice profiles are authorized synthetic/public models. Cloning real actors without consent is
  explicitly not supported.

---

## Quick start (Windows)

### 1. Install Python 3.10+
```powershell
winget install Python.Python.3.12
```
(Close and reopen your terminal afterwards so `python` is on PATH.)

### 2. Install FFmpeg
```powershell
winget install Gyan.FFmpeg
```
Then verify: `ffmpeg -version` (reopen the terminal if needed).

### 3. Set up the project
```powershell
cd VoiceMorph-AI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### 4. Add your API key
Get a key at [fish.audio](https://fish.audio) → put it in `.env`:
```
FISH_API_KEY=your_key_here
```

### 5. Run
```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```
(or double-click `run.bat`) → open **http://127.0.0.1:8000**

The header chip shows whether the engine is ready (ffmpeg + API key detected). A 30-second,
clean-voice MP4/MOV clip with one speaker is the ideal demo input.

---

## Project structure

```
VoiceMorph-AI/
├── app.py                     # FastAPI server: upload, job polling, transform, file serving
├── config.py                  # env-based config + the four voice profiles
├── agents/
│   ├── media_agent.py         # validates uploads, probes media, extracts mono audio
│   ├── dialogue_agent.py      # transcription + transcript similarity scoring
│   ├── voice_agent.py         # Drama 3 scripting, synthesis, verification
│   └── coordinator.py         # deterministic stage workflow (the "conductor")
├── services/
│   ├── fish_api.py            # the only place that talks to api.fish.audio
│   └── media.py               # all FFmpeg/FFprobe commands
├── static/
│   ├── index.html
│   └── assets/ css/ js/       # cinematic dark UI, no build step
├── data/jobs/<id>/            # runtime artifacts (gitignored)
├── .env.example
├── requirements.txt
└── run.bat
```

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | ffmpeg + API-key readiness, upload limits |
| GET | `/api/voices` | voice card data (no secrets) |
| GET | `/api/voices/{id}/preview` | short Drama 3 sample, cached server-side |
| POST | `/api/jobs` | multipart upload → starts analysis |
| GET | `/api/jobs/{id}` | stage status / result / error (poll ~1 s) |
| POST | `/api/jobs/{id}/transform` | `{voice_id, transcript}` → re-voice + mux |
| GET | `/files/jobs/{id}/...` | originals + generated WAV/final clip |

## Demo script (90 seconds)

1. Show a movie-style clip and play it.
2. Upload → watch the two analysis stages tick over.
3. Point out the **editable transcript** and the multiple-speaker warning if you show one.
4. Click ▶ on two voice cards to preview live Drama 3 samples.
5. Transform → stepper shows **Voice Agent → Post-Production → Dialogue Checker (match %)**.
6. Play before/after side by side, then the final video, and download it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Chip says "setup needed" | Open `/api/health` — it lists exactly what's missing (ffmpeg / API key). |
| 401 from Fish Audio | Wrong/missing `FISH_API_KEY` in `.env`. |
| 402 responses | Out of credit in your Fish Audio account. |
| "No speech detected" | Clip has no clear dialogue; try another clip. |
| Generated voice sounds off | Drama 3 takes vary — press Transform again for a new take. |
| Uploads rejected | Check the file type/size limits shown in the upload card. |
#   V o i c e M o r p h A I  
 