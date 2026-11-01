#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地投篮分析数据（tags + 逐球指标 CSV）回填到 Supabase。

前置：已在 Supabase SQL Editor 跑过 supabase/schema.sql（建表）。

用法：
    python3 tools/sync_to_supabase.py            # 真实写入（幂等 upsert）
    python3 tools/sync_to_supabase.py --dry-run  # 只打印将要写入的行数
    python3 tools/sync_to_supabase.py --reset    # 先清空 shooting_* 再写入

依赖：只用标准库；鉴权读 .env.supabase。
"""
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_client import SupabaseREST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# 场次元数据（来自 data/tags.json + 各报告头；时长单位秒）
SESSIONS = [
    dict(tag="c",  date="2026-09-12", label="9月12日 · 第 2 段（11 球）", video="9月12日(2).mp4",
         fps=30, resolution="1920×1080", duration_s=148.2, scanned_range="0:00–2:28.0",
         camera="侧后方 · 逆光", shots_identified=11, shots_measured=11,
         csv="9月12日_逐球指标.csv", report_path="9月12日_投篮分析报告.html"),
    dict(tag="b",  date="2026-09-12", label="9月12日 · 下午场（20 球）", video="9月12日(1).mp4",
         fps=30, resolution="1920×1080", duration_s=428.7, scanned_range="0:00–5:48.5",
         camera="侧后方 · 逆光", shots_identified=38, shots_measured=20,
         csv="9月12日_逐球指标.csv", report_path="9月12日_投篮分析报告.html"),
    dict(tag="d",  date="2026-09-13", label="9月13日 · 第 1 段（7 球）", video="9月13日.mp4",
         fps=60, resolution="1920×1080", duration_s=94.46, scanned_range="0:00–1:34.0",
         camera="侧后方（与9月20日疑似同场）", shots_identified=7, shots_measured=7,
         csv="9月13日_逐球指标.csv", report_path="9月13日_投篮分析报告.html"),
    dict(tag="e",  date="2026-09-13", label="9月13日 · 第 2 段（19 球）", video="9月13日(1).mp4",
         fps=60, resolution="1920×1080", duration_s=186.25, scanned_range="0:00–2:37.0",
         camera="另一侧", shots_identified=19, shots_measured=19,
         csv="9月13日_逐球指标_第2段.csv", report_path="9月13日_投篮分析报告_第2段.html"),
    dict(tag="v",  date="2026-09-20", label="9月20日 · 全场（13 球）", video="9月20日3.mp4",
         fps=60, resolution="1920×1080", duration_s=94.46, scanned_range="0:00–1:34.2",
         camera="侧后方（与9月13日疑似同场）", shots_identified=13, shots_measured=13,
         csv="9月20日_逐球指标.csv", report_path="9月20日_投篮分析报告.html"),
    dict(tag="h",  date="2026-10-06", label="10月6日 · 第 1 段（12 球）", video="10月6日（1）.mp4",
         fps=60, resolution="1920×1080", duration_s=45.35, scanned_range="0:00–0:45.0",
         camera="侧后方", shots_identified=12, shots_measured=12,
         csv="10月6日_逐球指标_第1段.csv", report_path="10月6日_投篮分析报告_第1段.html"),
    dict(tag="f",  date="2026-10-06", label="10月6日 · 第 2 段（11 球，与第1段重叠）", video="10月6日（2）.mp4",
         fps=60, resolution="1920×1080", duration_s=39.87, scanned_range="0:00–0:39.8",
         camera="侧后方", shots_identified=11, shots_measured=11,
         csv="10月6日_逐球指标.csv", report_path="10月6日_投篮分析报告.html"),
]

# CSV 列 → 数据库列
COLMAP = {
    "屈膝角(最低)": "knee_angle_min",
    "出手时膝角": "knee_angle_release",
    "出手时肘角_左": "elbow_angle_release_guide",
    "出手时肘角_右": "elbow_angle_release_shooting",
    "收球肘角_左": "elbow_angle_gather_guide",
    "收球肘角_右": "elbow_angle_gather_shooting",
    "出手小臂仰角": "forearm_angle",
    "出手点离地高度": "release_height_ratio",
    "垂直弹跳": "vertical_jump_ratio",
    "屈膝到出手": "knee_to_release_s",
    "膝伸展到出手": "knee_ext_to_release_s",
    "身体位移": "body_drift_ratio",
    "落地位移": "landing_drift_ratio",
}


def fnum(v):
    v = (v or "").strip()
    return None if v == "" else float(v)


def load_ranks(tag):
    """读 data/<tag>_ranks.json → {round(t,1): (score, rank)}"""
    p = os.path.join(DATA, f"{tag}_ranks.json")
    if not os.path.exists(p):
        return {}
    out = {}
    for r in json.load(open(p, encoding="utf-8")):
        out[round(float(r["t"]), 1)] = (r.get("score"), r.get("rank"))
    return out


def build_shots(sess):
    rows = []
    with open(os.path.join(ROOT, sess["csv"]), encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate([r for r in reader if r["tag"].strip() == sess["tag"]], 1):
            d = {
                "session_tag": sess["tag"],
                "seq": i,
                "t_rel": fnum(row["时间(秒)"]),
            }
            for csv_col, db_col in COLMAP.items():
                d[db_col] = fnum(row.get(csv_col, ""))
            # 误检规则：出手点 < 1.10×身高 或 弹跳 < 0.09×身高（与知识库口径一致）
            rh = d.get("release_height_ratio")
            vj = d.get("vertical_jump_ratio")
            d["is_outlier"] = bool((rh is not None and rh < 1.10) or (vj is not None and vj < 0.09))
            rows.append(d)
    ranks = load_ranks(sess["tag"])
    for d in rows:
        if d["t_rel"] is not None:
            key = round(d["t_rel"], 1)
            if key in ranks:
                d["score"], d["rank"] = ranks[key]
    return rows


def session_row(sess):
    return {k: v for k, v in sess.items() if k != "csv"}


def main():
    dry = "--dry-run" in sys.argv
    reset = "--reset" in sys.argv
    db = SupabaseREST()

    sessions = [session_row(s) for s in SESSIONS]
    shots = []
    for s in SESSIONS:
        shots.extend(build_shots(s))

    print(f"sessions: {len(sessions)}   shots: {len(shots)}")
    for s in sessions:
        n = sum(1 for x in shots if x["session_tag"] == s["tag"])
        print(f"  {s['tag']}  {s['label']:<28} -> {n} 球")

    if dry:
        print("\n[--dry-run] 未写入")
        return

    if reset:
        # 先删 shots（外键）再删 sessions
        st, _ = db.delete("shooting_shots", {"session_tag": "not.is.null"})
        st2, _ = db.delete("shooting_sessions", {"tag": "not.is.null"})
        print(f"reset: shots={st} sessions={st2}")

    st1, body1 = db.upsert("shooting_sessions", sessions, on_conflict="tag")
    print(f"upsert sessions -> {st1}")
    if st1 >= 400:
        print("ERR", body1)
        return

    st2, body2 = db.upsert("shooting_shots", shots, on_conflict="session_tag,seq")
    print(f"upsert shots -> {st2}")
    if st2 >= 400:
        print("ERR", body2)
        return

    # 校验
    st3, rows3 = db.select("shooting_sessions", select="tag", params={"order": "tag"})
    st4, agg = db.select("shooting_session_agg", select="tag,session_date,n_shots,knee_angle_min_avg")
    print(f"verify sessions={st3} ({len(rows3) if isinstance(rows3, list) else rows3} rows)")
    print(f"verify agg={st4}: {agg if st4 < 300 else ''}")


if __name__ == "__main__":
    main()
