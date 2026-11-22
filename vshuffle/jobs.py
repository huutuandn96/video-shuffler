"""Hàng đợi: đọc thông tin video song song, render lần lượt từng video."""

from __future__ import annotations

import base64
import itertools
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .planner import CLIP_LIMIT, PlanError, make_plan, max_clips, timing_error
from .probe import VideoInfo, probe
from .renderer import Cancelled, render_all, thumbnail_jpeg
from .settings import APP_SUPPORT, Settings
from .watermark import is_visible, normalize_spec, spec_key, video_transfer, write_overlays

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".3gp", ".hevc"}

# Trạng thái job
PROBING, READY, QUEUED, RENDERING, DONE, ERROR, CANCELLED = (
    "probing", "ready", "queued", "rendering", "done", "error", "cancelled")


@dataclass
class Job:
    id: int
    path: Path
    status: str = PROBING
    info: VideoInfo | None = None
    thumb: str | None = None          # data URI
    clips: int | None = None          # None = tối đa
    progress: float = 0.0
    current_clip: int = 0
    total_clips: int = 0
    message: str = ""
    out_dir: Path | None = None
    outputs: list[Path] = field(default_factory=list)
    seed: int | None = None
    started_at: float | None = None
    elapsed: float | None = None
    thumb_jpeg: bytes | None = None
    # Watermark riêng: cả video ("default" = theo cài đặt chung, "off", "custom")
    wm_mode: str = "default"
    wm_spec: dict | None = None
    # Riêng từng clip: {số thứ tự clip: {"mode": "off" | "custom", "spec": dict}}
    wm_clips: dict[int, dict] = field(default_factory=dict)
    # Thời lượng riêng của video (None = theo cài đặt chung)
    clip_seconds: float | None = None
    seg_min: float | None = None
    seg_max: float | None = None


class JobQueue:
    def __init__(self, settings: Settings, on_change: Callable[[], None]):
        self.settings = settings
        self._on_change = on_change
        self._jobs: list[Job] = []
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel_current = threading.Event()
        self._probe_pool = ThreadPoolExecutor(max_workers=2)
        self._last_push = 0.0
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- trạng thái gửi sang giao diện ----------

    def timing_for(self, job: Job) -> tuple[float, float, float]:
        """(thời lượng clip, đoạn ngắn nhất, đoạn dài nhất) áp dụng cho video: riêng > chung."""
        s = self.settings
        if job.clip_seconds is not None:
            return job.clip_seconds, job.seg_min, job.seg_max
        return s.clip_seconds, s.seg_min, s.seg_max

    def timing_problem(self, job: Job) -> str | None:
        if not job.info:
            return None
        clip_s, lo, hi = self.timing_for(job)
        err = timing_error(clip_s, lo, hi, job.info.fps)
        if err:
            return err
        if self.max_for(job) < 1:
            return f"Video dài {job.info.duration:.1f}s, ngắn hơn thời lượng clip {clip_s:g}s."
        return None

    def max_for(self, job: Job) -> int:
        if not job.info:
            return 0
        return max_clips(job.info.duration, job.info.fps, self.timing_for(job)[0])

    def preview_timing(self, job_id: int, clip_seconds, seg_min, seg_max) -> dict:
        """Kết quả nếu áp dụng thời lượng này (không lưu): số clip tối đa không lặp, lỗi."""
        with self._lock:
            job = self._get(job_id)
            if not job or not job.info:
                return {"maxClips": 0, "error": "Video chưa đọc xong."}
            lo = _clamp(seg_min, 0.2, 60, self.settings.seg_min)
            hi = max(lo, _clamp(seg_max, 0.2, 60, self.settings.seg_max))
            clip_s = max(lo, _clamp(clip_seconds, 1, 600, self.settings.clip_seconds))
            info = job.info
            m = max_clips(info.duration, info.fps, clip_s)
            err = timing_error(clip_s, lo, hi, info.fps)
            if not err and m < 1:
                err = f"Video dài {info.duration:.1f}s, ngắn hơn thời lượng clip {clip_s:g}s."
            return {"maxClips": m, "error": err, "clipSeconds": clip_s, "segMin": lo, "segMax": hi}

    def set_timing(self, job_id: int, clip_seconds: float | None, seg_min: float | None,
                   seg_max: float | None) -> None:
        """Đặt thời lượng riêng cho video; clip_seconds=None để quay về cài đặt chung."""
        with self._lock:
            job = self._get(job_id)
            if not job or job.status == RENDERING:
                return
            if clip_seconds is None:
                job.clip_seconds = job.seg_min = job.seg_max = None
            else:
                lo = _clamp(seg_min, 0.2, 60, self.settings.seg_min)
                hi = max(lo, _clamp(seg_max, 0.2, 60, self.settings.seg_max))
                job.clip_seconds = max(lo, _clamp(clip_seconds, 1, 600, self.settings.clip_seconds))
                job.seg_min, job.seg_max = lo, hi
        self._changed()

    def clips_for(self, job: Job) -> int:
        # Cho phép vượt mức tối đa: phần vượt là clip lặp cảnh
        m = self.max_for(job)
        if m < 1:
            return 0
        return m if job.clips is None else max(1, min(job.clips, CLIP_LIMIT))

    def effective_watermark(self, job: Job, clip_index: int) -> dict | None:
        """Watermark áp dụng cho 1 clip: riêng clip > riêng video > cài đặt chung."""
        override = job.wm_clips.get(clip_index)
        if override:
            spec = None if override["mode"] == "off" else override.get("spec")
        elif job.wm_mode == "off":
            spec = None
        elif job.wm_mode == "custom":
            spec = job.wm_spec
        else:
            spec = self.settings.watermark if self.settings.watermark_enabled else None
        return spec if is_visible(spec) else None

    def thumb_of(self, job_id: int) -> bytes | None:
        with self._lock:
            job = self._get(job_id)
            return job.thumb_jpeg if job else None

    def watermark_of(self, job_id: int) -> dict | None:
        with self._lock:
            job = self._get(job_id)
            if not job:
                return None
            return {"mode": job.wm_mode, "spec": job.wm_spec,
                    "clips": {str(k): v for k, v in job.wm_clips.items()}}

    def set_watermark(self, job_id: int, mode: str, spec: dict | None, clips: dict) -> None:
        with self._lock:
            job = self._get(job_id)
            if not job or job.status == RENDERING:
                return
            job.wm_mode = mode if mode in ("default", "off", "custom") else "default"
            job.wm_spec = normalize_spec(spec) if job.wm_mode == "custom" else None
            job.wm_clips = {}
            for key, value in (clips or {}).items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                m = (value or {}).get("mode")
                if 1 <= idx <= CLIP_LIMIT and m in ("off", "custom"):
                    job.wm_clips[idx] = {"mode": m,
                                         "spec": normalize_spec(value.get("spec")) if m == "custom" else None}
        self._changed()

    def snapshot(self) -> dict:
        with self._lock:
            jobs = []
            for j in self._jobs:
                info = j.info
                jobs.append({
                    "id": j.id,
                    "name": j.path.name,
                    "path": str(j.path),
                    "status": j.status,
                    "thumb": j.thumb,
                    "info": None if not info else {
                        "width": info.width, "height": info.height,
                        "fps": round(float(info.fps), 2), "codec": info.codec.upper(),
                        "hdr": info.is_hdr, "duration": info.duration,
                    },
                    "clipsAuto": j.clips is None,
                    "clips": self.clips_for(j) if info else 0,
                    "maxClips": self.max_for(j),
                    "clipLimit": CLIP_LIMIT,
                    "progress": j.progress,
                    "currentClip": j.current_clip,
                    "totalClips": j.total_clips,
                    "message": j.message,
                    "outDir": str(j.out_dir) if j.out_dir else None,
                    "outputs": [str(p) for p in j.outputs],
                    "seed": j.seed,
                    "elapsed": j.elapsed,
                    "startedAt": j.started_at,
                    "wmMode": j.wm_mode,
                    "timing": dict(zip(("clipSeconds", "segMin", "segMax"), self.timing_for(j)),
                                   custom=j.clip_seconds is not None),
                    "timingError": self.timing_problem(j),
                    "wmClips": {str(k): v["mode"] for k, v in j.wm_clips.items()},
                })
            running = any(j.status == RENDERING for j in self._jobs)
            queued = sum(j.status == QUEUED for j in self._jobs)
            return {"jobs": jobs, "running": running or queued > 0}

    def _changed(self, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_push < 0.25:
            return
        self._last_push = now
        self._on_change()

    def _get(self, job_id: int) -> Job | None:
        return next((j for j in self._jobs if j.id == job_id), None)

    # ---------- thêm / xoá ----------

    def add_paths(self, paths: list[str]) -> int:
        added = 0
        with self._lock:
            existing = {j.path for j in self._jobs if j.status not in (DONE, ERROR, CANCELLED)}
            for raw in paths:
                p = Path(raw)
                if p.is_dir():
                    files = sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
                else:
                    files = [p]
                for f in files:
                    if f in existing or not f.is_file():
                        continue
                    job = Job(id=next(self._ids), path=f)
                    self._jobs.append(job)
                    existing.add(f)
                    self._probe_pool.submit(self._probe, job)
                    added += 1
        self._changed()
        return added

    def _probe(self, job: Job) -> None:
        try:
            info = probe(job.path)
        except Exception as e:  # noqa: BLE001 - hiện lỗi cho người dùng
            with self._lock:
                job.status, job.message = ERROR, str(e)
            self._changed()
            return
        with self._lock:
            job.info = info
            # Đang render hàng loạt thì video mới thêm vào tự xếp hàng
            job.status = QUEUED if self._is_running() else READY
        self._changed()
        try:
            jpeg = thumbnail_jpeg(info)
            with self._lock:
                job.thumb_jpeg = jpeg
                job.thumb = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
            self._changed()
        except Exception:  # noqa: BLE001 - không có ảnh thu nhỏ cũng không sao
            pass
        self._wake.set()

    def remove(self, job_id: int) -> None:
        with self._lock:
            job = self._get(job_id)
            if job and job.status != RENDERING:
                self._jobs.remove(job)
        self._changed()

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = [j for j in self._jobs if j.status not in (DONE, ERROR, CANCELLED)]
        self._changed()

    def set_clips(self, job_id: int, clips: int | None) -> None:
        with self._lock:
            job = self._get(job_id)
            if job and job.status in (READY, QUEUED, ERROR, CANCELLED, DONE):
                if clips is None or int(clips) == self.max_for(job):
                    job.clips = None
                else:
                    job.clips = max(1, min(int(clips), CLIP_LIMIT))
        self._changed()

    # ---------- chạy / dừng ----------

    def _is_running(self) -> bool:
        return any(j.status in (RENDERING, QUEUED) for j in self._jobs)

    def start(self) -> None:
        with self._lock:
            for j in self._jobs:
                # Video chưa hợp lệ (quá ngắn / khoảng đoạn không chia được) thì không xếp hàng
                if j.status == READY and self.timing_problem(j) is None:
                    j.status = QUEUED
        self._wake.set()
        self._changed()

    def retry(self, job_id: int) -> None:
        with self._lock:
            job = self._get(job_id)
            if job and job.info and job.status in (ERROR, CANCELLED, DONE):
                job.status = QUEUED if self._is_running() else READY
                job.progress, job.message, job.outputs, job.out_dir = 0.0, "", [], None
        self._wake.set()
        self._changed()

    def stop_all(self) -> None:
        """Huỷ video đang render và đưa các video đang chờ về trạng thái sẵn sàng."""
        with self._lock:
            for j in self._jobs:
                if j.status == QUEUED:
                    j.status = READY
            self._cancel_current.set()
        self._changed()

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = self._get(job_id)
            if not job:
                return
            if job.status == RENDERING:
                self._cancel_current.set()
            elif job.status == QUEUED:
                job.status = READY
        self._changed()

    def _next_job(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.status == QUEUED), None)

    def _worker(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                self._wake.wait()
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        s = self.settings
        with self._lock:
            info = job.info
            n = self.clips_for(job)
            job.status, job.progress, job.message = RENDERING, 0.0, ""
            job.current_clip, job.total_clips = 0, n
            job.started_at, job.elapsed = time.time(), None
            self._cancel_current.clear()
        self._changed()
        assert info is not None
        try:
            seed = int(s.seed) if str(s.seed).strip() else None
            problem = self.timing_problem(job)
            if problem:
                raise PlanError(problem)
            clip_s, seg_lo, seg_hi = self.timing_for(job)
            plan = make_plan(info.duration, info.fps, n, clip_s, seg_lo, seg_hi, seed)
            out_dir = Path(s.output_root).expanduser()   # thư mục chung cho mọi video
            with self._lock:
                job.seed, job.out_dir = plan.seed, out_dir

            def progress(p: float, cur: int, total: int) -> None:
                with self._lock:
                    job.progress, job.current_clip = p, cur
                self._changed(throttle=True)

            # Ảnh watermark cho từng clip (các clip cùng kiểu watermark dùng chung ảnh)
            tmp = Path(tempfile.mkdtemp(prefix="wm-", dir=_tmp_root()))
            try:
                watermarks = {}
                transfer = video_transfer(info.color_transfer)
                cache: dict[str, list] = {}
                for clip in plan.clips:
                    spec = self.effective_watermark(job, clip.index)
                    if spec is None:
                        continue
                    key = spec_key(spec)
                    if key not in cache:
                        cache[key] = write_overlays(spec, info.width, info.height, transfer, tmp)
                    if cache[key]:
                        watermarks[clip.index] = cache[key]
                outputs = render_all(info, plan, out_dir, s.mode, progress, self._cancel_current,
                                     watermarks)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            with self._lock:
                job.status, job.outputs, job.progress = DONE, outputs, 1.0
        except Cancelled:
            # render_all đã xoá các clip của lần render này
            with self._lock:
                job.status, job.message, job.out_dir = CANCELLED, "Đã huỷ", None
        except (PlanError, ValueError, RuntimeError, OSError) as e:
            with self._lock:
                job.status, job.message = ERROR, str(e)
        finally:
            with self._lock:
                job.elapsed = time.time() - (job.started_at or time.time())
        self._changed()


def _tmp_root() -> Path:
    root = APP_SUPPORT / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else max(lo, min(hi, v))  # v != v: NaN
