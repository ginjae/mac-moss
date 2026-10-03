const $ = (selector) => document.querySelector(selector);
const state = {
  notes: [], current: null, saveTimer: null, pollTimer: null,
  pendingChanges: null, savePromise: null, noteRequest: 0, editRevision: 0, openingNote: null,
  transcriptView: "edited", maxUploadBytes: null, defaultChunkMinutes: 30, uploading: false, deletingId: null,
  elapsedAnchor: null, elapsedTimer: null,
  uploadPreparation: null, cancellingId: null, restartingId: null, cancellationNoteId: null, deletionNoteId: null,
};
const colors = ["#17ad6d", "#e49a3d", "#6878df", "#db667a", "#2ca5b7", "#9c67c5"];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `요청 실패 (${response.status})`;
    try {
      const detail = (await response.json()).detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = "입력 내용을 확인해 주세요.";
    } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  const payload = await response.json();
  const receivedAt = performance.now();
  for (const note of Array.isArray(payload) ? payload : [payload]) {
    if (note?.id && note.status) {
      Object.defineProperty(note, "_elapsedReceivedAt", { value: receivedAt, configurable: true });
    }
  }
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.className = "toast", 4000);
}

function formatDate(value) {
  return new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function clock(seconds = 0) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [ ...(h ? [h] : []), m, s ].map(part => String(part).padStart(2, "0")).join(":");
}

function stopElapsedClock() {
  clearTimeout(state.elapsedTimer);
  state.elapsedTimer = null;
  state.elapsedAnchor = null;
}

function elapsedSeconds(anchor, now = performance.now()) {
  return anchor.seconds + (anchor.running ? Math.max(0, now - anchor.at) / 1000 : 0);
}

function renderElapsedClock() {
  clearTimeout(state.elapsedTimer);
  state.elapsedTimer = null;
  const anchor = state.elapsedAnchor;
  if (!anchor) return;
  const seconds = elapsedSeconds(anchor);
  $("#progress-elapsed").textContent = `${anchor.total ? "총 경과" : "현재 구간 경과"} ${clock(seconds)}`;
  if (anchor.running) {
    // Schedule the next displayed second, independently of HTTP polling.
    const delay = Math.max(16, Math.ceil((Math.floor(seconds) + 1 - seconds) * 1000));
    state.elapsedTimer = setTimeout(renderElapsedClock, delay);
  }
}

function syncElapsedClock(note) {
  const model = note.model_progress || {};
  const total = note.processing_elapsed_seconds != null || note.processing_seconds != null;
  const sample = total ? (note.processing_elapsed_seconds ?? note.processing_seconds) : model.elapsed_seconds;
  const element = $("#progress-elapsed");
  if (sample == null || note.status === "queued") {
    stopElapsedClock();
    element.classList.add("hidden");
    element.textContent = "";
    return;
  }
  element.classList.remove("hidden");
  const previous = state.elapsedAnchor;
  if (previous?.note !== note) {
    const now = performance.now();
    const key = total ? `${note.id}:${note.processing_started_at}`
      : `${note.id}:${model.kind}:${model.chunk_index}`;
    const running = note.status === "processing";
    const at = note._elapsedReceivedAt ?? now;
    // Avoid stepping backward by a few milliseconds when a new snapshot arrives.
    const seconds = running && previous?.key === key
      ? Math.max(Number(sample), elapsedSeconds(previous, at)) : Number(sample);
    state.elapsedAnchor = { note, key, total, seconds, at, running };
  }
  renderElapsedClock();
}

function renderCompletionTime(note) {
  const element = $("#transcription-time");
  const known = note.status === "done" && note.processing_seconds != null;
  element.classList.toggle("hidden", !known);
  element.textContent = known
    ? `전사 소요 ${note.processing_seconds < 1 ? "1초 미만" : clock(Math.round(note.processing_seconds))}` : "";
}

function statusText(status) {
  return { queued: "대기 중", processing: "전사 중", done: "완료", error: "오류", cancelled: "취소됨" }[status] || status;
}

function transcriptionProgress(note) {
  const phase = note.progress_phase || (note.status === "processing" ? "normalizing" : note.status);
  const total = Math.max(0, note.progress_total_steps || 0);
  const completed = Math.max(0, Math.min(note.progress_completed_steps || 0, total));
  const indeterminate = ["queued", "normalizing"].includes(phase) || (!total && note.status === "processing");
  const fraction = note.duration > 0
    ? (note.processed_audio_seconds || 0) / note.duration
    : total ? completed / total : 0;
  const percent = note.status === "done" ? 100 : Math.max(0, Math.min(99, Math.floor(fraction * 100)));
  return { phase, total, completed, indeterminate, percent };
}

function noteStatus(note) {
  const progress = transcriptionProgress(note);
  return note.status === "processing" && !progress.indeterminate ? `전사 중 ${progress.percent}%` : statusText(note.status);
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function renderList() {
  const query = $("#note-search").value.trim().toLowerCase();
  const notes = state.notes.filter(note => note.title.toLowerCase().includes(query));
  $("#note-count").textContent = state.notes.length;
  $("#note-list").innerHTML = notes.length ? notes.map(note => `
    <button class="note-item ${state.current?.id === note.id ? "active" : ""}" data-id="${escapeHtml(note.id)}" title="${escapeHtml(note.title)}" aria-pressed="${state.current?.id === note.id}">
      <b>${escapeHtml(note.title)}</b>
      <small><span>${formatDate(note.created_at)}</span><span class="note-status ${note.status}">${noteStatus(note)}</span></small>
    </button>`).join("") : `<div class="empty-list">${query ? "검색 결과가 없습니다" : "첫 노트를 만들어 보세요"}</div>`;
  document.querySelectorAll(".note-item").forEach(button => button.addEventListener("click", () => openNote(button.dataset.id)));
}

async function loadNotes() {
  try {
    state.notes = await api("/api/notes");
    $("#welcome-upload").firstChild.textContent = state.notes.length ? "새 녹음 올리기 " : "첫 녹음 올리기 ";
    renderList();
    if (!state.pollTimer && state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  } catch (error) { toast(error.message, true); }
}

async function openNote(id, quiet = false) {
  if (state.deletingId) return;
  const request = ++state.noteRequest;
  state.openingNote = request;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  closeExport();
  try {
    await flushSave();
    if (request !== state.noteRequest) return;
    let note;
    let revision;
    do {
      revision = state.editRevision;
      note = await api(`/api/notes/${encodeURIComponent(id)}`);
      await flushSave();
      if (request !== state.noteRequest) return;
    } while (state.current?.id === id && state.editRevision !== revision);
    if (state.current?.id !== id) {
      state.transcriptView = "edited";
      $("#transcript-search").value = "";
    }
    state.current = note;
    $("#welcome").classList.add("hidden");
    $("#workspace").classList.remove("hidden");
    renderWorkspace();
    renderList();
    if (["queued", "processing"].includes(note.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  } catch (error) {
    if (!quiet) toast(error.message, true);
    if (["queued", "processing"].includes(state.current?.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  } finally {
    if (state.openingNote === request) state.openingNote = null;
  }
}

function renderTranscriptionControls(note) {
  const button = $("#cancel-transcription");
  const cancelling = state.cancellingId === note.id;
  const restarting = state.restartingId === note.id;
  const busy = Boolean(state.cancellingId || state.restartingId);
  button.classList.toggle("hidden", !["queued", "processing"].includes(note.status));
  button.disabled = busy;
  button.textContent = cancelling ? "취소 중…" : "전사 취소";
  const restart = $("#restart-transcription");
  restart.classList.toggle("hidden", !["cancelled", "error"].includes(note.status));
  restart.disabled = busy;
  restart.textContent = restarting ? "다시 시작 중…" : "전사 다시 시작";
  $("#note-title").disabled = cancelling || restarting;
  $("#delete-note").disabled = busy || ["queued", "processing"].includes(note.status);
}

function renderWorkspace() {
  const note = state.current;
  $("#note-title").value = state.pendingChanges?.id === note.id
    ? state.pendingChanges.changes.title ?? note.title : note.title;
  const chunkLength = note.chunk_seconds ? ` · 청크 ${note.chunk_seconds / 60}분` : "";
  const chunkMeta = `${chunkLength}${note.chunk_count > 1 ? ` · ${note.chunk_count}개 구간으로 나누어 전사` : ""}`;
  $("#note-meta").textContent = `${formatDate(note.created_at)} · ${note.original_filename}${chunkMeta}`;
  const pending = ["queued", "processing"].includes(note.status);
  $("#processing-card").classList.toggle("hidden", !pending && !["error", "cancelled"].includes(note.status));
  $("#processing-card").classList.toggle("failed", note.status === "error");
  $("#processing-card").classList.toggle("cancelled", note.status === "cancelled");
  $("#editor").classList.toggle("hidden", note.status !== "done");
  $("#export-toggle").disabled = note.status !== "done";
  renderTranscriptionControls(note);
  renderProgress(note);
  if (pending) {
    const phase = transcriptionProgress(note).phase;
    $("#processing-title").textContent = {
      queued: "전사 순서를 기다리고 있어요",
      normalizing: "녹음 파일을 준비하고 있어요",
      transcribing: "음성을 기록으로 옮기고 있어요",
      linking: "같은 화자의 발화를 연결하고 있어요",
      finalizing: "전사 결과를 저장하고 있어요",
    }[phase] || "음성을 읽고 있어요";
    $("#processing-copy").textContent = phase === "queued" ? "앞선 작업이 끝나면 자동으로 시작됩니다." : "페이지를 닫아도 앱이 실행 중이면 작업은 계속됩니다.";
  } else if (note.status === "error") {
    $("#processing-title").textContent = "전사하지 못했습니다";
    $("#processing-copy").textContent = note.error || "전사 서버와 파일을 확인한 뒤 ‘전사 다시 시작’을 눌러 주세요.";
  } else if (note.status === "cancelled") {
    $("#processing-title").textContent = "전사를 취소했습니다";
    $("#processing-copy").textContent = "녹음 파일은 보관되어 있습니다. ‘전사 다시 시작’을 누르면 기존 파일로 처음부터 다시 처리합니다.";
  } else if (note.status === "done") {
    renderEditor();
  }
}

function renderProgress(note) {
  syncElapsedClock(note);
  const { phase, total, indeterminate, percent } = transcriptionProgress(note);
  const failed = note.status === "error";
  const stopped = failed || note.status === "cancelled";
  const waiting = ["queued", "normalizing"].includes(phase);
  const model = note.model_progress || {};
  $("#processing-card").classList.toggle("waiting", waiting);
  $("#processing-progress").classList.toggle("hidden", stopped && !total);
  $("#progress-stage").textContent = {
    queued: "대기열에 등록됨", normalizing: "음성 파일 준비", transcribing: "음성 전사",
    linking: "화자 연결", finalizing: "결과 저장", done: "전사 완료", error: "작업 중단", cancelled: "전사 취소됨",
  }[phase] || "음성 전사";
  $("#progress-percent").textContent = waiting ? (phase === "queued" ? "대기 중" : "준비 중") : `${percent}%`;
  const track = $("#progress-track");
  track.classList.toggle("indeterminate", indeterminate && !stopped);
  if (indeterminate && !stopped) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", phase === "queued" ? "전사 순서를 기다리는 중" : "음성 파일을 준비하는 중");
  } else {
    track.setAttribute("aria-valuenow", String(percent));
    track.setAttribute("aria-valuetext", `${percent}%${stopped ? ", 작업 중단" : " 완료"}`);
  }
  $("#progress-fill").style.width = indeterminate && !stopped ? "" : `${percent}%`;
  const count = $("#progress-count");
  if (note.status === "cancelled") count.textContent = "전사 작업이 중단되었습니다.";
  else if (phase === "queued") count.textContent = "순서가 되면 자동으로 시작합니다.";
  else if (phase === "normalizing") count.textContent = "음성 추출과 파일 정규화 중";
  else if (phase === "linking") count.textContent = `${note.processed_bridges || 0}/${note.bridge_count || 0}개 경계 화자 연결 완료`;
  else if (phase === "finalizing") count.textContent = "화자와 문장을 정리해 노트에 저장 중";
  else if (model.chunk_index && model.kind === "main") count.textContent = `${model.chunk_index}/${note.chunk_count || 1}번째 구간 전사 중`;
  else count.textContent = `${note.processed_chunks || 0}/${note.chunk_count || 1}개 구간 전사 완료`;
  $("#progress-audio").textContent = !waiting && note.duration
    ? `${clock(note.processed_audio_seconds || 0)} / ${clock(note.duration)} 전사`
    : "";
  const hasModelProgress = !!model.stage && !waiting && !stopped;
  $("#model-progress").classList.toggle("hidden", !hasModelProgress);
  const inputPercent = model.prompt_total > 0
    ? Math.min(100, Math.floor((model.prompt_processed || 0) / model.prompt_total * 100)) : 0;
  const modelText = {
    waiting: "전사 서버에서 시작을 기다리고 있습니다",
    encoding: "음성의 특징을 분석하고 있습니다",
    prefill: `음성 입력 처리 ${inputPercent}%`,
    decoding: `전사 문장 생성 중 · ${(model.generation_tokens || 0).toLocaleString("ko-KR")}토큰 생성`,
    complete: "현재 구간 전사 완료",
  }[model.stage] || "";
  $("#model-progress-text").textContent = modelText;
  $("#prefill-track").classList.toggle("hidden", model.stage !== "prefill");
  $("#prefill-track").setAttribute("aria-valuenow", String(inputPercent));
  $("#prefill-track").setAttribute("aria-valuetext", `음성 입력 ${inputPercent}% 처리`);
  $("#prefill-fill").style.width = `${inputPercent}%`;
  const stage = { queued: -1, normalizing: 0, transcribing: 1, linking: 2, finalizing: 2, done: 3, error: -1, cancelled: -1 }[phase] ?? 1;
  document.querySelectorAll("[data-step]").forEach(element => {
    const index = Number(element.dataset.step);
    element.classList.toggle("completed", phase === "done" || index < stage);
    element.classList.toggle("current", index === stage && phase !== "done");
    if (index === stage && phase !== "done") element.setAttribute("aria-current", "step");
    else element.removeAttribute("aria-current");
  });
  $("#progress-detail").textContent = phase === "queued" ? "이 페이지를 열어 두지 않아도 작업은 시작됩니다."
    : phase === "normalizing" ? "준비가 끝나면 전사 진행률을 표시합니다."
    : stopped ? "작업을 중단한 시점의 진행률입니다."
    : "생성된 전사의 시간표시를 기준으로 갱신합니다. 화자 정리와 저장이 끝나면 100%가 됩니다.";
}

function displayedSegments() {
  return (state.transcriptView === "raw" ? state.current?.raw_segments : state.current?.segments) || [];
}

function fitText(element) {
  element.style.height = "auto";
  element.style.height = `${element.scrollHeight}px`;
}

function renderEditor() {
  const note = state.current;
  const segments = displayedSegments();
  const editable = state.transcriptView === "edited";
  const speakers = [...new Set(segments.map(segment => segment.speaker))];
  const allSpeakers = [...new Set([...(note.raw_segments || []), ...note.segments].map(segment => segment.speaker))];
  const speakerIndex = Object.fromEntries(allSpeakers.map((speaker, index) => [speaker, index]));
  const media = $("#media-player");
  const mediaUrl = `/api/notes/${encodeURIComponent(note.id)}/media`;
  if (media.getAttribute("src") !== mediaUrl) media.src = mediaUrl;
  $("#file-pill").textContent = note.original_filename;
  $("#duration-label").textContent = note.duration ? `녹음 ${clock(note.duration)}` : "";
  renderCompletionTime(note);
  $("#editing-hint").textContent = editable ? "본문과 화자 이름을 눌러 수정할 수 있습니다." : "처음 전사한 본문입니다. 원본은 그대로 보존됩니다.";
  document.querySelectorAll("[data-transcript-view]").forEach(button => {
    const active = button.dataset.transcriptView === state.transcriptView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $("#chunk-notice").classList.toggle("hidden", note.chunk_count <= 1);
  $("#chunk-notice").textContent = `긴 녹음을 ${note.chunk_count}개 구간으로 나누어 전사했습니다. 경계에서 확인되지 않은 화자는 여러 이름으로 표시될 수 있습니다. 화자 이름을 수정해 정리할 수 있습니다.`;
  $("#speaker-panel").classList.toggle("hidden", !speakers.length);
  $("#speaker-panel").innerHTML = speakers.map(speaker => `
    <label class="speaker-chip" style="--speaker-color:${colors[speakerIndex[speaker] % colors.length]}">
      <i></i><small>${escapeHtml(speaker)}</small><input data-speaker="${escapeHtml(speaker)}" value="${escapeHtml(note.speaker_names[speaker] || speaker)}" maxlength="30" aria-label="${escapeHtml(speaker)} 화자 이름" />
    </label>`).join("");
  $("#segments").innerHTML = segments.map((segment, index) => `
    <article class="segment" data-index="${index}" data-start="${segment.start}" data-end="${segment.end}">
      <button class="time-button" data-seek="${segment.start}" aria-label="${clock(segment.start)}부터 재생">${clock(segment.start)}</button>
      <div class="speaker-label"><i class="speaker-dot" style="--speaker-color:${colors[speakerIndex[segment.speaker] % colors.length]}"></i><span>${escapeHtml(note.speaker_names[segment.speaker] || segment.speaker)}</span></div>
      <div class="segment-text-wrap"><textarea class="segment-text" rows="1" ${editable ? "" : "readonly"} aria-label="${clock(segment.start)} ${escapeHtml(note.speaker_names[segment.speaker] || segment.speaker)} ${editable ? "전사 내용 편집" : "전사 원본"}">${escapeHtml(segment.text)}</textarea></div>
    </article>`).join("");
  bindEditorEvents();
  filterTranscript();
  document.querySelectorAll(".segment-text").forEach(fitText);
  updateSaveState();
}

function bindEditorEvents() {
  document.querySelectorAll("[data-seek]").forEach(button => button.addEventListener("click", async () => {
    const media = $("#media-player");
    media.currentTime = Number(button.dataset.seek);
    try { await media.play(); } catch (_) { toast("녹음 파일을 재생할 수 없습니다.", true); }
  }));
  document.querySelectorAll(".segment-text:not([readonly])").forEach(element => element.addEventListener("input", () => {
    state.current.segments[Number(element.closest(".segment").dataset.index)].text = element.value;
    fitText(element);
    queueSave({ segments: state.current.segments });
    filterTranscript();
  }));
  document.querySelectorAll("[data-speaker]").forEach(input => input.addEventListener("input", () => {
    state.current.speaker_names[input.dataset.speaker] = input.value.trim() || input.dataset.speaker;
    const segments = displayedSegments();
    document.querySelectorAll(".segment").forEach((row, index) => {
      const speaker = segments[index].speaker;
      row.querySelector(".speaker-label span").textContent = state.current.speaker_names[speaker] || speaker;
    });
    queueSave({ speaker_names: state.current.speaker_names });
    filterTranscript();
  }));
}

function updateSaveState(failed = false) {
  const pending = Boolean(state.pendingChanges || state.savePromise);
  $("#save-state").textContent = failed ? "저장 실패" : pending ? "저장 중…" : "저장됨";
  $("#save-state").className = `save-state${failed ? " failed" : pending ? " saving" : ""}`;
  $("#retry-save").classList.toggle("hidden", !failed);
}

function queueSave(changes) {
  ++state.editRevision;
  state.pendingChanges = {
    id: state.current.id,
    changes: { ...(state.pendingChanges?.changes || {}), ...structuredClone(changes) },
  };
  updateSaveState();
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushSave().catch(() => {}), 650);
}

async function flushSave() {
  clearTimeout(state.saveTimer);
  if (state.savePromise) {
    await state.savePromise;
    return flushSave();
  }
  const pending = state.pendingChanges;
  if (!pending) return;
  state.pendingChanges = null;
  const changes = pending.changes;
  if ("title" in changes) {
    changes.title = changes.title.trim();
    if (!changes.title) {
      if (state.current?.id === pending.id) $("#note-title").value = state.current.title;
      delete changes.title;
    }
  }
  if (!Object.keys(changes).length) return updateSaveState();
  state.savePromise = api(`/api/notes/${encodeURIComponent(pending.id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
  }).then(async note => {
    if (state.current?.id === pending.id && !state.pendingChanges) state.current = note;
    await loadNotes();
  }).catch(error => {
    state.pendingChanges = {
      id: pending.id,
      changes: { ...changes, ...(state.pendingChanges?.changes || {}) },
    };
    updateSaveState(true);
    toast(`저장하지 못했습니다. ${error.message}`, true);
    throw error;
  }).finally(() => { state.savePromise = null; });
  await state.savePromise;
  if (state.pendingChanges) return flushSave();
  updateSaveState();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    const id = state.current?.id;
    const previous = state.current?.status;
    if (id && state.cancellingId !== id && state.restartingId !== id && !state.openingNote && ["queued", "processing"].includes(previous)) await openNote(id, true);
    await loadNotes();
    if (id && state.current?.id === id && previous !== "done" && state.current.status === "done") toast("전사가 완료되었습니다.");
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }, 1000);
}

async function openHome() {
  if (state.deletingId) return;
  const request = ++state.noteRequest;
  state.openingNote = request;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  try {
    await flushSave();
    if (request !== state.noteRequest) return;
    state.current = null;
    stopElapsedClock();
    state.transcriptView = "edited";
    const media = $("#media-player");
    media.pause();
    media.removeAttribute("src");
    media.load();
    closeExport();
    $("#transcript-search").value = "";
    $("#workspace").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    renderList();
    window.scrollTo({ top: 0, left: 0 });
  } catch (_) {
    // flushSave keeps the draft and reports the error before navigation.
  } finally {
    if (state.openingNote === request) state.openingNote = null;
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}

$("#go-home").addEventListener("click", event => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openHome();
});

function openUpload() {
  if (state.deletingId || $("#upload-dialog").open) return;
  const input = $("#chunk-minutes");
  input.defaultValue = state.defaultChunkMinutes;
  if (!input.value) input.value = state.defaultChunkMinutes;
  $("#upload-dialog").showModal();
}
$("#open-upload").addEventListener("click", openUpload);
$("#welcome-upload").addEventListener("click", openUpload);
$("#close-upload").addEventListener("click", () => $("#upload-dialog").close());
$("#upload-dialog").addEventListener("cancel", event => { if (state.uploading) event.preventDefault(); });
$("#upload-dialog").addEventListener("close", () => {
  if (state.uploading) return;
  cancelFilePreparation();
  $("#upload-form").reset();
  resetDropZone();
});
$("#upload-dialog").addEventListener("click", event => {
  if (event.target !== event.currentTarget || state.uploading) return;
  const rect = event.currentTarget.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.currentTarget.close();
});
$("#note-search").addEventListener("input", renderList);
$("#note-title").addEventListener("input", event => { if (state.current) queueSave({ title: event.target.value }); });
$("#retry-save").addEventListener("click", () => flushSave().catch(() => {}));
document.querySelectorAll("[data-transcript-view]").forEach(button => button.addEventListener("click", async () => {
  if (!state.current || button.dataset.transcriptView === state.transcriptView) return;
  const id = state.current.id;
  try {
    await flushSave();
    if (state.current?.id !== id) return;
    state.transcriptView = button.dataset.transcriptView;
    renderEditor();
  } catch (_) {}
}));

const dropZone = $("#drop-zone");
const fileInput = $("#audio-file");
function resetDropZone() {
  $("#drop-title").textContent = "파일을 선택하거나 여기에 놓으세요";
  const limit = state.maxUploadBytes ? ` · 최대 ${+(state.maxUploadBytes / 1024 ** 3).toFixed(2)} GB` : "";
  $("#drop-copy").textContent = `MP3, WAV, M4A, MP4 등 · 긴 녹음은 자동 분할${limit}`;
  renderFilePreparation();
}
function validateFile(file) {
  if (!file) throw new Error("전사할 음성 또는 영상 파일을 선택해 주세요.");
  if (state.maxUploadBytes && file.size > state.maxUploadBytes) throw new Error(`파일이 업로드 제한(${+(state.maxUploadBytes / 1024 ** 3).toFixed(2)} GB)을 초과합니다.`);
  if (file.size === 0) throw new Error("빈 파일은 전사할 수 없습니다.");
}
function releaseFileMetadata(preparation) {
  if (preparation?.audio) {
    preparation.audio.removeAttribute("src");
    preparation.audio.load();
    preparation.audio = null;
  }
  if (preparation?.objectUrl) {
    URL.revokeObjectURL(preparation.objectUrl);
    preparation.objectUrl = null;
  }
}
function deletePreparedFile(id) {
  if (id) return api(`/api/uploads/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
}
function cancelFilePreparation() {
  const preparation = state.uploadPreparation;
  state.uploadPreparation = null;
  if (!preparation) return;
  clearTimeout(preparation.timer);
  preparation.controller.abort();
  releaseFileMetadata(preparation);
  deletePreparedFile(preparation.id);
}
function renderFilePreparation() {
  const preparation = state.uploadPreparation;
  $("#upload-preparation").classList.toggle("hidden", !preparation);
  $("#upload-preparation").classList.toggle("failed", preparation?.status === "error");
  $("#upload-form").querySelector("button[type=submit]").disabled = state.uploading || preparation?.status !== "ready";
  $("#retry-preparation").classList.toggle("hidden", preparation?.status !== "error");
  if (!preparation) return;
  $("#upload-duration").textContent = Number.isFinite(preparation.duration) && preparation.duration > 0
    ? `음성 길이 ${clock(preparation.duration)}` : "음성 길이 확인 중…";
  $("#upload-preparation-status").textContent = {
    uploading: "파일 업로드 중…", preparing: "전처리 중…",
    ready: "전처리 완료 · 전사를 시작할 수 있어요", error: preparation.error,
  }[preparation.status];
}
async function pollFilePreparation(preparation) {
  if (state.uploadPreparation !== preparation) return;
  try {
    const upload = await api(`/api/uploads/${encodeURIComponent(preparation.id)}`, { signal: preparation.controller.signal });
    if (state.uploadPreparation !== preparation) return;
    preparation.status = upload.status;
    if (Number.isFinite(upload.duration) && upload.duration > 0) preparation.duration = upload.duration;
    preparation.error = upload.error;
    renderFilePreparation();
    if (upload.status === "preparing") preparation.timer = setTimeout(() => pollFilePreparation(preparation), 1000);
  } catch (error) {
    if (state.uploadPreparation !== preparation) return;
    preparation.status = "error";
    preparation.error = error.message;
    renderFilePreparation();
  }
}
async function showFile(file) {
  cancelFilePreparation();
  if (!file) return resetDropZone();
  try { validateFile(file); } catch (error) {
    fileInput.value = "";
    resetDropZone();
    return toast(error.message, true);
  }
  $("#drop-title").textContent = file.name;
  $("#drop-copy").textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB`;
  const preparation = { file, id: null, status: "uploading", duration: null, controller: new AbortController() };
  state.uploadPreparation = preparation;
  renderFilePreparation();
  // Browser metadata can show duration before the upload completes. ffprobe
  // supplies the authoritative duration for formats the browser cannot read.
  const audio = new Audio();
  preparation.audio = audio;
  preparation.objectUrl = URL.createObjectURL(file);
  audio.preload = "metadata";
  audio.addEventListener("loadedmetadata", () => {
    if (state.uploadPreparation === preparation && Number.isFinite(audio.duration) && audio.duration > 0) {
      preparation.duration = audio.duration;
      renderFilePreparation();
    }
    releaseFileMetadata(preparation);
  }, { once: true });
  audio.addEventListener("error", () => releaseFileMetadata(preparation), { once: true });
  audio.src = preparation.objectUrl;
  try {
    const body = new FormData();
    body.set("file", file);
    const upload = await api("/api/uploads", { method: "POST", body, signal: preparation.controller.signal });
    if (state.uploadPreparation !== preparation) {
      deletePreparedFile(upload.id);
      return;
    }
    preparation.id = upload.id;
    preparation.status = upload.status;
    if (Number.isFinite(upload.duration) && upload.duration > 0) preparation.duration = upload.duration;
    preparation.error = upload.error;
    renderFilePreparation();
    if (upload.status === "preparing") await pollFilePreparation(preparation);
  } catch (error) {
    if (state.uploadPreparation !== preparation) return;
    preparation.status = "error";
    preparation.error = error.message;
    renderFilePreparation();
  }
}
$("#retry-preparation").addEventListener("click", () => showFile(fileInput.files[0]));
fileInput.addEventListener("change", () => showFile(fileInput.files[0]));
["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); if (!state.uploading) dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", event => {
  if (state.uploading || !event.dataTransfer.files.length) return;
  const files = new DataTransfer();
  files.items.add(event.dataTransfer.files[0]);
  fileInput.files = files.files;
  showFile(fileInput.files[0]);
});

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.uploading) return;
  try { validateFile(fileInput.files[0]); } catch (error) { return toast(error.message, true); }
  const preparation = state.uploadPreparation;
  if (preparation?.status !== "ready") return toast("전처리가 완료된 후 전사를 시작해 주세요.", true);
  const form = event.target;
  const body = new FormData(form);
  body.delete("file");
  body.set("upload_id", preparation.id);
  state.uploading = true;
  const button = form.querySelector("button[type=submit]");
  const fields = [...form.elements];
  fields.forEach(field => field.disabled = true);
  button.firstChild.textContent = "전사 시작 중… ";
  try {
    await flushSave();
    const note = await api("/api/notes", { method: "POST", body });
    releaseFileMetadata(preparation);
    state.uploadPreparation = null;
    $("#upload-dialog").close();
    form.reset();
    resetDropZone();
    $("#note-search").value = "";
    await loadNotes();
    await openNote(note.id);
    toast("전사 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
  finally {
    state.uploading = false;
    fields.forEach(field => field.disabled = false);
    button.firstChild.textContent = "전사 시작하기 ";
    renderFilePreparation();
  }
});

function filterTranscript() {
  if (!state.current) return;
  const query = $("#transcript-search").value.trim().toLowerCase();
  const segments = displayedSegments();
  let visible = 0;
  document.querySelectorAll(".segment").forEach((row, index) => {
    const segment = segments[index];
    const speaker = state.current.speaker_names[segment.speaker] || segment.speaker;
    const match = !query || `${speaker} ${segment.text}`.toLowerCase().includes(query);
    row.classList.toggle("filtered", !match);
    if (match) { visible++; fitText(row.querySelector(".segment-text")); }
  });
  const speakers = new Set(segments.map(segment => segment.speaker)).size;
  $("#segment-count").textContent = query ? `${visible}/${segments.length}개 구간` : `${segments.length}개 구간 · 화자 ${speakers}명`;
  $("#transcript-empty").classList.toggle("hidden", visible > 0);
  $("#transcript-empty").textContent = segments.length ? "검색 결과가 없습니다. 다른 단어로 검색해 보세요." : "인식된 발화가 없습니다. 녹음에 음성이 있는지 확인해 주세요.";
}
$("#transcript-search").addEventListener("input", filterTranscript);
window.addEventListener("resize", () => document.querySelectorAll(".segment:not(.filtered) .segment-text").forEach(fitText));
$("#media-player").addEventListener("timeupdate", event => {
  const time = event.target.currentTime;
  document.querySelectorAll(".segment").forEach(row => row.classList.toggle("active", time >= Number(row.dataset.start) && time < Number(row.dataset.end)));
});

function closeExport() {
  $("#export-options").classList.remove("open");
  $("#export-toggle").setAttribute("aria-expanded", "false");
}
$("#export-toggle").addEventListener("click", () => {
  const open = $("#export-options").classList.toggle("open");
  $("#export-toggle").setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", event => { if (!event.target.closest(".export-menu")) closeExport(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") closeExport(); });
document.querySelectorAll("[data-format]").forEach(button => button.addEventListener("click", async () => {
  closeExport();
  if (!state.current) return;
  const id = state.current.id;
  const version = state.transcriptView;
  try {
    await flushSave();
    const link = document.createElement("a");
    link.href = `/api/notes/${encodeURIComponent(id)}/export/${button.dataset.format}?version=${version}`;
    link.download = "";
    document.body.append(link);
    link.click();
    link.remove();
  } catch (_) {}
}));

function bindConfirmationDialog(selector, close) {
  const dialog = $(selector);
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener("close", () => {
    if (!dialog.open) close();
  });
  dialog.addEventListener("click", event => {
    if (event.target !== event.currentTarget) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
}

function openCancellationDialog() {
  if (!state.current || state.cancellingId || state.restartingId || state.deletingId) return;
  if (!["queued", "processing"].includes(state.current.status)) return;
  const dialog = $("#cancel-dialog");
  if (dialog.open) return;
  state.cancellationNoteId = state.current.id;
  $("#cancel-dialog-note-title").textContent = state.current.title;
  dialog.showModal();
}

function closeCancellationDialog() {
  state.cancellationNoteId = null;
  const dialog = $("#cancel-dialog");
  if (dialog.open) dialog.close();
}

async function cancelTranscription(noteId) {
  if (!state.current || state.cancellingId || state.restartingId || state.deletingId) return;
  if (state.current.id !== noteId) return;
  if (!["queued", "processing"].includes(state.current.status)) return;
  const id = state.current.id;
  state.cancellingId = id;
  ++state.noteRequest;
  renderTranscriptionControls(state.current);
  try {
    await flushSave();
    const note = await api(`/api/notes/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (state.current?.id === id) state.current = note;
    await loadNotes();
    toast("전사를 취소했습니다.");
  } catch (error) {
    toast(`전사를 취소하지 못했습니다. ${error.message}`, true);
  } finally {
    state.cancellingId = null;
    if (state.current?.id === id) renderWorkspace();
    else if (state.current) renderTranscriptionControls(state.current);
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}
$("#cancel-transcription").addEventListener("click", openCancellationDialog);
$("#keep-transcribing").addEventListener("click", closeCancellationDialog);
$("#close-cancel-dialog").addEventListener("click", closeCancellationDialog);
$("#confirm-cancel-transcription").addEventListener("click", async () => {
  const noteId = state.cancellationNoteId;
  closeCancellationDialog();
  if (noteId) await cancelTranscription(noteId);
});
bindConfirmationDialog("#cancel-dialog", closeCancellationDialog);

async function restartTranscription() {
  if (!state.current || state.restartingId || state.cancellingId || state.deletingId) return;
  if (!["cancelled", "error"].includes(state.current.status)) return;
  const id = state.current.id;
  state.restartingId = id;
  ++state.noteRequest;
  renderTranscriptionControls(state.current);
  try {
    await flushSave();
    const note = await api(`/api/notes/${encodeURIComponent(id)}/restart`, { method: "POST" });
    if (state.current?.id === id) state.current = note;
    await loadNotes();
    toast("전사 대기열에 다시 추가했습니다.");
  } catch (error) {
    toast(`다시 시작하지 못했습니다. ${error.message}`, true);
  } finally {
    state.restartingId = null;
    if (state.current?.id === id) renderWorkspace();
    else if (state.current) renderTranscriptionControls(state.current);
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}
$("#restart-transcription").addEventListener("click", restartTranscription);

function openDeletionDialog() {
  if (!state.current || state.deletingId || state.cancellingId || state.restartingId) return;
  if (["queued", "processing"].includes(state.current.status)) return;
  const dialog = $("#delete-dialog");
  if (dialog.open) return;
  state.deletionNoteId = state.current.id;
  $("#delete-dialog-note-title").textContent = state.pendingChanges?.id === state.current.id
    ? state.pendingChanges.changes.title ?? state.current.title : state.current.title;
  dialog.showModal();
}

function closeDeletionDialog() {
  state.deletionNoteId = null;
  const dialog = $("#delete-dialog");
  if (dialog.open) dialog.close();
}

async function deleteNote(noteId) {
  if (!state.current || state.deletingId || state.cancellingId || state.restartingId) return;
  if (state.current.id !== noteId || ["queued", "processing"].includes(state.current.status)) return;
  const note = state.current;
  state.deletingId = note.id;
  ++state.noteRequest;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  const controls = [...document.querySelectorAll(".app-shell button, #workspace input, #workspace textarea")];
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => control.disabled = true);
  try {
    await flushSave();
    await api(`/api/notes/${encodeURIComponent(note.id)}`, { method: "DELETE" });
    if (state.pendingChanges?.id === note.id) state.pendingChanges = null;
    clearTimeout(state.saveTimer);
    if (state.current?.id === note.id) {
      state.current = null;
      $("#media-player").pause();
      $("#media-player").removeAttribute("src");
      $("#workspace").classList.add("hidden");
      $("#welcome").classList.remove("hidden");
    }
    await loadNotes();
    toast("노트를 삭제했습니다.");
  } catch (error) { toast(error.message, true); }
  finally {
    state.deletingId = null;
    controls.forEach((control, index) => control.disabled = disabled[index]);
    if (["queued", "processing"].includes(state.current?.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  }
}
$("#delete-note").addEventListener("click", openDeletionDialog);
$("#keep-note").addEventListener("click", closeDeletionDialog);
$("#close-delete-dialog").addEventListener("click", closeDeletionDialog);
$("#confirm-delete-note").addEventListener("click", async () => {
  const noteId = state.deletionNoteId;
  closeDeletionDialog();
  if (noteId) await deleteNote(noteId);
});
bindConfirmationDialog("#delete-dialog", closeDeletionDialog);

async function checkHealth() {
  const element = $("#server-state");
  try {
    const health = await api("/api/health");
    const online = health.moss.ok;
    state.maxUploadBytes = health.max_upload_bytes;
    const defaultMinutes = health.default_chunk_seconds / 60;
    if (Number.isInteger(defaultMinutes) && defaultMinutes >= 1 && defaultMinutes <= 90) {
      state.defaultChunkMinutes = defaultMinutes;
      $("#chunk-minutes").defaultValue = defaultMinutes;
    }
    if (health.max_chunk_seconds) $("#chunk-minutes").max = health.max_chunk_seconds / 60;
    if (!fileInput.files.length) resetDropZone();
    element.className = `server-state ${online ? "online" : "offline"}`;
    element.querySelector("b").textContent = online ? "전사 준비 완료" : "전사 서버 연결 안 됨";
    element.querySelector("small").textContent = online ? "MLX · 내 Mac에서 처리" : "scripts/start-mac.sh로 실행하세요";
  } catch (_) {
    element.className = "server-state offline";
    element.querySelector("b").textContent = "앱 연결을 확인해 주세요";
    element.querySelector("small").textContent = "잠시 후 다시 연결합니다";
  }
}
window.addEventListener("beforeunload", event => {
  if (state.pendingChanges || state.savePromise || state.uploading) event.preventDefault();
});
loadNotes();
checkHealth();
setInterval(checkHealth, 10000);
