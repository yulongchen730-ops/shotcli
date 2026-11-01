#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""粗扫诊断：为什么没识别到出手事件？

「事件数 = 0」是最容易让人白忙一场的情况 —— 后面所有阶段都会空跑。
这里把粗扫信号本身拆开看，直接给出原因和下一步。

用法: python3 diag_scan.py <tag>
"""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbkit import frame_features, SIDES          # noqa: E402
from shots import detect_events, load_sig        # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 事件检测的判据（与 shots.detect_events 保持一致）
EXIST, MIN_PEAK = 0.03, 0.08


def main(tag):
    p = os.path.join(DATA, "%s_coarse.jsonl" % tag)
    if not os.path.exists(p):
        print("找不到粗扫数据：%s\n先跑 `shotcli run %s --to scan`" % (p, tag))
        return 2
    sig = load_sig(p)
    n = len(sig)
    span = (sig[-1]["t"] - sig[0]["t"]) if n else 0.0
    nkp = sum(1 for r in sig if r.get("kp"))
    evs = detect_events(sig)

    vals = {s: [] for s in SIDES}
    for r in sig:
        if not r.get("kp"):
            continue
        f = frame_features({"t": r["t"], "i": 0, "kp": r["kp"]})
        if not f:
            continue
        for s in SIDES:
            v = f[s]["wr_over_sh"]
            if v is not None:
                vals[s].append(v)

    print("=" * 66)
    print("粗扫诊断  tag=%s" % tag)
    print("=" * 66)
    print("  采样点      %d（覆盖 %.1fs）" % (n, span))
    print("  关键点可用  %d/%d = %.0f%%" % (nkp, n, 100.0 * nkp / n if n else 0))
    for s in SIDES:
        v = vals[s]
        if not v:
            print("  %-4s 手腕过肩  没有可用帧" % s)
            continue
        over = sum(1 for x in v if x > EXIST)
        print("  %-4s 手腕过肩  中位 %+.3f｜最高 %+.3f｜超阈值 %d/%d 帧"
              % (s, st.median(v), max(v), over, len(v)))
    print("  判定        判据：手腕过肩 >%.2f 且峰值 ≥%.2f → 事件 %d 次"
          % (EXIST, MIN_PEAK, len(evs)))

    print("\n" + "-" * 66)
    if evs and len(evs) > 0:
        print("✓ 有事件，可继续跑：shotcli run %s --from shots --to report" % tag)
        for e in evs[:12]:
            print("    t=%.2fs  %s  peak=%+.3f  dur=%.2fs"
                  % (e["t"], e.get("hand", "?"), e["peak"], e["end"] - e["start"]))
        return 0

    ratio = (nkp / n) if n else 0
    hi = max([max(vals[s]) for s in SIDES if vals[s]], default=None)
    print("✗ 没识别到出手事件。原因按可能性排序：\n")
    if ratio < 0.5:
        print("  ① 姿态识别覆盖不足（关键点只有 %.0f%% 的采样点可用，正常应 >90%%）" % (100 * ratio))
        print("     → 机位太远/逆光/人物被挡/球衣与背景接近。按《拍摄要点》把机位挪到侧面 90°、")
        print("       人物占画面高度 60–70%、顺光拍，重新录一段再跑。")
    if hi is not None and hi <= EXIST:
        print("  %s 手腕全程没高过肩（最高 %+.3f，判据 %.2f）"
              % ("②" if ratio < 0.5 else "①", hi, EXIST))
        print("     → 这段视频里**没有投篮动作**：可能是纯运球/上篮/热身，或机位没拍到出手。")
    if hi is not None and hi > EXIST:
        print("  %s 手腕高过肩了但没到峰值 %.2f —— 像是投篮，只是幅度不够/被截断" % (
            "②" if ratio < 0.5 else "①", MIN_PEAK))
        print("     → 确认这段确实是投篮后，把阈值放宽再试：")
        print("       编辑 scripts/shots.py 的 detect_events(sig, min_peak=0.05)")
    print("\n  兜底：也可以直接指定时间点硬跑深扫，绕开事件检测 ——")
    print("    shotcli run %s --from deep    # 需要 data/%s/shot_*.json 存在" % (tag, tag))
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
