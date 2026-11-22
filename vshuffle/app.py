"""App desktop (cửa sổ macOS) dùng pywebview.

Video được đọc trực tiếp từ đường dẫn trên máy, không sao chép / upload,
nên thêm video gần như tức thì kể cả với file vài GB.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image

import webview
from webview.dom import DOMEventHandler

from .jobs import JobQueue
from .probe import FFmpegMissing, require_ffmpeg
from .renderer import QUALITY_MODES
from .settings import LOGO_DIR, Settings
from . import watermark as wm

UI_DIR = Path(__file__).resolve().parent / "ui"
IMAGE_FILE_TYPES = ("Ảnh (*.png;*.jpg;*.jpeg;*.webp;*.heic)",)
VIDEO_FILE_TYPES = ("Video (*.mov;*.mp4;*.m4v;*.mkv;*.avi;*.mts;*.webm)", "Tất cả (*.*)")


class Api:
    """Các hàm JS gọi được qua window.pywebview.api.*"""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._settings = Settings.load()
        self._queue = JobQueue(self._settings, self._push_state)

    # ---------- đẩy trạng thái sang giao diện ----------

    def _push_state(self) -> None:
        if self._window is None:
            return
        payload = json.dumps(self._queue.snapshot(), ensure_ascii=False)
        try:
            self._window.run_js(f"window.__onState && window.__onState({payload})")
        except Exception:  # noqa: BLE001 - cửa sổ đang đóng
            pass

    # ---------- khởi tạo ----------

    def init(self) -> dict:
        ffmpeg_error = None
        try:
            require_ffmpeg()
        except FFmpegMissing as e:
            ffmpeg_error = str(e)
        return {"settings": asdict(self._settings), "state": self._queue.snapshot(),
                "ffmpegError": ffmpeg_error}

    # ---------- cài đặt ----------

    def update_settings(self, values: dict) -> dict:
        s = self._settings
        try:
            if "clip_seconds" in values:
                s.clip_seconds = max(1.0, float(values["clip_seconds"]))
            if "seg_min" in values:
                s.seg_min = max(0.2, float(values["seg_min"]))
            if "seg_max" in values:
                s.seg_max = max(0.2, float(values["seg_max"]))
            if s.seg_max < s.seg_min:
                s.seg_max = s.seg_min
            if s.clip_seconds < s.seg_min:
                s.clip_seconds = s.seg_min
            if values.get("mode") in QUALITY_MODES:
                s.mode = values["mode"]
            if "seed" in values:
                seed = str(values["seed"]).strip()
                s.seed = seed if seed.isdigit() else ""
        except (TypeError, ValueError):
            pass
        s.save()
        self._push_state()  # số clip tối đa thay đổi theo thời lượng clip
        return asdict(s)

    def pick_output_dir(self) -> dict:
        assert self._window
        result = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=self._settings.output_root)
        if result:
            self._settings.output_root = str(result[0])
            self._settings.save()
        return asdict(self._settings)

    # ---------- hàng đợi ----------

    def pick_files(self) -> int:
        assert self._window
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, file_types=VIDEO_FILE_TYPES)
        return self._queue.add_paths(list(result)) if result else 0

    def add_paths(self, paths: list[str]) -> int:
        return self._queue.add_paths(paths)

    def remove(self, job_id: int) -> None:
        self._queue.remove(int(job_id))

    def clear_finished(self) -> None:
        self._queue.clear_finished()

    def set_clips(self, job_id: int, clips: int | None) -> None:
        self._queue.set_clips(int(job_id), None if clips in (None, "") else int(clips))

    def start(self) -> None:
        self._queue.start()

    def stop(self) -> None:
        self._queue.stop_all()

    def cancel(self, job_id: int) -> None:
        self._queue.cancel(int(job_id))

    def retry(self, job_id: int) -> None:
        self._queue.retry(int(job_id))

    # ---------- watermark ----------

    def watermark_options(self) -> dict:
        return {"fonts": [{"id": k, "name": v[0]} for k, v in wm.FONTS.items()],
                "positions": list(wm.POSITIONS)}

    def update_watermark(self, enabled: bool, spec: dict) -> dict:
        self._settings.watermark_enabled = bool(enabled)
        self._settings.watermark = wm.normalize_spec(spec)
        self._settings.save()
        self._push_state()
        return asdict(self._settings)

    def pick_logo(self) -> str | None:
        """Chọn logo; lưu 1 bản PNG vào thư mục của app để file gốc có di chuyển cũng không sao."""
        assert self._window
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False, file_types=IMAGE_FILE_TYPES)
        if not result:
            return None
        src = Path(result[0])
        try:
            with Image.open(src) as im:
                im.load()
                img = im.convert("RGBA")
        except OSError:
            return None
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        dest = LOGO_DIR / f"{src.stem[:40]}_{hashlib.sha1(data).hexdigest()[:8]}.png"
        dest.write_bytes(data)
        return str(dest)

    def preview_watermark(self, spec: dict, job_id: int | None = None, portrait: bool = True) -> dict:
        """Ảnh xem trước watermark trên khung hình của video (hoặc khung mẫu),
        kèm các lớp khó nhìn thấy trên khung hình đó."""
        bg = None
        if job_id is not None:
            jpeg = self._queue.thumb_of(int(job_id))
            if jpeg:
                bg = Image.open(io.BytesIO(jpeg))
        if bg is None:
            bg = wm.placeholder_frame(bool(portrait))
        jpeg, hard = wm.preview(wm.normalize_spec(spec), bg)
        return {"image": wm.data_uri(jpeg), "hard": hard}

    def job_watermark(self, job_id: int) -> dict | None:
        return self._queue.watermark_of(int(job_id))

    def set_job_watermark(self, job_id: int, mode: str, spec: dict | None, clips: dict) -> None:
        self._queue.set_watermark(int(job_id), mode, spec, clips)

    # ---------- thời lượng riêng của video ----------

    def set_job_timing(self, job_id: int, clip_seconds: float | None = None,
                       seg_min: float | None = None, seg_max: float | None = None) -> None:
        self._queue.set_timing(int(job_id), clip_seconds, seg_min, seg_max)

    def check_timing(self, job_id: int, clip_seconds: float, seg_min: float, seg_max: float) -> dict:
        """Xem trước trong hộp thoại: số clip tối đa không lặp + lỗi (nếu có), chưa lưu."""
        return self._queue.preview_timing(int(job_id), clip_seconds, seg_min, seg_max)

    # ---------- Finder / QuickTime ----------

    def open_path(self, path: str) -> None:
        if path and Path(path).exists():
            subprocess.run(["open", path], check=False)

    def reveal(self, path: str) -> None:
        if path and Path(path).exists():
            subprocess.run(["open", "-R", path], check=False)

    # ---------- kéo thả ----------

    def _on_drop(self, event: dict) -> None:
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            self._queue.add_paths(paths)

    def _bind(self, window: webview.Window) -> None:
        noop = lambda e: None  # noqa: E731
        doc = window.dom.document
        doc.events.dragenter += DOMEventHandler(noop, True, True)
        doc.events.dragover += DOMEventHandler(noop, True, True, debounce=500)
        doc.events.drop += DOMEventHandler(self._on_drop, True, True)


def _disable_mach_interrupt_when_frozen() -> None:
    """Trong bản .app đóng gói bằng PyInstaller, việc pywebview cài bộ bắt Ctrl+C
    (PyObjCTools.MachSignals) làm tiến trình nhận SIGTERM và thoát ngay khi mở.
    App giao diện không cần Ctrl+C, nên tắt bước này."""
    if getattr(sys, "frozen", False):
        from PyObjCTools import AppHelper
        AppHelper.installMachInterrupt = lambda: None


def main() -> None:
    _disable_mach_interrupt_when_frozen()
    api = Api()
    window = webview.create_window(
        "Video Shuffler",
        url=str(UI_DIR / "index.html"),
        js_api=api,
        width=1180, height=780, min_size=(900, 600),
        background_color="#0E0F13",
    )
    api._window = window
    webview.start(api._bind, window)


if __name__ == "__main__":
    main()
