# -*- coding: utf-8 -*-
"""第二段（e）球轨迹：[顺序解码 + 背景减除] 检测

为什么不能用之前那套：
- `frame_at()` 随机访问在这个视频上拿不到相邻帧（seek 精度不够，60fps 相邻两帧取回同一张）
  → 实测「帧间运动像素占比」只有 0–2%，差分法必然失效。必须用 `iter_frames` 顺序解码。
- 球在起球段是深色（身前阴影里）、举到头顶后对着暗背景反光成浅灰 → 亮度跨越大，单一阈值不可靠。
  → 改用背景减除：机位固定，取窗口内多帧的逐像素最大/最小投影得到背景范围，任何偏离即前景。
- 球出手时已在 deep 裁剪框（box）之上 → 必须在全画幅上做。

用法: python balltrack_e.py <tag> [t_rel ...]
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import CoreMedia as CM
import Quartz.CoreVideo as CV
from bbkit import iter_frames

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sb_to_np(sb):
    """CMSampleBuffer → RGB ndarray

    坑：PixelFormatType 32 = kCVPixelFormatType_32**ARGB**，pyobjc 里内存字节序是 (A,R,G,B)，
    所以取 RGB 要用 [:, :, [1,2,3]]。按 (B,G,R,A) 取会让颜色全错、检测全空。
    """
    pb = CM.CMSampleBufferGetImageBuffer(sb)
    CV.CVPixelBufferLockBaseAddress(pb, CV.kCVPixelBufferLock_ReadOnly)
    w, h = CV.CVPixelBufferGetWidth(pb), CV.CVPixelBufferGetHeight(pb)
    bpr = CV.CVPixelBufferGetBytesPerRow(pb)
    base = CV.CVPixelBufferGetBaseAddress(pb)
    buf = base.as_buffer(bpr * h) if hasattr(base, "as_buffer") else bytes(base)
    CV.CVPixelBufferUnlockBaseAddress(pb, CV.kCVPixelBufferLock_ReadOnly)
    a = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr // 4, 4)[:, :w, :]
    return a[:, :, [1, 2, 3]].copy()
ROI = (760, 180, 560, 440)      # x0, y0, w, h（全画幅坐标，覆盖起球→出手的球路）
DS = 2


def collect(video, t0, t1, roi):
    x0, y0, w, h = roi
    out = []
    for t, sb in iter_frames(video, t0, t1):
        rgb = sb_to_np(sb)
        out.append((t, np.ascontiguousarray(rgb[y0:y0 + h, x0:x0 + w])))
    return out


def fg_mask(fr, bg, th=26):
    """与时间中位背景的偏差超过 th 的像素 = 前景

    注意：不能用 min/max 投影当背景 —— 每帧的像素必然落在 [min, max] 内，前景会恒为空（踩过）。
    """
    d = np.abs(fr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    return d > th


def blobs(mask, min_r=6, max_r=48, min_fill=0.30):
    """降采样连通域 → (cx, cy, r)（ROI 坐标）"""
    sm = mask[::DS, ::DS]
    H, W = sm.shape
    seen = np.zeros_like(sm, dtype=bool)
    out = []
    for sy in range(H):
        for sx in np.nonzero(sm[sy] & ~seen[sy])[0]:
            st = [(sy, sx)]
            seen[sy, sx] = True
            n = 0
            x0 = x1 = sx
            y0 = y1 = sy
            while st:
                y, x = st.pop()
                n += 1
                x0 = min(x0, x); x1 = max(x1, x)
                y0 = min(y0, y); y1 = max(y1, y)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and sm[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        st.append((ny, nx))
            h = y1 - y0 + 1
            w = x1 - x0 + 1
            if n < 10 or h > 2.2 * w or w > 2.2 * h:
                continue
            if n / (h * w) < min_fill:
                continue
            r = DS * math.sqrt(n / math.pi)
            if not (min_r <= r <= max_r):
                continue
            out.append((DS * (x0 + x1) / 2, DS * (y0 + y1) / 2, r, n * DS * DS))
    return out


def hand_full(tag, t_rel):
    """出手时刻投篮手的全画幅坐标"""
    dsp = os.path.join(ROOT, "data", tag, "deep_%.1f.json" % t_rel)
    if not os.path.exists(dsp):
        return None
    dj = json.load(open(dsp))
    box = dj["meta"]["box"]
    sx = box[2] / dj["meta"]["crop_w"]
    sy = box[3] / dj["meta"]["crop_h"]
    for r in dj["seq"]:
        if abs(r["t"] - t_rel) < 0.05:
            kp = r.get("kp") or {}
            hh = kp.get("right_hand_joint") or kp.get("left_hand_joint")
            if hh:
                return (box[0] + hh[0] * sx, box[1] + hh[1] * sy)
    return None


def track(video, tag, t_rel, back=0.85, fwd=0.06):
    x0, y0, _, _ = ROI
    t0, t1 = t_rel - back, t_rel + fwd
    fr = collect(video, t0, t1, ROI)
    if len(fr) < 10:
        return None
    stack = np.stack([f for _, f in fr])
    # 背景 = 逐像素时间中位数（球在画面上停留时间短，中位数≈背景）
    bg = np.median(stack, axis=0).astype(np.uint8)
    del stack
    cands = []
    for t, f in fr:
        m = fg_mask(f, bg)
        cands.append(blobs(m))

    hp = hand_full(tag, t_rel)
    i_rel = min(range(len(fr)), key=lambda i: abs(fr[i][0] - t_rel))
    # 种子：出手帧附近、离手上方最近的块
    seed = None
    for i in range(i_rel, max(-1, i_rel - 30), -1):
        if not cands[i] or hp is None:
            continue
        px, py = hp[0] - x0, hp[1] - y0
        for cx, cy, r, n in sorted(cands[i], key=lambda b: b[1]):
            if math.hypot(cx - px, cy - py) <= 210 and r >= 8:
                seed = (i, cx, cy, r)
                break
        if seed:
            break
    if seed is None:
        return None

    pts = {seed[0]: seed[1:]}
    i, cx, cy = seed[0], seed[1], seed[2]
    vx = vy = 0.0
    miss = 0
    while i - 1 >= 0 and miss <= 9:
        nx, ny = cx + vx, cy + vy
        gate = max(55.0, 2.0 * math.hypot(vx, vy))
        bl = [b for b in cands[i - 1] if math.hypot(b[0] - nx, b[1] - ny) <= gate]
        if not bl:
            miss += 1
            i -= 1
            cx, cy = nx, ny
            continue
        miss = 0
        i -= 1
        b = min(bl, key=lambda b: math.hypot(b[0] - nx, b[1] - ny))
        pts[i] = b
        vx, vy = b[0] - cx, b[1] - cy
        cx, cy = b[0], b[1]

    out = []
    for i, (t, _) in enumerate(fr):
        p = pts.get(i)
        out.append({"t": round(t, 4),
                    "x": None if not p else round(x0 + p[0], 1),
                    "y": None if not p else round(y0 + p[1], 1),
                    "r": None if not p else round(p[2], 1)})
    return {"t_rel": t_rel, "fps": round(1.0 / (fr[1][0] - fr[0][0]), 2) if len(fr) > 1 else 60.0,
            "roi": ROI, "pts": out,
            "hand": None if hp is None else [round(hp[0], 1), round(hp[1], 1)]}


def visualize(video, res, out_png):
    """把轨迹点叠画到 ROI 帧上（取 3 帧：起点/中段/出手）"""
    import Quartz
    from bbkit import save_jpeg
    from montage import montage
    x0, y0, w, h = res["roi"]
    pts = res["pts"]
    ok, _ = make_generator(video, (1920, 1080))
    idx = [i for i, p in enumerate(pts) if p["x"] is not None]
    if len(idx) < 2:
        return None
    picks = [idx[0], idx[len(idx) // 2], idx[-1]]
    tiles = []
    for k in picks:
        t = pts[k]["t"]
        cg = frame_at(ok, t)
        if cg is None:
            continue
        full = cg_to_np(cg)
        rgb = np.ascontiguousarray(full[y0:y0 + h, x0:x0 + w])
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(rgb, w, h, 8, w * 4, cs,
                                           Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h),
                                  Quartz.CGBitmapContextCreate(rgb, w, h, 8, w * 4, cs,
                                      Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little))
        # 画到当前帧为止的轨迹
        seg = [pts[j] for j in idx if j <= k]
        if len(seg) > 1:
            Quartz.CGContextSetRGBStrokeColor(ctx, 1, 0.82, 0.4, 1)
            Quartz.CGContextSetLineWidth(ctx, 4)
            Quartz.CGContextBeginPath(ctx)
            Quartz.CGContextMoveToPoint(ctx, seg[0]["x"] - x0, h - (seg[0]["y"] - y0))
            for q in seg[1:]:
                Quartz.CGContextAddLineToPoint(ctx, q["x"] - x0, h - (q["y"] - y0))
            Quartz.CGContextStrokePath(ctx)
        for q in seg:
            Quartz.CGContextSetRGBFillColor(ctx, 1, 0.3, 0.3, 1)
            Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(q["x"] - x0 - 3, h - (q["y"] - y0) - 3, 6, 6))
        img = Quartz.CGBitmapContextCreateImage(ctx)
        p1 = "/tmp/ev_%d.jpg" % k
        save_jpeg(img, p1, 0.9)
        tiles.append(p1)
    return montage(tiles, out_png, cols=len(tiles), cell=(420, 330)), len(idx)


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "e"
    ts = [float(x) for x in sys.argv[2:]]
    if not ts:
        import report as R
        ms = [m for m in (R.shot_metrics(s) for s in R.load_shots(tag)) if m]
        ts = [round(m["t_rel"], 1) for m in ms]
    video = os.path.join(os.path.dirname(ROOT), "9月13日(1).mp4" if tag == "e" else "9月13日.mp4")
    print("video:", video, flush=True)
    for t_rel in ts:
        res = track(video, tag, t_rel)
        if res is None:
            print("t=%.1f -> 未找到球" % t_rel, flush=True)
            continue
        json.dump(res, open(os.path.join(ROOT, "data", tag, "ballfly_%.1f.json" % t_rel), "w"))
        v = visualize(video, res, os.path.join(ROOT, "frames", tag, "_flytrack_%.1f.jpg" % t_rel))
        print("   可视化:", v[0] if v else "无（点太少）", flush=True)
        got = [p for p in res["pts"] if p["x"] is not None]
        print("t=%.1f -> %d 帧，%d 个点，y %.0f→%.0f，r %.0f–%.0f"
              % (t_rel, len(res["pts"]), len(got),
                 max(p["y"] for p in got) if got else 0, min(p["y"] for p in got) if got else 0,
                 min(p["r"] for p in got) if got else 0, max(p["r"] for p in got) if got else 0),
              flush=True)


if __name__ == "__main__":
    main()
