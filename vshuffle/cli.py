"""Dòng lệnh: vshuffle video1.mov [video2.mov ...] [--clips N] --length 15"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .planner import PlanError, make_plan, max_clips
from .probe import FFmpegMissing, probe
from .renderer import QUALITY_MODES, render_all
from .settings import default_output_root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cắt video thành đoạn 1-2s, xáo trộn, ghép thành clip ngắn.")
    ap.add_argument("inputs", type=Path, nargs="+")
    ap.add_argument("--clips", type=int, default=None,
                    help="số clip mỗi video (mặc định: tối đa không lặp cảnh; "
                         "lớn hơn thì các clip cuối dùng lại cảnh)")
    ap.add_argument("--length", type=float, default=15, help="thời lượng mỗi clip, giây (mặc định 15)")
    ap.add_argument("--seg-min", type=float, default=1.0)
    ap.add_argument("--seg-max", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=None, help="dùng lại seed để ra đúng kết quả cũ")
    ap.add_argument("--quality", choices=[*QUALITY_MODES, "high", "fast"], default="gpu",
                    help="gpu = chip Mac (mặc định) · cpu_quiet = x265 lõi tiết kiệm điện · cpu_max = x265 toàn bộ lõi")
    ap.add_argument("--out", type=Path, default=None, help="thư mục lưu clip (chung cho mọi video)")
    args = ap.parse_args(argv)

    out_root = args.out or default_output_root()
    failed = 0
    for path in args.inputs:
        try:
            info = probe(path)
            n = args.clips or max_clips(info.duration, info.fps, args.length)
            extra = max(0, n - max_clips(info.duration, info.fps, args.length))
            note = f" ({extra} clip cuối có cảnh lặp lại)" if extra else ""
            print(f"\n{path.name}: {info.summary()} → {n} clip{note}")
            plan = make_plan(info.duration, info.fps, n, args.length,
                             args.seg_min, args.seg_max, args.seed)
        except (FFmpegMissing, PlanError, ValueError) as e:
            print(f"Lỗi ({path.name}): {e}", file=sys.stderr)
            failed += 1
            continue

        print(f"Seed: {plan.seed} · {sum(len(c.segments) for c in plan.clips)} đoạn")

        def progress(p: float, cur: int, total: int) -> None:
            print(f"\rClip {cur}/{total} … {p * 100:5.1f}%", end="", flush=True)

        try:
            outputs = render_all(info, plan, out_root, args.quality, progress)
        except RuntimeError as e:
            print(f"\nLỗi: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"\n→ {out_root}: {outputs[0].name} … {outputs[-1].name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
