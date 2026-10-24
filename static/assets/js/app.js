/* ============================================================
   VoiceMorph AI — frontend logic (vanilla JS, no build step)
   ------------------------------------------------------------
   Flow:  upload → poll job (analyze) → review transcript →
          pick voice (+ takes/speed/speakers) → transform →
          compare takes → export
   ============================================================ */

const el = (id) => document.getElementById(id);

const state = {
  jobId: null,
  flow: null,
  voices: [],
  selectedVoice: "atlas",
  transcriptEdited: false,
  transcriptFilled: false,
  pollTimer: null,
  previewAudio: null,
  limits: null,
  lastWaves: { before: null, after: null },
};

/* ---------------------------------------------------------- helpers */

function toast(message, type = "info", ms = 4200) {
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = message;
  el("toast-root").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

async function api(path, options = {}) {
  let res, body;
  try {
    res = await fetch(path, options);
    body = await res.json();
  } catch {
    throw new Error("Can't reach the VoiceMorph server — is it still running?");
  }
  if (!res.ok) throw new Error(body.detail || body.message || `Request failed (${res.status})`);
  return body;
}

function show(node, yes = true) { node.classList.toggle("hidden", !yes); }

function fmtDuration(s) {
  const v = Number(s) || 0;
  return `${v.toFixed(1)}s`;
}

/* ---------------------------------------------------------- theme */

function wireTheme() {
  el("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("vm-theme", next);
    // repaint waveforms that are already drawn so they use the new palette
    for (const key of ["before", "after"]) {
      const url = state.lastWaves[key];
      if (url && waveCache.has(url)) paint(el(key === "before" ? "wave-before" : "wave-after"), waveCache.get(url));
    }
  });
}

/* ---------------------------------------------------------- health */

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const chip = el("chip-health");
    if (h.ready) {
      chip.className = "status ok";
      chip.innerHTML = '<span class="dot"></span>ready';
    } else {
      chip.className = "status bad";
      chip.innerHTML = '<span class="dot"></span>setup needed';
      showBanner(`Server setup incomplete: ${h.missing.join(" · ")}`, true);
    }
    document.querySelector(".js-limit-mb").textContent = `${h.limits.max_upload_mb} MB`;
    document.querySelector(".js-limit-dur").textContent = `${h.limits.max_duration_s} s`;
    state.limits = h.limits;
  } catch {
    el("chip-health").className = "status bad";
  }
}

function showBanner(message, persistent = false) {
  const b = el("warn-banner");
  b.textContent = message;
  show(b, true);
  if (!persistent) setTimeout(() => show(b, false), 8000);
}

/* ---------------------------------------------------------- stepper */

function renderStepper(nodeId, stages) {
  const box = el(nodeId);
  box.innerHTML = "";
  for (const st of stages) {
    const row = document.createElement("div");
    row.className = `step ${st.status}`;
    const label = { pending: "", running: "", done: "done", warn: "review", error: "failed" }[st.status] || "";
    row.innerHTML = `
      <span class="s-name"></span>
      ${st.note ? '<span class="s-note"></span>' : ''}
      <span class="s-status"></span>`;
    row.querySelector(".s-name").textContent = st.name;
    if (st.note) row.querySelector(".s-note").textContent = st.note;
    row.querySelector(".s-status").textContent = label;
    box.appendChild(row);
  }
}

/* ---------------------------------------------------------- voices */

const VOICE_INITIAL = { atlas: "A", nova: "N", vera: "V", ignis: "I" };

async function loadVoices() {
  try {
    state.voices = await api("/api/voices");
  } catch (e) {
    showBanner(`Could not load voice profiles: ${e.message}`);
    return;
  }
  if (!state.voices.find((v) => v.id === state.selectedVoice) && state.voices[0]) {
    state.selectedVoice = state.voices[0].id;
  }
  const grid = el("voice-grid");
  grid.innerHTML = "";
  for (const v of state.voices) {
    const card = document.createElement("div");
    card.className = `voice-card ${v.id === state.selectedVoice ? "selected" : ""}`;
    card.dataset.voiceId = v.id;
    card.innerHTML = `
      <span class="vc-check">✓</span>
      <div class="vc-top">
        <div class="vc-mark"></div>
        <div>
          <p class="vc-name"></p>
          <p class="vc-persona"></p>
        </div>
      </div>
      <p class="vc-desc"></p>
      <div class="vc-row">
        <span class="tag">${v.tags[0]}</span><span class="tag">${v.tags[1] || ""}</span>
        <button class="btn-play" title="Hear a live sample" aria-label="Preview voice">▶</button>
      </div>`;
    card.querySelector(".vc-mark").textContent = VOICE_INITIAL[v.id] || v.name[0].toUpperCase();
    card.querySelector(".vc-name").textContent = v.name;
    card.querySelector(".vc-persona").textContent = v.persona;
    card.querySelector(".vc-desc").textContent = v.description;

    card.addEventListener("click", () => selectVoice(v.id));
    card.querySelector(".btn-play").addEventListener("click", (e) => {
      e.stopPropagation();
      playPreview(v.id, e.currentTarget);
    });
    grid.appendChild(card);
  }
}

function selectVoice(id) {
  state.selectedVoice = id;
  document.querySelectorAll(".voice-card").forEach((c) => {
    c.classList.toggle("selected", c.dataset.voiceId === id);
  });
  // keep speaker mapping selects in sync when the user hasn't customized them
  document.querySelectorAll("#speaker-map select").forEach((s) => {
    if (s.dataset.auto === "1") s.value = id;
  });
  rebuildSpeakerMap(true);
}

async function playPreview(voiceId, btn) {
  if (state.previewAudio) state.previewAudio.pause();
  btn.classList.add("loading");
  try {
    const res = await fetch(`/api/voices/${voiceId}/preview`);
    if (!res.ok) {
      let msg = "Preview failed.";
      if (res.headers.get("content-type")?.includes("json")) {
        const j = await res.json();
        msg = j.message || msg;
      }
      throw new Error(msg);
    }
    const blob = await res.blob();
    if (!btn.dataset.src) btn.dataset.src = URL.createObjectURL(blob);
    const audio = new Audio(btn.dataset.src);
    await audio.play();
    state.previewAudio = audio;
  } catch (err) {
    toast(err.message, "error");
  }
  btn.classList.remove("loading");
}

/* ---------------------------------------------------------- upload */

function wireDropzone() {
  const dz = el("dropzone");
  const input = el("file-input");

  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") input.click(); });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  input.addEventListener("change", () => { if (input.files.length) handleFile(input.files[0]); });

  el("btn-clear-upload").addEventListener("click", () => location.reload());
}

const ALLOWED_EXT = ["mp4", "mov", "mkv", "webm", "m4v", "avi", "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus"];

function handleFile(file) {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  const maxMb = (state.limits && state.limits.max_upload_mb) || 100;
  if (!ALLOWED_EXT.includes(ext)) {
    showBanner("That file type isn't supported. Use MP4/MOV/WEBM/MKV/MP3/WAV/M4A/AAC/FLAC/OGG.");
    return;
  }
  if (file.size > maxMb * 1024 * 1024) {
    showBanner(`File is larger than the ${maxMb} MB limit.`);
    return;
  }
  startUpload(file);
}

function startUpload(file) {
  show(el("upload-progress"), true);
  el("meter-fill").style.width = "0%";

  const fd = new FormData();
  fd.append("file", file, file.name);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.addEventListener("progress", (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      el("meter-fill").style.width = `${pct}%`;
      el("meter-label").textContent = pct < 100 ? `Uploading… ${pct}%` : "Processing…";
    }
  });
  xhr.addEventListener("load", () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch { /* handled below */ }
    if (xhr.status === 200) {
      state.jobId = body.id;
      state.flow = "analyze";
      state.transcriptEdited = false;
      state.transcriptFilled = false;
      show(el("sec-analyze"), true);
      show(el("transcript-panel"), false);
      show(el("sec-voice"), false);
      renderStepper("analyze-steps", [
        { name: "Media Agent", note: "", status: "running" },
        { name: "Dialogue Agent", note: "", status: "pending" },
      ]);
      startPolling();
    } else {
      const msg = (body && (body.detail || body.message)) || `Upload failed (${xhr.status}).`;
      showBanner(msg);
      show(el("upload-progress"), false);
    }
  });
  xhr.addEventListener("error", () => {
    showBanner("Network error while uploading — check the server and try again.");
    show(el("upload-progress"), false);
  });
  xhr.send(fd);
}

/* ---------------------------------------------------------- polling */

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    if (!state.jobId) return stopPolling();
    let job;
    try {
      job = await api(`/api/jobs/${state.jobId}`);
    } catch (err) {
      toast(err.message, "error");
      stopPolling();
      return;
    }
    onJobUpdate(job);
  }, 1100);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function onJobUpdate(job) {
  state.flow = job.flow;
  if (job.status === "error") stopPolling();

  if (job.flow === "analyze") {
    renderStepper("analyze-steps", job.stages);
    if (job.status === "done") onAnalyzeDone(job);
    if (job.status === "error") {
      const b = el("transcript-error");
      b.textContent = `Analysis failed: ${job.error || "unknown error"}`;
      show(b, true);
      enableTransform(false, "Fix the analysis problem first.");
      renderHistory();
    }
  } else {
    show(el("sec-result"), true);
    renderStepper("transform-steps", job.stages);
    if (job.status === "done") {
      stopPolling();
      const ok = job.stages.every((s) => ["done", "warn"].includes(s.status));
      if (ok) showResult(job);
    }
    if (job.status === "error") {
      stopPolling();
      const b = el("transform-error");
      b.textContent = `Transformation failed: ${job.error || "unknown error"}`;
      show(b, true);
      enableTransform(true, "Adjust the transcript or voice, then try again.");
    }
  }
  renderHistory();
}

/* ---------------------------------------------------------- speaker map */

const SPEAKER_RE = /<\|speaker:(\d+)\|>/g;

function detectedSpeakers(text) {
  const seen = [];
  for (const m of text.matchAll(SPEAKER_RE)) {
    if (!seen.includes(m[1])) seen.push(m[1]);
  }
  return seen;
}

function rebuildSpeakerMap(resetAuto) {
  const panel = el("speaker-map");
  const speakers = detectedSpeakers(el("transcript").value);
  if (speakers.length === 0) { show(panel, false); return; }

  panel.innerHTML = "";
  const label = document.createElement("p");
  label.className = "row-label";
  label.textContent = `Voices per speaker (${speakers.length} detected)`;
  panel.appendChild(label);

  for (const sp of speakers) {
    const row = document.createElement("div");
    row.className = "sp-row";
    const select = document.createElement("select");
    select.dataset.speaker = sp;
    for (const v of state.voices) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = `${v.name} · ${v.persona}`;
      select.appendChild(opt);
    }
    if (resetAuto) { select.dataset.auto = "1"; select.value = state.selectedVoice; }
    select.addEventListener("change", () => { select.dataset.auto = "0"; });
    const name = document.createElement("span");
    name.textContent = `Speaker ${Number(sp) + 1}`;
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = "color-mix(in srgb, var(--accent) 60%, transparent)";
    row.appendChild(name);
    row.appendChild(document.createTextNode(" "));
    row.appendChild(swatch);
    row.appendChild(select);
    panel.appendChild(row);
  }
  show(panel, true);
}

/* ---------------------------------------------------------- job flows */

function onAnalyzeDone(job) {
  stopPolling();
  const r = job.result;

  show(el("transcript-panel"), true);
  show(el("transcript-error"), false);
  show(el("sec-voice"), true);
  scrollIntoViewCard(el("sec-voice"));

  const kind = r.kind;
  show(el("media-preview"), true);
  const mediaEl = kind === "video" ? el("preview-video") : el("preview-audio");
  el("preview-video").classList.toggle("hidden", kind !== "video");
  el("preview-audio").classList.toggle("hidden", kind === "video");
  mediaEl.src = r.media_url;
  el("meta-kind").textContent = kind === "video" ? "Video" : "Audio";

  const meta = el("analysis-meta");
  meta.innerHTML = "";
  addTag(meta, `${fmtDuration(r.duration)}`);
  addTag(meta, r.language || "Unknown language");
  const spk = r.speaker_count || 1;
  addTag(meta, spk > 1 ? `${spk} speakers` : "1 speaker", spk > 1 ? "warn" : "");

  if (spk > 1) {
    showBanner(
      `This clip contains ${spk} speakers. Assign a different voice to each below — Drama 3 will perform the scene with multiple voices in one take.`,
      true
    );
  }

  const ta = el("transcript");
  if (!state.transcriptEdited) {
    // For multi-speaker clips keep the raw text (with <|speaker:N|> tags) so the
    // mapping survives editing. For single speakers show the clean transcript.
    ta.value = (spk > 1 ? r.raw_text : r.text) || "";
    state.transcriptFilled = true;
  }
  ta.addEventListener("input", () => { state.transcriptEdited = true; rebuildSpeakerMap(true); });

  rebuildSpeakerMap(true);
  enableTransform(true, "Pick a voice, choose takes, transform.");
}

function addTag(parent, text, cls = "") {
  const t = document.createElement("span");
  t.className = `tag ${cls}`;
  t.textContent = text;
  parent.appendChild(t);
}

function enableTransform(enabled, hint) {
  el("btn-transform").disabled = !enabled;
  el("transform-hint").textContent = hint || "";
}

/* ---------------------------------------------------------- transform */

function wireTransform() {
  el("opt-speed").addEventListener("input", (e) => {
    el("speed-val").textContent = `${Number(e.currentTarget.value).toFixed(2)}×`;
  });

  el("btn-transform").addEventListener("click", async () => {
    if (!state.jobId) return;
    show(el("transform-error"), false);
    show(el("result-panel"), false);
    show(el("sec-result"), true);
    enableTransform(false, "Transforming…");
    scrollIntoViewCard(el("sec-result"));

    const ta = el("transcript").value.trim();
    const speakers = detectedSpeakers(ta);
    const speakerVoices = {};
    if (speakers.length > 1) {
      document.querySelectorAll("#speaker-map select").forEach((s) => {
        speakerVoices[s.dataset.speaker] = s.value;
      });
    }

    const payload = {
      voice_id: state.selectedVoice,
      transcript: ta,
      takes: parseInt(el("opt-takes").value, 10) || 1,
      speed: parseFloat(el("opt-speed").value) || 1.0,
      speaker_voices: speakerVoices,
    };
    try {
      const job = await api(`/api/jobs/${state.jobId}/transform`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onJobUpdate(job);
      startPolling();
    } catch (err) {
      toast(err.message, "error");
      enableTransform(true, "Something went wrong — try again.");
    }
  });

  el("btn-retry").addEventListener("click", () => {
    show(el("result-panel"), false);
    show(el("sec-result"), false);
    enableTransform(true, "Pick a different voice and transform again.");
    scrollIntoViewCard(el("sec-voice"));
  });

  el("btn-reset").addEventListener("click", () => location.reload());
}

function scrollIntoViewCard(node) {
  node.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---------------------------------------------------------- result */

function showResult(job) {
  const r = job.result;
  show(el("result-panel"), true);
  scrollIntoViewCard(el("sec-result"));
  el("result-sub").textContent = `${r.voice} · ${r.speakers > 1 ? `${r.speakers} voices · ` : ""}speed ${r.speed ? Number(r.speed).toFixed(2) + "×" : "1.00×"}`;

  // takes
  if ((r.takes || []).length > 1) {
    show(el("takes-row"), true);
    const box = el("takes");
    box.innerHTML = "";
    for (const t of r.takes) {
      const chip = document.createElement("div");
      chip.className = `take-chip ${t.take === r.selected_take ? "active" : ""}`;
      const pct = Math.round((t.similarity || 0) * 100);
      chip.innerHTML = `<b>Take ${t.take}</b><span>${pct}% · ${fmtDuration(t.duration)}${t.paced_generation ? " · paced" : ""}</span>`;
      const use = document.createElement("button");
      use.className = "btn-use";
      use.textContent = t.take === r.selected_take ? "exported" : "export this";
      if (t.take === r.selected_take) use.disabled = true;
      use.addEventListener("click", (e) => { e.stopPropagation(); selectTake(t.take); });
      chip.appendChild(use);
      chip.addEventListener("click", () => {
        el("audio-after").src = t.url;
        drawWaveform(el("wave-after"), t.url);
      });
      box.appendChild(chip);
    }
  } else {
    show(el("takes-row"), false);
  }

  // similarity
  const pct = Math.round((r.similarity || 0) * 100);
  const low = r.needs_review ?? pct < 85;
  addTag(el("result-meta"), `match ${pct}%`, low ? "warn" : "");
  addTag(el("result-meta"), r.kind === "video" ? "video sync done" : "audio only");
  addTag(el("result-meta"), `before ${fmtDuration(r.duration_before)} → after ${fmtDuration(r.duration_after)}`);

  // transcripts
  el("tc-before").textContent = r.source_transcript || r.text || "";
  el("tc-after").textContent = r.generated_text || "";
  const note = el("tc-match-note");
  note.textContent = low ? "· review recommended" : "· verified";
  note.className = `match-note ${low ? "low" : "ok"}`;

  // audio compare
  el("audio-before").src = r.before_audio_url;
  el("audio-after").src = r.after_audio_url;
  drawWaveform(el("wave-before"), r.before_audio_url);
  drawWaveform(el("wave-after"), r.after_audio_url);
  state.lastWaves = { before: r.before_audio_url, after: r.after_audio_url };

  // output preview
  const isVideo = r.kind === "video";
  show(el("output-video"), isVideo);
  show(el("output-audio"), !isVideo);
  if (isVideo) el("output-video").src = r.output_url;
  else el("output-audio").src = r.output_url;

  const dl = el("btn-download");
  dl.href = r.output_url;
  dl.setAttribute("download", r.output_name || "voicemorph_output");

  const da = el("btn-download-audio");
  da.href = r.after_audio_url;
  da.setAttribute("download", `voicemorph_${r.voice_id || "voice"}.wav`);
}

async function selectTake(take) {
  try {
    const job = await api(`/api/jobs/${state.jobId}/select-take`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ take }),
    });
    toast(`Exporting take ${take}…`, "ok");
    onJobUpdate(job);
    startPolling();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------------------------------------------------------- history */

async function renderHistory() {
  let rows;
  try { rows = await api("/api/jobs"); } catch { return; }
  if (!rows.length) return;
  show(el("sec-history"), true);
  const list = el("history-list");
  list.innerHTML = "";
  for (const h of rows) {
    const row = document.createElement("div");
    row.className = "h-row";
    const cls = h.status === "done" ? "ok" : h.status === "error" ? "low" : "";
    const pct = h.similarity != null ? Math.round(h.similarity * 100) : null;
    row.innerHTML = `
      <span class="h-status">${h.status}</span>
      <span class="h-desc"></span>
      ${pct != null ? `<span class="h-match">${pct}%</span>` : ""}`;
    row.querySelector(".h-desc").textContent =
      `${h.kind === "video" ? "Video" : "Audio"}${h.voice ? " · " + h.voice : ""}${h.preview ? " · “" + h.preview + "”" : ""}`;
    row.addEventListener("click", () => openHistoryJob(h.id, h.status));
    list.appendChild(row);
  }
}

async function openHistoryJob(id, status) {
  try {
    const job = await api(`/api/jobs/${id}`);
    if (job.status !== "done") { toast("That job didn't finish successfully.", "error"); return; }
    state.jobId = id;
    if (job.result.voice) {
      // completed transform: show the result view directly
      show(el("sec-analyze"), true);
      show(el("transcript-panel"), true);
      show(el("transcript-error"), false);
      show(el("sec-voice"), true);
      const ta = el("transcript");
      ta.value = (job.result.speakers > 1 ? (job.result.raw_text || job.result.source_transcript) : job.result.source_transcript) || "";
      show(el("media-preview"), false);
      if (job.stages && job.stages.length && job.stages[0].note) {
        renderStepper("analyze-steps", job.stages.map((s, i) => ({
          name: ["Transcript", "Takes", "Sync", "Check"][i] || s.name || "stage",
          note: s.note, status: "done",
        })));
      }
      show(el("sec-result"), true);
      renderStepper("transform-steps", job.stages);
      showResult(job);
      scrollIntoViewCard(el("sec-result"));
      state.transcriptEdited = false;
      rebuildSpeakerMap(true);
    } else {
      // analysis only: restore transcript + voices
      onAnalyzeDone(job);
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------------------------------------------------------- waveforms */

const waveCache = new Map();

async function drawWaveform(canvas, url) {
  if (waveCache.has(url)) { paint(canvas, waveCache.get(url)); return; }
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error();
    const buf = await (await res.blob()).arrayBuffer();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const audio = await ctx.decodeAudioData(buf);
    ctx.close();
    const ch = audio.getChannelData(0);
    const samples = 90;
    const step = Math.floor(ch.length / samples) || 1;
    const peaks = new Float32Array(samples);
    for (let i = 0; i < samples; i++) {
      let peak = 0;
      const base = i * step;
      for (let j = base; j < base + step && j < ch.length; j += 8) {
        const v = Math.abs(ch[j]);
        if (v > peak) peak = v;
      }
      peaks[i] = peak;
    }
    waveCache.set(url, peaks);
    paint(canvas, peaks);
  } catch {
    canvas.style.display = "none";
  }
}

function paint(canvas, peaks) {
  const dpr = window.devicePixelRatio || 1;
  const h = 56;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 300;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.height = `${h}px`;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const color = css.getPropertyValue("--accent").trim() || "#6a8dff";
  const waveTint = css.getPropertyValue("--wave").trim() || "#6a8dff33";
  ctx.fillStyle = waveTint;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = color;

  const bw = w / peaks.length;
  for (let i = 0; i < peaks.length; i++) {
    const barH = Math.max(2, peaks[i] * (h * 0.86));
    const x = i * bw + bw * 0.18;
    const y = (h - barH) / 2;
    const bwid = bw * 0.64;
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(x, y, bwid, barH, bwid / 2);
      ctx.fill();
    } else {
      ctx.fillRect(x, y, bwid, barH);
    }
  }
}

/* ------------------------------------------------------------ boot */

async function boot() {
  wireTheme();
  wireDropzone();
  wireTransform();
  await checkHealth();
  await loadVoices();
  await renderHistory();
}

boot();
