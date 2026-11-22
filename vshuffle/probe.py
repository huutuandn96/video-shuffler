"""Đọc thông tin video bằng ffprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from functools import cache
from pathlib import Path


class FFmpegMissing(RuntimeError):
    pass


@cache
def tool(name: str) -> str:
    """Đường dẫn tới ffmpeg / ffprobe.

    Ưu tiên bản đóng gói sẵn trong app (.app/Contents/Resources/bin),
    sau đó tới PATH và Homebrew (app mở từ Finder không có PATH của Homebrew).
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundled = Path(bundle) / "bin" / name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    found = shutil.which(name) or shutil.which(name, path="/opt/homebrew/bin:/usr/local/bin")
    if not found:
        raise FFmpegMissing(f"Không tìm thấy '{name}'. Cài bằng lệnh: brew install ffmpeg")
    return found


def require_ffmpeg() -> None:
    tool("ffmpeg")
    tool("ffprobe")


@dataclass
class VideoInfo:
    path: Path
    duration: float          # giây
    fps: Fraction            # khung hình / giây
    width: int
    height: int
    codec: str               # 'hevc', 'h264', ...
    pix_fmt: str             # 'yuv420p10le' với video HDR của iPhone
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    color_range: str | None

    @property
    def is_10bit(self) -> bool:
        return "10" in self.pix_fmt

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("arib-std-b67", "smpte2084")

    def summary(self) -> str:
        hdr = " HDR" if self.is_hdr else ""
        return (
            f"{self.width}x{self.height} · {float(self.fps):.2f} fps · "
            f"{self.codec.upper()}{hdr} · {self.duration:.1f}s"
        )


def _clean(value: str | None) -> str | None:
    if not value or value in ("unknown", "reserved", "unspecified"):
        return None
    return value


def probe(path: str | Path) -> VideoInfo:
    require_ffmpeg()
    path = Path(path)
    cmd = [
        tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-select_streams", "v:0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"Không đọc được video: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise ValueError("File không có luồng video.")
    s = data["streams"][0]

    fps = Fraction(s.get("avg_frame_rate") or "0/1")
    if fps <= 0:
        fps = Fraction(s.get("r_frame_rate") or "30/1")
    # iPhone ghi VFR, avg_frame_rate có thể là số lẻ như 29.97xxx -> làm tròn về chuẩn
    fps = fps.limit_denominator(1001)

    duration = float(s.get("duration") or data["format"].get("duration") or 0)
    if duration <= 0:
        raise ValueError("Không xác định được thời lượng video.")

    # Video quay dọc trên iPhone được lưu ngang kèm metadata xoay 90°.
    # ffmpeg tự xoay khi xuất, nên hiển thị kích thước sau khi xoay.
    width, height = int(s["width"]), int(s["height"])
    rotation = next((sd.get("rotation") for sd in s.get("side_data_list", [])
                     if "rotation" in sd), 0)
    if abs(int(rotation or 0)) % 180 == 90:
        width, height = height, width

    return VideoInfo(
        path=path,
        duration=duration,
        fps=fps,
        width=width,
        height=height,
        codec=s.get("codec_name", "h264"),
        pix_fmt=s.get("pix_fmt", "yuv420p"),
        color_primaries=_clean(s.get("color_primaries")),
        color_transfer=_clean(s.get("color_transfer")),
        color_space=_clean(s.get("color_space")),
        color_range=_clean(s.get("color_range")),
    )
