# -*- coding: utf-8 -*-
"""生成分析报告的配图（深色主题，与 HTML 报告一致）

产出 5 张图到 投篮分析/配图/：
  1 关键指标总览（KPI 卡 + 文献区间条）
  2 多球曲线对比（手腕高度 / 膝角 / 投篮手肘角 / 小臂仰角）
  3 逐球诊断矩阵（7 球 × 8 指标，按文献区间标色）
  4 出手瞬间七连图（骨架 + 每球数据标注）
  5 结论与训练处方

用法: python makecharts.py [tag]
"""
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import Quartz
import fontkit
import report as R

TAG = sys.argv[1] if len(sys.argv) > 1 else "d"
VERT = len(sys.argv) > 2 and sys.argv[2].startswith("v")
R.apply_tag(TAG)      # 让 R.REPORT_DATE / HEIGHT_CM 跟着这个 tag 走（别靠 sys.argv[2] 猜）
OUTDIR = os.path.join(R.ROOT, "配图", "竖版") if VERT else os.path.join(R.ROOT, "配图")
os.makedirs(OUTDIR, exist_ok=True)

BG = (13, 17, 23)
CARD = (22, 27, 34)
CARD2 = (28, 34, 42)
LINE = (48, 54, 61)
TX = (230, 237, 243)
TX2 = (139, 148, 158)
ACC = (88, 166, 255)
GOOD = (63, 185, 80)
WARN = (210, 153, 34)
BAD = (248, 81, 73)
YEL = (255, 209, 102)
PINK = (255, 143, 171)
CYAN = (34, 211, 238)

SERIES = [(255, 107, 107), (34, 211, 238), (255, 209, 102), (197, 146, 234),
          (63, 185, 80), (255, 143, 171), (88, 166, 255)]

_FONT_CACHE = {}
FSCALE = 1.0        # 竖版模式下全局放大字体


def font(size, bold=False):
    # 字体选择统一走 fontkit（它保证 PingFang 取的是简体 SC 那一支，
    # 加粗不再错取繁体的 TC —— 否则「稳」这类简体字会静默变成空白）
    size = int(round(size * FSCALE))
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = fontkit.load(size, bold)
    _FONT_CACHE[key] = f
    return f


def new_canvas(w, h):
    im = Image.new("RGB", (w, h), BG)
    return im, ImageDraw.Draw(im)


def rrect(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def text(d, xy, s, size=22, color=TX, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=color, anchor=anchor)


def wrap(d, s, size, max_w, bold=False):
    """按像素宽度对中文/英文混排做折行"""
    f = font(size, bold)
    lines, cur = [], ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if d.textlength(cur + ch, font=f) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def fmt(v, dig=0, unit=""):
    if v is None:
        return "—"
    return f"{v:.{dig}f}{unit}"


def qual(k, v):
    """返回 (文本, 颜色, 判定类别)"""
    if v is None or k not in R.REFERENCE:
        return "—", TX2, "na"
    lo, hi = R.REFERENCE[k][0], R.REFERENCE[k][1]
    if lo <= v <= hi:
        return "区间内", GOOD, "ok"
    span = max(hi - lo, 1e-6)
    dev = (lo - v) / span if v < lo else (v - hi) / span
    if dev < 0.5:
        return ("偏低" if v < lo else "偏高"), WARN, "warn"
    return ("明显偏低" if v < lo else "明显偏高"), BAD, "bad"


# ------------------------------------------------------------------ 数据准备
def load_data():
    shots = R.load_shots(TAG)
    ms = [m for m in (R.shot_metrics(s) for s in shots) if m]
    ms.sort(key=lambda x: x["t_rel"])
    agg = R.aggregate(ms, R.METRIC_KEYS)
    deeps = []
    files = sorted(glob.glob(os.path.join(R.DATA, TAG, "deep_*.json")),
                   key=lambda f: float(os.path.basename(f)[5:-5]))
    for fp in files:
        d = json.load(open(fp))
        seq = R.deflip(d["seq"])
        t_rel = float(os.path.basename(fp)[5:-5])
        m = min(ms, key=lambda x: abs(x["t_rel"] - t_rel))
        feats = [f for f in (R.frame_features(r) for r in seq) if f]
        deeps.append({"fp": fp, "meta": d["meta"], "feats": feats, "t_rel": m["t_rel"], "m": m})
    return ms, agg, deeps


# ------------------------------------------------------------------ 图 1 关键指标
# 判定级别 → 配色（结论类文字/数字一律走它，别再手写 GOOD/WARN/BAD）
_LEVEL_COL = {"ok": GOOD, "warn": WARN, "bad": BAD}


def _col(key, value, default=GOOD):
    """按 qual() 的判定级别取颜色；指标不在参考表里就用默认色。"""
    if value is None or key not in R.REFERENCE:
        return default
    return _LEVEL_COL.get(R.qual(key, value)[1], default)


def _cv_note(agg, key):
    """「全场最不稳的一项」这种话必须算出来，不能写死 —— 它未必就是屈膝到出手。"""
    a = agg.get(key) or {}
    txt = "CV %.1f%%" % a.get("cv", 0)
    cvk = [x for x in R.METRIC_KEYS
           if x in agg and agg[x].get("cv") is not None and x not in R.LEFT_KEYS]
    if not cvk:
        return txt
    worst = max(cvk, key=lambda x: agg[x]["cv"])
    if worst == key:
        return txt + "　全场最不稳的一项"
    return txt + "　全场最不稳的是「%s」（CV %.1f%%）" % (
        R.COVER_SHORT.get(worst, R.label(worst)), agg[worst]["cv"])


def fig_overview(ms, agg, deeps):
    W, H = 1640, 1180
    im, d = new_canvas(W, H)
    text(d, (50, 40), f"{R.REPORT_DATE} 投篮动作分析 · 关键指标总览", 44, TX, True)
    text(d, (50, 104), f"（1080p60）· 识别 {len(ms)} 次出手，全部逐帧测算 · "
                       f"手势识别 Apple Vision 19 点 · 尺度按身高 {R.HEIGHT_CM:.0f}cm 标定",
         22, TX2)

    # KPI 卡（结论与配色都由数据定：qual() 判在不在参考区间内）
    k = agg.get("出手点离地高度", {})
    j = agg.get("垂直弹跳", {})
    e = agg.get("出手时肘角_右", {})
    t = agg.get("屈膝到出手", {})
    # 本场波动最大的指标（只在对外指标里找）——不要写死说自己是「最不稳的一项」
    t_note = _cv_note(agg, "屈膝到出手")
    cards = [
        ("出手点离地高度", f"{k.get('mean', 0) * R.HEIGHT_CM:.0f} cm", f"{k.get('mean', 0):.2f} 倍身高",
         f"波动 CV {k.get('cv', 0):.1f}%　职业男 1.31–1.38", _col("出手点离地高度", k.get("mean"))),
        ("垂直弹跳（踝口径）", f"{j.get('mean', 0) * R.HEIGHT_CM:.0f} cm", f"{j.get('mean', 0):.2f} 倍身高",
         f"波动 CV {j.get('cv', 0):.1f}%　文献 2 分球 26.9cm", _col("垂直弹跳", j.get("mean"))),
        ("投篮手出手肘角", f"{e.get('mean', 0):.0f}°", R.qual("出手时肘角_右", e.get("mean"))[0],
         f"波动 CV {e.get('cv', 0):.1f}%　文献 134–138°／166–168°", _col("出手时肘角_右", e.get("mean"))),
        ("出手节奏（屈膝最低→出手）", f"{t.get('mean', 0):.2f} s", f"范围 {t.get('min', 0):.2f}–{t.get('max', 0):.2f}s",
         t_note, _col("屈膝到出手", t.get("mean"))),
    ]
    cw, ch, gap = 372, 168, 24
    for i, (k1, v1, v2, n1, col) in enumerate(cards):
        x = 50 + i * (cw + gap)
        rrect(d, (x, 152, x + cw, 152 + ch), 12, CARD, LINE)
        text(d, (x + 20, 172), k1, 21, TX2)
        text(d, (x + 20, 202), v1, 44, col, True)
        text(d, (x + 20, 258), v2, 20, TX)
        for li, ln in enumerate(wrap(d, n1, 17, cw - 40)):
            text(d, (x + 20, 288 + li * 22), ln, 17, TX2)

    # 区间条
    y0 = 360
    text(d, (50, y0 - 46), "各项指标 vs 文献区间（灰带 = 文献区间，●  = 本场均值，横线 = 本场波动范围）", 24, TX, True)
    keys = ["屈膝角(最低)", "出手时膝角", "收球肘角_右", "出手时肘角_右",
            "出手小臂仰角", "出手点离地高度", "垂直弹跳", "屈膝到出手", "身体位移"]
    row_h = 82
    bx0, bx1 = 560, 1290
    for i, key in enumerate(keys):
        if key not in agg:
            continue
        a = agg[key]
        lo, hi, unit, desc, lit = R.REFERENCE[key]
        lo, hi = float(lo), float(hi)
        y = y0 + i * row_h
        text(d, (50, y + 18), R.label(key), 23, TX)
        # 条带背景
        rrect(d, (bx0, y + 26, bx1, y + 48), 11, CARD2)
        span = max(hi - lo, 1e-6)

        def pos(v):
            if lo <= v <= hi:
                f = 0.30 + (v - lo) / span * 0.40
            elif v < lo:
                f = 0.30 - min((lo - v) / span * 0.40, 0.28)
            else:
                f = 0.70 + min((v - hi) / span * 0.40, 0.28)
            return bx0 + f * (bx1 - bx0)

        rrect(d, (pos(lo), y + 24, pos(hi), y + 50), 13, (58, 66, 76))
        mean = a["mean"]
        mn, mx = a.get("min", mean), a.get("max", mean)
        if mn is not None and mx is not None:
            d.line([(pos(mn), y + 37), (pos(mx), y + 37)], fill=(150, 160, 172), width=3)
        px = pos(mean)
        txt, col, cls = qual(key, mean)
        d.ellipse([px - 9, y + 28, px + 9, y + 46], fill=col)
        dig = 2 if unit in ("倍身高", "秒") else 0
        text(d, (1310, y + 18), f"{mean:.{dig}f}{unit}", 25, TX, True)
        text(d, (1310, y + 50), f"CV {a['cv']:.1f}%" if a.get("cv") is not None else "", 19, TX2)
        text(d, (1460, y + 20), txt, 21, col)
        dig_ref = 2 if unit in ("倍身高", "秒") else 0
        text(d, (1460, y + 48), f"文献 {lo:.{dig_ref}f}–{hi:.{dig_ref}f}{unit}", 17, TX2)
    p = os.path.join(OUTDIR, "%s_图1_关键指标总览.png" % R.REPORT_DATE)
    im.save(p)
    return p


# ------------------------------------------------------------------ 图 2 曲线
def fig_curves(ms, agg, deeps):
    W, H = 1640, 1000
    im, d = new_canvas(W, H)
    text(d, (50, 36), "多球曲线对比（按出手帧对齐）", 42, TX, True)
    text(d, (50, 96), f"{len(deeps)} 个 60fps 逐帧样本 · 黄色竖线 = 出手时刻 · 颜色对应不同球次", 21, TX2)

    panels = [("wr", "投篮手手腕高于肩（倍身高）", None),
              ("knee", "膝角（°，越小蹲得越深）", None),
              ("elbowR", "投篮手肘角（°）", None),
              ("fore", "出手小臂仰角（°）", None)]
    pw, ph = 760, 370
    for idx, (keyname, title, _) in enumerate(panels):
        col = idx % 2
        row = idx // 2
        x0 = 50 + col * (pw + 60)
        y0 = 150 + row * (ph + 70)
        rrect(d, (x0, y0, x0 + pw, y0 + ph), 12, CARD, LINE)
        text(d, (x0 + 22, y0 + 16), title, 24, TX, True)
        # 绘图区
        gx0, gx1 = x0 + 70, x0 + pw - 26
        gy0, gy1 = y0 + 64, y0 + ph - 46
        series = []
        for si, dp in enumerate(deeps):
            pts = []
            for f in dp["feats"]:
                x = f["t"] - dp["t_rel"]
                if x < -0.85 or x > 0.35:
                    continue
                if keyname == "knee":
                    v = f["right"]["knee"] if f["right"]["knee"] is not None else f["left"]["knee"]
                elif keyname == "elbowR":
                    v = f["right"]["elbow"]
                elif keyname == "fore":
                    v = f["right"]["fore_ang"]
                else:
                    v = f["right"]["wr_over_sh"]
                if v is None:
                    continue
                pts.append((x, v))
            if len(pts) >= 3:
                series.append((si, pts))
        if not series:
            continue
        allv = [v for _, pts in series for _, v in pts]
        vmin, vmax = min(allv), max(allv)
        pad = (vmax - vmin) * 0.12 + 1e-6
        vmin, vmax = vmin - pad, vmax + pad

        def sx(x):
            return gx0 + (x + 0.85) / 1.20 * (gx1 - gx0)

        def sy(v):
            return gy1 - (v - vmin) / (vmax - vmin) * (gy1 - gy0)

        # 网格
        for gi in range(5):
            yy = gy0 + gi * (gy1 - gy0) / 4
            d.line([(gx0, yy), (gx1, yy)], fill=(38, 44, 52))
            val = vmax - gi * (vmax - vmin) / 4
            text(d, (gx0 - 8, yy), f"{val:.2f}" if abs(vmax) < 5 else f"{val:.0f}", 15, TX2, anchor="ra")
        for xt in (-0.8, -0.4, 0.0, 0.3):
            xx = sx(xt)
            d.line([(xx, gy0), (xx, gy1)], fill=(38, 44, 52))
        # 出手线
        d.line([(sx(0), gy0), (sx(0), gy1)], fill=YEL, width=2)
        for si, pts in series:
            col_ = SERIES[si % len(SERIES)]
            d.line([(sx(x), sy(v)) for x, v in pts], fill=col_, width=3, joint="curve")
        text(d, (gx1, gy1 + 8), "时间（秒，0 = 出手）", 16, TX2, anchor="ra")
    p = os.path.join(OUTDIR, "%s_图2_多球曲线对比.png" % R.REPORT_DATE)
    im.save(p)
    return p


# ------------------------------------------------------------------ 图 3 逐球矩阵
def fig_matrix(ms, agg):
    keys = [("屈膝角(最低)", "屈膝角", 0), ("出手时膝角", "出手时膝角", 0),
            ("收球肘角_右", "收球肘角\n(投篮手)", 0), ("出手时肘角_右", "出手肘角\n(投篮手)", 0),
            ("出手时肘角_左", "出手肘角\n(辅助手)", 0), ("出手小臂仰角", "小臂仰角", 0),
            ("出手点离地高度", "出手点高度", 2), ("垂直弹跳", "垂直弹跳", 2),
            ("屈膝到出手", "出手节奏", 2)]
    W = 1640
    row_h = 104
    H = 250 + row_h * len(ms) + 40
    im, d = new_canvas(W, H)
    text(d, (50, 36), "逐球诊断矩阵", 42, TX, True)
    text(d, (50, 96), "每格按文献区间标色：绿=区间内　黄=略偏　红=明显偏　灰=无数据。"
                      "括号内为该球相对全场的偏离", 21, TX2)
    x0 = 60
    label_w = 130
    cell_w = (W - x0 - 60 - label_w) // len(keys)
    # 表头
    hy = 150
    text(d, (x0, hy + 16), "球次", 21, TX2)
    for ci, (_, name, _) in enumerate(keys):
        cx = x0 + label_w + ci * cell_w + cell_w // 2
        for li, ln in enumerate(name.split("\n")):
            text(d, (cx, hy + 4 + li * 22), ln, 18, TX, anchor="ma")
    d.line([(x0, hy + 56), (W - 60, hy + 56)], fill=LINE, width=2)
    means = {k: (agg[k]["mean"] if k in agg else None) for k, _, _ in keys}
    for ri, m in enumerate(ms):
        y = hy + 66 + ri * row_h
        rrect(d, (x0, y, W - 60, y + row_h - 12), 10, CARD)
        text(d, (x0 + 16, y + 16), f"第 {ri + 1} 次", 21, TX, True)
        text(d, (x0 + 16, y + 46), f"{int(m['t_rel'] // 60)}:{m['t_rel'] % 60:04.1f}", 18, TX2)
        for ci, (k, _, dig) in enumerate(keys):
            v = m.get(k)
            cx = x0 + label_w + ci * cell_w
            if v is None:
                text(d, (cx + cell_w // 2, y + 28), "—", 22, TX2, anchor="ma")
                continue
            unit = R.REFERENCE[k][2] if k in R.REFERENCE else ""
            if k == "出手点离地高度" or k == "垂直弹跳":
                s = f"{v:.2f}"
            elif unit == "秒":
                s = f"{v:.2f}s"
            else:
                s = f"{v:.0f}°"
            _, col, cls = qual(k, v)
            text(d, (cx + cell_w // 2, y + 10), s, 25, col, True, anchor="ma")
            mu = means.get(k)
            if mu:
                dev = (v - mu) / (abs(mu) or 1) * 100
                text(d, (cx + cell_w // 2, y + 44), f"{dev:+.0f}%", 17, TX2, anchor="ma")
            text(d, (cx + cell_w // 2, y + 66), "区间内" if cls == "ok" else
                 ("偏低" if cls == "warn" and v < R.REFERENCE[k][0] else
                  ("偏高" if cls == "warn" else
                   ("明显偏低" if v < R.REFERENCE[k][0] else "明显偏高"))), 16, col, anchor="ma")
    text(d, (x0, H - 28), "注：辅助手肘角偶有识别异常（手臂在头顶交叉时左右标签跳变，如出现 20° 量级的数值请忽略）；"
                          "出手点高度与垂直弹跳以本球场比例尺换算。", 17, TX2)
    p = os.path.join(OUTDIR, "%s_图3_逐球诊断矩阵.png" % R.REPORT_DATE)
    im.save(p)
    return p


# ------------------------------------------------------------------ 图 4 出手瞬间
EDGES = [("neck_1_joint", "head_joint", (0.31, 0.63, 1.0)),
         ("neck_1_joint", "right_shoulder_1_joint", (1.0, 0.42, 0.42)),
         ("right_shoulder_1_joint", "right_forearm_joint", (1.0, 0.42, 0.42)),
         ("right_forearm_joint", "right_hand_joint", (1.0, 0.42, 0.42)),
         ("neck_1_joint", "left_shoulder_1_joint", (0.13, 0.83, 0.93)),
         ("left_shoulder_1_joint", "left_forearm_joint", (0.13, 0.83, 0.93)),
         ("left_forearm_joint", "left_hand_joint", (0.13, 0.83, 0.93)),
         ("right_shoulder_1_joint", "right_upLeg_joint", (1.0, 0.56, 0.67)),
         ("right_upLeg_joint", "right_leg_joint", (1.0, 0.56, 0.67)),
         ("left_shoulder_1_joint", "left_upLeg_joint", (1.0, 0.56, 0.67)),
         ("left_upLeg_joint", "left_leg_joint", (1.0, 0.56, 0.67)),
         ("right_upLeg_joint", "left_upLeg_joint", (1.0, 0.56, 0.67)),
         ("right_leg_joint", "right_foot_joint", (0.78, 0.57, 0.92)),
         ("left_leg_joint", "left_foot_joint", (0.78, 0.57, 0.92))]


def render_frame(dp, idx, out, ball=None, lw=5.0):
    """把某一帧的画面 + 骨架 + 球轨迹渲染成 jpg"""
    from bbkit import load_cg, save_jpeg
    rec = dp["meta"]
    seq_imgs = None
    fp = dp["fp"]
    dj = json.load(open(fp))
    r = dj["seq"][idx]
    cg = load_cg(os.path.join(R.ROOT, "frames", TAG, r["img"]))
    if cg is None:
        return None
    W, H = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, W, H), cg)
    kp = r.get("kp") or {}
    Quartz.CGContextSetLineWidth(ctx, lw)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for a, b, c in EDGES:
        p, q = kp.get(a), kp.get(b)
        if not (p and q):
            continue
        Quartz.CGContextSetRGBStrokeColor(ctx, c[0], c[1], c[2], 0.96)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextMoveToPoint(ctx, p[0], H - p[1])
        Quartz.CGContextAddLineToPoint(ctx, q[0], H - q[1])
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 0.95)
    for v in kp.values():
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(v[0] - 4, H - v[1] - 4, 8, 8))
    img = Quartz.CGBitmapContextCreateImage(ctx)
    save_jpeg(img, out, 0.92)
    return out


def fig_release(ms, agg, deeps):
    from PIL import Image as I
    titles = []
    cols = 4
    cell_w, cell_h = 380, 560
    rows = math.ceil(len(deeps) / cols)
    W = 60 + cols * cell_w + 20 * (cols - 1) + 60
    H = 200 + rows * cell_h + 30
    im, d = new_canvas(W, H)
    text(d, (50, 36), "出手瞬间逐球对比（骨架 + 关键数据）", 42, TX, True)
    text(d, (50, 96), "每格取该球出手那一帧；红色骨架 = 投篮手（右手），青色 = 辅助手，粉色 = 躯干与腿", 21, TX2)
    for i, dp in enumerate(deeps):
        r = i % cols
        c = i // cols
        x = 60 + r * (cell_w + 20)
        y = 170 + c * cell_h
        idx = min(int(dp["m"]["t_rel"] and 0) + 40, 0)
        # 出手帧索引：seq 里 t >= t_rel 的第一帧
        dj = json.load(open(dp["fp"]))
        ridx = next((k for k, rec in enumerate(dj["seq"]) if rec["t"] >= dp["t_rel"] - 1e-6), len(dj["seq"]) // 2)
        tmp = "/tmp/chart_frame_%d.jpg" % i
        if render_frame(dp, ridx, tmp) is None:
            continue
        img = I.open(tmp)
        iw, ih = img.size
        th = cell_h - 118
        tw = int(iw * th / ih)
        img = img.resize((tw, th), I.LANCZOS)
        rrect(d, (x, y, x + cell_w, y + cell_h - 12), 12, CARD, LINE)
        im.paste(img, (x + (cell_w - tw) // 2, y + 10))
        m = dp["m"]
        text(d, (x + 16, y + th + 20), f"第 {i + 1} 次　{int(m['t_rel'] // 60)}:{m['t_rel'] % 60:04.1f}", 23, TX, True)
        rows_txt = [
            f"屈膝 {fmt(m['屈膝角(最低)'])}°　投篮手肘 {fmt(m['出手时肘角_右'])}°",
            f"小臂仰角 {fmt(m['出手小臂仰角'])}°　出手点 {fmt(m['出手点离地高度'], 2)}×身高",
            f"节奏 {fmt(m['屈膝到出手'], 2)}s　垂直弹跳 {fmt(m['垂直弹跳'], 2)}×身高",
        ]
        for li, ln in enumerate(rows_txt):
            text(d, (x + 16, y + th + 52 + li * 26), ln, 19, TX2)
    p = os.path.join(OUTDIR, "%s_图4_出手瞬间逐球对比.png" % R.REPORT_DATE)
    im.save(p)
    return p


# ------------------------------------------------------------------ 图 5 结论
# ------------------------------------------------------------------ 图 5 结论（数据驱动）
# 2026-10-12 改：这一图原来**分组与点评全部写死** —— 固定把「屈膝深度」放进「已经稳定，不用动」
# 栏并配文案「下肢蓄力稳定」，右栏固定写「出手小臂仰角…已接近精英 78.7°，保持即可」。
# 换任何新数据都会印错（9月20日 实测屈膝角 98° 是严重偏低、小臂仰角 69° 离 78.7° 还差得远）。
# 现在分组与点评统一来自 R.derive_conclusions()：分组看「是否落在参考区间内 + CV」，
# 点评取 COACH 表，数值取本场实测 —— 不再有任何写死的判断。
def _conc(ms, agg):
    return R.derive_conclusions(agg, ms, tag=TAG)


def _stable_items(ms, agg, n_max):
    """左栏：已稳定**且**在参考区间内（先别动）"""
    return [e for e in _conc(ms, agg)["by_consistency"]
            if e["public"] and e["in_ref"] and (e["cv"] or 0) < 5][:n_max]


def _issue_items(ms, agg, n_max):
    """右栏：优先改 —— 先按「超出参考区间」的偏离程度，再补高波动项"""
    c = _conc(ms, agg)
    out = [e for e in c["items"] if e["public"] and not e["in_ref"]]
    for e in c["by_consistency"]:
        if len(out) >= n_max:
            break
        if e["public"] and e["in_ref"] and (e["cv"] or 0) > 12 and e not in out:
            out.append(e)
    return out[:n_max]


def _chart_val(e):
    """图上的数值文本：高度类换算成 cm 更好读"""
    if e["mean"] is None:
        return "—"
    if e["key"] in ("出手点离地高度", "垂直弹跳"):
        return "%.0f cm" % (e["mean"] * R.HEIGHT_CM)
    return R.card_value(e["mean"], e["unit"])


def _chart_note(e, good=False):
    """说明文本。
    越界的 → 给参考区间（这才是它该改的依据）；
    区间内的高波动项 → 给 COACH 的负面描述（good=False）；
    已稳定且在区间内的 → 给 COACH 的正面描述（good=True）。
    ⚠ 两者不能混：左栏若用了负面描述，会出现「出手点高度…出手点偏低」这种自相矛盾的卡。
    """
    cv = e["cv"] or 0.0
    if not e["in_ref"]:
        return "CV %.1f%%　参考 %s" % (cv, R.ref_text(e["lo"], e["hi"], e["unit"]))
    return "CV %.1f%%　%s" % (cv, (e["good_text"] if good else e["bad_text"]) or "")


def _plans(ms, agg, n_max=3):
    """训练处方：前 n 条由「优先改」的项目直接生成，最后一条是固定的拍摄改进建议。"""
    out = []
    for i, e in enumerate(_issue_items(ms, agg, n_max)):
        if not e["in_ref"]:
            goal = "把「%s」拉回参考区间 %s" % (e["short"], R.ref_text(e["lo"], e["hi"], e["unit"]))
        else:
            goal = "把「%s」的 CV 从 %.1f%% 压到 12%% 以内" % (e["short"], e["cv"] or 0)
        out.append(("%s %s" % ("①②③④"[i], e["short"]), e["advice"] or "", goal))
    out.append(("%s 下次拍侧面 90°（改测量）" % "①②③④"[len(out)],
                "机位放投篮方向侧面 90°、距 3–6 米、腰部高度，人物占画面 60–70%。",
                "解锁真实出手角与出手瞬间球速 —— 现在这两项测不了"))
    return out


def fig_insights(ms, agg, deeps):
    W, H = 1640, 1180
    im, d = new_canvas(W, H)
    text(d, (50, 36), "结论与训练处方", 42, TX, True)
    text(d, (50, 96), f"基于 {len(ms)} 次出手（全部逐帧测算）· 参考区间均标注文献出处", 21, TX2)

    y = 150
    # 左列：已稳定且在参考区间内
    rrect(d, (50, y, 800, y + 420), 12, CARD, LINE)
    text(d, (74, y + 20), "已经稳定且在区间内（CV < 5%，先别动）", 26, GOOD, True)
    items = [(e["short"], _chart_val(e), _chart_note(e, good=True))
             for e in _stable_items(ms, agg, 4)]
    yy = y + 66
    for k, v, n in items:
        text(d, (74, yy), k, 22, TX)
        text(d, (520, yy - 6), v, 26, GOOD, True)
        for li, ln in enumerate(wrap(d, n, 17, 250)):
            text(d, (520, yy + 26 + li * 20), ln, 17, TX2)
        yy += 86

    # 右列：优先改（偏离参考区间 → 高波动）
    issues = _issue_items(ms, agg, 3)
    rrect(d, (840, y, 1590, y + 420), 12, CARD, (120, 80, 30))
    text(d, (864, y + 20), "偏离参考区间 / 波动大，优先改", 26, WARN, True)
    yy = y + 66
    for e in issues:
        text(d, (864, yy), e["short"], 22, TX)
        text(d, (1240, yy - 6), _chart_val(e), 26, WARN, True)
        for li, ln in enumerate(wrap(d, _chart_note(e), 17, 330)):
            text(d, (1240, yy + 26 + li * 20), ln, 17, TX2)
        yy += 112

    # 处方
    y2 = y + 450
    text(d, (50, y2), "训练处方（按优先级）", 30, TX, True)
    yy = y2 + 50
    for k, v, n in _plans(ms, agg, 3):
        rrect(d, (50, yy, 1590, yy + 122), 12, CARD, LINE)
        text(d, (74, yy + 14), k, 25, ACC, True)
        for li, ln in enumerate(wrap(d, v, 20, 1450)):
            text(d, (74, yy + 50 + li * 26), ln, 20, TX)
        text(d, (74, yy + 96), f"目标：{n}", 19, YEL)
        yy += 136
    p = os.path.join(OUTDIR, "%s_图5_结论与训练处方.png" % R.REPORT_DATE)
    im.save(p)
    return p


def main():
    if VERT:
        main_vertical()
        return
    ms, agg, deeps = load_data()
    print("样本 %d，深扫 %d" % (len(ms), len(deeps)))
    outs = [fig_overview(ms, agg, deeps), fig_curves(ms, agg, deeps),
            fig_matrix(ms, agg), fig_release(ms, agg, deeps), fig_insights(ms, agg, deeps)]
    for p in outs:
        print("OK", p, "%.0f KB" % (os.path.getsize(p) / 1024))




# ================================================================== 竖版（抖音 9:16）
def fig_overview_v(ms, agg, deeps):
    W, H = 1080, 1920
    im, d = new_canvas(W, H)
    text(d, (40, 36), f"{R.REPORT_DATE} 投篮分析", 42, TX, True)
    text(d, (40, 92), "关键指标总览 · %d 次出手全部逐帧测算 · 身高 %dcm 标定" % (len(ms), R.HEIGHT_CM), 19, TX2)
    k = agg.get("出手点离地高度", {})
    j = agg.get("垂直弹跳", {})
    e = agg.get("出手时肘角_右", {})
    t = agg.get("屈膝到出手", {})
    cards = [
        ("出手点离地高度", f"{k.get('mean', 0) * R.HEIGHT_CM:.0f} cm", f"CV {k.get('cv', 0):.1f}%　职业男 1.31–1.38 倍身高", _col("出手点离地高度", k.get("mean"))),
        ("垂直弹跳（踝口径）", f"{j.get('mean', 0) * R.HEIGHT_CM:.0f} cm", f"CV {j.get('cv', 0):.1f}%　文献 2 分球 26.9cm", _col("垂直弹跳", j.get("mean"))),
        ("投篮手出手肘角", f"{e.get('mean', 0):.0f}°", f"CV {e.get('cv', 0):.1f}%　文献 134–138°／166–168°", _col("出手时肘角_右", e.get("mean"))),
        ("出手节奏", f"{t.get('mean', 0):.2f} s", _cv_note(agg, "屈膝到出手"), _col("屈膝到出手", t.get("mean"))),
    ]
    cw, ch, gap = 490, 176, 20
    for i, (k1, v1, n1, col) in enumerate(cards):
        x = 40 + (i % 2) * (cw + gap)
        y = 148 + (i // 2) * (ch + gap)
        rrect(d, (x, y, x + cw, y + ch), 14, CARD, LINE)
        text(d, (x + 20, y + 16), k1, 20, TX2)
        text(d, (x + 20, y + 44), v1, 40, col, True)
        for li, ln in enumerate(wrap(d, n1, 16, cw - 40)):
            text(d, (x + 20, y + 104 + li * 22), ln, 16, TX2)
    y0 = 560
    text(d, (40, y0 - 40), "各项指标 vs 文献区间", 28, TX, True)
    keys = ["屈膝角(最低)", "出手时膝角", "收球肘角_右", "出手时肘角_右",
            "出手小臂仰角", "出手点离地高度", "垂直弹跳", "屈膝到出手", "身体位移"]
    row_h = 136
    bx0, bx1 = 330, 850
    for i, key in enumerate(keys):
        if key not in agg:
            continue
        a = agg[key]
        lo, hi, unit, desc, lit = R.REFERENCE[key]
        lo, hi = float(lo), float(hi)
        y = y0 + i * row_h
        text(d, (40, y + 2), R.label(key).replace("（", "\n（"), 20, TX)
        rrect(d, (bx0, y + 6, bx1, y + 32), 13, CARD2)
        span = max(hi - lo, 1e-6)

        def pos(v):
            if lo <= v <= hi:
                f = 0.30 + (v - lo) / span * 0.40
            elif v < lo:
                f = 0.30 - min((lo - v) / span * 0.40, 0.28)
            else:
                f = 0.70 + min((v - hi) / span * 0.40, 0.28)
            return bx0 + f * (bx1 - bx0)

        rrect(d, (pos(lo), y + 4, pos(hi), y + 34), 15, (58, 66, 76))
        mean = a["mean"]
        mn, mx = a.get("min", mean), a.get("max", mean)
        if mn is not None and mx is not None:
            d.line([(pos(mn), y + 19), (pos(mx), y + 19)], fill=(150, 160, 172), width=3)
        px = pos(mean)
        txt, col, cls = qual(key, mean)
        d.ellipse([px - 9, y + 10, px + 9, y + 28], fill=col)
        dig = 2 if unit in ("倍身高", "秒") else 0
        text(d, (870, y), f"{mean:.{dig}f}{unit}", 25, TX, True)
        text(d, (870, y + 34), f"CV {a['cv']:.1f}%" if a.get("cv") is not None else "", 17, TX2)
        text(d, (870, y + 60), txt, 18, col)
        dig_ref = 2 if unit in ("倍身高", "秒") else 0
        text(d, (40, y + 92), f"文献 {lo:.{dig_ref}f}–{hi:.{dig_ref}f}{unit}", 15, TX2)
    p = os.path.join(OUTDIR, "%s_图1_关键指标总览.png" % R.REPORT_DATE)
    im.save(p)
    return p


def fig_curves_v(ms, agg, deeps):
    W, H = 1080, 1920
    im, d = new_canvas(W, H)
    text(d, (40, 36), "多球曲线对比", 40, TX, True)
    text(d, (40, 90), "%d 个 60fps 逐帧样本 · 黄线 = 出手时刻 · 颜色 = 不同球次" % len(deeps), 19, TX2)
    panels = [("wr", "投篮手手腕高于肩（倍身高）"),
              ("knee", "膝角（°，越小蹲得越深）"),
              ("elbowR", "投篮手肘角（°）"),
              ("fore", "出手小臂仰角（°）")]
    for idx, (keyname, title) in enumerate(panels):
        x0, y0, pw, ph_ = 40, 134 + idx * 446, 1000, 396
        rrect(d, (x0, y0, x0 + pw, y0 + ph_), 12, CARD, LINE)
        text(d, (x0 + 22, y0 + 14), title, 22, TX, True)
        gx0, gx1 = x0 + 74, x0 + pw - 28
        gy0, gy1 = y0 + 58, y0 + ph_ - 48
        series = []
        for si, dp in enumerate(deeps):
            pts = []
            for f in dp["feats"]:
                x = f["t"] - dp["t_rel"]
                if x < -0.85 or x > 0.35:
                    continue
                if keyname == "knee":
                    v = f["right"]["knee"] if f["right"]["knee"] is not None else f["left"]["knee"]
                elif keyname == "elbowR":
                    v = f["right"]["elbow"]
                elif keyname == "fore":
                    v = f["right"]["fore_ang"]
                else:
                    v = f["right"]["wr_over_sh"]
                if v is None:
                    continue
                pts.append((x, v))
            if len(pts) >= 3:
                series.append((si, pts))
        if not series:
            continue
        allv = [v for _, pts in series for _, v in pts]
        vmin, vmax = min(allv), max(allv)
        pad = (vmax - vmin) * 0.12 + 1e-6
        vmin, vmax = vmin - pad, vmax + pad

        def sx(x):
            return gx0 + (x + 0.85) / 1.20 * (gx1 - gx0)

        def sy(v):
            return gy1 - (v - vmin) / (vmax - vmin) * (gy1 - gy0)

        for gi in range(5):
            yy = gy0 + gi * (gy1 - gy0) / 4
            d.line([(gx0, yy), (gx1, yy)], fill=(38, 44, 52))
            val = vmax - gi * (vmax - vmin) / 4
            text(d, (gx0 - 8, yy), f"{val:.2f}" if abs(vmax) < 5 else f"{val:.0f}", 14, TX2, anchor="ra")
        for xt in (-0.8, -0.4, 0.0, 0.3):
            d.line([(sx(xt), gy0), (sx(xt), gy1)], fill=(38, 44, 52))
        d.line([(sx(0), gy0), (sx(0), gy1)], fill=YEL, width=2)
        for si, pts in series:
            d.line([(sx(x), sy(v)) for x, v in pts], fill=SERIES[si % len(SERIES)], width=3, joint="curve")
        text(d, (gx1, gy1 + 6), "时间（秒，0 = 出手）", 15, TX2, anchor="ra")
    p = os.path.join(OUTDIR, "%s_图2_多球曲线对比.png" % R.REPORT_DATE)
    im.save(p)
    return p


def fig_matrix_v(ms, agg):
    keys = [("屈膝角(最低)", "屈膝角", 0), ("出手时膝角", "出手膝角", 0),
            ("收球肘角_右", "收球肘角\n投篮手", 0), ("出手时肘角_右", "出手肘角\n投篮手", 0),
            ("出手时肘角_左", "出手肘角\n辅助手", 0), ("出手小臂仰角", "小臂仰角", 0),
            ("出手点离地高度", "出手点\n高度", 2), ("垂直弹跳", "垂直\n弹跳", 2),
            ("屈膝到出手", "出手\n节奏", 2)]
    W = 1080
    x0, label_w = 36, 120
    cell_w = (W - x0 - 36 - label_w) // len(keys)
    row_h = 122
    H = max(220 + row_h * len(ms) + 90, 1920)   # 统一 9:16
    im, d = new_canvas(W, H)
    text(d, (36, 34), "逐球诊断矩阵", 40, TX, True)
    text(d, (36, 90), "绿=区间内　黄=略偏　红=明显偏　% 为相对全场均值偏离", 18, TX2)
    hy = 150
    text(d, (x0, hy + 8), "球次", 17, TX2)
    for ci, (_, name, _) in enumerate(keys):
        cx = x0 + label_w + ci * cell_w + cell_w // 2
        for li, ln in enumerate(name.split("\n")):
            text(d, (cx, hy - 2 + li * 20), ln, 15, TX, anchor="ma")
    d.line([(x0, hy + 46), (W - 36, hy + 46)], fill=LINE, width=2)
    means = {k: (agg[k]["mean"] if k in agg else None) for k, _, _ in keys}
    for ri, m in enumerate(ms):
        y = hy + 56 + ri * row_h
        rrect(d, (x0, y, W - 36, y + row_h - 12), 10, CARD)
        text(d, (x0 + 12, y + 12), f"第 {ri + 1} 次", 18, TX, True)
        text(d, (x0 + 12, y + 38), f"{int(m['t_rel'] // 60)}:{m['t_rel'] % 60:04.1f}", 15, TX2)
        for ci, (k, _, dig) in enumerate(keys):
            v = m.get(k)
            cx = x0 + label_w + ci * cell_w
            if v is None:
                text(d, (cx + cell_w // 2, y + 22), "—", 20, TX2, anchor="ma")
                continue
            unit = R.REFERENCE[k][2] if k in R.REFERENCE else ""
            if unit in ("倍身高", "秒"):
                sv = f"{v:.2f}" + ("s" if unit == "秒" else "")
            else:
                sv = f"{v:.0f}°"
            _, col, cls = qual(k, v)
            text(d, (cx + cell_w // 2, y + 10), sv, 22, col, True, anchor="ma")
            mu = means.get(k)
            if mu:
                dev = (v - mu) / (abs(mu) or 1) * 100
                text(d, (cx + cell_w // 2, y + 40), f"{dev:+.0f}%", 14, TX2, anchor="ma")
    text(d, (36, H - 46), "注：辅助手肘角偶有识别异常（左右标签跳变），出现 20° 量级请忽略。", 15, TX2)
    p = os.path.join(OUTDIR, "%s_图3_逐球诊断矩阵.png" % R.REPORT_DATE)
    im.save(p)
    return p


def fig_release_v(ms, agg, deeps):
    from PIL import Image as I
    top3 = json.load(open(os.path.join(R.DATA, "%s_top3.json" % TAG)))
    W, H = 1080, 1920
    im, d = new_canvas(W, H)
    text(d, (36, 34), "最佳 3 球 · 出手瞬间", 40, TX, True)
    text(d, (36, 90), "评分权重：轨迹直度 25% · 小臂仰角 20% · 投篮手肘角 20% · 屈膝 15% · 出手点 10% · 节奏 10%", 15, TX2)
    for i, it in enumerate(top3):
        y = 140 + i * 588
        dp = min(deeps, key=lambda x: abs(x["t_rel"] - it["t"]))
        dj = json.load(open(dp["fp"]))
        ridx = next((k for k, rec in enumerate(dj["seq"]) if rec["t"] >= dp["t_rel"] - 1e-6), len(dj["seq"]) // 2)
        tmp = "/tmp/chart_frame_v_%d.jpg" % i
        if render_frame(dp, ridx, tmp) is None:
            continue
        img = I.open(tmp)
        th = 520
        tw = int(img.width * th / img.height)
        img = img.resize((tw, th), I.LANCZOS)
        rrect(d, (36, y, W - 36, y + 566), 14, CARD, LINE)
        im.paste(img, (50, y + 12))
        m = dp["m"]
        x2 = 60 + tw + 24
        text(d, (x2, y + 16), f"第 {i + 1} 名", 30, ACC, True)
        text(d, (x2, y + 54), f"评分 {it['score']:.0f} · {int(m['t_rel'] // 60)}:{m['t_rel'] % 60:04.1f}", 19, TX2)
        rows = [
            ("屈膝角", f"{fmt(m['屈膝角(最低)'])}°", "屈膝角(最低)"),
            ("投篮手肘角", f"{fmt(m['出手时肘角_右'])}°", "出手时肘角_右"),
            ("小臂仰角", f"{fmt(m['出手小臂仰角'])}°", "出手小臂仰角"),
            ("出手点高度", f"{fmt(m['出手点离地高度'], 2)}×身高", "出手点离地高度"),
            ("出手节奏", f"{fmt(m['屈膝到出手'], 2)}s", "屈膝到出手"),
            ("垂直弹跳", f"{fmt(m['垂直弹跳'], 2)}×身高", "垂直弹跳"),
        ]
        yy = y + 92
        for name, val, key in rows:
            _, col, _ = qual(key, m.get(key))
            text(d, (x2, yy), name, 16, TX2)
            text(d, (x2 + 170, yy - 8), val, 22, col, True)
            yy += 64
    p = os.path.join(OUTDIR, "%s_图4_最佳3球出手瞬间.png" % R.REPORT_DATE)
    im.save(p)
    return p


def fig_insights_v(ms, agg, deeps):
    W, H = 1080, 1920
    im, d = new_canvas(W, H)
    text(d, (36, 34), "结论与训练处方", 40, TX, True)
    text(d, (36, 90), f"基于 {len(ms)} 次出手（全部逐帧测算）· 参考区间均标注文献出处", 18, TX2)
    y = 140
    rrect(d, (36, y, W - 36, y + 470), 14, CARD, LINE)
    text(d, (60, y + 18), "已经稳定且在区间内（CV < 5%，先别动）", 25, GOOD, True)
    items = [(e["short"], _chart_val(e), _chart_note(e, good=True))
             for e in _stable_items(ms, agg, 4)]
    yy = y + 62
    for k, v, n in items:
        text(d, (60, yy), k, 20, TX)
        text(d, (60, yy + 28), v, 30, GOOD, True)
        for li, ln in enumerate(wrap(d, n, 16, 330)):
            text(d, (560, yy + 4 + li * 22), ln, 16, TX2)
        yy += 100
    y2 = y + 500
    rrect(d, (36, y2, W - 36, y2 + 400), 14, CARD, (120, 80, 30))
    text(d, (60, y2 + 18), "偏离参考区间 / 波动大，优先改", 25, WARN, True)
    items2 = _issue_items(ms, agg, 3)
    yy = y2 + 62
    for e in items2:
        text(d, (60, yy), e["short"], 20, TX)
        text(d, (60, yy + 28), _chart_val(e), 30, WARN, True)
        for li, ln in enumerate(wrap(d, _chart_note(e), 16, 400)):
            text(d, (560, yy + 4 + li * 22), ln, 16, TX2)
        yy += 112
    y3 = y2 + 436
    text(d, (36, y3), "训练处方（按优先级）", 28, TX, True)
    yy = y3 + 46
    for k, v, n in _plans(ms, agg, 3):
        rrect(d, (36, yy, W - 36, yy + 122), 12, CARD, LINE)
        text(d, (60, yy + 10), k, 22, ACC, True)
        for li, ln in enumerate(wrap(d, v, 18, 950)):
            text(d, (60, yy + 44 + li * 30), ln, 18, TX)
        text(d, (60, yy + 100), f"目标：{n}", 15, YEL)
        yy += 134
    p = os.path.join(OUTDIR, "%s_图5_结论与训练处方.png" % R.REPORT_DATE)
    im.save(p)
    return p


def main_vertical():
    global FSCALE
    FSCALE = 1.0        # 恢复默认字号（用户要求）
    ms, agg, deeps = load_data()
    outs = [fig_overview_v(ms, agg, deeps), fig_curves_v(ms, agg, deeps),
            fig_matrix_v(ms, agg), fig_release_v(ms, agg, deeps), fig_insights_v(ms, agg, deeps)]
    for p in outs:
        print("OK", p, "%.0f KB" % (os.path.getsize(p) / 1024))


if __name__ == "__main__":
    main()
