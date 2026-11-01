# -*- coding: utf-8 -*-
"""按参考视频（LadderLab）同口径计算我方球的「画面平面内分速度」曲线
speed = 相邻帧球心位移(全图px) / 比例尺(px/cm) * fps / 100  → m/s
用法: python speedcurve.py <tag>
"""
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R

ROOT = R.ROOT


def curve(fp, deep_fp, metric):
    b = json.load(open(fp))
    d = json.load(open(deep_fp))
    meta = d["meta"]
    fps = meta["fps"]
    sx = meta["box"][2] / meta["crop_w"]
    sy = meta["box"][3] / meta["crop_h"]
    # scale_px 是「身体高度像素」（= 1 倍身高），换算成 px/cm 要除以身高
    body_px = metric.get("scale_px") or 1.0
    px_per_cm = body_px / R.HEIGHT_CM
    pts = b["pts"]
    out = []
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        if not (p0 and p1):
            out.append(None)
            continue
        dpx = math.hypot((p1["x"] - p0["x"]) * sx, (p1["y"] - p0["y"]) * sy)
        out.append(dpx / px_per_cm * fps / 100.0)   # m/s（画面平面内分速度）
    # 3 帧滑动平均（检测抖动）
    sm = []
    for i in range(len(out)):
        w = [out[j] for j in range(max(0, i - 1), min(len(out), i + 2)) if out[j] is not None]
        sm.append(sum(w) / len(w) if w else None)
    rel = b.get("release_i")
    pocket = (b.get("fit") or {}).get("pocket_i")
    seg = [(i, v) for i, v in enumerate(sm) if v is not None and (pocket is None or i >= pocket)]
    peak = max(seg, key=lambda t: t[1]) if seg else None
    dip = None
    if seg and peak:
        after = [t for t in seg if t[0] < peak[0]]
        if after:
            dip = min(after, key=lambda t: t[1])
    return {"fps": fps, "rel": rel, "pocket": pocket, "px_per_cm": round(px_per_cm, 3),
            "curve": [None if v is None else round(v, 2) for v in sm],
            "peak": None if not peak else {"i": peak[0], "v": round(peak[1], 2)},
            "dip": None if not dip else {"i": dip[0], "v": round(dip[1], 2)},
            "start_v": None if not seg else round(seg[0][1], 2)}


def main(tag):
    shots = R.load_shots(tag)
    ms = [R.shot_metrics(s) for s in shots]
    res = {}
    for fp in sorted(glob.glob(os.path.join(ROOT, "data", tag, "ball_*.json")),
                     key=lambda f: float(os.path.basename(f)[5:-5])):
        t0 = float(os.path.basename(fp)[5:-5])
        m = min(ms, key=lambda x: abs(x["t_rel"] - t0))
        c = curve(fp, fp.replace("ball_", "deep_"), m)
        res[round(m["t_rel"], 2)] = c
        if c["peak"]:
            print("t=%6.1fs 起始 %.2f → 峰值 %.2f m/s（第%d帧）  谷值 %s  出手帧 %s" % (
                m["t_rel"], c["start_v"] or 0, c["peak"]["v"], c["peak"]["i"],
                ("%.2f m/s @%d" % (c["dip"]["v"], c["dip"]["i"])) if c["dip"] else "—", c["rel"]))
        else:
            print("t=%6.1fs 轨迹点不足" % m["t_rel"])
    json.dump(res, open(os.path.join(ROOT, "data", f"{tag}_speed.json"), "w"), ensure_ascii=False)
    print("written data/%s_speed.json" % tag)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "d")
