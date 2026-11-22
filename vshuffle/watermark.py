"""Watermark chữ và logo.

Mỗi lớp (chữ / logo) được vẽ thành 1 ảnh PNG nhỏ đúng kích thước pixel của video,
rồi ffmpeg chèn lên video bằng filter `overlay` trong cùng lần encode duy nhất.

Kích thước và lề tính theo % cạnh ngắn của video, nên video dọc và ngang
cùng độ phân giải nhìn cân đối như nhau, và ảnh xem trước (thu nhỏ) khớp
đúng bố cục với video thật.

Video HDR của iPhone (HLG) không dùng được màu RGB thông thường của logo:
nếu chèn thẳng, chữ trắng sẽ sáng chói hoặc lệch màu. Màu được chuyển sang
BT.2020 + HLG/PQ, với màu trắng SDR = mức trắng chuẩn 203 nit (BT.2408).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

POSITIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)

_SYS = "/System/Library/Fonts"
_SUP = "/System/Library/Fonts/Supplemental"
# id -> (tên hiển thị, (file, index | tên biến thể) thường, (…) đậm). Đều là font có sẵn trên macOS.
FONTS: dict[str, tuple[str, tuple[str, int | str], tuple[str, int | str]]] = {
    "sf": ("SF Pro", (f"{_SYS}/SFNS.ttf", "Regular"), (f"{_SYS}/SFNS.ttf", "Bold")),
    "sf-rounded": ("SF Pro Rounded", (f"{_SYS}/SFNSRounded.ttf", "Regular"), (f"{_SYS}/SFNSRounded.ttf", "Bold")),
    "helvetica": ("Helvetica Neue", (f"{_SYS}/HelveticaNeue.ttc", 0), (f"{_SYS}/HelveticaNeue.ttc", 1)),
    "avenir": ("Avenir Next", (f"{_SYS}/Avenir Next.ttc", 7), (f"{_SYS}/Avenir Next.ttc", 0)),
    "arial": ("Arial", (f"{_SUP}/Arial.ttf", 0), (f"{_SUP}/Arial Bold.ttf", 0)),
    "georgia": ("Georgia", (f"{_SUP}/Georgia.ttf", 0), (f"{_SUP}/Georgia Bold.ttf", 0)),
}
# Futura và Impact không có dấu tiếng Việt nên không đưa vào danh sách.

DEFAULT_SPEC = {
    "text": {
        "enabled": True, "content": "@tenkenh", "font": "sf", "bold": True,
        "size": 4.5, "color": "#ffffff", "opacity": 85, "shadow": True, "outline": False, "box": False,
        "position": "bottom-center", "margin": 6.0,
    },
    "logo": {
        "enabled": False, "path": "", "size": 16.0, "opacity": 90,
        "position": "top-right", "margin": 4.0,
    },
}


def _num(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_spec(spec: dict | None) -> dict:
    """Kiểm tra / chuẩn hoá dữ liệu từ giao diện (dữ liệu ngoài, không tin tưởng)."""
    out = copy.deepcopy(DEFAULT_SPEC)
    spec = spec or {}
    t, d = spec.get("text") or {}, out["text"]
    d["enabled"] = bool(t.get("enabled", d["enabled"]))
    d["content"] = unicodedata.normalize("NFC", str(t.get("content", d["content"])))[:200]
    d["font"] = t.get("font") if t.get("font") in FONTS else d["font"]
    d["bold"] = bool(t.get("bold", d["bold"]))
    d["size"] = _num(t.get("size"), 1, 20, d["size"])
    color = str(t.get("color", d["color"]))
    d["color"] = color if len(color) == 7 and color.startswith("#") and \
        all(c in "0123456789abcdefABCDEF" for c in color[1:]) else d["color"]
    d["opacity"] = _num(t.get("opacity"), 5, 100, d["opacity"])
    d["shadow"] = bool(t.get("shadow", d["shadow"]))
    d["box"] = bool(t.get("box", d["box"]))
    d["outline"] = bool(t.get("outline", d["outline"]))
    d["position"] = t.get("position") if t.get("position") in POSITIONS else d["position"]
    d["margin"] = _num(t.get("margin"), 0, 30, d["margin"])

    lg, d = spec.get("logo") or {}, out["logo"]
    d["enabled"] = bool(lg.get("enabled", d["enabled"]))
    d["path"] = str(lg.get("path") or "")
    d["size"] = _num(lg.get("size"), 2, 80, d["size"])
    d["opacity"] = _num(lg.get("opacity"), 5, 100, d["opacity"])
    d["position"] = lg.get("position") if lg.get("position") in POSITIONS else d["position"]
    d["margin"] = _num(lg.get("margin"), 0, 30, d["margin"])
    return out


def is_visible(spec: dict | None) -> bool:
    if not spec:
        return False
    t, lg = spec["text"], spec["logo"]
    return (t["enabled"] and t["content"].strip() != "") or \
        (lg["enabled"] and bool(lg["path"]) and Path(lg["path"]).is_file())


def spec_key(spec: dict) -> str:
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- màu HDR

_BT709_TO_BT2020 = np.array([
    [0.6274040, 0.3292820, 0.0433136],
    [0.0690970, 0.9195400, 0.0113612],
    [0.0163916, 0.0880132, 0.8955950],
])
# HLG: tín hiệu 75% ứng với trắng chuẩn 203 nit -> ánh sáng cảnh tuyến tính ≈ 0.265
_HLG_A, _HLG_B, _HLG_C = 0.17883277, 0.28466892, 0.55991073
_HLG_REF_WHITE = (np.exp((0.75 - _HLG_C) / _HLG_A) + _HLG_B) / 12


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _hlg_oetf(e: np.ndarray) -> np.ndarray:
    e = np.clip(e, 0, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        high = _HLG_A * np.log(np.maximum(12 * e - _HLG_B, 1e-12)) + _HLG_C
    return np.where(e <= 1 / 12, np.sqrt(3 * e), high)


def _pq_inverse_eotf(nits: np.ndarray) -> np.ndarray:
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    y = np.clip(nits / 10000, 0, 1) ** m1
    return ((c1 + c2 * y) / (1 + c3 * y)) ** m2


def convert_colors(img: Image.Image, transfer: str) -> Image.Image:
    """sRGB -> tín hiệu HDR (BT.2020 + HLG/PQ). transfer: 'sdr' | 'hlg' | 'pq'."""
    if transfer == "sdr":
        return img
    a = np.asarray(img, dtype=np.float64) / 255.0
    lin = _srgb_to_linear(a[..., :3]) @ _BT709_TO_BT2020.T
    if transfer == "hlg":
        sig = _hlg_oetf(lin * _HLG_REF_WHITE)
    else:
        sig = _pq_inverse_eotf(lin * 203.0)
    out = np.concatenate([sig, a[..., 3:4]], axis=-1)
    return Image.fromarray(np.round(np.clip(out, 0, 1) * 255).astype(np.uint8), "RGBA")


# ---------------------------------------------------------------- vẽ lớp


@dataclass
class Layer:
    image: Image.Image  # RGBA, màu sRGB
    x: int              # vị trí góc trên trái trên khung hình (có thể âm)
    y: int


def _font(font_id: str, bold: bool, px: int) -> ImageFont.FreeTypeFont:
    _, regular, heavy = FONTS.get(font_id, FONTS["sf"])
    path, which = heavy if bold else regular
    if isinstance(which, str):
        f = ImageFont.truetype(path, px)
        try:
            f.set_variation_by_name(which)
        except (OSError, ValueError):
            pass
        return f
    return ImageFont.truetype(path, px, index=which)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


_POS = {
    "top-left": ("top", "left"), "top-center": ("top", "center"), "top-right": ("top", "right"),
    "middle-left": ("middle", "left"), "center": ("middle", "center"), "middle-right": ("middle", "right"),
    "bottom-left": ("bottom", "left"), "bottom-center": ("bottom", "center"), "bottom-right": ("bottom", "right"),
}


def _place(position: str, W: int, H: int, w: int, h: int, margin: int) -> tuple[int, int]:
    vert, horiz = _POS.get(position, ("bottom", "center"))
    x = {"left": margin, "center": (W - w) // 2, "right": W - w - margin}[horiz]
    y = {"top": margin, "middle": (H - h) // 2, "bottom": H - h - margin}[vert]
    return x, y


def _apply_opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 100:
        return img
    r, g, b, a = img.split()
    a = a.point(lambda v: round(v * opacity / 100))
    return Image.merge("RGBA", (r, g, b, a))


def _text_layer(t: dict, W: int, H: int) -> Layer | None:
    content = t["content"].strip("\n")
    if not t["enabled"] or not content.strip():
        return None
    short = min(W, H)
    px = max(6, round(short * t["size"] / 100))
    font = _font(t["font"], t["bold"], px)
    align = "left" if t["position"].endswith("left") else \
        "right" if t["position"].endswith("right") else "center"
    spacing = round(px * 0.18)

    # Viền chữ: màu tương phản với màu chữ (chữ sáng -> viền tối), để nền nào cũng nhìn thấy
    sw = max(1, round(px * 0.06)) if t["outline"] else 0
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, top, r, b = probe.multiline_textbbox((0, 0), content, font=font, spacing=spacing, align=align,
                                            stroke_width=sw)
    l, top, r, b = math.floor(l), math.floor(top), math.ceil(r), math.ceil(b)
    tw, th = r - l, b - top

    box_px, box_py = (round(px * 0.45), round(px * 0.28)) if t["box"] else (0, 0)
    vis_w, vis_h = tw + 2 * box_px, th + 2 * box_py   # phần nhìn thấy (chữ hoặc nền)
    blur = max(1, round(px * 0.07)) if t["shadow"] else 0
    off = max(1, round(px * 0.045)) if t["shadow"] else 0
    pad = 3 * blur + off + 2                          # chừa chỗ cho bóng đổ (viền đã nằm trong bbox)
    cw, ch = vis_w + 2 * pad, vis_h + 2 * pad

    text_origin = (pad + box_px - l, pad + box_py - top)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).multiline_text(text_origin, content, font=font, fill=255,
                                        spacing=spacing, align=align)
    if sw:
        outline_mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(outline_mask).multiline_text(text_origin, content, font=font, fill=255,
                                                    spacing=spacing, align=align,
                                                    stroke_width=sw, stroke_fill=255)
    else:
        outline_mask = mask
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    if t["box"]:
        box = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle(
            (pad, pad, pad + vis_w - 1, pad + vis_h - 1),
            radius=round(px * 0.35), fill=(0, 0, 0, 115))
        canvas = Image.alpha_composite(canvas, box)
    elif t["shadow"]:
        shadow_mask = Image.new("L", (cw, ch), 0)
        shadow_mask.paste(outline_mask, (0, off))
        shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: v * 150 // 255)
        shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        shadow.putalpha(shadow_mask)
        canvas = Image.alpha_composite(canvas, shadow)

    if sw:
        r_, g_, b_ = _hex_rgb(t["color"])
        light = (0.2126 * r_ + 0.7152 * g_ + 0.0722 * b_) > 140
        stroke = Image.new("RGBA", (cw, ch), (20, 20, 20, 0) if light else (245, 245, 245, 0))
        stroke.putalpha(outline_mask.point(lambda v: v * 220 // 255))
        canvas = Image.alpha_composite(canvas, stroke)

    fill = Image.new("RGBA", (cw, ch), (*_hex_rgb(t["color"]), 0))
    fill.putalpha(mask)
    canvas = _apply_opacity(Image.alpha_composite(canvas, fill), t["opacity"])

    margin = round(short * t["margin"] / 100)
    vx, vy = _place(t["position"], W, H, vis_w, vis_h, margin)
    return Layer(canvas, vx - pad, vy - pad)


def _logo_layer(lg: dict, W: int, H: int) -> Layer | None:
    if not lg["enabled"] or not lg["path"]:
        return None
    try:
        src = Image.open(lg["path"])
        src.load()
    except OSError:
        return None
    src = src.convert("RGBA")
    short = min(W, H)
    w = max(4, round(short * lg["size"] / 100))
    h = max(1, round(src.height * w / src.width))
    img = _apply_opacity(src.resize((w, h), Image.LANCZOS), lg["opacity"])
    margin = round(short * lg["margin"] / 100)
    x, y = _place(lg["position"], W, H, w, h, margin)
    return Layer(img, x, y)


def render_layers(spec: dict, W: int, H: int) -> list[Layer]:
    """Các lớp watermark cho khung hình W×H (màu sRGB). Logo nằm dưới chữ."""
    layers = [_logo_layer(spec["logo"], W, H), _text_layer(spec["text"], W, H)]
    return [layer for layer in layers if layer is not None]


def _even(v: int) -> int:
    # Video 4:2:0: đặt lớp ở toạ độ chẵn để màu viền không bị lệch nửa pixel
    return v - (v % 2)


def write_overlays(spec: dict, W: int, H: int, transfer: str, folder: Path) -> list[tuple[Path, int, int]]:
    """Ghi các lớp ra PNG (đã chuyển màu theo video) để ffmpeg chèn. Trả về (file, x, y)."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, layer in enumerate(render_layers(spec, W, H)):
        path = folder / f"wm_{spec_key(spec)}_{transfer}_{W}x{H}_{i}.png"
        convert_colors(layer.image, transfer).save(path)
        out.append((path, _even(layer.x), _even(layer.y)))
    return out


def preview_jpeg(spec: dict, background: Image.Image) -> bytes:
    """Ảnh xem trước: watermark (màu SDR) chồng lên ảnh thu nhỏ của video."""
    return preview(spec, background)[0]


# Watermark khó thấy khi độ thay đổi nó tạo ra không lớn hơn hẳn độ "rối" sẵn có của nền
# (chênh lệch giữa các pixel liền kề: vân vải, sọc, lá cây...). Ngưỡng hiệu chỉnh trên video thật:
# chữ trắng 20% trên quần sọc ≈ 2.3-2.7 (khó thấy), chữ 40% trở lên ≥ 4.
VISIBILITY_RATIO = 3.0


def preview(spec: dict, background: Image.Image) -> tuple[bytes, list[str]]:
    """Ảnh xem trước + danh sách lớp khó nhìn thấy trên khung hình này ("text" / "logo")."""
    frame = background.convert("RGBA")
    W, H = frame.size
    hard: list[str] = []
    for name, layer in (("logo", _logo_layer(spec["logo"], W, H)), ("text", _text_layer(spec["text"], W, H))):
        if layer is None:
            continue
        img, pos = _clip_to_frame(layer, W, H)
        before = np.asarray(frame.crop((*pos, pos[0] + img.width, pos[1] + img.height)).convert("L"), dtype=float)
        frame.alpha_composite(img, pos)
        after = np.asarray(frame.crop((*pos, pos[0] + img.width, pos[1] + img.height)).convert("L"), dtype=float)
        alpha = np.asarray(img.getchannel("A"), dtype=float)
        core = alpha >= alpha.max() * 0.5 if alpha.max() > 0 else alpha > 0
        texture = (np.abs(np.diff(before, axis=0)).mean() + np.abs(np.diff(before, axis=1)).mean()) / 2 \
            if before.shape[0] > 1 and before.shape[1] > 1 else 0.0
        if not core.any() or np.abs(after - before)[core].mean() / (texture + 2) < VISIBILITY_RATIO:
            hard.append(name)
    buf = io.BytesIO()
    frame.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue(), hard


def _clip_to_frame(layer: Layer, W: int, H: int) -> tuple[Image.Image, tuple[int, int]]:
    """alpha_composite không nhận toạ độ âm: cắt phần lớp nằm ngoài khung."""
    x0, y0 = max(0, -layer.x), max(0, -layer.y)
    x1 = min(layer.image.width, W - layer.x)
    y1 = min(layer.image.height, H - layer.y)
    if x1 <= x0 or y1 <= y0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), (0, 0)
    return layer.image.crop((x0, y0, x1, y1)), (layer.x + x0, layer.y + y0)


def placeholder_frame(portrait: bool = True) -> Image.Image:
    """Khung xem trước khi chưa có video."""
    w, h = (360, 640) if portrait else (640, 360)
    grad = np.linspace(0, 1, h)[:, None] * np.ones((1, w))
    r = (40 + 50 * grad).astype(np.uint8)
    g = (46 + 30 * grad).astype(np.uint8)
    b = (70 + 60 * (1 - grad)).astype(np.uint8)
    return Image.fromarray(np.stack([r, g, b], axis=-1), "RGB")


def data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def video_transfer(color_transfer: str | None) -> str:
    return {"arib-std-b67": "hlg", "smpte2084": "pq"}.get(color_transfer or "", "sdr")


def color_matrix(color_space: str | None, height: int) -> str:
    """Tên ma trận màu cho tham số out_color_matrix của filter scale."""
    if color_space in ("bt2020nc", "bt2020c"):
        return "bt2020"
    if color_space in ("smpte170m", "bt470bg"):
        return "bt601"
    if color_space == "bt709" or color_space is None and height >= 720:
        return "bt709"
    return "bt601"
