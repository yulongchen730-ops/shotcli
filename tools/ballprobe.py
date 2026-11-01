# -*- coding: utf-8 -*-
"""帧间差分探针（ROI 内）：固定机位下用相邻帧差分把运动中的球从静止背景分离出来

e 段特点：球是浅灰白色、背景是深绿树叶；球出手后即飞出 deep 裁剪框，
所以必须在全画幅上做，且只在与手相关的 ROI 内检测（速度快）。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import Quartz
from bbkit import make_generator, frame_at, save_jpeg, scale_cg
from ball import cg_to_np

VIDEO = sys.argv[1] if len(sys.argv) > 1 else ""   # 用法: python ballprobe.py <视频路径>
ROI = (760, 60, 560, 520)     # x0, y0, w, h（全图坐标，覆盖手及其上方）
DS = 2


def diff_blobs(a, b, roi, th=30, min_r=6, max_r=45):
    x0, y0, w, h = roi
    da = a[y0:y0 + h, x0:x0 + w].astype(np.int16)
    db = b[y0:y0 + h, x0:x0 + w].astype(np.int16)
    d = np.abs(da - db).max(axis=2)
    small = (d[::DS, ::DS] > th)
    H, W = small.shape
    seen = np.zeros_like(small, dtype=bool)
    out = []
    for sy in range(H):
        for sx in np.nonzero(small[sy] & ~seen[sy])[0]:
            st = [(sy, sx)]
            seen[sy, sx] = True
            n = 0
            ax0 = ax1 = sx
            ay0 = ay1 = sy
            while st:
                y, x = st.pop()
                n += 1
                ax0 = min(ax0, x); ax1 = max(ax1, x)
                ay0 = min(ay0, y); ay1 = max(ay1, y)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and small[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        st.append((ny, nx))
            hh = ay1 - ay0 + 1
            ww = ax1 - ax0 + 1
            if n < 6 or hh > 2.5 * ww or ww > 2.5 * hh:
                continue
            r = DS * math.sqrt(n / math.pi)
            if not (min_r <= r <= max_r):
                continue
            out.append((x0 + DS * (ax0 + ax1) / 2, y0 + DS * (ay0 + ay1) / 2, r, n * DS * DS))
    return out


def main():
    if not VIDEO:
        sys.exit("用法: python ballprobe.py <视频路径>   （帧间差分调试探针）")
    ok, _ = make_generator(VIDEO, (1920, 1080))
    for t in (151.50, 151.70, 151.85, 151.95, 152.05):
        a = cg_to_np(frame_at(ok, t))
        b = cg_to_np(frame_at(ok, t - 1.0 / 60))
        bl = diff_blobs(b, a, ROI)
        print("t=%.2f ROI 内差分块 %d 个:" % (t, len(bl)), flush=True)
        for cx, cy, r, n in sorted(bl, key=lambda x: -x[3])[:6]:
            print("    (%.0f,%.0f) r=%.1f n=%d" % (cx, cy, r, n), flush=True)
        # 可视化
        x0, y0, w, h = ROI
        d = np.abs(b[y0:y0 + h, x0:x0 + w].astype(np.int16) - a[y0:y0 + h, x0:x0 + w].astype(np.int16)).max(axis=2)
        vis = np.clip(d * 4, 0, 255).astype(np.uint8)
        rgb = np.ascontiguousarray(np.dstack([vis, vis, vis]))
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(rgb, w, h, 8, w * 4, cs,
                                           Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
        img = Quartz.CGBitmapContextCreateImage(ctx)
        # 并排：原图 ROI + 差分
        arr_a = np.ascontiguousarray(a[y0:y0 + h, x0:x0 + w])
        ctx2 = Quartz.CGBitmapContextCreate(arr_a, w, h, 8, w * 4, cs,
                                            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
        img2 = Quartz.CGBitmapContextCreateImage(ctx2)
        both = Quartz.CGBitmapContextCreate(None, w * 2, h, 8, w * 2 * 4, cs,
                                            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
        Quartz.CGContextDrawImage(both, Quartz.CGRectMake(0, 0, w, h), img2)
        Quartz.CGContextDrawImage(both, Quartz.CGRectMake(w, 0, w, h), img)
        save_jpeg(Quartz.CGBitmapContextCreateImage(both), "/tmp/diff2_%.2f.jpg" % t, 0.9)


if __name__ == "__main__":
    main()
