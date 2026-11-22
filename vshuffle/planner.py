"""Lập kế hoạch cắt / xáo trộn. Thuần logic, không gọi ffmpeg.

Mọi phép tính dùng đơn vị frame để clip ra đúng thời lượng và không bị lệch.

Cách làm:
1. Với mỗi clip đầu ra, chia thời lượng mục tiêu (vd 15s) thành các đoạn
   ngẫu nhiên dài 1-2s, tổng đúng bằng 15s.
2. Gom tất cả các đoạn của mọi clip, xếp chúng theo thứ tự ngẫu nhiên lên
   timeline video gốc, không chồng lên nhau (=> không dùng lại đoạn nào).
   Phần thời gian thừa được rải thành các khoảng trống ngẫu nhiên, nên các
   đoạn được lấy từ khắp video chứ không dồn ở đầu.
3. Thử nhiều lần xáo trộn, chọn phương án mà hai đoạn liền nhau trong clip
   mới không phải là hai đoạn liền nhau trong video gốc.
4. Nếu số clip yêu cầu vượt mức tối đa, các clip vượt là clip lặp
   (xem `_repeat_clips`). Các clip đầu không bị ảnh hưởng.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass
class Segment:
    start_frame: int     # vị trí trong video gốc
    frames: int          # độ dài (frame)
    timeline_index: int  # thứ tự trên timeline gốc

    def start(self, fps: Fraction) -> float:
        return float(self.start_frame / fps)

    def duration(self, fps: Fraction) -> float:
        return float(self.frames / fps)


@dataclass
class ClipPlan:
    index: int
    segments: list[Segment] = field(default_factory=list)
    repeat: bool = False        # clip vượt mức tối đa, có cảnh dùng lại
    reused_frames: int = 0      # số frame đã xuất hiện ở clip trước đó

    @property
    def frames(self) -> int:
        return sum(s.frames for s in self.segments)


@dataclass
class Plan:
    fps: Fraction
    clips: list[ClipPlan]
    seed: int
    adjacent_violations: int
    unique_clips: int = 0       # số clip đầu không lặp cảnh

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "fps": str(self.fps),
            "adjacent_violations": self.adjacent_violations,
            "unique_clips": self.unique_clips,
            "clips": [
                {
                    "clip": c.index,
                    "duration": round(float(c.frames / self.fps), 3),
                    "repeat": c.repeat,
                    "reused_percent": round(100 * c.reused_frames / c.frames, 1) if c.frames else 0,
                    "segments": [
                        {
                            "source_start": round(s.start(self.fps), 3),
                            "duration": round(s.duration(self.fps), 3),
                        }
                        for s in c.segments
                    ],
                }
                for c in self.clips
            ],
        }


class PlanError(ValueError):
    pass


def _split_frames(total: int, min_f: int, max_f: int, rng: random.Random) -> list[int]:
    """Chia `total` frame thành các phần ngẫu nhiên trong [min_f, max_f]."""
    k_min = math.ceil(total / max_f)
    k_max = total // min_f
    if k_min > k_max:
        raise PlanError("Không thể chia thời lượng clip theo độ dài đoạn đã chọn.")
    k = rng.randint(k_min, k_max)
    parts = [min_f] * k
    extra = total - k * min_f
    room = max_f - min_f
    while extra > 0:
        candidates = [i for i in range(k) if parts[i] - min_f < room]
        i = rng.choice(candidates)
        add = min(extra, rng.randint(1, max(1, room // 2)), max_f - parts[i])
        parts[i] += add
        extra -= add
    rng.shuffle(parts)
    return parts


def _random_gaps(leftover: int, n_gaps: int, rng: random.Random, chunk: int | None = None) -> list[int]:
    """Rải `leftover` frame vào `n_gaps` khoảng trống ngẫu nhiên.

    Nếu có `chunk`: gom phần dư thành ít khoảng trống lớn (mỗi khoảng ≥ chunk frame)
    để clip lặp về sau lấy được trọn các đoạn chưa ai dùng.
    """
    if leftover <= 0:
        return [0] * n_gaps
    if chunk and leftover >= chunk:
        g = min(n_gaps, leftover // chunk)
        parts = [chunk] * g
        for _ in range(leftover - g * chunk):
            parts[rng.randrange(g)] += 1
        gaps = [0] * n_gaps
        for idx, size in zip(rng.sample(range(n_gaps), g), parts):
            gaps[idx] = size
        return gaps
    cuts = sorted(rng.randint(0, leftover) for _ in range(n_gaps - 1))
    edges = [0, *cuts, leftover]
    return [edges[i + 1] - edges[i] for i in range(n_gaps)]


def _count_violations(clips: list[list[int]]) -> int:
    """Đếm số chỗ hai đoạn kề nhau trong clip cũng kề nhau trên timeline gốc."""
    bad = 0
    for order in clips:
        for a, b in zip(order, order[1:]):
            if abs(a - b) == 1:
                bad += 1
    return bad


def _source_frames(duration: float, fps: Fraction) -> int:
    # +1e-6 để 120.0s * 30fps ra đúng 3600 frame, không bị làm tròn xuống 3599
    return math.floor(duration * fps + Fraction(1, 1_000_000))


def timing_error(clip_seconds: float, seg_min: float, seg_max: float, fps: Fraction) -> str | None:
    """Kiểm tra trước: thời lượng clip có chia được thành các đoạn trong khoảng đã chọn không."""
    if seg_min <= 0 or seg_max < seg_min:
        return "Độ dài đoạn không hợp lệ (cần tối thiểu ≤ tối đa)."
    if clip_seconds < seg_min:
        return f"Clip {clip_seconds:g}s ngắn hơn đoạn tối thiểu {seg_min:g}s."
    target = round(clip_seconds * fps)
    min_f, max_f = math.ceil(seg_min * fps), math.floor(seg_max * fps)
    if max_f < min_f or math.ceil(target / max_f) > target // min_f:
        return (f"Không chia được clip {clip_seconds:g}s thành các đoạn {seg_min:g}–{seg_max:g}s. "
                f"Hãy nới rộng khoảng độ dài đoạn hoặc đổi thời lượng clip.")
    return None


def max_clips(duration: float, fps: Fraction, clip_seconds: float) -> int:
    """Số clip tối đa mà không clip nào dùng lại cảnh của clip khác."""
    return max(0, _source_frames(duration, fps) // round(clip_seconds * fps))


# Giới hạn trên của số clip (kể cả clip lặp) cho mỗi video
CLIP_LIMIT = 99


def make_plan(
    duration: float,
    fps: Fraction,
    n_clips: int,
    clip_seconds: float,
    seg_min: float = 1.0,
    seg_max: float = 2.0,
    seed: int | None = None,
    attempts: int = 2000,
) -> Plan:
    """Lập kế hoạch cho `n_clips` clip.

    - Các clip đầu (tối đa `max_clips`) không dùng lại cảnh nào: giống hệt bản trước,
      cùng seed ra cùng kết quả.
    - Nếu `n_clips` lớn hơn mức đó, các clip sau là clip lặp: lấy lại cảnh từ video
      gốc nhưng ưu tiên phần chưa dùng và phần ít được dùng nhất, cắt ở vị trí mới,
      nên mỗi clip lặp khác với các clip trước và khác nhau.
    """
    if seg_min <= 0 or seg_max < seg_min:
        raise PlanError("Độ dài đoạn không hợp lệ (cần 0 < tối thiểu ≤ tối đa).")
    if clip_seconds < seg_min:
        raise PlanError("Thời lượng clip phải ≥ độ dài đoạn tối thiểu.")
    if n_clips < 1:
        raise PlanError("Số clip phải ≥ 1.")
    if n_clips > CLIP_LIMIT:
        raise PlanError(f"Tối đa {CLIP_LIMIT} clip mỗi video.")

    if seed is None:
        seed = random.randrange(1_000_000)
    rng = random.Random(seed)

    source_frames = _source_frames(duration, fps)
    target = round(clip_seconds * fps)
    min_f = math.ceil(seg_min * fps)
    max_f = math.floor(seg_max * fps)

    n_unique = min(n_clips, source_frames // target)
    if n_unique < 1:
        raise PlanError(
            f"Video dài {duration:.1f}s ngắn hơn thời lượng clip {clip_seconds:g}s."
        )

    clips, violations = _unique_clips(n_unique, source_frames, target, min_f, max_f, rng, attempts,
                                      keep_free_chunks=n_clips > n_unique)
    if n_clips > n_unique:
        clips += _repeat_clips(clips, n_clips - n_unique, source_frames, target,
                               min_f, max_f, rng)
    return Plan(fps=fps, clips=clips, seed=seed, adjacent_violations=violations,
                unique_clips=n_unique)


def _unique_clips(n_clips: int, source_frames: int, target: int, min_f: int, max_f: int,
                  rng: random.Random, attempts: int,
                  keep_free_chunks: bool = False) -> tuple[list[ClipPlan], int]:
    """Các clip không dùng lại cảnh nào của nhau."""
    lengths = [_split_frames(target, min_f, max_f, rng) for _ in range(n_clips)]
    slots = [(c, p) for c, parts in enumerate(lengths) for p in range(len(parts))]
    n = len(slots)

    # 2-3. Xáo trộn thứ tự trên timeline, giữ phương án ít vi phạm nhất
    best_perm, best_bad = None, None
    for _ in range(attempts):
        perm = list(range(n))           # perm[timeline_pos] = slot index
        rng.shuffle(perm)
        pos_of_slot = [0] * n
        for t, slot in enumerate(perm):
            pos_of_slot[slot] = t
        clip_orders: list[list[int]] = []
        i = 0
        for parts in lengths:
            clip_orders.append(pos_of_slot[i:i + len(parts)])
            i += len(parts)
        bad = _count_violations(clip_orders)
        if best_bad is None or bad < best_bad:
            best_perm, best_bad = perm, bad
            if bad == 0:
                break

    # Đặt các đoạn lên timeline gốc với khoảng trống ngẫu nhiên
    used = sum(sum(p) for p in lengths)
    gaps = _random_gaps(source_frames - used, n + 1, rng, chunk=max_f if keep_free_chunks else None)
    seg_by_slot: dict[int, Segment] = {}
    cursor = 0
    for t, slot in enumerate(best_perm):
        cursor += gaps[t]
        c, p = slots[slot]
        frames = lengths[c][p]
        seg_by_slot[slot] = Segment(start_frame=cursor, frames=frames, timeline_index=t)
        cursor += frames

    clips: list[ClipPlan] = []
    i = 0
    for c, parts in enumerate(lengths):
        clip = ClipPlan(index=c + 1)
        for _ in parts:
            clip.segments.append(seg_by_slot[i])
            i += 1
        clips.append(clip)

    return clips, best_bad or 0


def _prefix(values: list[int] | bytearray) -> list[int]:
    out = [0] * (len(values) + 1)
    total = 0
    for i, v in enumerate(values):
        total += v
        out[i + 1] = total
    return out


def _repeat_clips(previous: list[ClipPlan], count: int, source_frames: int, target: int,
                  min_f: int, max_f: int, rng: random.Random) -> list[ClipPlan]:
    """Clip lặp: cảnh có thể đã xuất hiện ở clip trước, nhưng mỗi clip vẫn khác nhau.

    Với từng đoạn của clip lặp, chọn vị trí cắt trên video gốc sao cho tổng
    "số lần đã dùng" của các frame trong đoạn là nhỏ nhất. Nhờ vậy:
    - clip lặp đầu tiên dùng hết phần video chưa clip nào lấy, rồi tới phần mới dùng 1 lần;
    - mỗi clip lặp sau tránh cảnh của clip lặp trước, nên các clip lặp khác nhau;
    - điểm cắt mới không trùng điểm cắt cũ, nên không có đoạn nào giống hệt đoạn trước;
    - trong cùng một clip, không frame nào xuất hiện 2 lần.
    """
    F = source_frames
    usage = [0] * F                 # số lần mỗi frame đã được dùng
    owners: list[list[int]] = [[] for _ in range(F)]  # clip nào đã dùng frame này
    near_cut = bytearray(F)         # 1 nếu gần một điểm cắt cũ (±3 frame)

    def mark_cut(start: int) -> None:
        for f in range(max(0, start - 3), min(F, start + 4)):
            near_cut[f] = 1

    for clip in previous:
        for seg in clip.segments:
            for f in range(seg.start_frame, seg.start_frame + seg.frames):
                usage[f] += 1
                owners[f].append(clip.index)
            mark_cut(seg.start_frame)

    # Phạt khi lấy nhiều cảnh từ cùng một clip cũ: lấy 1/4 thời lượng clip từ
    # cùng 1 clip thì mỗi frame tiếp theo từ clip đó bị tính như dùng lại thêm 1 lần.
    spread_weight = 4 / target

    results: list[ClipPlan] = []
    for k in range(count):
        index = len(previous) + k + 1
        lengths = _split_frames(target, min_f, max_f, rng)
        lengths.sort(reverse=True)  # đặt đoạn dài trước để dễ xếp
        in_clip = bytearray(F)      # frame đã có trong clip này
        drawn: dict[int, int] = {}  # số frame đã lấy từ mỗi clip cũ
        chosen: list[Segment] = []
        reused = 0
        for L in lengths:
            pu, pb = _prefix(usage), _prefix(in_clip)
            if drawn:
                pw = _prefix([sum(drawn.get(c, 0) for c in owners[f]) * spread_weight
                              for f in range(F)])
            else:
                pw = None
            best, best_cost = None, math.inf
            for allow_overlap in (False, True):
                for s in range(0, F - L + 1):
                    if not allow_overlap and pb[s + L] - pb[s]:
                        continue
                    cost = pu[s + L] - pu[s]
                    if pw is not None:
                        cost += pw[s + L] - pw[s]
                    if near_cut[s]:
                        cost += L / 2
                    cost += rng.random() * 0.9  # chọn ngẫu nhiên giữa các vị trí ngang nhau
                    if cost < best_cost:
                        best, best_cost = s, cost
                if best is not None:
                    break
            assert best is not None
            for f in range(best, best + L):
                if usage[f]:
                    reused += 1
                for c in owners[f]:
                    drawn[c] = drawn.get(c, 0) + 1
                usage[f] += 1
                in_clip[f] = 1
            mark_cut(best)
            chosen.append(Segment(start_frame=best, frames=L, timeline_index=-1))
        for seg in chosen:
            for f in range(seg.start_frame, seg.start_frame + seg.frames):
                owners[f].append(index)

        # Xáo thứ tự: tránh 2 đoạn liền nhau trên video gốc đứng cạnh nhau trong clip
        def contiguous(a: Segment, b: Segment) -> bool:
            return abs(a.start_frame + a.frames - b.start_frame) <= 1 or \
                abs(b.start_frame + b.frames - a.start_frame) <= 1

        best_order, best_bad = list(chosen), None
        for _ in range(300):
            order = list(chosen)
            rng.shuffle(order)
            bad = sum(contiguous(a, b) for a, b in zip(order, order[1:]))
            if best_bad is None or bad < best_bad:
                best_order, best_bad = order, bad
                if bad == 0:
                    break

        results.append(ClipPlan(index=index, segments=best_order,
                                repeat=True, reused_frames=reused))
    return results
