"""基础身体数据：用画面内篮球直径(24cm)做绝对尺度标定，再量身体各段

用法: python body.py <tag> [--ball 24.0]
"""
import sys, os, json, glob, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import math
import numpy as np
import Quartz
from bbkit import make_generator, frame_at, crop_cg, frame_features, joint, SIDES
from ball import cg_to_np, orange_mask, label_components

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BALL_CM = 24.0   # 7 号球直径 24.0–24.6cm


def seg(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def detect_ball_near(cg_or_path, cx, cy, half=90, min_d=18, max_d=90):
    """在 (cx,cy) 附近找橙色球体，返回直径(px) 或 None"""
    arr = cg_to_np(cg_or_path)
    H, W = arr.shape[:2]
    x0, y0 = max(0, int(cx - half)), max(0, int(cy - half))
    x1, y1 = min(W, int(cx + half)), min(H, int(cy + half))
    sub = arr[y0:y1, x0:x1]
    m = orange_mask(sub)
    if m.sum() < 40:
        return None
    comps = label_components(m, min_px=30)
    best = None
    for bx0, by0, bx1, by1, n in comps:
        w, h = bx1 - bx0 + 1, by1 - by0 + 1
        dia = (w + h) / 2
        if not (min_d <= dia <= max_d):
            continue
        if max(w, h) > 1.7 * min(w, h):
            continue
        fill = n / (w * h)
        if fill < 0.55:
            continue
        if best is None or n > best[0]:
            best = (n, dia, x0 + (bx0 + bx1) / 2, y0 + (by0 + by1) / 2, round(fill, 2))
    return best


def main(tag, ball_cm=BALL_CM, sample_shots=8):
    video_map = {"b": "9月12日(1).mp4", "c": "9月12日(2).mp4", "a": "9月12日.MP4"}
    video = os.path.join(os.path.dirname(ROOT), video_map[tag])
    files = sorted(glob.glob(os.path.join(DATA, tag, "deep_*.json"))) or \
            sorted(glob.glob(os.path.join(DATA, tag, "shot_*.json")))[:sample_shots]
    if not files:
        print("no shot data"); return
    # 合并多个样本的逐帧特征
    frames = []
    for fp in files[:sample_shots]:
        d = json.load(open(fp))
        for r in d["seq"]:
            f = frame_features(r)
            if f:
                frames.append(f)
    print(f"样本文件 {len(files)} 个，逐帧 {len(frames)} 帧", flush=True)

    # 站立帧：膝角 > 158°
    def knee(f):
        vs = [f[s]["knee"] for s in SIDES if f[s]["knee"] is not None]
        return sum(vs) / len(vs) if vs else None

    stand = [f for f in frames if knee(f) and knee(f) > 158]
    print(f"站立帧 {len(stand)}", flush=True)

    # 球径：在双手附近探测
    gen, _ = make_generator(video, (1920, 1080))
    balls = []
    for f in stand[:60]:
        wr = [f["kp"].get(s + "_hand_joint") for s in SIDES]
        wr = [w for w in wr if w]
        if not wr:
            continue
        cx = sum(w[0] for w in wr) / len(wr)
        cy = sum(w[1] for w in wr) / len(wr)
        cg = frame_at(gen, f["t"])
        if cg is None:
            continue
        b = detect_ball_near(cg, cx, cy)
        if b:
            balls.append(b)
    print(f"探到球 {len(balls)} 帧", flush=True)
    for b in balls[:10]:
        print(f"   n={b[0]} 直径={b[1]:.1f}px 位置=({b[2]:.0f},{b[3]:.0f}) fill={b[4]}", flush=True)
    if not balls:
        print("未探到球，无法做绝对标定")
        return
    dia = st.median([b[1] for b in balls])
    px_per_cm = dia / ball_cm
    print(f"\n球径中位 {dia:.1f}px -> {px_per_cm:.3f} px/cm", flush=True)

    # 身体各段（取站立帧中位）
    def med_seg(a, b):
        vals = []
        for f in stand:
            p, q = f["kp"].get(a), f["kp"].get(b)
            if p and q:
                vals.append(seg(p, q))
        return st.median(vals) if vals else None

    def med_point(name):
        vals = [f["kp"][name] for f in stand if f["kp"].get(name)]
        return (st.median([v[0] for v in vals]), st.median([v[1] for v in vals])) if vals else None

    head = med_point("head_joint")
    neck = med_point("neck_1_joint")
    root = med_point("root")
    ank = [med_point(s + "_foot_joint") for s in SIDES]
    ank = [a for a in ank if a]
    body_px = None
    if head and ank:
        body_px = st.median([abs(a[1] - head[1]) for a in ank])

    out = {
        "尺度": {"球径_px": round(dia, 1), "px_per_cm": round(px_per_cm, 3), "球径假设_cm": ball_cm,
               "检测帧数": len(balls)},
        "身高_cm_估算": round(body_px / px_per_cm / 0.93, 1) if body_px else None,   # 头顶到踝约 0.93 身高
        "头顶到踝_px": round(body_px, 1) if body_px else None,
        "躯干_颈到髋_cm": round(med_seg("neck_1_joint", "root") / px_per_cm, 1),
        "肩宽_cm": round(max((seg(f["kp"]["right_shoulder_1_joint"], f["kp"]["left_shoulder_1_joint"])
                          for f in stand
                          if f["kp"].get("right_shoulder_1_joint") and f["kp"].get("left_shoulder_1_joint")), default=0) / px_per_cm, 1),
        "上臂_cm": round(st.median([seg(f["kp"]["right_shoulder_1_joint"], f["kp"]["right_forearm_joint"])
                                 for f in stand if f["kp"].get("right_shoulder_1_joint") and f["kp"].get("right_forearm_joint")]) / px_per_cm, 1),
        "前臂_cm": round(st.median([seg(f["kp"]["right_forearm_joint"], f["kp"]["right_hand_joint"])
                                 for f in stand if f["kp"].get("right_forearm_joint") and f["kp"].get("right_hand_joint")]) / px_per_cm, 1),
        "大腿_cm": round(st.median([seg(f["kp"]["right_upLeg_joint"], f["kp"]["right_leg_joint"])
                                 for f in stand if f["kp"].get("right_upLeg_joint") and f["kp"].get("right_leg_joint")]) / px_per_cm, 1),
        "小腿_cm": round(st.median([seg(f["kp"]["right_leg_joint"], f["kp"]["right_foot_joint"])
                                 for f in stand if f["kp"].get("right_leg_joint") and f["kp"].get("right_foot_joint")]) / px_per_cm, 1),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    json.dump(out, open(os.path.join(DATA, f"{tag}_body.json"), "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    args = sys.argv[1:]
    tag = args[0]
    bc = BALL_CM
    if "--ball" in args:
        bc = float(args[args.index("--ball") + 1])
    main(tag, bc)
