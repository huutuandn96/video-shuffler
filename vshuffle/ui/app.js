"use strict";

const $ = (sel) => document.querySelector(sel);
const api = () => window.pywebview.api;

const ICON = {
  x: '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>',
};

const STATUS_TEXT = {
  probing: "Đang đọc video…",
  ready: "Sẵn sàng",
  queued: "Đang chờ",
  rendering: "Đang render",
  done: "Xong",
  error: "Lỗi",
  cancelled: "Đã huỷ",
};
const EDITABLE = new Set(["ready", "queued", "done", "error", "cancelled"]);

let settings = {};
let state = { jobs: [], running: false };
let batch = new Set();       // id các video trong lượt render hiện tại
const cards = new Map();     // id -> phần tử DOM

// ---------- tiện ích ----------

function fmtTime(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function resLabel(info) {
  const short = Math.min(info.width, info.height);
  const base = short >= 2160 ? "4K" : short >= 1440 ? "1440p" : short >= 1080 ? "1080p" : short >= 720 ? "720p" : `${short}p`;
  return info.height > info.width ? `${base} dọc` : base;
}

function shortPath(p) {
  // dấu LRM giữ "~" ở đầu khi khung hiển thị dùng direction: rtl (để cắt "…" ở đầu đường dẫn)
  return p ? "\u200E" + p.replace(/^\/Users\/[^/]+/, "~") + "\u200E" : "";
}

let toastTimer;
function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

// ---------- thẻ video ----------

function createCard(job) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="thumb loading"><div class="bgimg"></div><img alt="" hidden><span class="dur" hidden></span></div>
    <div class="body">
      <div class="name"></div>
      <div class="meta-line"><div class="meta"></div><button class="tag time-tag" data-act="timing" hidden title="Thời lượng clip và độ dài đoạn cắt của video này"></button><button class="tag wm-tag" data-act="wm" hidden title="Watermark của video này / từng clip"></button></div>
      <div class="status"><span class="pill"></span><span class="msg"></span></div>
      <div class="bar" hidden><i></i></div>
    </div>
    <div class="right">
      <div class="done-actions" hidden>
        <button class="btn small" data-act="open">${ICON.folder}Hiện trong Finder</button>
        <button class="btn small ghost" data-act="play">${ICON.play}Xem</button>
      </div>
      <div class="counter" hidden>
        <span class="counter-label">Số clip</span>
        <div class="stepper">
          <button data-act="dec" title="Bớt 1 clip">−</button>
          <input type="number" min="1">
          <button data-act="inc" title="Thêm 1 clip">+</button>
        </div>
        <button class="max-toggle" data-act="max" title="Số clip nhiều nhất mà không lặp cảnh">Tối đa</button>
        <button class="repeat-note" data-act="max" hidden></button>
      </div>
      <div class="actions">
        <button class="icon-btn" data-act="retry" title="Render lại" hidden>${ICON.retry}</button>
        <button class="icon-btn" data-act="cancel" title="Huỷ" hidden>${ICON.stop}</button>
        <button class="icon-btn" data-act="remove" title="Xoá khỏi danh sách">${ICON.x}</button>
      </div>
    </div>`;
  el.querySelector(".name").textContent = job.name;
  el.querySelector(".name").title = job.path;

  el.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const j = state.jobs.find((x) => x.id === job.id);
    if (!j) return;
    switch (btn.dataset.act) {
      case "dec": api().set_clips(j.id, Math.max(1, j.clips - 1)); break;
      case "inc": api().set_clips(j.id, Math.min(j.clipLimit, j.clips + 1)); break;
      case "max": if (!j.clipsAuto) api().set_clips(j.id, null); break;
      case "remove": api().remove(j.id); break;
      case "wm": openJobWatermark(j); break;
      case "timing": openJobTiming(j); break;
      case "cancel": api().cancel(j.id); break;
      case "retry": batchAdd(j.id); api().retry(j.id); break;
      case "open": if (j.outputs[0]) api().reveal(j.outputs[0]); else api().open_path(j.outDir); break;
      case "play": if (j.outputs[0]) api().open_path(j.outputs[0]); break;
    }
  });
  const input = el.querySelector(".stepper input");
  input.addEventListener("change", () => {
    const v = parseInt(input.value, 10);
    if (v > 0) api().set_clips(job.id, v);
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  return el;
}

function updateCard(el, job, queuePos) {
  el.className = `card ${job.status}`;

  // ảnh thu nhỏ + thông tin
  const thumb = el.querySelector(".thumb");
  const img = thumb.querySelector("img");
  if (job.thumb && img.dataset.src !== String(job.id)) {
    img.src = job.thumb;
    img.dataset.src = String(job.id);
    img.hidden = false;
    thumb.querySelector(".bgimg").style.backgroundImage = `url(${job.thumb})`;
    thumb.classList.remove("loading");
  }
  if (!job.thumb && job.status === "error") thumb.classList.remove("loading");

  const dur = thumb.querySelector(".dur");
  const meta = el.querySelector(".meta");
  if (job.info) {
    dur.hidden = false;
    dur.textContent = fmtTime(job.info.duration);
    const key = JSON.stringify(job.info);
    if (meta.dataset.key !== key) {
      meta.dataset.key = key;
      const i = job.info;
      meta.innerHTML = [resLabel(i), `${i.fps} fps`, i.codec, i.hdr ? "HDR" : null]
        .filter(Boolean)
        .map((t) => `<span class="tag${t === "HDR" ? " hdr" : ""}">${t}</span>`).join("");
    }
  }

  // nhãn thời lượng
  const tTag = el.querySelector(".time-tag");
  tTag.hidden = !job.info;
  if (!tTag.hidden) {
    const t = timingTagText(job);
    tTag.textContent = t.text;
    tTag.classList.toggle("on", t.on);
    tTag.classList.toggle("bad", !!job.timingError);
    tTag.disabled = job.status === "rendering";
  }

  // nhãn watermark
  const wmTag = el.querySelector(".wm-tag");
  wmTag.hidden = !job.info || job.maxClips < 1;
  if (!wmTag.hidden) {
    const t = wmTagText(job);
    wmTag.textContent = t.text;
    wmTag.classList.toggle("on", t.on);
    wmTag.disabled = job.status === "rendering";
  }

  // trạng thái
  const pill = el.querySelector(".pill");
  pill.className = `pill ${job.status}`;
  pill.textContent = job.status === "queued" && queuePos ? `Đang chờ · #${queuePos}` : STATUS_TEXT[job.status];
  if (job.status === "ready" && job.timingError) {
    pill.className = "pill error";
    pill.textContent = "Cần chỉnh";
  }
  const msg = el.querySelector(".msg");
  msg.className = "msg";
  let text = "";
  if (job.status === "rendering") {
    const pct = Math.round(job.progress * 100);
    text = `Clip ${Math.max(1, job.currentClip)}/${job.totalClips} · ${pct}%`;
    const elapsed = Date.now() / 1000 - job.startedAt;
    if (job.progress > 0.03) text += ` · còn ~${fmtTime(elapsed * (1 - job.progress) / job.progress)}`;
  } else if (job.status === "done") {
    const reps = job.outputs.filter((p) => p.endsWith("_lap.mp4")).length;
    text = `${job.outputs.length} clip${reps ? ` (${reps} lặp cảnh)` : ""} · ${fmtTime(job.elapsed)} · seed ${job.seed}`;
  } else if (job.status === "error") {
    text = job.message;
    msg.classList.add("err");
    msg.title = job.message;
  } else if (job.info && job.timingError) {
    text = job.timingError;
    msg.classList.add("err");
    msg.title = job.timingError;
  } else if (job.info) {
    const extra = job.clips - job.maxClips;
    const sec = fmtSec(job.timing.clipSeconds);
    text = extra > 0
      ? `${job.maxClips} clip không lặp + ${extra} clip lặp cảnh × ${sec}`
      : `${job.clips} clip × ${sec}`;
  }
  msg.textContent = text;

  const bar = el.querySelector(".bar");
  bar.hidden = !(job.status === "rendering" || job.status === "done");
  bar.querySelector("i").style.width = `${Math.round(job.progress * 100)}%`;

  // số clip
  const counter = el.querySelector(".counter");
  const editable = EDITABLE.has(job.status) && job.maxClips > 0 && job.status !== "done";
  counter.hidden = !editable;
  if (editable) {
    const input = counter.querySelector("input");
    input.max = job.clipLimit;
    if (document.activeElement !== input) input.value = job.clips;
    counter.querySelector('[data-act="dec"]').disabled = job.clips <= 1;
    counter.querySelector('[data-act="inc"]').disabled = job.clips >= job.clipLimit;
    const extra = job.clips - job.maxClips;
    counter.querySelector(".stepper").classList.toggle("over", extra > 0);
    const mt = counter.querySelector(".max-toggle");
    mt.classList.toggle("on", job.clipsAuto);
    mt.hidden = extra > 0;
    mt.textContent = job.clipsAuto ? `Tối đa (${job.maxClips})` : `Dùng tối đa (${job.maxClips})`;
    const note = counter.querySelector(".repeat-note");
    note.hidden = extra <= 0;
    if (extra > 0) {
      const from = job.maxClips + 1;
      note.textContent = extra === 1 ? `Clip ${from} lặp cảnh` : `Clip ${from}–${job.clips} lặp cảnh`;
      note.title = `Clip 1–${job.maxClips} không lặp cảnh. Bấm để về ${job.maxClips} clip.`;
    }
  }

  el.querySelector(".done-actions").hidden = job.status !== "done";
  el.querySelector('[data-act="retry"]').hidden = !["done", "error", "cancelled"].includes(job.status) || !job.info;
  el.querySelector('[data-act="cancel"]').hidden = !["rendering", "queued"].includes(job.status);
  el.querySelector('[data-act="remove"]').hidden = job.status === "rendering";
}

// ---------- toàn bộ giao diện ----------

function batchAdd(id) { batch.add(id); }

function render() {
  const jobs = state.jobs;
  const list = $("#list");
  const ids = new Set(jobs.map((j) => j.id));
  for (const [id, el] of cards) {
    if (!ids.has(id)) { el.remove(); cards.delete(id); batch.delete(id); }
  }
  let pos = 0;
  jobs.forEach((job) => {
    let el = cards.get(job.id);
    if (!el) { el = createCard(job); cards.set(job.id, el); }
    if (list.children[jobs.indexOf(job)] !== el) list.insertBefore(el, list.children[jobs.indexOf(job)] || null);
    if (job.status === "queued" || job.status === "rendering") batch.add(job.id);
    updateCard(el, job, job.status === "queued" ? ++pos : 0);
  });

  $("#dropzone").hidden = jobs.length > 0;
  $("#queue").hidden = jobs.length === 0;
  $("#clearBtn").hidden = !jobs.some((j) => ["done", "error", "cancelled"].includes(j.status));

  const pending = jobs.filter((j) => ["ready", "queued", "rendering"].includes(j.status) && j.maxClips > 0);
  const nClips = pending.reduce((a, j) => a + j.clips, 0);
  const doneCount = jobs.filter((j) => j.status === "done").length;
  $("#queueSummary").textContent =
    `${jobs.length} video` + (doneCount ? ` · ${doneCount} xong` : "");

  const ready = jobs.filter((j) => j.status === "ready" && j.maxClips > 0 && !j.timingError);
  const startBtn = $("#startBtn");
  const overall = $("#overall");
  if (state.running) {
    startBtn.textContent = "Dừng render";
    startBtn.className = "btn danger big";
    startBtn.disabled = false;
    const members = jobs.filter((j) => batch.has(j.id));
    const finished = members.filter((j) => ["done", "error", "cancelled"].includes(j.status)).length;
    const cur = members.find((j) => j.status === "rendering");
    const total = members.length || 1;
    const p = (finished + (cur ? cur.progress : 0)) / total;
    overall.hidden = false;
    $("#overallLabel").textContent = cur ? `Video ${finished + 1}/${total}` : `Đang chuẩn bị…`;
    $("#overallPct").textContent = `${Math.round(p * 100)}%`;
    $("#overallBar").style.width = `${p * 100}%`;
    $("#footSummary").textContent = `${nClips} clip đang chờ / đang render`;
  } else {
    batch = new Set();
    overall.hidden = true;
    startBtn.className = "btn primary big";
    startBtn.disabled = ready.length === 0;
    startBtn.textContent = ready.length ? `Bắt đầu render (${ready.length} video)` : "Bắt đầu render";
    const readyClips = ready.reduce((a, j) => a + j.clips, 0);
    const lengths = [...new Set(ready.map((j) => j.timing.clipSeconds))];
    $("#footSummary").textContent = jobs.length === 0 ? "Chưa có video"
      : ready.length ? `${readyClips} clip${lengths.length === 1 ? ` × ${fmtSec(lengths[0])}` : ""} sẽ được tạo`
      : doneCount ? "Đã render xong" : "Không có video sẵn sàng";
  }
}

function renderSettings() {
  $("#clipSeconds").value = settings.clip_seconds;
  $("#segMin").value = settings.seg_min;
  $("#segMax").value = settings.seg_max;
  $("#seed").value = settings.seed || "";
  document.querySelectorAll('input[name="mode"]').forEach((r) => { r.checked = r.value === settings.mode; });
  document.querySelectorAll("#lengthChips button").forEach((b) => {
    b.classList.toggle("on", Number(b.dataset.v) === Number(settings.clip_seconds));
  });
  $("#outputRoot").textContent = shortPath(settings.output_root);
  $("#outputRoot").title = settings.output_root;
  if (window.renderWmSummary) renderWmSummary();
  const locked = state.running;
  ["#clipSeconds", "#segMin", "#segMax", "#seed"].forEach((s) => { $(s).disabled = locked; });
}

async function saveSettings(values) {
  settings = await api().update_settings(values);
  renderSettings();
  render();
}

// ---------- sự kiện ----------

function bindUi() {
  const pick = async () => {
    const n = await api().pick_files();
    if (n === 0) return;
    toast(`Đã thêm ${n} video`);
  };
  $("#addBtn").onclick = pick;
  $("#pickBtn").onclick = pick;
  $("#addMore").onclick = pick;
  $("#clearBtn").onclick = () => api().clear_finished();

  $("#startBtn").onclick = () => {
    if (state.running) {
      api().stop();
    } else {
      state.jobs.filter((j) => j.status === "ready").forEach((j) => batch.add(j.id));
      api().start();
    }
  };

  document.querySelectorAll("#lengthChips button").forEach((b) => {
    b.onclick = () => !state.running && saveSettings({ clip_seconds: Number(b.dataset.v) });
  });
  $("#clipSeconds").onchange = (e) => saveSettings({ clip_seconds: Number(e.target.value) });
  $("#segMin").onchange = (e) => saveSettings({ seg_min: Number(e.target.value) });
  $("#segMax").onchange = (e) => saveSettings({ seg_max: Number(e.target.value) });
  $("#seed").onchange = (e) => saveSettings({ seed: e.target.value });
  document.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.onchange = () => saveSettings({ mode: r.value });
  });
  $("#changeDir").onclick = async () => { settings = await api().pick_output_dir(); renderSettings(); };
  $("#openDir").onclick = () => api().open_path(settings.output_root);

  // lớp phủ khi kéo file vào cửa sổ (Python nhận đường dẫn file)
  let depth = 0;
  const overlay = $("#dragOverlay");
  window.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    depth++; overlay.classList.add("show");
  });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; overlay.classList.remove("show"); } });
  window.addEventListener("drop", () => { depth = 0; overlay.classList.remove("show"); });
  window.addEventListener("dragover", (e) => e.preventDefault());

  // cập nhật thời gian còn lại mỗi giây
  setInterval(() => { if (state.running) render(); }, 1000);
}

window.__onState = (s) => {
  const wasRunning = state.running;
  state = s;
  render();
  if (wasRunning !== s.running) renderSettings();
  if (wasRunning && !s.running) {
    const done = s.jobs.filter((j) => j.status === "done").length;
    if (done) toast("Render xong");
  }
};

window.addEventListener("pywebviewready", async () => {
  bindUi();
  const init = await api().init();
  settings = init.settings;
  state = init.state;
  if (init.ffmpegError) {
    const a = $("#ffmpegAlert");
    a.textContent = init.ffmpegError;
    a.hidden = false;
  }
  renderSettings();
  render();
});
