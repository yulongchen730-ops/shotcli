# -*- coding: utf-8 -*-
"""诊断：tag v 各球的「出手节奏」在不在参考区间、以及现有判定链路实际会标出什么。

用法: diag_tempo.py [tag]
"""
import glob
import json
import os
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
os.chdir(TOOLS)

TAG = sys.argv[1] if len(sys.argv) > 1 else "v"
sys.argv = ["makevid.py", TAG]          # makevid 在 import 期读 argv
import report as R
import makevid as V

R.apply_tag(TAG)
shots = R.load_shots(TAG)
ms = {}
for s in shots:
    m = R.shot_metrics(s)
    if m:
        ms[round(m["t_rel"], 1)] = m
agg = R.aggregate(list(ms.values()), R.METRIC_KEYS)

KEY = "屈膝到出手"
lo, hi = R.REFERENCE[KEY][0], R.REFERENCE[KEY][1]
span = max(hi - lo, 1e-6)
a = agg[KEY]
print("参考区间 %s = %.2f–%.2f（span %.3f）来源：%s" % (KEY, lo, hi, span, R.REFERENCE[KEY][4][:60]))
print("全场 mean=%.3f sd=%.3f cv=%.1f%% n=%s" % (
    a["mean"], a.get("sd", 0), (a.get("sd", 0) / a["mean"] * 100) if a.get("mean") else 0, a.get("n")))
print("=" * 118)

files = sorted(glob.glob(os.path.join(R.DATA, TAG, "deep*_*.json")),
               key=lambda f: float(os.path.basename(f).split("_")[-1][:-5]))
for t_rel in sorted(ms):
    m = ms[t_rel]
    v = m.get(KEY)
    t_dip = t_rel - (v or 0.45)
    if v is None:
        print("t=%-6.1f  节奏=—" % t_rel)
        continue
    dev = (lo - v) / span if v < lo else ((v - hi) / span if v > hi else 0.0)
    txt, cls = R.qual(KEY, v)
    flag = "★超出" if dev > 0 else " 区间内"
    fp = min(files, key=lambda f: abs(float(os.path.basename(f).split("_")[-1][:-5]) - t_rel))
    dj = json.load(open(fp))
    seq = R.deflip(dj["seq"])
    issues = V.find_issues(m, seq, t_rel, t_dip, agg)
    print("t=%-6.1f  节奏=%.2fs  dev=%+.2f %s  qual=%-6s | 定格标注 %d 条: %s"
          % (t_rel, v, dev, flag, txt or "-", len(issues),
             " ／ ".join("[i=%d]%s" % (i["idx"], i["text"]) for i in issues) or "无"))
