"""Dựng lệnh ffmpeg và xuất clip.

Mỗi clip đầu ra được encode đúng 1 lần, đọc trực tiếp từ file gốc:
mỗi đoạn là 1 input (`-ss` + `-t`), sau đó nối bằng filter `concat`.
Audio bị loại bỏ.

Tối ưu CPU (đo trên Apple M2, video 4K HDR HEVC, 1 clip 15s):
- Giải mã bằng chip Mac (`-hwaccel videotoolbox`): phần giải mã từ ~24s CPU
  xuống ~1.3s CPU, chất lượng không đổi (giải mã không mất dữ liệu).
- Chế độ "gpu" encode bằng chip Mac. Theo VMAF (thang 0-100), chất lượng
  bằng hoặc cao hơn libx265 CRF18 / libx264 CRF17 của bản cũ:
    HEVC: x265 CRF18 = 98.2 · VideoToolbox q65 = 98.7-98.8
    H.264: x264 CRF17 = 98.8 · VideoToolbox q70 = 99.2
  CPU giảm từ ~300s xuống vài giây cho mỗi clip, quạt gần như không quay.
- Chế độ "cpu_quiet" giữ nguyên encoder x265/x264 như bản cũ nhưng chạy ở
  QoS background của macOS (chỉ dùng lõi tiết kiệm điện): chậm hơn, êm hơn.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from .planner import ClipPlan, Plan
from PIL import Image

from .probe import VideoInfo, tool
from .watermark import color_matrix

# gpu       = chip Mac (VideoToolbox), khuyên dùng
# cpu_quiet = x265/x264 như bản cũ, chỉ chạy trên lõi tiết kiệm điện
# cpu_max   = x265/x264 như bản cũ, dùng toàn bộ lõi (nhanh nhất với CPU, quạt kêu)
QUALITY_MODES = ("gpu", "cpu_quiet", "cpu_max")
# Tên cũ, giữ cho dòng lệnh không bị gãy
_ALIASES = {"fast": "gpu", "high": "cpu_max"}

VT_Q_HEVC = 65
VT_Q_H264 = 70


class Cancelled(Exception):
    pass


def normalize_mode(mode: str) -> str:
    mode = _ALIASES.get(mode, mode)
    if mode not in QUALITY_MODES:
        raise ValueError(f"Chế độ không hợp lệ: {mode}")
    return mode


def _encoder_args(info: VideoInfo, mode: str) -> list[str]:
    hevc = info.codec == "hevc"
    ten_bit = info.is_10bit

    if mode == "gpu":
        if hevc:
            args = ["-c:v", "hevc_videotoolbox", "-q:v", str(VT_Q_HEVC), "-tag:v", "hvc1"]
            args += ["-profile:v", "main10", "-pix_fmt", "p010le"] if ten_bit \
                else ["-pix_fmt", "nv12"]
        else:
            args = ["-c:v", "h264_videotoolbox", "-q:v", str(VT_Q_H264), "-pix_fmt", "nv12"]
    else:
        if hevc:
            args = [
                "-c:v", "libx265", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p",
                "-tag:v", "hvc1", "-x265-params", "log-level=error",
            ]
        else:
            args = [
                "-c:v", "libx264", "-crf", "17", "-preset", "medium",
                "-pix_fmt", "yuv420p",
            ]

    # Giữ thông tin màu (quan trọng với video HDR của iPhone)
    for flag, value in (
        ("-color_primaries", info.color_primaries),
        ("-color_trc", info.color_transfer),
        ("-colorspace", info.color_space),
        ("-color_range", info.color_range),
    ):
        if value:
            args += [flag, value]
    return args


Overlay = tuple[Path, int, int]   # (ảnh PNG watermark, x, y)


def _watermark_filters(info: VideoInfo, mode: str, n_inputs: int, overlays: list[Overlay],
                       cmd: list[str], filters: list[str]) -> str:
    """Thêm filter chèn watermark, trả về nhãn đầu ra.

    Filter `overlay` chỉ nhận định dạng YUV dạng planar, còn chip Mac giải mã / encode
    ở định dạng p010 / nv12. Đổi cả khung 4K qua lại tốn ~17s CPU mỗi clip 15s, nên
    chỉ đổi dải ngang chứa watermark: cắt khung thành các dải (cắt không tốn CPU),
    chèn watermark vào dải nhỏ, rồi ghép dải lại (vstack chỉ chép bộ nhớ).

    PNG được chuyển sang YUV bằng đúng ma trận màu của video (BT.2020 cho HDR
    iPhone), tránh để ffmpeg tự chuyển bằng ma trận mặc định BT.601 làm lệch màu.
    """
    if not overlays:
        return "cat"
    W, H = info.width, info.height
    ten_bit = info.is_10bit
    planar = "yuv420p10le" if ten_bit else "yuv420p"
    # Định dạng khung chính: giữ đúng định dạng encoder cần, để không phải đổi cả khung
    main_fmt = ("p010le" if ten_bit else "nv12") if mode == "gpu" else planar
    matrix = color_matrix(info.color_space, H)

    # Các lớp PNG -> YUV có alpha
    sizes = []
    for j, (png, _x, _y) in enumerate(overlays):
        cmd += ["-i", str(png)]
        filters.append(
            f"[{n_inputs + j}:v]scale=out_color_matrix={matrix}:out_range=tv,"
            f"format={'yuva420p10le' if ten_bit else 'yuva420p'}[w{j}]")
        with Image.open(png) as im:
            sizes.append(im.size)

    # Gom các lớp chồng nhau theo chiều dọc thành dải [y0, y1) (toạ độ chẵn cho 4:2:0)
    rows = []
    for j, ((_png, x, y), (w, h)) in enumerate(zip(overlays, sizes)):
        y0, y1 = max(0, y), min(H, y + h)
        if y1 > y0 and x < W and x + w > 0:
            rows.append([y0 - y0 % 2, min(H, y1 + y1 % 2), [j]])
    rows.sort()
    strips: list[list] = []
    for r in rows:
        if strips and r[0] <= strips[-1][1]:
            strips[-1][1] = max(strips[-1][1], r[1])
            strips[-1][2] += r[2]
        else:
            strips.append(r)
    if not strips:
        return "cat"

    pieces = []  # (y0, y1, các lớp | None)
    cursor = 0
    for y0, y1, layers in strips:
        if y0 > cursor:
            pieces.append((cursor, y0, None))
        pieces.append((y0, y1, layers))
        cursor = y1
    if cursor < H:
        pieces.append((cursor, H, None))

    ov_fmt = "yuv420p10" if ten_bit else "yuv420"
    k = len(pieces)
    split = "".join(f"[p{i}]" for i in range(k))
    filters.append(f"[cat]format={main_fmt}" + (f",split={k}{split}" if k > 1 else "[p0]"))
    outs = []
    for i, (y0, y1, layers) in enumerate(pieces):
        label = f"p{i}"
        if k > 1:
            filters.append(f"[{label}]crop=w={W}:h={y1 - y0}:x=0:y={y0}:exact=1[c{i}]")
            label = f"c{i}"
        if layers:
            filters.append(f"[{label}]format={planar}[d{i}_0]")
            cur = f"d{i}_0"
            for m, j in enumerate(layers):
                _png, x, y = overlays[j]
                nxt = f"d{i}_{m + 1}"
                filters.append(f"[{cur}][w{j}]overlay=x={x}:y={y - y0}:eof_action=repeat:"
                               f"format={ov_fmt}[{nxt}]")
                cur = nxt
            filters.append(f"[{cur}]format={main_fmt}[e{i}]")
            label = f"e{i}"
        outs.append(f"[{label}]")
    if k == 1:
        return outs[0][1:-1]
    filters.append(f"{''.join(outs)}vstack=inputs={k}[wm]")
    return "wm"


def build_command(info: VideoInfo, plan: Plan, clip: ClipPlan, out_path: Path,
                  mode: str = "gpu", overlays: list[Overlay] | None = None) -> list[str]:
    mode = normalize_mode(mode)
    fps = plan.fps
    fps_str = f"{fps.numerator}/{fps.denominator}"
    cmd: list[str] = []
    if mode == "cpu_quiet":
        # QoS background: macOS chỉ cho chạy trên lõi tiết kiệm điện
        cmd += ["/usr/sbin/taskpolicy", "-c", "background"]
    cmd += [tool("ffmpeg"), "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats"]

    overshoot = 2 / fps  # đọc dư 2 frame, trim bên dưới cắt chính xác
    filters = []
    for i, seg in enumerate(clip.segments):
        cmd += ["-hwaccel", "videotoolbox",
                "-ss", f"{seg.start(fps):.6f}",
                "-t", f"{float(seg.frames / fps + overshoot):.6f}",
                "-an", "-i", str(info.path)]
        filters.append(
            f"[{i}:v:0]fps={fps_str},trim=end_frame={seg.frames},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
    n = len(clip.segments)
    joined = "".join(f"[v{i}]" for i in range(n))
    filters.append(f"{joined}concat=n={n}:v=1:a=0[cat]")

    # Watermark: chèn lên video sau khi ghép, vẫn trong 1 lần encode duy nhất.
    last = _watermark_filters(info, mode, n, overlays or [], cmd, filters)
    # Gắn lại thông tin màu (HDR) lên khung hình: một số filter (crop/vstack) làm rơi mất
    params = [f"{k}={v}" for k, v in (("color_primaries", info.color_primaries),
                                       ("color_trc", info.color_transfer),
                                       ("colorspace", info.color_space),
                                       ("range", info.color_range)) if v]
    filters.append(f"[{last}]" + (f"setparams={':'.join(params)}" if params else "null") + "[out]")

    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", "-an",
            "-r", fps_str, *_encoder_args(info, mode),
            "-map_metadata", "-1", "-movflags", "+faststart", str(out_path)]
    return cmd


def render_clip(info: VideoInfo, plan: Plan, clip: ClipPlan, out_path: Path,
                mode: str = "gpu", on_progress: Callable[[float], None] | None = None,
                cancel: threading.Event | None = None,
                overlays: list[Overlay] | None = None) -> None:
    cmd = build_command(info, plan, clip, out_path, mode, overlays)
    total_us = float(clip.frames / plan.fps) * 1_000_000
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Theo dõi lệnh huỷ ở luồng riêng, vì vòng đọc stdout bên dưới bị chặn
    stop_watch = threading.Event()
    if cancel is not None:
        def watch() -> None:
            while not stop_watch.wait(0.2):
                if cancel.is_set():
                    proc.terminate()
                    return
        threading.Thread(target=watch, daemon=True).start()

    stderr_lines: list[str] = []
    threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_progress and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                on_progress(max(0.0, min(1.0, us / total_us)))
        code = proc.wait()
    finally:
        stop_watch.set()

    if cancel is not None and cancel.is_set():
        out_path.unlink(missing_ok=True)
        raise Cancelled()
    if code != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg lỗi khi xuất {out_path.name}:\n{''.join(stderr_lines).strip()}")
    if on_progress:
        on_progress(1.0)


def output_base(out_dir: Path, stem: str) -> str:
    """Tên gốc cho các clip của một lần render, không đè file đã có.

    Lần đầu: IMG_4821_clip01.mp4 ... Render lại cùng video (hoặc 2 video trùng tên)
    vào cùng thư mục: IMG_4821_2_clip01.mp4, IMG_4821_3_clip01.mp4 ...
    """
    def taken(base: str) -> bool:
        return any(out_dir.glob(f"{glob_escape(base)}_clip[0-9][0-9]*.mp4"))

    if not taken(stem):
        return stem
    n = 2
    while taken(f"{stem}_{n}"):
        n += 1
    return f"{stem}_{n}"


def glob_escape(text: str) -> str:
    return "".join(f"[{c}]" if c in "*?[]" else c for c in text)


def render_all(info: VideoInfo, plan: Plan, out_dir: Path, mode: str = "gpu",
               on_progress: Callable[[float, int, int], None] | None = None,
               cancel: threading.Event | None = None,
               watermarks: dict[int, list[Overlay]] | None = None) -> list[Path]:
    """Xuất mọi clip thẳng vào `out_dir` (thư mục chung cho mọi video).

    watermarks: {số thứ tự clip: các lớp watermark}; clip không có trong dict thì không có watermark.

    on_progress(tiến độ tổng 0-1, clip hiện tại (1-based), tổng số clip).
    Kế hoạch cắt (để tra cứu / dùng lại seed) được lưu trong `out_dir/.plans/`
    (thư mục ẩn, không làm rối thư mục kết quả). Nếu bị huỷ, xoá các clip
    đã xuất trong lần này.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base = output_base(out_dir, info.path.stem)
    outputs: list[Path] = []
    n = len(plan.clips)
    try:
        for k, clip in enumerate(plan.clips):
            suffix = "_lap" if clip.repeat else ""
            out_path = out_dir / f"{base}_clip{clip.index:02d}{suffix}.mp4"

            def cb(p: float, k=k) -> None:
                if on_progress:
                    on_progress((k + p) / n, k + 1, n)

            render_clip(info, plan, clip, out_path, mode, cb, cancel,
                        (watermarks or {}).get(clip.index))
            outputs.append(out_path)
    except Cancelled:
        for p in outputs:
            p.unlink(missing_ok=True)
        raise

    plans_dir = out_dir / ".plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{base}_{datetime.now():%Y%m%d-%H%M%S}.json").write_text(
        json.dumps({"source": str(info.path), "mode": normalize_mode(mode),
                    "outputs": [p.name for p in outputs],
                    "watermark_clips": sorted(i for i, o in (watermarks or {}).items() if o),
                    **plan.to_dict()},
                   indent=2, ensure_ascii=False)
    )
    return outputs


def thumbnail_jpeg(info: VideoInfo, width: int = 480) -> bytes:
    """Ảnh thu nhỏ lấy ở 1/3 video, giải mã bằng chip Mac (~0.2s cho video 4K)."""
    vf = f"scale={width}:-2"
    if info.is_hdr:
        # Xem HDR trên màn hình SDR bị nhạt màu; tăng nhẹ cho dễ nhìn (chỉ ảnh thu nhỏ)
        vf += ",eq=contrast=1.08:saturation=1.35"
    cmd = [tool("ffmpeg"), "-v", "error", "-hwaccel", "videotoolbox",
           "-ss", f"{info.duration / 3:.3f}", "-i", str(info.path),
           "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(result.stderr.decode(errors="ignore").strip() or "Không tạo được ảnh thu nhỏ")
    return result.stdout
