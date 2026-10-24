"""Workflow Coordinator — runs the agent stages in a deterministic order.

Responsibilities:
  * manage job records and stage status,
  * call Media Agent -> Dialogue Agent -> Voice Agent in order,
  * hand outputs between stages,
  * catch every failure and record a clear, honest error,
  * keep the stage notes flowing to the UI via polling.

Deliberately NOT an autonomous multi-agent LLM loop — this is one ordinary
function per flow, executed in a background thread. Easy to explain at a
demo: every stage has a name and a note, and failures say what happened.
"""
import shutil
import time
import uuid
from pathlib import Path

import agents.dialogue_agent as dialogue_agent
import agents.media_agent as media_agent
import agents.voice_agent as voice_agent
import services.media as media

from agents.media_agent import MediaAgentError
from agents.voice_agent import VoiceAgentError
from config import (JOBS_DIR, STRETCH_ENABLED, STRETCH_FACTOR_MAX, STRETCH_FACTOR_MIN,
                    STRETCH_THRESHOLD, get_voice)

JOBS: dict[str, dict] = {}

FLOW_STAGES = {
    "analyze": [
        ("media", "Media Agent", "Validate media and prepare audio"),
        ("dialogue", "Dialogue Agent", "Transcribe the spoken words"),
    ],
    "transform": [
        ("voice", "Voice Agent", "Generate takes with Drama 3"),
        ("remix", "Post-Production", "Sync audio and video"),
        ("verify", "Dialogue Checker", "Score every take"),
    ],
}


def new_job() -> dict:
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "dir": str(job_dir),
        "ext": "",
        "flow": "analyze",
        "status": "running",
        "current_stage": "",
        "stages": {},
        "result": {},
        "error": None,
        "created": time.time(),
    }
    JOBS[job_id] = job
    return job


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


def set_stage(job: dict, key: str, status: str, note: str = "", detail: str = "") -> None:
    job["stages"][key] = {"status": status, "note": note, "detail": detail}
    if status == "running":
        job["current_stage"] = key


def fail_current_stage(job: dict) -> None:
    key = job.get("current_stage")
    if key:
        stage = job["stages"].get(key)
        if stage:
            stage["status"] = "error"
            stage["note"] = job.get("error", "")


def job_view(job: dict) -> dict:
    """The cleaned-up version of a job that is safe to send to the frontend."""
    flow = job.get("flow") or "analyze"
    stages = []
    for key, name, desc in FLOW_STAGES[flow]:
        state = job["stages"].get(key) or {}
        stages.append({
            "key": key,
            "name": name,
            "desc": desc,
            "status": state.get("status", "pending"),
            "note": state.get("note", ""),
        })
    return {
        "id": job["id"],
        "flow": flow,
        "status": job["status"],
        "kind": job["result"].get("kind"),
        "stages": stages,
        "result": job["result"],
        "error": job["error"],
        "created": job.get("created"),
    }


def history_view() -> list[dict]:
    """Compact recent-jobs list for the history panel."""
    jobs = sorted(JOBS.values(), key=lambda j: j.get("created") or 0, reverse=True)
    out = []
    for j in jobs[:8]:
        r = j.get("result") or {}
        out.append({
            "id": j["id"],
            "status": j["status"],
            "kind": r.get("kind"),
            "voice": r.get("voice"),
            "similarity": r.get("similarity"),
            "preview": (r.get("text") or "")[:60],
        })
    return out


# ---------------------------------------------------------------- voices plan

def _clamp_speed(s: float) -> float:
    return max(0.5, min(2.0, s))


def plan_voices(job: dict, voice_id: str, speaker_voices: dict[str, str] | None,
                text: str) -> tuple[str, list[str], float]:
    """Build the Drama 3 script + reference_id list + final speed.

    Returns (script, refs, speed) for the Voice Agent.

    Multi-speaker auto-detection: if the (possibly user-edited) text still
    contains <|speaker:N|> markers, we use Drama 3's native multi-speaker
    mode with one voice per marker; otherwise single-speaker.
    """
    user_speed = float(job.get("_speed") or 1.0)

    if get_voice(voice_id) is None:
        raise VoiceAgentError(f"Unknown voice '{voice_id}'.")

    import re as _re
    markers = _re.findall(r"<\|speaker:(\d+)\|>", text)

    if markers:
        voices_by_label = {}
        for old, vid in (speaker_voices or {}).items():
            v = get_voice(vid)
            if v is None:
                raise VoiceAgentError(f"Unknown voice '{vid}' for speaker {old}.")
            voices_by_label[int(old)] = v
        default_voice = get_voice(voice_id)
        script, refs = voice_agent.build_multi_script(text, voices_by_label)
        # per-speaker speeds average for the prosody control
        speeds = [v["speed"] for v in (voices_by_label.values())] or [default_voice["speed"]]
        speed = _clamp_speed((sum(speeds) / len(speeds)) * user_speed)
        return script, refs, speed

    voice = get_voice(voice_id)
    script = voice_agent.build_single_script(text, voice)
    return script, [voice["fish_id"]], _clamp_speed(voice["speed"] * user_speed)


def _fit_and_mux(job: dict, kind: str, source_path: Path, source_duration: float,
                 take_path: Path, final_path: Path,
                 mono_path: Path | None = None) -> str:
    """Shared Post-Production logic: pacing, onset alignment, exact length fit.

    Three corrections, in order:
      1. atempo — re-pace the take to the source duration (only when the
         mismatch is above STRETCH_THRESHOLD),
      2. onset alignment — find where speech actually starts in the original
         and in the take, then pad/trim the take's head so both speech
         onsets coincide (this is what makes the video mouth "move with" words),
      3. exact length — pad/trim the tail so output duration == source
         duration (no drifting half-seconds).
    """
    note_parts = []
    voice_path = take_path
    voice_duration = media.probe(take_path)["duration"]

    # ---- 1. overall pacing ----
    if (
        STRETCH_ENABLED
        and source_duration > 0
        and voice_duration > 0
        and abs(source_duration - voice_duration) / source_duration > STRETCH_THRESHOLD
    ):
        # audio longer than the clip -> speed it up (factor > 1), and vice versa
        factor = max(STRETCH_FACTOR_MIN, min(STRETCH_FACTOR_MAX, voice_duration / source_duration))
        fitted = take_path.parent / (take_path.stem + "_fitted.wav")
        media.stretch_audio(take_path, fitted, factor)
        voice_path = fitted
        voice_duration = media.probe(fitted)["duration"]
        note_parts.append(f"paced ×{factor:.2f}")

    # ---- 2. speech-onset alignment (video lip-sync aid) ----
    target = source_duration if source_duration > 0 else (voice_duration or 0)
    if mono_path and target > 0 and voice_duration > 0:
        try:
            onset_src = media.speech_onset(mono_path)
            onset_take = media.speech_onset(voice_path)
        except media.MediaError:
            onset_src = onset_take = None
        if onset_src is not None and onset_take is not None:
            offset = onset_take - onset_src
            if abs(offset) > 0.05:   # only move it if it's off by >50ms
                aligned = take_path.parent / (take_path.stem + "_aligned.wav")
                trim_head = offset if offset > 0 else 0.0        # take started late -> cut its leading silence
                delay_head = -offset if offset < 0 else 0.0      # take started early -> pad its front
                media.fit_audio(voice_path, aligned, target, trim_head, delay_head)
                voice_path = aligned
                word = ("started %.0fms early — padded" % (-offset * 1000)) if offset < 0 \
                       else ("started %.0fms late — trimmed" % (offset * 1000))
                note_parts.append(f"speech onset {word}")

    # ---- 3. exact final length (pad/trim the tail) ----
    if target > 0:
        final_dur = media.probe(voice_path)["duration"]
        if abs(final_dur - target) > 0.05:   # >50ms — worth an exact pass
            exact = voice_path.parent / (voice_path.stem + "_exact.wav")
            media.fit_audio(voice_path, exact, target)
            voice_path = exact
            note_parts.append(f"length exact at {target:.1f}s")

    if kind == "video":
        media.remix_video(source_path, voice_path, final_path)
        note_parts.append("original video reused untouched")
    else:
        shutil.copyfile(voice_path, final_path)

    note = " • ".join(note_parts) if note_parts else "generated audio ready"
    return note, voice_path


# ---------------------------------------------------------------- the flows

def run_analyze(job: dict, upload_path: Path, filename: str, size: int) -> None:
    """Flow 1: Media Agent -> Dialogue Agent. Produces a reviewable transcript."""
    try:
        job["stages"] = {}
        job["flow"] = "analyze"
        job["status"] = "running"

        ext = media_agent.validate_file(filename, size)
        job["ext"] = ext

        # ---- stage 1: Media Agent ----
        set_stage(job, "media", "running", "Inspecting your media...")
        prepared = media_agent.prepare(Path(job["dir"]))
        label = "Video clip" if prepared["kind"] == "video" else "Audio clip"
        set_stage(job, "media", "done", f"{label} {prepared['duration']:.1f}s • audio prepared")

        # ---- stage 2: Dialogue Agent ----
        set_stage(job, "dialogue", "running", "Listening and transcribing...")
        transcript = dialogue_agent.transcribe(prepared["mono_path"])
        if not transcript["text"]:
            raise MediaAgentError("No speech was detected in this clip. Try one with clear dialogue.")
        set_stage(
            job,
            "dialogue",
            "done",
            f"{transcript['language']} • {transcript['speaker_count']} speaker(s) detected",
        )

        job["status"] = "done"
        job["result"] = {
            "kind": prepared["kind"],
            "duration": prepared["duration"],
            "text": transcript["text"],
            "raw_text": transcript["raw_text"],
            "language": transcript["language"],
            "speaker_count": transcript["speaker_count"],
            "media_url": f"/files/jobs/{job['id']}/upload{prepared['ext']}",
            "before_audio_url": f"/files/jobs/{job['id']}/original_mono.wav",
        }
    except Exception as exc:  # noqa: BLE001 — one pipeline, one honest catch-all
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        fail_current_stage(job)


def run_transform(job: dict, voice_id: str, transcript: str | None = None,
                  speaker_voices: dict[str, str] | None = None,
                  takes: int = 1, speed: float = 1.0) -> None:
    """Flow 2: Voice Agent (takes) -> Post-Production -> Dialogue Checker.

    Up to `takes` Drama 3 takes are generated and scored; the best-matching
    one is used for the final export. All takes stay available for preview
    and can be re-selected via run_select_take without paying for new TTS.
    """
    try:
        job["stages"] = {}
        job["flow"] = "transform"
        job["status"] = "running"
        job["_speed"] = speed

        text = (transcript or (job["result"].get("text") or "")).strip()
        if not text:
            raise MediaAgentError("No transcript to transform. Run the analysis step first.")
        takes = max(1, min(3, int(takes)))

        job_dir = Path(job["dir"])
        sources = list(job_dir.glob("upload.*"))
        if not sources:
            raise MediaAgentError("The original media file is missing. Upload the clip again.")
        source_path = sources[0]
        kind = job["result"].get("kind", "audio")
        source_duration = float(job["result"].get("duration") or 0)

        # A verification reference that mirrors the spoken words (no markers).
        reference_text = dialogue_agent.clean_text(text)

        # ---- stage 1: Voice Agent (Drama 3, N takes) ----
        set_stage(job, "voice", "running", "Directing Drama 3...")
        script, refs, final_speed = plan_voices(job, voice_id, speaker_voices, text)

        take_rows = []
        for i in range(takes):
            take_path = job_dir / f"take{i + 1}.wav"
            dur = voice_agent.generate_speech(script, refs, final_speed, take_path)
            check = voice_agent.verify(take_path, reference_text)
            take_rows.append({
                "take": i + 1,
                "url": f"/files/jobs/{job['id']}/{take_path.name}",
                "duration": dur,
                "similarity": check["similarity"],
                "generated_text": check["generated_text"],
                "needs_review": check["needs_review"],
            })
            # in-flow progress notes so the UI sees takes landing
            set_stage(job, "voice", "running",
                      f"Drama 3 take {i + 1}/{takes} • {dur:.1f}s • match {int(round(check['similarity'] * 100))}%")

        # ---- auto-paced extra take: if the take's performance runs way longer
        # or shorter than the source (>10%), atempo-compressing it would sound
        # rushed and break lip-sync. One extra Drama 3 pass with the prosody
        # speed adjusted by the measured ratio is a much more natural way to
        # recover the timing, then only minor atempo remains.
        if source_duration > 0:
            top = max(t["similarity"] for t in take_rows)
            pace_best = min(
                (t for t in take_rows if t["similarity"] >= top - 0.02),
                key=lambda t: abs(t["duration"] / source_duration - 1),
            )
            ratio = pace_best["duration"] / source_duration
            if abs(ratio - 1) > 0.10 and len(take_rows) < 4:
                adj = max(0.5, min(2.0, final_speed * ratio))
                extra_path = job_dir / f"take{len(take_rows) + 1}.wav"
                set_stage(job, "voice", "running", f"re-performing at {adj:.2f}× to match {source_duration:.1f}s...")
                dur = voice_agent.generate_speech(script, refs, adj, extra_path)
                check = voice_agent.verify(extra_path, reference_text)
                take_rows.append({
                    "take": len(take_rows) + 1,
                    "url": f"/files/jobs/{job['id']}/{extra_path.name}",
                    "duration": dur,
                    "similarity": check["similarity"],
                    "generated_text": check["generated_text"],
                    "needs_review": check["needs_review"],
                    "paced_generation": True,
                })
                if dur and abs(dur / source_duration - 1) < abs(pace_best["duration"] / source_duration - 1):
                    set_stage(job, "voice", "running",
                              f"auto-paced take {len(take_rows)} better fits the clip timing")

        def _best_row():
            top = max(t["similarity"] for t in take_rows)
            # among takes within ~2% of the best word match, prefer the one
            # whose timing runs closest to the source (lip-sync quality beats
            # a marginal text-score difference)
            candidates = [t for t in take_rows if t["similarity"] >= top - 0.02]
            if source_duration > 0:
                return min(candidates, key=lambda t: abs(t["duration"] / source_duration - 1))
            return max(take_rows, key=lambda t: t["similarity"])

        best = _best_row()
        best_path = job_dir / f"take{best['take']}.wav"
        set_stage(job, "voice", "done",
                  f"best take {best['take']} • {best['duration']:.1f}s • match {int(round(best['similarity'] * 100))}%")

        # ---- stage 2: Post-Production (mux / tempo fit) ----
        set_stage(job, "remix", "running", "Syncing the new voice...")
        out_ext = source_path.suffix if kind == "video" else ".wav"
        final_path = job_dir / f"final{out_ext}"
        remix_note, exported_voice = _fit_and_mux(job, kind, source_path, source_duration, best_path, final_path,
                                                  mono_path=job_dir / "original_mono.wav")
        final_duration = media.probe(final_path)["duration"]
        set_stage(job, "remix", "done", remix_note)

        # ---- stage 3: Dialogue Checker ----
        set_stage(job, "verify", "running", "Scoring the words...")
        pct = int(round(best["similarity"] * 100))
        set_stage(
            job,
            "verify",
            "done" if not best["needs_review"] else "warn",
            f"take {best['take']}: {pct}% match"
            + ("" if not best["needs_review"] else " — low match, review the transcript"),
        )

        job["status"] = "done"
        job["result"] = {
            **job["result"],
            "voice": get_voice(voice_id)["name"] if get_voice(voice_id) else voice_id,
            "voice_id": voice_id,
            "speakers": len(refs) if len(refs) > 1 else 1,
            "takes": take_rows,
            "selected_take": best["take"],
            "source_transcript": reference_text,
            "generated_text": best["generated_text"],
            "similarity": best["similarity"],
            "needs_review": best["needs_review"],
            "before_audio_url": f"/files/jobs/{job['id']}/original_mono.wav",
            # "after" preview uses the SYNCED audio (pace/onset/length corrected),
            # not the raw take — raw takes stay previewable in the chips
            "after_audio_url": f"/files/jobs/{job['id']}/{exported_voice.name}",
            "output_url": f"/files/jobs/{job['id']}/{final_path.name}",
            "output_name": f"voicemorph_{voice_id}{out_ext}",
            "speed": final_speed,
            "duration_before": source_duration,
            "duration_after": final_duration,
        }
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        fail_current_stage(job)


def run_select_take(job: dict, take: int) -> None:
    """Re-run only Post-Production for a previously generated take (no new TTS)."""
    try:
        job["stages"] = {}
        job["flow"] = "transform"
        job["status"] = "running"

        job_dir = Path(job["dir"])
        take_rows = job["result"].get("takes") or []
        row = next((t for t in take_rows if t["take"] == take), None)
        if not row:
            raise MediaAgentError(f"Take {take} isn't available for this job.")

        set_stage(job, "voice", "done", f"using existing take {take} (no new generation)")
        set_stage(job, "remix", "running", "Re-syncing...")

        sources = list(job_dir.glob("upload.*"))
        if not sources:
            raise MediaAgentError("The original media file is missing. Upload the clip again.")
        source_path = sources[0]
        kind = job["result"].get("kind", "audio")
        source_duration = float(job["result"].get("duration") or 0)
        out_ext = source_path.suffix if kind == "video" else ".wav"
        final_path = job_dir / f"final{out_ext}"

        remix_note, exported_voice = _fit_and_mux(job, kind, source_path, source_duration,
                                                  job_dir / f"take{take}.wav", final_path,
                                                  mono_path=job_dir / "original_mono.wav")
        final_duration = media.probe(final_path)["duration"]
        set_stage(job, "remix", "done", remix_note)
        set_stage(job, "verify", "done",
                  f"Dialogue match: {int(round(row['similarity'] * 100))}% (take {take})")

        job["status"] = "done"
        job["result"] = {
            **job["result"],
            "selected_take": take,
            "after_audio_url": f"/files/jobs/{job['id']}/{exported_voice.name}",
            "generated_text": row["generated_text"],
            "similarity": row["similarity"],
            "needs_review": row["needs_review"],
            "output_url": f"/files/jobs/{job['id']}/{final_path.name}",
            "duration_after": final_duration,
        }
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        fail_current_stage(job)
