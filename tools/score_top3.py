# -*- coding: utf-8 -*-
"""给某个 tag 的每一球打分，输出 data/<tag>_top3.json（makevid.py / makecharts.py 按它排序）

评分权重（与配图图注一致）：
    轨迹直度 25% · 小臂仰角 20% · 投篮手肘角 20% · 屈膝 15% · 出手点 10% · 节奏 10%

每个分项把指标映射成 0–100 分（离目标越远扣越多），再加权求和；
某项数据缺失（轨迹跟踪失败等）时按剩余权重归一化，不直接判 0。

用法:
    python score_top3.py <tag>                  全量打分 → data/<tag>_top3.json（取分数最高的 3 个）
    python score_top3.py <tag> --first 5        按时间取最早 5 个 → data/<tag>_first5.json
    python score_top3.py <tag> --pick 16.4,65.4,78.0
                                                用户看过全量排名后指定出哪几个 → data/<tag>_picked.json

无论哪种模式，**都会**额外写一份 data/<tag>_ranks.json（全量排名，按分数降序），
因为「先把视频里所有球都打分排序、再由人决定讲哪几个」是既定口径（用户 2026-10-12 定）：
出片前必须先把全量排名摆给人看，不允许静默替你挑。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R

TAG = sys.argv[1] if len(sys.argv) > 1 else "f"


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def near(v, target, tol):
    """离目标值越近分越高：目标处 100，偏离 tol 及以上 0"""
    if v is None:
        return None
    return clamp(100.0 * (1.0 - abs(v - target) / tol))


def main(tag):
    shots = R.load_shots(tag)
    ms = [m for m in (R.shot_metrics(s) for s in shots) if m]
    balls = {}
    for fp in glob.glob(os.path.join(R.DATA, tag, "ball_*.json")):
        try:
            b = json.load(open(fp))
            balls[round(b["t_rel"], 1)] = b.get("fit") or {}
        except Exception:
            pass

    rows = []
    for m in ms:
        t = round(m["t_rel"], 1)
        dev = (balls.get(t) or {}).get("dev_rel")
        parts = [
            ("轨迹直度", near(dev, 0.0, 0.30), 0.25),
            ("小臂仰角", near(m.get("出手小臂仰角"), 78.7, 25.0), 0.20),
            ("投篮手肘角", near(m.get("出手时肘角_右"), 155.0, 25.0), 0.20),
            ("屈膝", near(m.get("屈膝角(最低)"), 110.0, 22.0), 0.15),
            ("出手点", near(m.get("出手点离地高度"), 1.29, 0.15), 0.10),
            ("节奏", near(m.get("屈膝到出手"), 0.45, 0.22), 0.10),
        ]
        num = sum(w * s for _, s, w in parts if s is not None)
        den = sum(w for _, s, w in parts if s is not None)
        if den <= 0:
            continue
        rows.append({
            "t": t, "score": round(num / den, 1), "cover": round(den, 2),
            "parts": {k: (None if s is None else round(s)) for k, s, _ in parts},
            "dev_rel": dev,
        })

    # ★ 全量排名无条件落盘 —— 「所有球都被打分排序」这件事必须有据可查，
    #   哪怕最后只讲其中几个。排序口径：综合评分降序。
    ranks = sorted(rows, key=lambda r: -r["score"])
    for i, r in enumerate(ranks, 1):
        r["rank"] = i
    fp_ranks = os.path.join(R.DATA, "%s_ranks.json" % tag)
    json.dump(ranks, open(fp_ranks, "w"), ensure_ascii=False, indent=1)

    if "--pick" in sys.argv:
        spec = sys.argv[sys.argv.index("--pick") + 1]
        want = [float(x) for x in spec.replace("，", ",").split(",") if x.strip()]
        by_t = {r["t"]: r for r in ranks}
        sel = []
        for w in want:
            hit = min(by_t, key=lambda t: abs(t - w))
            if abs(hit - w) > 0.6:
                print("！没有出手时间 %.1fs 的球（最近的是 %.1fs），跳过" % (w, hit))
                continue
            sel.append(by_t[hit])
        out = os.path.join(R.DATA, "%s_picked.json" % tag)
        print("按指定时间挑 %d 球（顺序照给定的走）" % len(sel))
    elif "--first" in sys.argv:
        n = int(sys.argv[sys.argv.index("--first") + 1])
        rows_t = sorted(rows, key=lambda r: r["t"])          # 按出手时间，取最早的 N 球
        sel = rows_t[:n]
        out = os.path.join(R.DATA, "%s_first%d.json" % (tag, n))
        print("按时间取前 %d 球（不是按分数）" % n)
    else:
        sel = ranks[:3]                                       # 按分数取最高 3 个
        out = os.path.join(R.DATA, "%s_top3.json" % tag)
    top3 = [{"t": r["t"], "score": r["score"]} for r in sel]
    json.dump(top3, open(out, "w"), ensure_ascii=False)
    print("写出", out, top3)
    print("全量排名", fp_ranks)
    print("%-4s %-8s %-7s %-6s %s" % ("名次", "出手(s)", "评分", "覆盖", "分项"))
    for r in ranks:
        print("%-4d %-8.1f %-7.1f %-6.2f %s" % (r["rank"], r["t"], r["score"], r["cover"],
              " ".join("%s=%s" % (k, v) for k, v in r["parts"].items())))
    return out


if __name__ == "__main__":
    main(TAG)
