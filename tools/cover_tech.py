# -*- coding: utf-8 -*-
"""科技风封面（竖版 3:4 / 横版 4:3）—— 真实帧 + 仪表盘式读数

为什么另起一个脚本，而不是改 cover_from_frame.py：
    老版（v2）的视觉语言是「深色玻璃卡片 + 圆角 + 发光描边 + 暖黄/冷蓝双色描边」，
    这套组合是典型的 AI 生图模板味 —— 圆角、发光、饱和撞色三件套叠在一起，
    信息密度看着高，但每个数字都是孤立的字符串，读不出「跟参考区间差多少」。
    用户 2026-10-14 反馈「AI 味太浓，要更科技风」。

本版的差异（一条条都对应"AI 味"的反面）：
    1. **不用圆角卡片** → 读数块只用「顶部细规线 + 左侧色块 + 字重层级」界定边界，
       没有圆角、没有半透明填充、没有发光描边；
    2. **量程条**（range bar）替代纯文字 —— 把实测值画在参考区间上，
       值落在带外就直接看得见偏移方向和距离。这是"科技"的实质：偏差可视化；
    3. 深底 + 大范围压暗 + 暗角，构成仪表/HUD 的底子。**默认不再铺网格/四角/刻度规线**
       —— 2026-10-14 第二轮反馈「横条竖条太多显得杂乱」，线条只保留承载信息的那几根
       （见 SHOTVID_COVER_DETAIL）；
    4. 骨架改**动捕标记**：细冷白线 + 关节空心圈 + 白点，去掉暖黄发光；
    5. 数字走 **DIN Alternate Bold**（工业/运动计时器字形）、拉丁微标签走 SF Mono、
       中文走 PingFang SC（经 fontkit 保证是简体字库），字形层级本身就是科技感的来源；
    6. 配色语义化：青 = 区间内、琥珀 = 越界、灰 = 次要信息。不再用撞色装饰。

用法:
    python cover_tech.py <出手秒> [后缀] [ori=v|h] [tag]
    python cover_tech.py 2.4 "" v v          # 竖版，tag v
    python cover_tech.py 2.4 精简 h v        # 横版 + 文件名后缀

环境变量:
    SHOTVID_COVER_CMP=0   去掉跨日期对比卡，只留本场结论（默认 2）
    SHOTVID_COVER_TITLE   手工覆盖三行标题，用 | 分隔
    SHOTVID_COVER_DETAIL  min（默认，线条最少）/ full（第一版满配 HUD，用于对照）
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from bbkit import make_generator, frame_at, probe
from ball import cg_to_np
import fontkit
import report as R

T_REL = float(sys.argv[1]) if len(sys.argv) > 1 else 2.42
SUFFIX = sys.argv[2] if len(sys.argv) > 2 else ""
ORI = (sys.argv[3] if len(sys.argv) > 3 else "v").lower()
TAG = sys.argv[4] if len(sys.argv) > 4 else "v"

R.apply_tag(TAG)
OUTDIR = os.path.join(R.ROOT, "抖音封面")
os.makedirs(OUTDIR, exist_ok=True)

VERT = ORI.startswith("v")
W, H = (1080, 1440) if VERT else (1440, 1080)
M = 56 if VERT else 70                     # 外边距
# 竖版构图（2026-10-14 改）：老版把人物顶到 29.5% 处，标题必然压在头上、手臂横穿白字。
# 现在把人物整体压到 41% 以下，标题独占上半幅的干净暗场 —— 这是"版面感"的来源。
PERSON_FRAC = 0.50 if VERT else 0.74       # 人物（含举手）占画面高度
TOP_FRAC = 0.41 if VERT else 0.11          # 人物最高点在裁切区的位置
PERSON_X_FRAC = 0.5 if VERT else 0.76      # 横版把人靠右，左侧留给文字

# 线条密度：min（默认）= 只留承载信息的线；full = 第一版的满配 HUD（网格 / 四角 / 刻度规线 / 卡片顶规线）
# 用户 2026-10-14 第二轮反馈「横条竖条太多显得杂乱」→ 默认降到 min。
DETAIL = (os.environ.get("SHOTVID_COVER_DETAIL", "min") or "min").strip().lower()
FULL_LINES = DETAIL == "full"

# ---------------- 配色（语义化，不做装饰性撞色）----------------
C_GRID = np.array([16, 26, 36], np.float32)
C_RULE = (46, 62, 76)          # 细规线
C_TRACK = (26, 37, 47)         # 量程条底槽
C_BAND = (22, 84, 104)         # 参考区间带
C_TX = (238, 245, 252)         # 主文字
C_MUT = (128, 144, 160)        # 次要
C_DIM = (92, 106, 120)         # 最弱
C_CYAN = (45, 214, 245)        # 强调 / 标线
C_AMBER = (255, 184, 48)       # 本场被标记的问题项（与本场结论同色）
C_STEEL = (172, 204, 224)      # 跨日期对比卡：中性冷灰，一眼区别于本场结论
C_SKEL = (206, 238, 255)       # 骨架冷白
C_MARK = (120, 226, 255)       # 关节标记圈

# ---------------- 字体 ----------------
_DIN = "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf"
_DIN2 = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
_MONO = "/System/Library/Fonts/SFNSMono.ttf"
_F = {}


def cf(size, bold=True):
    """中文（PingFang SC，经 fontkit 校验简体字形）"""
    return fontkit.load(size, bold)


def nf(size):
    """数字：DIN Alternate Bold（工业字形）"""
    k = ("n", size)
    if k not in _F:
        for p in (_DIN, _DIN2):
            try:
                _F[k] = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
        else:
            _F[k] = fontkit.load(size, True)
    return _F[k]


def mf(size):
    """拉丁微标签：等宽（等宽才有仪表盘味）"""
    k = ("m", size)
    if k not in _F:
        try:
            _F[k] = ImageFont.truetype(_MONO, size)
        except Exception:
            _F[k] = fontkit.load(size, False)
    return _F[k]


def fit_cf(d, text, target_w, size_max=300, size_min=12, bold=True):
    lo, hi = size_min, size_max
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(text, font=cf(mid, bold)) <= target_w:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------- 文案（一律由 derive_conclusions 推导，不写死）----------------
def cover_info():
    info = {}
    try:
        info = json.load(open(os.path.join(R.DATA, "tags.json"), encoding="utf-8")).get(TAG, {}) or {}
    except Exception:
        pass
    ms = [m for m in (R.shot_metrics(s) for s in R.load_shots(TAG)) if m]
    agg = R.aggregate(ms, R.METRIC_KEYS)
    conc = R.derive_conclusions(agg, ms, tag=TAG)

    line3 = conc["title"]["line3"]
    vt = info.get("video_title")
    if isinstance(vt, (list, tuple)) and len(vt) >= 2:
        title = (vt[0], vt[1], vt[2] if len(vt) > 2 else line3)
    else:
        title = ("投篮精进", "日拱一卒", line3)
    env = os.environ.get("SHOTVID_COVER_TITLE")
    if env:
        p = env.split("|")
        title = (p[0], p[1] if len(p) > 1 else title[1], p[2] if len(p) > 2 else title[2])

    cards = [(main, sub) for main, sub in conc["title"]["cards"]]

    n_cmp = int(os.environ.get("SHOTVID_COVER_CMP", "2"))
    cmp_cards = []
    if n_cmp > 0:
        try:
            cmpd = R.load_compare(TAG)
            cmp_cards = [tuple(c) for c in cmpd.get("cards", [])[:n_cmp]]
        except Exception as e:
            print("（对比卡跳过：%s）" % e)

    vw, vh, fps = 1920, 1080, 60.0
    try:
        vm = probe(os.path.join(R.BASE, info.get("video", "")))
        vw, vh, fps = vm["size"][0], vm["size"][1], vm["fps"]
    except Exception:
        pass
    foot = "%d×%d · %.0ffps · %d 次出手逐帧测算" % (vw, vh, fps, len(ms))
    return {"title": tuple(title), "cards": cards, "cmp": cmp_cards,
            "foot": foot, "n": len(ms), "conc": conc}


RE_NUM = re.compile(r"[\d.]+")
RE_RANGE = re.compile(r"([\d.]+)\s*[–—~-]\s*([\d.]+)")
RE_ARROW = re.compile(r"([\d.]+)\D*?→\D*?([\d.]+)")


def parse_card(main, sub):
    """把卡片文案拆成结构化字段，好画量程条。拆不出就退回纯文本，绝不猜。"""
    m = None
    for m in RE_NUM.finditer(main):
        pass
    if not m:
        return {"label": main, "val": None, "unit": "", "judge": sub, "ref": None, "arrow": None}
    label = main[:m.start()].strip()
    unit = main[m.end():].strip()
    judge = (sub or "").split("·")[0].strip()

    ref = None
    rr = RE_RANGE.search(sub or "")
    if rr and "参考" in (sub or ""):
        ref = (float(rr.group(1)), float(rr.group(2)))
    arrow = None
    if ref is None:
        ar = RE_ARROW.search(sub or "")
        if ar:
            arrow = (float(ar.group(1)), float(ar.group(2)))
            judge = ""      # 对比卡的"对比上次"下面条上已经写了，别重复
    return {"label": label, "val": m.group(0), "unit": unit,
            "judge": judge, "ref": ref, "arrow": arrow}


# ---------------- 骨架 ----------------
EDGES = [("neck_1_joint", "head_joint"),
         ("neck_1_joint", "right_shoulder_1_joint"),
         ("right_shoulder_1_joint", "right_forearm_joint"),
         ("right_forearm_joint", "right_hand_joint"),
         ("neck_1_joint", "left_shoulder_1_joint"),
         ("left_shoulder_1_joint", "left_forearm_joint"),
         ("left_forearm_joint", "left_hand_joint"),
         ("right_shoulder_1_joint", "right_upLeg_joint"),
         ("right_upLeg_joint", "right_leg_joint"),
         ("left_shoulder_1_joint", "left_upLeg_joint"),
         ("left_upLeg_joint", "left_leg_joint"),
         ("right_upLeg_joint", "left_upLeg_joint"),
         ("right_leg_joint", "right_foot_joint"),
         ("left_leg_joint", "left_foot_joint")]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------- 绘制小工具 ----------------
def soft_text(img, xy, text, font, fill, anchor="la", blur=12, dark=205, stroke=0):
    """先压一层模糊暗影，再画清晰字 —— 有可读性，又没有硬描边那股市面模板味。

    注意：**暗影和清晰字都要画**。只画暗影会得到一张"全是字影、没有字"的雾图
    （2026-10-14 第一版就这么翻的车：标题看着像盖了层灰膜）。
    """
    lay = Image.new("L", img.size, 0)
    ImageDraw.Draw(lay).text(xy, text, font=font, fill=dark, anchor=anchor,
                             stroke_width=stroke, stroke_fill=dark)
    lay = lay.filter(ImageFilter.GaussianBlur(blur))
    img.paste(Image.composite(Image.new("RGB", img.size, (2, 4, 8)), img, lay), (0, 0))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill, anchor=anchor)


def grid_overlay(img):
    gm = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(gm)
    step = 72 if VERT else 80
    for x in range(0, W + 1, step):
        gd.line([(x, 0), (x, H)], fill=255, width=1)
    for y in range(0, H + 1, step):
        gd.line([(0, y), (W, y)], fill=255, width=1)
    a = (np.asarray(gm).astype(np.float32) / 255.0)[:, :, None]
    arr = np.asarray(img).astype(np.float32) + a * C_GRID.reshape(1, 1, 3)
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def corners(d):
    L, g = 46, 16
    col = (38, 104, 128)
    for cx, cy, sx, sy in ((g, g, 1, 1), (W - g, g, -1, 1),
                           (g, H - g, 1, -1), (W - g, H - g, -1, -1)):
        d.line([(cx, cy), (cx + sx * L, cy)], fill=col, width=3)
        d.line([(cx, cy), (cx, cy + sy * L)], fill=col, width=3)


def tick_rule(d, x0, x1, y, n=8, color=C_RULE, tick=7):
    d.line([(x0, y), (x1, y)], fill=color, width=1)
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        d.line([(x, y), (x, y + (tick if i % 2 == 0 else tick * 0.5))], fill=color, width=1)


def bar_domain(vals, lo, hi):
    if lo is not None and hi is not None and hi > lo:
        span = hi - lo
        pad = span * 0.62
        d0 = min(lo - pad, min(vals) - span * 0.14)
        d1 = max(hi + pad, max(vals) + span * 0.14)
    else:
        d0, d1 = min(vals), max(vals)
        pad = (d1 - d0) * 0.35 or 1.0
        d0, d1 = d0 - pad, d1 + pad
    return d0, d1


def measure_bar(d, x, y, w, card, accent):
    """量程条：底槽 + 参考区间带 + 实测值游标；对比卡则画 前→后 双游标。"""
    ref, arrow = card["ref"], card["arrow"]
    v = float(card["val"]) if card["val"] else None
    vals = [t for t in ([v] if v is not None else []) +
            (list(ref) if ref else []) + (list(arrow) if arrow else [])]
    if not vals:
        return
    d0, d1 = bar_domain(vals, *(ref if ref else (None, None)))

    def mx(t):
        return x + w * (t - d0) / (d1 - d0)

    # 底槽（横贯整列）是"横条"里最显眼的一根。默认版去掉，让参考带 + 游标自己说话；
    # 满配版保留，用于需要"刻度尺"观感的场合。
    if FULL_LINES:
        d.rectangle((x, y, x + w, y + 7), fill=C_TRACK)
    if ref:
        d.rectangle((mx(ref[0]), y - 5, mx(ref[1]), y + 12), fill=C_BAND)
    if arrow:
        a, b = mx(arrow[0]), mx(arrow[1])
        d.line([(a, y + 3), (b, y + 3)], fill=(74, 98, 116), width=3)
        d.rectangle((a - 1, y - 5, a + 2, y + 12), fill=C_DIM)
    if v is not None:
        p = mx(v)
        d.rectangle((p - 1.5, y - 10, p + 1.5, y + 17), fill=accent)
        d.polygon([(p - 6, y - 12), (p + 6, y - 12), (p, y - 5)], fill=accent)

    # 条下注释：参考区间 + 偏离量 / 前→后
    # 注意：这类注释里带中文，必须走 cf（PingFang）；SF Mono 没有 CJK 字形，会渲染成空白
    cy = y + 22
    if ref:
        shown = "%g–%g%s" % (ref[0], ref[1], card["unit"] or "")
        d.text((x, cy), "参考 " + shown, font=cf(20, False), fill=C_MUT, anchor="la")
        if v is not None:
            tol = max((ref[1] - ref[0]) * 0.005, 1e-9)
            if v < ref[0] - tol:
                txt, col = "低于区间 %g%s" % (ref[0] - v, card["unit"] or ""), C_AMBER
            elif v > ref[1] + tol:
                txt, col = "高于区间 %g%s" % (v - ref[1], card["unit"] or ""), C_AMBER
            elif abs(v - ref[0]) <= tol or abs(v - ref[1]) <= tol:
                txt, col = "恰在区间边界", C_DIM     # 0.50s 正压上界，别谎报「区间内」
            else:
                txt, col = "区间内", C_DIM
            d.text((x + w, cy), txt, font=cf(19, True), fill=col, anchor="ra")
    elif arrow:
        d.text((x, cy), "对比上次", font=cf(20, False), fill=C_MUT, anchor="la")
        # 箭头必须自己画：DIN Alternate Bold **没有 U+2192 字形**，写字符会渲染成豆腐块
        # （2026-10-14 实测，横版 03/04 卡上就是「59 ▯ 66」）。PIL 的 getmask().getbbox()
        # 对 .notdef 也返回非空，用它做字形探测会假阴性 —— 所以别探测，直接画矢量。
        fa = nf(22)
        sa, sb = "%g" % arrow[0], "%g" % arrow[1]
        wa = d.textlength(sa, font=fa)
        head, gap = 11, 9
        sx0 = x + w - (wa + gap + head + gap + d.textlength(sb, font=fa))
        d.text((sx0, cy - 2), sa, font=fa, fill=C_MUT, anchor="la")
        x1 = sx0 + wa + gap
        my = cy + 10
        d.line([(x1, my), (x1 + head, my)], fill=C_MUT, width=2)
        d.polygon([(x1 + head + 5, my), (x1 + head - 3, my - 5), (x1 + head - 3, my + 5)],
                  fill=C_MUT)
        d.text((x1 + head + gap + 3, cy - 2), sb, font=fa, fill=C_MUT, anchor="la")


def scrim(img, x, y, w, h, alpha=188, blur=34):
    """读数块底下的柔和暗池。

    不是卡片：没有描边、没有圆角边、没有填充边界 —— 只有一片大范围羽化的压暗，
    目的是把文字从虚化后的亮背景里"拉"出来。读数块放在画面两侧、背景亮度不可控，
    没有这层就会像第一版那样白字糊在墙上。
    """
    lay = Image.new("L", img.size, 0)
    ImageDraw.Draw(lay).rounded_rectangle((x - 18, y - 16, x + w + 18, y + h), radius=20, fill=alpha)
    lay = lay.filter(ImageFilter.GaussianBlur(blur))
    img.paste(Image.composite(Image.new("RGB", img.size, (4, 7, 12)), img, lay), (0, 0))


def readout(img, d, x, y, w, idx, card, is_cmp, compact=False):
    """一个读数块：暗池 + 顶部规线 + 标签/序号 + 大号数字 + 量程条。没有圆角、没有方框。

    配色语义：本场结论卡 = 琥珀（被标记的问题项）；跨日期对比卡 = 中性冷灰。
    这样"哪个是这场的结论、哪个是跟上次比"不用读字就分得开。
    """
    accent = C_STEEL if is_cmp else C_AMBER
    vsz = 56 if compact else 62
    # 高度要盖住最后一行注释（条下 22px 处的「参考 …/低于区间 …」），
    # 只盖到量程条是不够的 —— 注释那行会正好落在羽化边缘上，等于没盖。
    scrim(img, x, y, w, (44 if compact else 50) + vsz + 120)
    if FULL_LINES:
        d.line([(x, y), (x + w, y)], fill=C_RULE, width=1)
        d.line([(x, y), (x + 54, y)], fill=accent, width=3)
    else:
        # 横贯整列的细规线是主要噪声源之一，去掉；只留一段色标当"块首"，
        # 边界交给间距 + 暗池，不交给线条。
        d.line([(x, y), (x + 46, y)], fill=accent, width=3)

    lab_top = y + 16
    val_top = y + (44 if compact else 50)
    d.text((x, lab_top), card["label"], font=cf(27, True), fill=C_TX, anchor="la")
    d.text((x + w, lab_top + 5), idx, font=nf(24), fill=C_DIM, anchor="ra")

    d.text((x, val_top), card["val"] or "—", font=nf(vsz), fill=accent, anchor="la")
    if card["val"]:
        vw = d.textlength(card["val"], font=nf(vsz))
        d.text((x + vw + 5, val_top + vsz * 0.46), card["unit"] or "",
               font=cf(25, True), fill=C_MUT, anchor="ls")
    # 这里**不再重复写判定词**：标题第三行已经是「蹲得过深 · 出手偏慢」，
    # 卡里再写一遍就是同一句话说两次（第一版渲染出来就是这样，一眼像没校对）。
    # 判定交给条下的「低于区间 7°」这种量化注释 —— 同一个意思，但多了偏差量。

    measure_bar(d, x, val_top + vsz + 26, w, card, accent)


# ---------------- 主流程 ----------------
def prepare_frame():
    cds = sorted(glob.glob(os.path.join(R.DATA, TAG, "deep_*.json")),
                 key=lambda fp: abs(float(os.path.basename(fp)[5:-5]) - T_REL))
    dj = json.load(open(cds[0]))
    meta, seq = dj["meta"], dj["seq"]
    rec = min(seq, key=lambda r: abs(r["t"] - T_REL))
    kp = rec.get("kp") or {}
    box, cw0, ch0 = meta["box"], rec["cw"], rec["ch"]
    ok, _ = make_generator(meta["video"], (1920, 1080))
    cg = frame_at(ok, rec["t"])
    if cg is None:
        raise SystemExit("取帧失败")
    full = cg_to_np(cg)
    fh, fw = full.shape[:2]

    sxc, syc = box[2] / cw0, box[3] / ch0
    fpts = {k: (box[0] + v[0] * sxc, box[1] + v[1] * syc) for k, v in kp.items()}
    fxs = [p[0] for p in fpts.values()]
    fys = [p[1] for p in fpts.values()]
    cx_person = (min(fxs) + max(fxs)) / 2

    person_h = max(fys) - min(fys)
    crop_h = int(round(person_h / PERSON_FRAC))
    crop_w = int(round(crop_h * W / H))
    crop_h = min(crop_h, fh)
    crop_w = min(crop_w, fw)
    y0 = clamp(min(fys) - crop_h * TOP_FRAC, 0, fh - crop_h)
    x0 = clamp(cx_person - crop_w * PERSON_X_FRAC, 0, fw - crop_w)
    y0, x0 = int(y0), int(x0)
    print("人物高 %.0fpx → 裁切 %dx%d 放大 %.2f×" % (person_h, crop_w, crop_h, W / crop_w))

    sub = full[y0:y0 + crop_h, x0:x0 + crop_w]
    img = Image.fromarray(sub)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    img = img.resize((W, H), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2.6, percent=145, threshold=3))
    sx, sy = W / crop_w, H / crop_h

    pts = {k: ((p[0] - x0) * sx, (p[1] - y0) * sy) for k, p in fpts.items()}
    pts = {k: p for k, p in pts.items() if -6 <= p[0] <= W + 6 and -6 <= p[1] <= H + 6}

    # 背景：虚化 + 深度去饱和压暗（比老版更狠，让画面"降噪"，这是科技风的底子）
    bx0 = min(p[0] for p in pts.values()) - 90
    bx1 = max(p[0] for p in pts.values()) + 90
    by0 = min(p[1] for p in pts.values()) - 70
    by1 = max(p[1] for p in pts.values()) + 110
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse((bx0 - 20, by0 - 40, bx1 + 20, by1 + 60), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(72))
    blur = img.filter(ImageFilter.GaussianBlur(26))
    b = np.asarray(blur).astype(np.float32)
    g = b.mean(axis=2, keepdims=True)
    b = (b * 0.30 + g * 0.70) * 0.50
    img = Image.composite(img, Image.fromarray(b.clip(0, 255).astype(np.uint8)), mask)

    # 主体分离：提亮 + 加局部对比（比单纯提亮更能把人物从虚化背景里"切"出来）
    a2 = np.asarray(img).astype(np.float32)
    m2 = np.asarray(mask).astype(np.float32) / 255.0
    a2 *= (1.0 + 0.26 * m2[:, :, None])                     # 主体提亮
    a2 = a2 * (1 + 0.18 * m2[:, :, None]) - 10 * m2[:, :, None]   # 主体加对比
    a2[:, :, 2] *= 1.04                                     # 冷调：轻微抬蓝，与青系呼应
    arr = a2.clip(0, 255)

    # 渐变 + 暗角
    yy = np.arange(H, dtype=np.float32).reshape(H, 1)
    xx = np.arange(W, dtype=np.float32).reshape(1, W)
    if VERT:
        top_a = np.clip((H * 0.42 - yy) / (H * 0.42), 0, 1) ** 1.3 * 0.84
        bot_a = np.clip((yy - H * 0.76) / (H * 0.24), 0, 1) ** 1.4 * 0.72
        # 左右两侧各压出一条暗带：读数块正好落在暗场上。
        # 不压的话，白色标签/冷灰数字会直接坐在虚化后的亮墙面上，糊成一片（第一版就是这样）。
        left_a = 0.62 * np.maximum(
            np.clip((W * 0.34 - xx) / (W * 0.34), 0, 1) ** 1.1,
            np.clip((xx - W * 0.66) / (W * 0.34), 0, 1) ** 1.1)
        cx, cy = W * 0.5, H * 0.60
    else:
        top_a = np.clip((H * 0.30 - yy) / (H * 0.30), 0, 1) ** 1.2 * 0.60
        bot_a = np.clip((yy - H * 0.82) / (H * 0.18), 0, 1) ** 1.5 * 0.72
        left_a = np.clip((W * 0.50 - xx) / (W * 0.50), 0, 1) ** 1.15 * 0.80
        cx, cy = W * 0.76, H * 0.55
    alpha = np.clip(np.maximum(np.maximum(top_a, bot_a), left_a), 0, 1)
    dark = np.array([4.0, 7.0, 12.0], np.float32).reshape(1, 1, 3)
    arr = arr * (1 - alpha[:, :, None]) + dark * alpha[:, :, None]

    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    rr = np.sqrt(((gx - cx) / (W * 0.80)) ** 2 + ((gy - cy) / (H * 0.70)) ** 2)
    vig = np.clip(1.0 - 0.36 * np.clip(rr - 0.46, 0, 1) / 0.54, 0.60, 1.0)
    arr = arr * vig[:, :, None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    return img, pts, rec["t"], (x0, y0, crop_w, crop_h, sx, sy)


def draw_skeleton(img, pts):
    max_edge = (max(p[1] for p in pts.values()) - min(p[1] for p in pts.values())) * 0.72

    def seg(a, b_):
        p, q = pts.get(a), pts.get(b_)
        if not p or not q:
            return None
        if ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5 > max_edge:
            return None
        return p, q

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for a, b_ in EDGES:
        s = seg(a, b_)
        if s:
            gd.line([s[0], s[1]], fill=(120, 226, 255, 46), width=11)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)

    for a, b_ in EDGES:
        s = seg(a, b_)
        if not s:
            continue
        d.line([s[0], s[1]], fill=(6, 12, 20), width=7)      # 底线保证在亮背景上也读得出
        d.line([s[0], s[1]], fill=C_SKEL, width=2)
    for p in pts.values():                                    # 动捕标记：空心圈 + 实心点
        d.ellipse([p[0] - 11, p[1] - 11, p[0] + 11, p[1] + 11], outline=C_MARK, width=2)
        d.ellipse([p[0] - 3.5, p[1] - 3.5, p[0] + 3.5, p[1] + 3.5], fill=(255, 255, 255))
    return img


def build_vertical(cov, d, cards):
    # ---- 顶部：kicker + 刻度规线 ----
    d.rectangle((M, M + 4, M + 12, M + 16), fill=C_CYAN)
    d.text((M + 26, M - 2), "投篮动作逐帧拆解", font=cf(25, True), fill=C_MUT, anchor="la")
    d.text((W - M, M - 2), "%d 次出手 · 逐帧测算" % cov["n"], font=cf(25, True),
           fill=C_CYAN, anchor="ra")
    if FULL_LINES:
        tick_rule(d, M, W - M, M + 46)

    # ---- 标题 ----
    l1, l2, l3 = cov["title"]
    tw = W * 0.60
    s = min(fit_cf(d, l1, tw, 200), fit_cf(d, l2, tw, 200))
    s = min(s, 168)
    f = cf(s, True)
    adv = int(s * 1.14)
    y = int(H * 0.098)
    for i, line in enumerate((l1, l2)):
        wl = d.textlength(line, font=f)
        soft_text(img, ((W - wl) / 2, y + i * adv), line, f, C_TX)
    y3 = y + 2 * adv + 6
    if l3:
        f3 = cf(42, True)
        w3 = d.textlength(l3, font=f3)
        soft_text(img, ((W - w3) / 2, y3), l3, f3, C_AMBER)
        y3 += 62
    # 标题下的技术规线：默认只留中心一段短青线；满配版才补左右细刻度。
    if FULL_LINES:
        d.rectangle((W / 2 - 60, y3, W / 2 + 60, y3 + 3), fill=C_CYAN)
        d.line([(M + 40, y3 + 1), (W / 2 - 76, y3 + 1)], fill=C_RULE, width=1)
        d.line([(W / 2 + 76, y3 + 1), (W - M - 40, y3 + 1)], fill=C_RULE, width=1)
    else:
        d.rectangle((W / 2 - 34, y3, W / 2 + 34, y3 + 3), fill=C_CYAN)

    # ---- 读数块：两列，行数随卡数自适应 ----
    n = len(cards)
    n_local = len(cov["cards"])
    cw = 424
    xs = {"left": M - 22, "right": W - M + 22 - cw}
    # 只有 2 张卡时左右错开高度，不要并排放在同一行 —— 并排会在下三分之一留出一大块空白
    slots = ([("left", 0, 0.545), ("right", 1, 0.700)] if n <= 2 else
             [("left", 0, 0.500), ("right", 1, 0.500),
              ("left", 2, 0.700), ("right", 3, 0.700)])
    for side, ci, yf in slots:
        if ci >= n:
            continue
        main, sub = cards[ci]
        readout(img, d, xs[side], int(H * yf), cw, "%02d" % (ci + 1), parse_card(main, sub),
                is_cmp=(ci >= n_local))


def build_wide(cov, d, cards):
    LX = int(W * 0.055)
    d.rectangle((LX, M + 4, LX + 12, M + 16), fill=C_CYAN)
    d.text((LX + 26, M - 2), "投篮动作逐帧拆解", font=cf(25, True), fill=C_MUT, anchor="la")
    if FULL_LINES:
        tick_rule(d, LX, W - M, M + 46)

    l1, l2, l3 = cov["title"]
    tw = W * 0.34
    s = min(fit_cf(d, l1, tw, 170), fit_cf(d, l2, tw, 170))
    f = cf(s, True)
    adv = int(s * 1.12)
    y = int(H * 0.135)
    for i, line in enumerate((l1, l2)):
        soft_text(img, (LX, y + i * adv), line, f, C_TX)
    y3 = y + 2 * adv + 4
    if l3:
        f3 = cf(38, True)
        soft_text(img, (LX, y3), l3, f3, C_AMBER)
        y3 += 56
    d.rectangle((LX, y3, LX + 96, y3 + 3), fill=C_CYAN)

    # 读数块走 2×2 网格，不用单列。
    # 单列放不下：一个读数块实高约 190px（规线 + 标签 + 数字 + 量程条 + 注释），
    # 4 块单列要 800px，而标题下方只剩 ~500px —— 会叠在一起（2026-10-14 第一版横版就是这样）。
    cw = int(W * 0.247)
    ncol, n_local = 2, len(cov["cards"])
    top = max(y3 + 34, int(H * 0.46))
    for i, (main, sub) in enumerate(cards):
        r, c = divmod(i, ncol)
        # 列间距 56：30px 时左卡右对齐的「低于区间 7°」会贴上右卡的「参考 0.3–0.5s」，
        # 两卡读起来像一行（2026-10-14 横版实测）。行距 214 略大于卡高 212，避免暗池互啃。
        readout(img, d, LX + c * (cw + 56), top + r * 214, cw, "%02d" % (i + 1),
                parse_card(main, sub), is_cmp=(i >= n_local), compact=True)


def build():
    global img
    cov = cover_info()
    img, pts, t_used, geom = prepare_frame()
    if FULL_LINES:
        img = grid_overlay(img)
    img = draw_skeleton(img, pts)
    d = ImageDraw.Draw(img)
    if FULL_LINES:
        corners(d)

    cards = [(m, s) for m, s in cov["cards"]] + [(m, s) for m, s in cov.get("cmp", [])]
    if VERT:
        build_vertical(cov, d, cards)
    else:
        build_wide(cov, d, cards)

    foot = cov["foot"]          # soft_text 是原地 paste，img 对象没换，d 仍然有效
    ff = cf(22, False)
    wf = d.textlength(foot, font=ff)
    fy = H - 56
    fx = (W - wf) / 2 + 6 if VERT else M + 18
    scrim(img, fx - 20, fy - 10, wf + 30, 44, alpha=150, blur=22)
    d = ImageDraw.Draw(img)
    d.rectangle((fx - 14, fy + 9, fx - 8, fy + 15), fill=C_CYAN)
    d.text((fx, fy), foot, font=ff, fill=C_MUT, anchor="la")

    ori = "竖版" if VERT else "横版"
    name = "%s_封面_%s_%dx%d_科技风%s.png" % (R.REPORT_DATE, ori, W, H,
                                             ("_" + SUFFIX) if SUFFIX else "")
    out = os.path.join(OUTDIR, name)
    img.save(out)
    print("输出", out, img.size, "%.0f KB" % (os.path.getsize(out) / 1024))
    print("裁切区 源(%d,%d)+%dx%d  放大 %.2f×  帧 t=%.3f" % (geom[0], geom[1], geom[2], geom[3],
                                                          geom[4], t_used))
    return out


if __name__ == "__main__":
    build()
