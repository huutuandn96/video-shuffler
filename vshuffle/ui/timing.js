"use strict";
/* Thời lượng clip + độ dài đoạn cắt riêng cho từng video.
   Dùng chung từ app.js: api, settings, state, $ ; từ wm.js: openModal, closeModal, escapeHtml */

const fmtSec = (v) => `${Number(v).toLocaleString("vi-VN", { maximumFractionDigits: 2 })}s`;

function timingTagText(job) {
  const t = job.timing;
  if (!t) return { text: "", on: false };
  const text = `⏱ ${fmtSec(t.clipSeconds)} · ${fmtSec(t.segMin).slice(0, -1)}–${fmtSec(t.segMax)}`;
  return { text: t.custom ? `${text} riêng` : text, on: t.custom };
}

const timing = {
  el: null, job: null, mode: "default", seq: 0, timer: null,

  init() {
    this.el = $("#timingModal");
    $("#tmMode").addEventListener("click", (e) => {
      const b = e.target.closest("[data-m]");
      if (!b) return;
      this.mode = b.dataset.m;
      this.render();
    });
    $("#tmChips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (!b) return;
      $("#tmClip").value = b.dataset.v;
      this.render();
    });
    ["#tmClip", "#tmMin", "#tmMax"].forEach((id) => $(id).addEventListener("input", () => this.render()));
    $("#tmSave").onclick = async () => {
      if (this.mode === "default") {
        await api().set_job_timing(this.job.id, null, null, null);
      } else {
        await api().set_job_timing(this.job.id, Number($("#tmClip").value), Number($("#tmMin").value), Number($("#tmMax").value));
      }
      closeModal(this.el);
    };
    this.el.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => closeModal(this.el); });
    this.el.addEventListener("mousedown", (e) => { if (e.target === this.el) closeModal(this.el); });
  },

  open(job) {
    this.job = job;
    this.mode = job.timing.custom ? "custom" : "default";
    const t = job.timing.custom ? job.timing
      : { clipSeconds: settings.clip_seconds, segMin: settings.seg_min, segMax: settings.seg_max };
    $("#tmClip").value = t.clipSeconds;
    $("#tmMin").value = t.segMin;
    $("#tmMax").value = t.segMax;
    $("#tmTitle").textContent = `Thời lượng · ${job.name}`;
    this.render();
    openModal(this.el);
  },

  values() {
    if (this.mode === "default") return [settings.clip_seconds, settings.seg_min, settings.seg_max];
    return [Number($("#tmClip").value), Number($("#tmMin").value), Number($("#tmMax").value)];
  },

  render() {
    $("#tmMode").querySelectorAll("[data-m]").forEach((b) => b.classList.toggle("on", b.dataset.m === this.mode));
    $("#tmCustom").classList.toggle("dim", this.mode === "default");
    $("#tmDefaultHint").textContent =
      `Mặc định: clip ${fmtSec(settings.clip_seconds)}, đoạn cắt ${fmtSec(settings.seg_min)}–${fmtSec(settings.seg_max)}.`;
    const clip = Number($("#tmClip").value);
    $("#tmChips").querySelectorAll("[data-v]").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === clip));
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.check(), 120);
  },

  // Hỏi Python: với thời lượng này, video ra được tối đa bao nhiêu clip không lặp
  async check() {
    const [c, lo, hi] = this.values();
    const seq = ++this.seq;
    const r = await api().check_timing(this.job.id, c, lo, hi);
    if (seq !== this.seq) return;
    const box = $("#tmResult");
    box.classList.toggle("err", !!r.error);
    if (r.error) {
      box.textContent = r.error;
      $("#tmSave").disabled = this.mode === "custom";
      return;
    }
    $("#tmSave").disabled = false;
    const job = state.jobs.find((j) => j.id === this.job.id) || this.job;
    const n = job.clipsAuto ? r.maxClips : job.clips;
    const extra = n - r.maxClips;
    let text = `Video ${fmtTime(job.info.duration)} → tối đa ${r.maxClips} clip ${fmtSec(r.clipSeconds)} không lặp cảnh.`;
    if (!job.clipsAuto) {
      text += extra > 0 ? ` Đang chọn ${n} clip: ${extra} clip cuối sẽ lặp cảnh.` : ` Đang chọn ${n} clip.`;
    }
    box.textContent = text;
  },
};

function openJobTiming(job) { timing.open(job); }

window.addEventListener("pywebviewready", () => timing.init());
