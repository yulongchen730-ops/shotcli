# -*- coding: utf-8 -*-
"""一份脚本生成多份报告（每个日期一份独立 HTML/CSV），可传日期筛选。
用法：
  python report_all.py               # 全部
  python report_all.py 9月13日       # 只出 9月13日
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R   # noqa: E402

BASE = R.BASE
SETS = [
    ("9月13日", [
        {"tag": "d", "file": os.path.join(BASE, "9月13日.mp4"),
         "title": "9月13日 · 第 1 段（本次训练）"},
    ]),
    ("9月13日第2段", [
        {"tag": "e", "file": os.path.join(BASE, "9月13日(1).mp4"),
         "title": "9月13日 · 第 2 段（1080p60，3 分钟）"},
    ]),
    ("9月12日", [
        {"tag": "c", "file": os.path.join(BASE, "9月12日(2).mp4"),
         "title": "9月12日 · 第 3 段（2.5 分钟）"},
        {"tag": "b", "file": os.path.join(BASE, "9月12日(1).mp4"),
         "title": "9月12日 · 下午场（7 分钟）"},
    ]),
]


def main():
    only = sys.argv[1:]
    for date, vids in SETS:
        if only and date not in only:
            continue
        R.VIDEOS = vids
        R.REPORT_DATE = date
        R.PLAYER_INITS.clear()     # 关键：跨报告不能累加，否则播放器编号会串
        R.main(4)


if __name__ == "__main__":
    main()
