#!/usr/bin/env python3
"""渲染 1~2 帧版式预览（普通帧 + 定格帧），用来快速确认排版，不用跑整条视频。

用法:
    python3 preview_frame.py <tag> <出手时间> [深扫后缀] [输出前缀]

环境变量:
    SHOTVID_AR  画面显示比例（宽÷高）。不设 = 源比例（16:9 不裁切）；1.5 / 1.333 = 左右裁切
    SAVE_DIR    输出目录（默认 <工作目录>/预览）

为什么单独做一个：版式调整要迭代很多轮，每轮跑整条视频（几分钟）太慢；
这个只渲染两帧（几秒），改完立刻能看。
"""
import os
import sys
import json

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

TAG = sys.argv[1] if len(sys.argv) > 1 else "f"
T_REL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
DEEP_SUF = sys.argv[3] if len(sys.argv) > 3 else "60"
PREFIX = sys.argv[4] if len(sys.argv) > 4 else ("%s_ar%s" % (TAG, os.environ.get("SHOTVID_AR", "src")))

# makevid 在 import 时会读 sys.argv 定位 tag / 版式，必须先摆好
sys.argv = ["makevid.py", TAG, "v", "top3", DEEP_SUF]

import glob
import makevid as MV
import report as R

SAVE_DIR = os.environ.get("SAVE_DIR") or os.path.join(MV.R.ROOT, "预览")
os.makedirs(SAVE_DIR, exist_ok=True)


def pick_deep():
    pat = os.path.join(R.DATA, TAG, "deep%s_*.json" % DEEP_SUF)
    fs = glob.glob(pat)
    if not fs:
        raise SystemExit("没有深扫数据：%s" % pat)
    st = len("deep%s_" % DEEP_SUF)
    return min(fs, key=lambda f: abs(float(os.path.basename(f)[st:-5]) - T_REL))


def main():
    fp = pick_deep()
    dj = json.load(open(fp, encoding="utf-8"))
    seq = R.deflip(dj["seq"])
    t_rel = dj["meta"]["t_center"]

    shots = R.load_shots(TAG)
    ms = {round(m["t_rel"], 1): m for m in (R.shot_metrics(s) for s in shots) if m}
    m = ms[min(ms.keys(), key=lambda k: abs(k - t_rel))]
    agg = R.aggregate(list(ms.values()), R.METRIC_KEYS)
    # 评分/名次从清单里取（用户手选的清单最优先，其次顶分清单，再次按时间清单）
    rank, score = 1, 0.0
    for suf in ("picked", "top3", "first5", "first3"):
        fp2 = os.path.join(R.DATA, "%s_%s.json" % (TAG, suf))
        if not os.path.exists(fp2):
            continue
        lst = json.load(open(fp2, encoding="utf-8"))
        i = min(range(len(lst)), key=lambda k: abs(lst[k]["t"] - t_rel))
        if abs(lst[i]["t"] - t_rel) < 0.6:
            rank, score = i + 1, lst[i].get("score") or 0
            break
    t_dip = t_rel - (m.get("屈膝到出手") or 0.45)

    def make(idx):
        f = R.frame_features(seq[idx]) or {}
        cur = {"t": seq[idx]["t"], "elbowR": (f.get("right") or {}).get("elbow"),
               "elbowL": (f.get("left") or {}).get("elbow"), "knee": None,
               "fore": (f.get("right") or {}).get("fore_ang"),
               "wr_sh": (f.get("right") or {}).get("wr_over_sh"),
               "head_dy": None, "judge": {}}
        kn = [v for v in ((f.get(s) or {}).get("knee") for s in ("right", "left")) if v is not None]
        cur["knee"] = sum(kn) / len(kn) if kn else None
        ph = MV.phase_of(seq[idx]["t"], t_dip, t_rel)
        # 判定必须和成片走同一套「按相位取区间」的逻辑（MV.PHASE_REF），
        # 否则预览会显示出成片里根本不存在的红标 —— 预览就失去意义了。（2026-10-12 修）
        for dest, src, mname in (("elbow", "elbowR", "投篮手肘角"),
                                 ("fore", "fore", "小臂仰角"),
                                 ("knee", "knee", "膝角")):
            rk = MV.phase_ref(mname, ph)
            if rk and cur[src] is not None:
                cur["judge"][dest] = MV.jq(rk, cur[src])
        return cur, ph

    MV.SPEED_TXT[0] = "2 倍慢放"
    # 标题：makevid 的 _apply_title 在拿到 agg 之前定不了数据推导那一路，这里补一次
    MV.set_titles(agg)
    i_rel = min(range(len(seq)), key=lambda k: abs(seq[k]["t"] - t_rel))
    i_dip = min(range(len(seq)), key=lambda k: abs(seq[k]["t"] - t_dip))

    outs = []
    cur, ph = make(i_rel)
    p = os.path.join(SAVE_DIR, "%s_普通帧.png" % PREFIX)
    # 名次要用上面算出来的 rank —— 这里原来写死 2，预览永远显示「第 2 名」
    MV.compose(MV.frame_to_pil(dj, i_rel), None, rank, score,
               t_rel, t_dip, m, agg, cur, ph).convert("RGB").save(p, quality=93)
    outs.append(p)

    cur2, ph2 = make(i_dip)
    ann = []
    for it in MV.find_issues(m, seq, t_rel, t_dip, agg):
        if it["idx"] != i_dip:
            continue
        q = (seq[i_dip].get("kp") or {}).get(it["joint"])
        if q:
            it["kp"] = MV.kp_to_frame(dj["meta"], q)
            ann.append(it)
    p2 = os.path.join(SAVE_DIR, "%s_定格帧.png" % PREFIX)
    MV.compose(MV.frame_to_pil(dj, i_dip), None, rank, score,
               t_rel, t_dip, m, agg, cur2, ph2, annot=ann or None).convert("RGB").save(p2, quality=93)
    outs.append(p2)

    print("tag=%s  t_rel=%.2f  AR=%.3f  画面 %dx%d @(%d,%d)  面板 y=%d  圈 R=%d" % (
        TAG, t_rel, MV.DISP_AR, MV.FRAME_W, MV.FRAME_H, MV.FRAME_X, MV.FRAME_Y,
        MV.PANEL_Y, MV.CIRCLE_R))
    print("裁切框 %s（源 %dx%d）→ 画面缩放 %.3f" % (
        MV._CROP[0], MV.SRC_SIZE[0], MV.SRC_SIZE[1],
        MV.FRAME_W / (MV._CROP[0][2] - MV._CROP[0][0])))
    print("定格标注 %d 条：%s" % (len(ann), " | ".join(a["text"] for a in ann)))
    for o in outs:
        print(o)


if __name__ == "__main__":
    main()
