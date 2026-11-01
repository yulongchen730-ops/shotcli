# -*- coding: utf-8 -*-
"""在深扫裁剪帧上跟踪【黑色】篮球的起球轨迹（持球→下沉收球→举球→出手）。

- 球是黑色的（实测亮度 ~16，RGB≈(9,26,12)），比深绿背景（亮度 40+）暗得多
- 起球全程球都在裁剪框内，直接用已保存的裁剪帧，无需重新解码
- 种子：出手帧附近、投篮手上方的暗色圆块，往回推
- 输出：crop 坐标逐帧球心 + 全图坐标 + 上升段横向偏移
用法: python balltrack.py <tag>
"""
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from bbkit import load_cg
from ball import cg_to_np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = 2
LUM_TH = 42          # 球亮度阈值（9月13日 阴天/背光：球核心 ~16，深绿背景 40+）
# 10月6日 是晴天顺光，同一颗黑球反光后亮度到 ~54，42 会完全检不到 —— 依次尝试多档，取轨迹最长的一档
LUM_THS = (62, 54, 46, 42)
HAND_GATE = 95       # 候选必须落在投篮手这个半径内（起球全程球都贴着手）


def blobs_dark(rgb, lum_th=None, min_r=10, max_r=58, min_fill=0.4):
    """暗色（黑球）连通域 → (cx, cy, r)（crop 全分辨率坐标）"""
    th = LUM_TH if lum_th is None else lum_th
    small = rgb[::DS, ::DS]
    lum = small.mean(axis=2)
    mask = lum < th
    H, W = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for sy in range(H):
        for sx in np.nonzero(mask[sy] & ~seen[sy])[0]:
            stack = [(sy, sx)]
            seen[sy, sx] = True
            n = 0
            sxmin = sxmax = sx
            symin = symax = sy
            while stack:
                y, x = stack.pop()
                n += 1
                sxmin = min(sxmin, x); sxmax = max(sxmax, x)
                symin = min(symin, y); symax = max(symax, y)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if n < 4:
                continue
            h = symax - symin + 1
            w = sxmax - sxmin + 1
            if h > 2.0 * w or w > 2.0 * h:
                continue
            if n / (h * w) < min_fill:
                continue
            r = DS * math.sqrt(n / math.pi)
            if not (min_r <= r <= max_r):
                continue
            out.append((DS * (sxmin + sxmax) / 2, DS * (symin + symax) / 2, r))
    return out


def _integral(a):
    """积分图，形状 (H+1, W+1)：I[y, x] = a[:y, :x] 的和"""
    return np.pad(np.cumsum(np.cumsum(a, axis=0), axis=1), ((1, 0), (1, 0)))


def _rect_mean(I, n, x0, y0, x1, y1):
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(n[1] - 1, x1); y1 = min(n[0] - 1, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    s = I[y1 + 1, x1 + 1] - I[y0, x1 + 1] - I[y1 + 1, x0] + I[y0, x0]
    return s / float((x1 - x0 + 1) * (y1 - y0 + 1))


def _search_ball(I, n, cx, cy, r_lo, r_hi, rad=85, step=2, topk=5):
    """在手周围找「比环形邻域明显暗的圆」——球天然满足，晾衣绳/树/长椅的暗块不满足。

    返回按 score 降序、位置互相分散的候选列表 [(score, x, y, r), ...]。
    搜索半径必须收窄（球始终贴着手，球心到手腕一般 < 60px）：范围一大就会把背景里的暗圆也纳进来。
    """
    cands = []
    r_mid = (r_lo + r_hi) / 2.0
    for yy in range(int(cy - rad), int(cy + rad) + 1, step):
        for xx in range(int(cx - rad), int(cx + rad) + 1, step):
            if not (0 < yy < n[0] - 3 and 0 < xx < n[1] - 3):
                continue
            if math.hypot(xx - cx, yy - cy) > rad:      # 圆形约束：正方形的话四个角会到 1.41×rad
                continue
            for rr in (r_lo, r_mid, r_hi):
                a = max(2, int(rr * 0.72))
                b = max(a + 2, int(rr * 1.75))
                inner = _rect_mean(I, n, xx - a, yy - a, xx + a, yy + a)
                outer = _rect_mean(I, n, xx - b, yy - b, xx + b, yy + b)
                if inner is None or outer is None:
                    continue
                n_in = (2 * a + 1) ** 2
                n_out = (2 * b + 1) ** 2
                ring = (outer * n_out - inner * n_in) / (n_out - n_in)
                cands.append((ring - inner, xx, yy, rr))
    if not cands:
        return []
    cands.sort(key=lambda t: -t[0])
    out = []
    for c in cands:
        if all(math.hypot(c[1] - o[1], c[2] - o[2]) > 16 for o in out):
            out.append(c)
        if len(out) >= topk:
            break
    return out


def track_ball_circle(fp, t_rel=None):
    """起球轨迹 v2：圆盘-环带对比度匹配（对暗背景干扰鲁棒）。

    半径先验来自前臂长（球直径 24cm ≈ 0.92 × 前臂长 26cm），逐帧放开 ±25% 搜索。
    """
    d = json.load(open(fp))
    meta, seq = d["meta"], d["seq"]
    n = len(seq)
    box = meta["box"]
    sx = box[2] / meta["crop_w"]
    sy = box[3] / meta["crop_h"]
    if t_rel is None:
        t_rel = meta["t_center"]
    idx0 = next((k for k, r in enumerate(seq) if r["t"] >= t_rel), n // 2)
    tag = meta["tag"][:1]

    def hand_at(k):
        kp = seq[k].get("kp") or {}
        return kp.get("right_hand_joint") or kp.get("left_hand_joint")

    # 球半径先验：前臂长的 0.46 倍
    fore = []
    for r in seq:
        kp = r.get("kp") or {}
        el, wr = kp.get("right_forearm_joint"), kp.get("right_hand_joint")
        if el and wr:
            fore.append(math.hypot(el[0] - wr[0], el[1] - wr[1]))
    r_ref = 0.46 * float(np.median(fore)) if fore else 30.0
    r_lo, r_hi = max(10.0, 0.72 * r_ref), max(14.0, 1.35 * r_ref)

    raw = []
    for k, r in enumerate(seq):
        hh = hand_at(k)
        if not hh:
            raw.append(None)
            continue
        lum = cg_to_np(load_cg(os.path.join(ROOT, "frames", tag, r["img"]))).astype(np.float32).mean(axis=2)
        raw.append(_search_ball(_integral(lum), lum.shape, hh[0], hh[1], r_lo, r_hi))

    # 从出手帧往回：在每帧的候选里挑「离速度预测位置最近」的（时序优先，不看单帧最高分）
    seed = raw[idx0][0] if raw[idx0] else None
    if seed is None:
        for k in range(idx0, max(0, idx0 - 12), -1):
            if raw[k]:
                seed = raw[k][0]
                idx0 = k
                break
    if seed is None:
        return None

    pts = [None] * n
    cx, cy = float(seed[1]), float(seed[2])
    vx = vy = 0.0
    miss = 0
    for k in range(idx0, -1, -1):
        if miss > 8:
            break
        pick = None
        if raw[k]:
            gate = max(45.0, 2.0 * math.hypot(vx, vy))
            for c in raw[k]:
                d0 = math.hypot(c[1] - (cx + vx), c[2] - (cy + vy))
                if d0 <= gate and (pick is None or d0 < pick[0]):
                    pick = (d0, c)
        if pick is not None:
            c = pick[1]
            miss = 0
            pts[k] = {"x": float(c[1]), "y": float(c[2]), "r": round(float(c[3]), 1)}
            if k < idx0:
                vx, vy = c[1] - cx, c[2] - cy
            cx, cy = float(c[1]), float(c[2])
            continue
        miss += 1
        cx, cy = cx + vx, cy + vy

    trk = [(k, b) for k, b in enumerate(pts) if b]
    if len(trk) < 8:
        return None

    # RANSAC 式提纯：起球段近似直线，剔除偏离主线的点
    def _devs(tr):
        P = np.array([[b["x"], b["y"]] for _, b in tr], dtype=float)
        dv = P[-1] - P[0]
        L = math.hypot(*dv) or 1e-6
        u = dv / L
        return np.abs((P - P[0]) @ np.array([-u[1], u[0]])), P, L
    dev, P, L = _devs(trk)
    med = float(np.median(dev))
    keep = [t for t, d0 in zip(trk, dev) if d0 <= max(3.0 * med, 0.05 * L)]
    if len(keep) >= 8:
        for k, _ in trk:
            if all(k != k2 for k2, _ in keep):
                pts[k] = None
        trk = keep

    # 3 点中值平滑
    ks = [k for k, _ in trk]
    for j in range(1, len(ks) - 1):
        k0, k1, k2 = ks[j - 1], ks[j], ks[j + 1]
        if k2 - k0 <= 3:
            for ax in ("x", "y"):
                pts[k1][ax] = round(sorted([pts[k0][ax], pts[k1][ax], pts[k2][ax]])[1], 1)

    fit = None
    trk = [(k, b) for k, b in enumerate(pts) if b]
    pocket_i = max(trk, key=lambda t: t[1]["y"])[0]
    rise = [(k, b) for k, b in trk if k >= pocket_i]
    if len(rise) >= 6:
        dev2, _, L2 = _devs(rise)
        fit = {"n_track": len(trk), "n_rise": len(rise), "span_px": round(L2, 1),
               "dev_px": round(float(dev2.max()), 1),
               "dev_rel": round(float(dev2.max()) / L2, 3),
               "pocket_i": pocket_i, "start_i": trk[0][0]}
    return {"release_i": idx0, "t_rel": t_rel, "pts": pts, "fit": fit,
            "seed_r": round(float(seed[3]), 1), "box": box, "sx": round(sx, 4), "sy": round(sy, 4),
            "r_ref": round(r_ref, 1)}


def ball_dark_center(rgb, cx, cy, rad=100, tol=26, min_n=8, max_n=6000):
    """在投篮手周围找「最暗的小团」质心。

    晴天顺光下黑球反光，只有一小块低于固定亮度阈值 → 阈值连通域会碎成几块、位置跳动。
    改成「窗口内最暗点 + 邻近同暗像素质心」，对亮度绝对值不敏感，逐帧位置更连续。
    """
    H, W = rgb.shape[:2]
    x0, x1 = max(0, int(cx - rad)), min(W, int(cx + rad))
    y0, y1 = max(0, int(cy - rad)), min(H, int(cy + rad))
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    p = rgb[y0:y1, x0:x1].mean(axis=2).astype(np.float32)
    q = (p + np.roll(p, 1, 0) + np.roll(p, -1, 0) + np.roll(p, 1, 1) + np.roll(p, -1, 1)) / 5.0
    iy, ix = np.unravel_index(int(np.argmin(q)), q.shape)
    vmin = float(q[iy, ix])
    ys, xs = np.nonzero(q <= vmin + tol)
    n = len(xs)
    if n < min_n or n > max_n:
        return None
    return (float(xs.mean()) + x0, float(ys.mean()) + y0, math.sqrt(n / math.pi), vmin)


def track_deep_dark(fp, t_rel=None):
    """基于「手周围最暗小团」跟踪起球轨迹（晴天顺光场景；阈值连通域失效时用它）"""
    d = json.load(open(fp))
    meta, seq = d["meta"], d["seq"]
    n = len(seq)
    box = meta["box"]
    sx = box[2] / meta["crop_w"]
    sy = box[3] / meta["crop_h"]
    if t_rel is None:
        t_rel = meta["t_center"]
    idx0 = next((k for k, r in enumerate(seq) if r["t"] >= t_rel), n // 2)

    def hand_at(k):
        kp = seq[k].get("kp") or {}
        return kp.get("right_hand_joint") or kp.get("left_hand_joint")

    raw = []
    for k, r in enumerate(seq):
        hh = hand_at(k)
        if not hh:
            raw.append(None)
            continue
        raw.append(ball_dark_center(
            cg_to_np(load_cg(os.path.join(ROOT, "frames", meta["tag"][:1], r["img"]))), hh[0], hh[1]))

    seed = raw[idx0]
    if seed is None:
        for k in range(idx0, max(0, idx0 - 12), -1):
            if raw[k]:
                seed = raw[k]
                idx0 = k
                break
    if seed is None:
        return None

    pts = [None] * n
    cx, cy = seed[0], seed[1]
    vx = vy = 0.0
    miss = 0
    for k in range(idx0, -1, -1):
        if miss > 10:
            break
        c = raw[k]
        if c is not None:
            gate = max(55.0, 2.2 * math.hypot(vx, vy))
            if math.hypot(c[0] - (cx + vx), c[1] - (cy + vy)) <= gate:
                miss = 0
                pts[k] = {"x": round(c[0], 1), "y": round(c[1], 1), "r": round(c[2], 1)}
                if k < idx0:
                    vx, vy = c[0] - cx, c[1] - cy
                cx, cy = c[0], c[1]
                continue
        miss += 1
        cx, cy = cx + vx, cy + vy

    # 剔除「离手太远」的点：起球全程球都贴着手，球心到手腕一般 < 60px（放宽到 85）。
    # 画面里的暗块（晾衣绳、树、长椅）一旦被误锁，几乎都落在手周围 100px 之外 —— 这一步专治幻觉点。
    keep = []
    for k, b in enumerate(pts):
        if not b:
            continue
        hh = hand_at(k)
        if hh and math.hypot(b["x"] - hh[0], b["y"] - hh[1]) > 85:
            pts[k] = None
        else:
            keep.append((k, b))
    trk = keep
    if len(trk) < 8:
        return None
    # 局部拟合提纯：用前后各 3 帧（不含自己）线性外推，偏离超过 tol 的点判为误锁，剔除。
    # 用残差而不是「步长」判异常 —— 起球加速段相邻帧位移本来就会变大，按步长判会误杀。
    if len(trk) >= 8:
        keep2 = []
        for i, (k, b) in enumerate(trk):
            lo, hi = max(0, i - 3), min(len(trk), i + 4)
            seg = [s for j, s in enumerate(trk[lo:hi]) if lo + j != i]
            if len(seg) < 4:
                keep2.append((k, b))
                continue
            T = np.array([s[0] for s in seg], dtype=float)
            X = np.array([s[1]["x"] for s in seg], dtype=float)
            Y = np.array([s[1]["y"] for s in seg], dtype=float)
            A = np.vstack([T, np.ones_like(T)]).T
            cx = np.linalg.lstsq(A, X, rcond=None)[0]
            cy = np.linalg.lstsq(A, Y, rcond=None)[0]
            px, py = cx[0] * k + cx[1], cy[0] * k + cy[1]
            if math.hypot(b["x"] - px, b["y"] - py) <= 20:
                keep2.append((k, b))
            else:
                pts[k] = None
        if len(keep2) >= 8:
            trk = keep2

    # 3 点中值平滑：消掉个别帧锁错位置造成的尖角（时间相邻才平滑）
    ks = [k for k, _ in trk]
    for j in range(1, len(ks) - 1):
        k0, k1, k2 = ks[j - 1], ks[j], ks[j + 1]
        if k2 - k0 <= 3:
            for ax in ("x", "y"):
                pts[k1][ax] = round(sorted([pts[k0][ax], pts[k1][ax], pts[k2][ax]])[1], 1)
    fit = None
    pocket_i = max(trk, key=lambda t: t[1]["y"])[0]
    rise = [(k, b) for k, b in trk if k >= pocket_i]
    if len(rise) >= 6:
        P = np.array([[b["x"], b["y"]] for _, b in rise], dtype=float)
        dvec = P[-1] - P[0]
        L = math.hypot(*dvec) or 1e-6
        u = dvec / L
        dev = np.abs((P - P[0]) @ np.array([-u[1], u[0]]))
        fit = {"n_track": len(trk), "n_rise": len(rise), "span_px": round(L, 1),
               "dev_px": round(float(dev.max()), 1),
               "dev_rel": round(float(dev.max()) / L, 3),
               "pocket_i": pocket_i, "start_i": trk[0][0]}
    return {"release_i": idx0, "t_rel": t_rel, "pts": pts, "fit": fit,
            "seed_r": round(seed[2], 1), "box": box, "sx": round(sx, 4), "sy": round(sy, 4)}


def track_deep(fp, t_rel=None, lum_th=None):
    d = json.load(open(fp))
    meta, seq = d["meta"], d["seq"]
    n = len(seq)
    box = meta["box"]
    sx = box[2] / meta["crop_w"]
    sy = box[3] / meta["crop_h"]

    # 每帧候选（crop 坐标）
    cands = []
    for r in seq:
        cands.append(blobs_dark(cg_to_np(load_cg(os.path.join(ROOT, "frames", meta["tag"][:1], r["img"]))), lum_th))

    # 出手帧索引
    if t_rel is None:
        t_rel = meta["t_center"]
    idx0 = next((k for k, r in enumerate(seq) if r["t"] >= t_rel), n // 2)

    def hand_at(k):
        kp = seq[k].get("kp") or {}
        return kp.get("right_hand_joint") or kp.get("left_hand_joint")

    # 静态背景剔除：>50% 帧出现在 16px 内的候选
    n_active = sum(1 for c in cands if c)
    statics = []
    for c0 in cands[idx0]:
        if any(math.hypot(c0[0] - s0[0], c0[1] - s0[1]) < 16 for s0 in statics):
            continue
        cnt = sum(1 for c in cands if c and any(math.hypot(c0[0] - x, c0[1] - y) < 16 for x, y, _ in c))
        if cnt > 0.5 * max(n_active, 1):
            statics.append(c0)
    cands = [[b for b in c if not any(math.hypot(b[0] - s0[0], b[1] - s0[1]) < 16 for s0 in statics)]
             for c in cands]

    # 参考半径：所有帧「手附近候选」的半径中位数。球大小固定，用它排掉画面顶部
    # 晾晒布料、树干之类的大暗块（10月6日视频里这类干扰特别多）。
    rref = []
    for k2, c in enumerate(cands):
        hh2 = hand_at(k2)
        if not hh2:
            continue
        for b in c:
            if math.hypot(b[0] - hh2[0], b[1] - hh2[1]) <= HAND_GATE:
                rref.append(b[2])
    r_med = float(np.median(rref)) if rref else None

    # 种子：从出手帧往回扫，找投篮手上方/附近的暗圆
    seed = None
    for k in range(idx0, max(0, idx0 - 30), -1):
        hh = hand_at(k)
        if not hh:
            continue
        bl = [b for b in cands[k]
              if math.hypot(b[0] - hh[0], b[1] - hh[1]) <= HAND_GATE
              and abs(b[0] - hh[0]) <= 55                       # 球在手上，水平方向不会偏太多
              and (r_med is None or 0.6 * r_med <= b[2] <= 1.6 * r_med)]
        if bl:
            seed = min(bl, key=lambda b: math.hypot(b[0] - hh[0], b[1] - (hh[1] - 45)))
            idx0 = k
            break
    if seed is None:
        return None

    pts = [None] * n
    def put(k, b):
        pts[k] = {"x": round(b[0], 1), "y": round(b[1], 1), "r": round(b[2], 1)}
    put(idx0, seed)

    # 往回推：速度滑行，miss 时跳帧，最多桥接 8 帧
    miss = 0
    k = idx0
    cx, cy = seed[0], seed[1]
    vx = vy = 0.0
    while k - 1 >= 0 and miss <= 8:
        nx_, ny_ = cx + vx, cy + vy
        gate = max(45.0, 1.8 * math.hypot(vx, vy))
        hh_p = hand_at(k - 1)
        bl = [b for b in cands[k - 1]
              if math.hypot(b[0] - nx_, b[1] - ny_) <= gate
              and (r_med is None or 0.55 * r_med <= b[2] <= 1.9 * r_med)
              and (hh_p is None or math.hypot(b[0] - hh_p[0], b[1] - hh_p[1]) <= HAND_GATE)]
        if not bl:
            miss += 1
            k -= 1
            cx, cy = nx_, ny_
        else:
            miss = 0
            k -= 1
            b = min(bl, key=lambda b: math.hypot(b[0] - nx_, b[1] - ny_))
            put(k, b)
            vx, vy = b[0] - cx, b[1] - cy
            cx, cy = b[0], b[1]

    # 离群点清理
    trk0 = [(k2, b) for k2, b in enumerate(pts) if b]
    if len(trk0) >= 6:
        steps = [math.hypot(trk0[i + 1][1]["x"] - trk0[i][1]["x"], trk0[i + 1][1]["y"] - trk0[i][1]["y"])
                 for i in range(len(trk0) - 1)]
        med = float(np.median(steps)) or 1.0
        keep = set(k2 for k2, _ in trk0)
        for j in range(1, len(trk0) - 1):
            k2, b = trk0[j]
            d_prev = math.hypot(b["x"] - trk0[j - 1][1]["x"], b["y"] - trk0[j - 1][1]["y"])
            d_next = math.hypot(trk0[j + 1][1]["x"] - b["x"], trk0[j + 1][1]["y"] - b["y"])
            if min(d_prev, d_next) > 3.0 * med:
                keep.discard(k2)
        for k2 in list(keep):
            pass
        for k2, b in trk0:
            if k2 not in keep:
                pts[k2] = None

    # 上升段（收球点=最低点 → 出手）横向偏差
    trk = [(k2, b) for k2, b in enumerate(pts) if b]
    fit = None
    if len(trk) >= 8:
        pocket_i = max(trk, key=lambda t: t[1]["y"])[0]
        rise = [(k2, b) for k2, b in trk if k2 >= pocket_i]
        if len(rise) >= 6:
            P = np.array([[b["x"], b["y"]] for _, b in rise], dtype=float)
            dvec = P[-1] - P[0]
            L = math.hypot(*dvec) or 1e-6
            u = dvec / L
            dev = np.abs((P - P[0]) @ np.array([-u[1], u[0]]))
            fit = {"n_track": len(trk), "n_rise": len(rise), "span_px": round(L, 1),
                   "dev_px": round(float(dev.max()), 1),
                   "dev_rel": round(float(dev.max()) / L, 3),
                   "pocket_i": pocket_i, "start_i": trk[0][0]}

    return {"release_i": idx0, "t_rel": t_rel, "pts": pts, "fit": fit,
            "seed_r": round(seed[2], 1), "box": box, "sx": round(sx, 4), "sy": round(sy, 4)}


def main(tag):
    import report as R
    shots = R.load_shots(tag)
    ms = [m for m in (R.shot_metrics(s) for s in shots) if m]
    files = sorted(glob.glob(os.path.join(ROOT, "data", tag, "deep_*.json")),
                   key=lambda f: float(os.path.basename(f)[5:-5]))
    for fp in files:
        tc = json.load(open(fp))["meta"]["t_center"]
        m = min(ms, key=lambda x: abs(x["t_rel"] - tc), default=None)
        t_rel = m["t_rel"] if m else None
        # 两种方法择优：先试「手周围最暗小团」（晴天顺光），再试多档亮度阈值的连通域
        best = None
        res2 = track_deep_dark(fp, t_rel)
        if res2 is not None:
            best = (res2, sum(1 for p in res2["pts"] if p), 0)
        if best is None:      # 只有「最暗小团」完全失败才退回较慢的阈值连通域
            for th in LUM_THS:
                res = track_deep(fp, t_rel, th)
                if res is None:
                    continue
                nf = sum(1 for p in res["pts"] if p)
                if best is None or nf > best[1]:
                    best = (res, nf, th)
        if best is None:
            print(os.path.basename(fp), "-> 未找到黑球种子", flush=True)
            continue
        res, nf, th = best
        # 补全图坐标
        box = res["box"]
        for p in res["pts"]:
            if p:
                p["fx"] = round(box[0] + p["x"] * res["sx"], 1)
                p["fy"] = round(box[1] + p["y"] * res["sy"], 1)
        out = {"release_i": res["release_i"], "t_rel": res["t_rel"], "pts": res["pts"],
               "fit": res["fit"], "seed_r": res["seed_r"], "lum_th": th}
        json.dump(out, open(fp.replace("deep_", "ball_"), "w"))
        f = res["fit"]
        print("%s -> 起球段 %d/%d 帧，上升段横向偏移 %s（亮度阈值 %d）" % (
            os.path.basename(fp), nf, len(res["pts"]),
            ("%s px (%.0f%%)" % (f["dev_px"], f["dev_rel"] * 100)) if f else "—", th), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "d")
