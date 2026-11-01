# -*- coding: utf-8 -*-
"""README 推广 banner：真实投篮帧 + 量程条 + 跨训练日「复利」趋势

为什么不直接用 cover_tech.py 的输出：
    cover_tech 是**小红书/抖音封面**，标题走的是个人 IP 话术（投篮精进 / 日拱一卒），
    那是运营物料，不属于开源仓库。仓库 banner 要的是「工具能力 + 一句钩子」。
    本脚本复用 cover_tech 已经做好的视觉语言（真实帧 / 骨架 / 量程条），
    只在底部追加一段跨训练日趋势 —— 它证明的不是"我能测一球"，而是"这东西会累积"。

用法:
    python promo_banner.py <底图png> [输出png]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw, ImageFont
import fontkit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_H = 340

# 与 cover_tech.py 保持同一套语义化配色
C_TX = (238, 245, 252)
C_MUT = (128, 144, 160)
C_DIM = (92, 106, 120)
C_RULE = (46, 62, 76)
C_CYAN = (45, 214, 245)
C_AMBER = (255, 184, 48)
C_BAND = (22, 84, 104)
C_STEEL = (172, 204, 224)

_DIN = "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf"
_MONO = "/System/Library/Fonts/SFNSMono.ttf"


def din(size):
    try:
        return ImageFont.truetype(_DIN, size)
    except Exception:
        return fontkit.load(size, True)


def mono(size):
    try:
        return ImageFont.truetype(_MONO, size)
    except Exception:
        return fontkit.load(size, False)


def cf(size):
    return fontkit.load(size, True)


# ------------------------------------------------------------------ 数据
def trend_by_date():
    """按训练日汇总「屈膝角(最低)」均值（跨 tag 合并，按球数加权 = 直接池化所有球）。"""
    import report as R
    tags = json.load(open(os.path.join(R.DATA, "tags.json"), encoding="utf-8"))
    buckets = {}
    for tag, info in tags.items():
        date = info.get("date")
        if not date:
            continue
        for s in R.load_shots(tag):
            m = R.shot_metrics(s)
            if m and m.get("屈膝角(最低)") is not None:
                buckets.setdefault(date, []).append(m["屈膝角(最低)"])
    if not buckets:
        return []

    def key(d):
        import re
        mm = re.match(r"(\d+)月(\d+)日", d)
        return (int(mm.group(1)), int(mm.group(2))) if mm else (99, 99)

    out = []
    for date in sorted(buckets, key=key):
        vals = buckets[date]
        out.append({"date": date, "n": len(vals), "v": sum(vals) / len(vals)})
    return out


def ref_range():
    import report as R
    lo, hi = R.REFERENCE["屈膝角(最低)"][0], R.REFERENCE["屈膝角(最低)"][1]
    return float(lo), float(hi)


# ------------------------------------------------------------------ 绘制
def build(base_path, out_path):
    base = Image.open(base_path).convert("RGB")
    W, H0 = base.size
    H = H0 + PANEL_H

    rows = trend_by_date()
    if len(rows) < 2:
        sys.exit("趋势数据不足，先跑完多场分析")

    # 底部面板底色：直接取底图最下一行的颜色，做到无缝
    bg = base.getpixel((4, H0 - 2))
    img = Image.new("RGB", (W, H), bg)
    img.paste(base, (0, 0))
    d = ImageDraw.Draw(img)

    y0 = H0
    d.rectangle((0, y0, W, y0 + 2), fill=C_RULE)

    lo_ref, hi_ref = ref_range()
    vals = [r["v"] for r in rows]
    vmin = min(min(vals), lo_ref) - 6
    vmax = max(max(vals), hi_ref) + 4

    # ---------------- 左侧文案块 ----------------
    LX = 70
    d.rectangle((LX, y0 + 50, LX + 10, y0 + 64), fill=C_CYAN)
    d.text((LX + 24, y0 + 46), "复利 · 同一把尺子量到底", font=cf(27), fill=C_TX, anchor="la")

    n_total = sum(r["n"] for r in rows)
    d.text((LX, y0 + 92), "屈膝角(最低) · %d 个训练日 · %d 球" % (len(rows), n_total),
           font=cf(19), fill=C_MUT, anchor="la")

    big = din(46)
    d.text((LX, y0 + 126), "%.1f°" % vals[0], font=big, fill=C_STEEL, anchor="la")
    w_first = d.textlength("%.1f°" % vals[0], font=big)
    d.text((LX + w_first + 12, y0 + 146), "→", font=cf(24), fill=C_DIM, anchor="la")
    d.text((LX + w_first + 48, y0 + 126), "%.1f°" % vals[-1], font=big, fill=C_AMBER, anchor="la")

    drop = vals[0] - vals[-1]
    msg = ("越练越深，已跌出参考区间" if vals[-1] < lo_ref
           else ("回到区间内" if vals[-1] >= lo_ref else ""))
    d.text((LX, y0 + 196), msg, font=cf(21), fill=C_AMBER, anchor="la")
    d.text((LX, y0 + 232), "−%.1f°  ·  肉眼无感，只有逐帧能看见" % abs(drop),
           font=cf(17), fill=C_DIM, anchor="la")

    # ---------------- 右侧趋势图 ----------------
    cx0, cx1 = 470, W - 70
    cy0, cy1 = y0 + 44, y0 + 232

    def ypx(v):
        return cy1 - (v - vmin) / (vmax - vmin) * (cy1 - cy0)

    # 参考区间色带
    by0, by1 = ypx(hi_ref), ypx(lo_ref)
    d.rectangle((cx0, by0, cx1, by1), fill=C_BAND)
    d.text((cx1 - 4, by0 - 24), "参考区间 %.0f–%.0f°" % (lo_ref, hi_ref),
           font=cf(16), fill=C_STEEL, anchor="ra")
    for yy in (by0, by1):
        d.rectangle((cx0, yy, cx1, yy + 1), fill=(40, 120, 140))

    # 折线
    n = len(rows)
    step = (cx1 - cx0) / (n - 1)
    pts = [(cx0 + i * step, ypx(r["v"])) for i, r in enumerate(rows)]
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=C_CYAN, width=3)

    for i, (px, py) in enumerate(pts):
        last = i == len(pts) - 1
        r = 7 if last else 5
        d.ellipse((px - r, py - r, px + r, py + r),
                  fill=(C_AMBER if last else C_CYAN), outline=bg, width=2)
        d.text((px, py - (26 if not last else 30)), "%.1f" % rows[i]["v"],
               font=din(19), fill=(C_AMBER if last else C_STEEL), anchor="mb")
        d.text((px, cy1 + 14), rows[i]["date"].replace("月", "/").replace("日", ""),
               font=mono(17), fill=C_MUT, anchor="ma")
        d.text((px, cy1 + 38), "%d球" % rows[i]["n"], font=cf(15), fill=C_DIM, anchor="ma")

    img.save(out_path)
    print("输出 %s  %dx%d  %.0f KB" % (out_path, W, H, os.path.getsize(out_path) / 1024))
    for r in rows:
        print("   %s  n=%2d  %.1f°" % (r["date"], r["n"], r["v"]))
    return out_path


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "抖音封面", "9月20日_封面_横版_1440x1080_科技风_推广.png")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "assets", "promo-banner.png")
    build(base, out)
