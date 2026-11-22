"""Lưu cài đặt giữa các lần mở app."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .watermark import DEFAULT_SPEC, normalize_spec

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "VideoShuffler"
SETTINGS_FILE = APP_SUPPORT / "settings.json"
LOGO_DIR = APP_SUPPORT / "logos"


def default_output_root() -> Path:
    return Path.home() / "Movies" / "Video Shuffler"


@dataclass
class Settings:
    clip_seconds: float = 15.0
    seg_min: float = 1.0
    seg_max: float = 2.0
    mode: str = "gpu"
    output_root: str = str(default_output_root())
    seed: str = ""  # để trống = ngẫu nhiên
    watermark_enabled: bool = False          # watermark mặc định cho mọi clip
    watermark: dict = field(default_factory=lambda: normalize_spec(DEFAULT_SPEC))

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known})
        if s.mode not in ("gpu", "cpu_quiet", "cpu_max"):
            s.mode = "gpu"
        s.watermark_enabled = bool(s.watermark_enabled)
        s.watermark = normalize_spec(s.watermark if isinstance(s.watermark, dict) else None)
        return s

    def save(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
