"use strict";
/* Watermark: cửa sổ chỉnh sửa + watermark riêng cho từng video / từng clip.
   Dùng chung biến toàn cục của app.js: api, settings, state, $ */

const POS_LABEL = {
  "top-left": "trên trái", "top-center": "trên giữa", "top-right": "trên phải",
  "middle-left": "giữa trái", "center": "chính giữa", "middle-right": "giữa phải",
  "bottom-left": "dưới trái", "bottom-center": "dưới giữa", "bottom-right": "dưới phải",
};
const POSITIONS = Object.keys(POS_LABEL);
let wmFonts = [];

const clone = (o) => JSON.parse(JSON.stringify(o));
const getK = (o, k) => k.split(".").reduce((a, p) => a && a[p], o);
function setK(o, k, v) {
  const parts = k.split(".");
  const last = parts.pop();
  parts.reduce((a, p) => a[p], o)[last] = v;
}

function describeWm(spec) {
  if (!spec) return "Không watermark";
  const bits = [];
  if (spec.text.enabled && spec.text.content.trim()) {
    const t = spec.text.content.trim().replace(/\s+/g, " ");
    bits.push(`Chữ “${t.length > 18 ? t.slice(0, 18) + "…" : t}” · ${POS_LABEL[spec.text.position]}`);
  }
  if (spec.logo.enabled && spec.logo.path) bits.push(`Logo · ${POS_LABEL[spec.logo.position]}`);
  return bits.length ? bits.join(" + ") : "Chưa có chữ hoặc logo";
}

// ---------- modal chung ----------

function openModal(el) {
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
}
function closeModal(el) {
  el.classList.remove("show");
  el.hidden = true;
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const top = [...document.querySelectorAll(".modal:not([hidden])")].pop();
  if (top) top.querySelector("[data-close]")?.click();
});

// ---------- cửa sổ chỉnh watermark ----------

const editor = {
  el: null, draft: null, resolve: null, timer: null, seq: 0,

  init() {
    this.el = $("#wmModal");
    const el = this.el;
    $("#edFont").innerHTML = wmFonts.map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
    el.querySelectorAll(".pos-grid").forEach((g) => {
      g.innerHTML = POSITIONS.map((p) => `<button data-p="${p}" title="${POS_LABEL[p]}"><i></i></button>`).join("");
      g.addEventListener("click", (e) => {
        const b = e.target.closest("[data-p]");
        if (!b) return;
        setK(this.draft, g.dataset.pos, b.dataset.p);
        this.sync();
      });
    });
    el.querySelectorAll("[data-k]").forEach((input) => {
      const ev = input.type === "range" || input.tagName === "TEXTAREA" || input.type === "color" ? "input" : "change";
      input.addEventListener(ev, () => {
        let v = input.type === "checkbox" ? input.checked : input.value;
        if (input.type === "range") v = Number(v);
        setK(this.draft, input.dataset.k, v);
        this.sync(input);
      });
    });
    $("#edColors").addEventListener("click", (e) => {
      const b = e.target.closest("[data-c]");
      if (!b) return;
      this.draft.text.color = b.dataset.c;
      this.sync();
    });
    el.querySelectorAll("[data-tab]").forEach((t) => {
      t.onclick = () => {
        el.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("on", x === t));
        el.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== t.dataset.tab; });
      };
    });
    $("#edPickLogo").onclick = async () => {
      const path = await api().pick_logo();
      if (!path) return;
      this.draft.logo.path = path;
      this.draft.logo.enabled = true;
      this.sync();
    };
    $("#edBg").onchange = () => this.refreshPreview();
    $("#edWarnFix").onclick = () => {
      this.draft.text.outline = true;
      if (this.draft.text.opacity < 60) this.draft.text.opacity = 60;
      this.sync();
    };
    $("#edSave").onclick = () => this.finish(clone(this.draft));
    el.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => this.finish(null); });
    el.addEventListener("mousedown", (e) => { if (e.target === el) this.finish(null); });
  },

  open({ title, spec, jobId }) {
    this.draft = clone(spec);
    $("#edTitle").textContent = title;
    // nền xem trước: các video trong hàng đợi + khung mẫu
    const withInfo = state.jobs.filter((j) => j.info && j.thumb);
    const opts = withInfo.map((j) => `<option value="job:${j.id}">${escapeHtml(j.name)}</option>`);
    opts.push('<option value="p">Khung dọc mẫu</option>', '<option value="l">Khung ngang mẫu</option>');
    $("#edBg").innerHTML = opts.join("");
    const preferred = jobId != null && withInfo.some((j) => j.id === jobId) ? `job:${jobId}`
      : withInfo.length ? `job:${withInfo[0].id}` : "p";
    $("#edBg").value = preferred;
    this.el.querySelector('[data-tab="text"]').click();
    this.sync();
    openModal(this.el);
    return new Promise((resolve) => { this.resolve = resolve; });
  },

  finish(result) {
    closeModal(this.el);
    if (this.resolve) this.resolve(result);
    this.resolve = null;
  },

  sync(skip) {
    const d = this.draft;
    this.el.querySelectorAll("[data-k]").forEach((input) => {
      if (input === skip) return;
      const v = getK(d, input.dataset.k);
      if (input.type === "checkbox") input.checked = !!v;
      else input.value = v;
    });
    const fmt = { "text.size": (v) => `${v}%`, "text.opacity": (v) => `${v}%`, "text.margin": (v) => `${v}%`,
      "logo.size": (v) => `${v}%`, "logo.opacity": (v) => `${v}%`, "logo.margin": (v) => `${v}%` };
    this.el.querySelectorAll("[data-out]").forEach((o) => { o.textContent = fmt[o.dataset.out](getK(d, o.dataset.out)); });
    this.el.querySelectorAll(".pos-grid").forEach((g) => {
      const cur = getK(d, g.dataset.pos);
      g.querySelectorAll("[data-p]").forEach((b) => b.classList.toggle("on", b.dataset.p === cur));
    });
    $("#edColors").querySelectorAll("[data-c]").forEach((b) => b.classList.toggle("on", b.dataset.c === d.text.color.toLowerCase()));
    const name = d.logo.path ? d.logo.path.split("/").pop().replace(/_[0-9a-f]{8}\.png$/, "") : "";
    $("#edLogoImg").parentElement.dataset.name = name || "Chưa chọn";
    $("#edLogoImg").parentElement.classList.toggle("empty", !name);
    ["text", "logo"].forEach((k) => {
      this.el.querySelector(`[data-pane="${k}"]`).classList.toggle("dim", !d[k].enabled);
    });
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refreshPreview(), 120);
  },

  async refreshPreview() {
    const bg = $("#edBg").value;
    const jobId = bg.startsWith("job:") ? Number(bg.slice(4)) : null;
    const seq = ++this.seq;
    const res = await api().preview_watermark(this.draft, jobId, bg !== "l");
    if (seq !== this.seq) return;  // bỏ kết quả cũ về muộn
    $("#edPreview").src = res.image;
    // cảnh báo khi watermark gần như trùng màu với nền ở vị trí đã chọn
    const hard = res.hard || [];
    const what = hard.includes("text") && hard.includes("logo") ? "Chữ và logo"
      : hard.includes("text") ? "Chữ" : hard.includes("logo") ? "Logo" : "";
    $("#edWarn").hidden = !what;
    if (what) {
      const tips = hard.includes("text") && !this.draft.text.outline && !this.draft.text.box
        ? "Thử bật Viền chữ, Nền mờ, tăng độ đậm hoặc đổi màu." : "Thử tăng độ đậm nhạt, đổi màu hoặc đổi vị trí.";
      $("#edWarnText").textContent = `${what} khó thấy trên nền ở vị trí này. ${tips}`;
      $("#edWarnFix").hidden = !(hard.includes("text") && !this.draft.text.outline && !this.draft.text.box);
    }
  },
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- watermark mặc định (thanh bên) ----------

let summarySeq = 0;
async function renderWmSummary() {
  if (!settings.watermark) return;
  const on = !!settings.watermark_enabled;
  $("#wmEnabled").checked = on;
  $("#wmTitle").textContent = on ? "Đang bật cho mọi clip" : "Đang tắt";
  $("#wmDesc").textContent = describeWm(settings.watermark);
  $("#wmEdit").classList.toggle("off", !on);
  const first = state.jobs.find((j) => j.info && j.thumb);
  const seq = ++summarySeq;
  const res = await api().preview_watermark(settings.watermark, first ? first.id : null, true);
  if (seq === summarySeq) $("#wmThumb").src = res.image;
}

async function saveDefaultWm(enabled, spec) {
  settings = await api().update_watermark(enabled, spec);
  renderWmSummary();
  render();
}

// ---------- watermark riêng của 1 video ----------

const jobWm = {
  el: null, job: null, draft: null,

  init() {
    this.el = $("#jobWmModal");
    $("#jwVideoMode").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-m]");
      if (!b) return;
      if (b.dataset.m === "custom" && !this.draft.spec) {
        this.draft.spec = clone(settings.watermark);
        if (!(await this.editVideo())) return;
      }
      this.draft.mode = b.dataset.m;
      this.render();
    });
    $("#jwEditVideo").onclick = () => this.editVideo();
    const onAction = (e) => {
      const b = e.target.closest("[data-a]");
      if (!b) return;
      const row = b.closest("[data-i]");
      this.act(b.dataset.a, row ? Number(row.dataset.i) : 0);
    };
    $("#jwClips").addEventListener("click", onAction);
    $("#jwClipHint").addEventListener("click", onAction);
    $("#jwSave").onclick = async () => {
      await api().set_job_watermark(this.job.id, this.draft.mode, this.draft.spec, this.draft.clips);
      closeModal(this.el);
    };
    this.el.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => closeModal(this.el); });
    this.el.addEventListener("mousedown", (e) => { if (e.target === this.el) closeModal(this.el); });
  },

  async act(a, idx) {
      const cur = this.draft.clips[idx];
      if (a === "inherit") delete this.draft.clips[idx];
      if (a === "off") this.draft.clips[idx] = { mode: "off", spec: null };
      if (a === "custom" || a === "edit") {
        const base = cur && cur.spec ? cur.spec : clone(this.videoSpec() || settings.watermark);
        const spec = await editor.open({ title: `Watermark riêng · clip ${idx}`, spec: base, jobId: this.job.id });
        if (!spec) return;
        this.draft.clips[idx] = { mode: "custom", spec };
      }
      if (a === "off-repeats") {
        for (let i = this.job.maxClips + 1; i <= this.job.clips; i++) this.draft.clips[i] = { mode: "off", spec: null };
      }
      if (a === "reset") this.draft.clips = {};
      this.render();
  },

  async open(job) {
    const data = await api().job_watermark(job.id);
    if (!data) return;
    this.job = job;
    this.draft = { mode: data.mode, spec: data.spec, clips: {} };
    for (const [k, v] of Object.entries(data.clips || {})) this.draft.clips[Number(k)] = v;
    $("#jwTitle").textContent = `Watermark · ${job.name}`;
    this.render();
    openModal(this.el);
  },

  videoSpec() {
    if (this.draft.mode === "off") return null;
    if (this.draft.mode === "custom") return this.draft.spec;
    return settings.watermark_enabled ? settings.watermark : null;
  },

  async editVideo() {
    const spec = await editor.open({ title: `Watermark riêng · ${this.job.name}`,
      spec: this.draft.spec || clone(settings.watermark), jobId: this.job.id });
    if (!spec) return false;
    this.draft.spec = spec;
    this.draft.mode = "custom";
    this.render();
    return true;
  },

  render() {
    const d = this.draft, job = this.job;
    $("#jwVideoMode").querySelectorAll("[data-m]").forEach((b) => b.classList.toggle("on", b.dataset.m === d.mode));
    $("#jwEditVideo").hidden = d.mode !== "custom";
    $("#jwVideoHint").textContent =
      d.mode === "default" ? (settings.watermark_enabled ? `Mặc định: ${describeWm(settings.watermark)}` : "Watermark mặc định đang tắt, nên video này không có watermark.")
      : d.mode === "off" ? "Video này không có watermark (trừ các clip đặt riêng bên dưới)."
      : describeWm(d.spec);

    const n = Math.max(job.clips, 0);
    const inherit = describeWm(this.videoSpec());
    const repeats = job.clips - job.maxClips;
    const nCustom = Object.keys(d.clips).filter((k) => Number(k) <= n).length;
    $("#jwClipHint").innerHTML =
      (repeats > 0 ? `<button class="link" data-a="off-repeats">Tắt cho ${repeats} clip lặp cảnh</button>` : "")
      + (nCustom ? `<button class="link" data-a="reset">Bỏ đặt riêng (${nCustom})</button>` : "");

    const rows = [];
    for (let i = 1; i <= n; i++) {
      const o = d.clips[i];
      const m = o ? o.mode : "inherit";
      const desc = m === "inherit" ? inherit : m === "off" ? "Không watermark" : describeWm(o.spec);
      rows.push(`
        <div class="clip-row${m !== "inherit" ? " custom" : ""}" data-i="${i}">
          <div class="clip-name"><b>Clip ${i}${i > job.maxClips ? '<em>lặp cảnh</em>' : ""}</b><span>${escapeHtml(desc)}</span></div>
          <div class="seg small">
            <button data-a="inherit" class="${m === "inherit" ? "on" : ""}">Theo video</button>
            <button data-a="off" class="${m === "off" ? "on" : ""}">Tắt</button>
            <button data-a="${m === "custom" ? "edit" : "custom"}" class="${m === "custom" ? "on" : ""}">${m === "custom" ? "Riêng · Sửa" : "Riêng"}</button>
          </div>
        </div>`);
    }
    $("#jwClips").innerHTML = rows.join("") || '<p class="hint">Video chưa đọc xong.</p>';
  },
};

// ---------- nhãn WM trên thẻ video ----------

function wmTagText(job) {
  const n = Object.keys(job.wmClips || {}).filter((k) => Number(k) <= job.clips).length;
  const base = job.wmMode === "off" ? "WM tắt" : job.wmMode === "custom" ? "WM riêng"
    : settings.watermark_enabled ? "WM" : "WM tắt";
  return { text: n ? `${base} · ${n} clip riêng` : base, on: base !== "WM tắt" || n > 0 };
}

function openJobWatermark(job) { jobWm.open(job); }

// ---------- khởi tạo ----------

window.addEventListener("pywebviewready", async () => {
  const opts = await api().watermark_options();
  wmFonts = opts.fonts;
  editor.init();
  jobWm.init();
  $("#wmEnabled").onchange = (e) => saveDefaultWm(e.target.checked, settings.watermark);
  $("#wmEdit").onclick = async () => {
    const spec = await editor.open({ title: "Watermark mặc định (mọi clip)", spec: settings.watermark, jobId: null });
    if (spec) saveDefaultWm(true, spec);
  };
  const wait = setInterval(() => {   // chờ app.js tải xong cài đặt
    if (settings.watermark) { clearInterval(wait); renderWmSummary(); }
  }, 50);
});
