# -*- coding: utf-8 -*-
"""横版封面（1920×1080，16:9）—— 与竖版同一视觉语言：真实帧 + 暖黄骨架 + 数据标注

构图：左文右人
- 右侧：真实出手帧裁切放大，人物（含举手）占画面高度 ~76%，背景虚化压暗 + 暗角
- 左侧：两行标题 + 两枚数据标注卡，保证信息在信息流缩略图里仍可读
- 底部：一条低调信息条

用法: python cover_wide.py [出手帧的时间(秒)] [输出名后缀]
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from bbkit import make_generator, frame_at, probe
from ball import cg_to_np
import fontkit
import report as R

TAG = sys.argv[4] if len(sys.argv) > 4 else "d"
T_REL = float(sys.argv[1]) if len(sys.argv) > 1 else 16.383
SUFFIX = sys.argv[2] if len(sys.argv) > 2 else ""
R.apply_tag(TAG)          # 让 R.REPORT_DATE / HEIGHT_CM 等跟着这个 tag 走
OUTDIR = os.path.join(R.ROOT, "抖音封面")
os.makedirs(OUTDIR, exist_ok=True)

# 画幅：默认 4:3（1440×1080）；第三个参数传 16x9 则切 1920×1080
RATIO = sys.argv[3] if len(sys.argv) > 3 else "4x3"
W, H = (1920, 1080) if RATIO == "16x9" else (1440, 1080)
PERSON_FRAC = 0.74          # 人物（含举手）占封面高度（留出底部空间，别把鞋裁掉）
PERSON_X_FRAC = 0.76        # 人物中心放在裁切区水平 76% 处，左侧留给文字（背身机位下人体很窄，靠右更聚焦）
TOP_FRAC = 0.11             # 人物最高点位置（上方留一点，别顶到边）

C_TITLE = (255, 255, 255)
C_STROKE = (255, 196, 0)
C_SKEL = (255, 202, 71)
C_ACC = (255, 196, 0)
C_CMP = (110, 205, 255)     # 跨日期对比卡用冷色，与本场结论卡区分
C_TX = (233, 241, 250)

# ---- 封面文案：**必须来自本次分析的结论**，不再写死（用户 2026-10-12 定）----
# 原来这里是硬编码的 10月6日 结论与数值，拿任何新 tag 跑都会印错。
# 统一由 R.derive_conclusions() 推导，手工覆盖仍走 SHOTVID_COVER_TITLE / tags.json。
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

    cards = [(main, sub) for main, sub in conc["title"]["cards"]]     # 横版数据卡是 2 元组

    # ---- 跨日期对比卡（新增）：同样由数据推导，见 report.derive_compare ----
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

FONT_PATH = "/System/Library/Fonts/PingFang.ttc"
_F = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _F:
        # 走 fontkit：确保取的是 PingFang SC（简体），不是 TC（繁体缺简体字）
        _F[key] = fontkit.load(size, bold)
    return _F[key]


def fit_font(draw, text, target_w, size_max=280, size_min=12, bold=True):
    lo, hi = size_min, size_max
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text, font=font(mid, bold)) <= target_w:
            lo = mid
        else:
            hi = mid - 1
    return lo


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


def build():
    cov = cover_info()          # 标题 / 数据卡 / 底部信息条全部由数据推导
    # ---- 0) 全画幅帧 + 该时刻姿态 ----
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

    # ---- 1) 按人物真实高度反算 16:9 裁切区 ----
    person_h = max(fys) - min(fys)
    crop_h = int(round(person_h / PERSON_FRAC))
    crop_w = int(round(crop_h * W / H))
    crop_h = min(crop_h, fh)
    crop_w = min(crop_w, fw)
    y0 = clamp(min(fys) - crop_h * TOP_FRAC, 0, fh - crop_h)
    x0 = clamp(cx_person - crop_w * PERSON_X_FRAC, 0, fw - crop_w)
    y0, x0 = int(y0), int(x0)
    print('人物高 %.0fpx → 裁切 %dx%d 放大 %.2f×' % (person_h, crop_w, crop_h, W / crop_w))
    print('源帧人物中心 x=%.0f 框=%s' % (cx_person, [round(v) for v in box]))

    sub = full[y0:y0 + crop_h, x0:x0 + crop_w]
    img = Image.fromarray(sub)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    img = img.resize((W, H), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2.6, percent=145, threshold=3))
    sx, sy = W / crop_w, H / crop_h

    def mp(p):
        return ((p[0] - x0) * sx, (p[1] - y0) * sy)

    pts = {k: mp(v) for k, v in fpts.items()}
    # 过滤：背身机位下 Vision 会把某一侧手臂的关节点估到画外，映射后会拉出穿越画面的长线 —— 直接剔除画外点
    pts = {k: p for k, p in pts.items() if -6 <= p[0] <= W + 6 and -6 <= p[1] <= H + 6}
    max_edge = (max(fys) - min(fys)) * sy * 0.72      # 正常骨段不会超过人体高度的 72%

    def seg(a, b_):
        p, q = pts.get(a), pts.get(b_)
        if not (p and q):
            return None
        if ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5 > max_edge:
            return None
        return p, q

    # ---- 2) 背景虚化 + 压暗 + 去饱和（保护人物）----
    bx0 = min(p[0] for p in pts.values()) - 90
    bx1 = max(p[0] for p in pts.values()) + 90
    by0 = min(p[1] for p in pts.values()) - 70
    by1 = max(p[1] for p in pts.values()) + 110
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse((bx0 - 20, by0 - 40, bx1 + 20, by1 + 60), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(72))
    blur = img.filter(ImageFilter.GaussianBlur(22))
    b = np.asarray(blur).astype(np.float32)
    g = b.mean(axis=2, keepdims=True)
    b = (b * 0.28 + g * 0.72) * 0.55
    blur = Image.fromarray(b.clip(0, 255).astype(np.uint8))
    img = Image.composite(img, blur, mask)

    # ---- 2b) 人物提亮 + 暖调 ----
    a2 = np.asarray(img).astype(np.float32)
    m2 = np.asarray(mask).astype(np.float32) / 255.0
    a2 *= (1.0 + 0.15 * m2[:, :, None])
    a2[:, :, 0] *= 1.05
    a2[:, :, 2] *= 0.96
    img = Image.fromarray(a2.clip(0, 255).astype(np.uint8))

    # ---- 3) 左侧文字区压暗（横向渐变）+ 上下渐变 + 暗角 ----
    arr = np.asarray(img).astype(np.float32)
    xx = np.arange(W, dtype=np.float32).reshape(1, W)
    left_a = np.clip((W * 0.52 - xx) / (W * 0.52), 0, 1) ** 1.15 * 0.80
    yy = np.arange(H, dtype=np.float32).reshape(H, 1)
    top_a = np.clip((H * 0.30 - yy) / (H * 0.30), 0, 1) ** 1.2 * 0.55
    bot_a = np.clip((yy - H * 0.82) / (H * 0.18), 0, 1) ** 1.5 * 0.80
    alpha = np.clip(np.maximum(np.maximum(left_a, top_a), bot_a), 0, 1)
    dark = np.array([6.0, 10.0, 18.0], dtype=np.float32).reshape(1, 1, 3)
    arr = arr * (1 - alpha[:, :, None]) + dark * alpha[:, :, None]
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    rr = np.sqrt(((gx - W * 0.74) / (W * 0.68)) ** 2 + ((gy - H * 0.55) / (H * 0.72)) ** 2)
    vig = np.clip(1.0 - 0.40 * np.clip(rr - 0.45, 0, 1) / 0.55, 0.58, 1.0)
    arr = arr * vig[:, :, None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

    # ---- 4) 骨架（发光层 + 清晰线）----
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for a, b_ in EDGES:
        s = seg(a, b_)
        if s:
            gd.line([s[0], s[1]], fill=(255, 198, 70, 82), width=15)
    for p in pts.values():
        gd.ellipse([p[0] - 22, p[1] - 22, p[0] + 22, p[1] + 22], fill=(255, 198, 70, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(9))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)

    for a, b_ in EDGES:
        s = seg(a, b_)
        if not s:
            continue
        d.line([s[0], s[1]], fill=(0, 0, 0), width=10)
        d.line([s[0], s[1]], fill=C_SKEL, width=4)
    for p in pts.values():
        d.ellipse([p[0] - 8, p[1] - 8, p[0] + 8, p[1] + 8], fill=(0, 0, 0))
        d.ellipse([p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5], fill=(255, 255, 255))

    # ---- 5) 标题（左侧上方；卡片多时整体上移，给卡片让位）----
    all_cards = [(t, s, C_ACC) for t, s in cov["cards"]] + \
                [(t, s, C_CMP) for t, s in cov.get("cmp", [])]
    n = len(all_cards)
    LX = int(W * 0.055)
    L1, L2, L3 = cov["title"]
    tw = W * 0.45
    s1 = fit_font(d, L1, tw, 200)
    s2 = fit_font(d, L2, tw, 200)
    s1 = min(s1, int(s2 * 1.7))
    f1, f2 = font(s1), font(s2)
    y1 = int(H * (0.115 if n <= 3 else 0.085))
    y2 = y1 + int(s1 * 1.10)
    for dx, dy, col, stro in ((5, 7, (0, 0, 0), (0, 0, 0)), (0, 0, C_TITLE, C_STROKE)):
        d.text((LX + dx, y1 + dy), L1, font=f1, fill=col, stroke_width=6, stroke_fill=stro)
        d.text((LX + dx, y2 + dy), L2, font=f2, fill=col, stroke_width=6, stroke_fill=stro)
    y3_end = y2 + int(s2 * 1.02)
    if L3:
        f3 = font(46, bold=False)
        y3 = y2 + int(s2 * 1.46)
        d.text((LX + 3, y3 + 3), L3, font=f3, fill=(0, 0, 0), stroke_width=7, stroke_fill=(0, 0, 0))
        d.text((LX, y3), L3, font=f3, fill=(255, 233, 152))
        y3_end = y3 + 46
    # 标题左侧的强调竖条
    d.rounded_rectangle((LX - 26, y1 + 8, LX - 12, y2 + int(s2 * 1.02)),
                        radius=7, fill=C_STROKE)

    # ---- 6) 数据标注卡（左侧下方，纵向排列；卡片数可变，整体自适应不越界）----
    # 跨日期对比卡用冷色描边，与本场结论卡（暖黄）区分，避免被误读成同一场的数据。
    def card(y, text, sub, accent=C_ACC, ch=96, fs1=40, fs2=27, pad=36):
        cw = int(min(470, W * 0.34))
        d.rounded_rectangle((LX, y, LX + cw, y + ch), radius=16,
                            fill=(9, 15, 25), outline=accent, width=3)
        d.rounded_rectangle((LX, y, LX + 9, y + ch), radius=5, fill=accent)
        inner = cw - pad - 14
        s1 = fit_font(d, text, inner, size_max=fs1, size_min=20)
        d.text((LX + pad, y + int(ch * 0.34)), text, font=font(s1), fill=C_TX, anchor="lm")
        if sub:
            s2 = fit_font(d, sub, inner, size_max=fs2, size_min=14, bold=False)
            d.text((LX + pad, y + int(ch * 0.74)), sub, font=font(s2, bold=False),
                   fill=(150, 162, 178), anchor="lm")

    # 卡片纵向预算：底边不越过信息条（H-105），且不压到标题
    ch, fs1, fs2, pad = (96, 40, 27, 36) if n <= 3 else (84, 34, 23, 30)
    y_bot = H - 105
    y0 = max(y3_end + 24, int(H * (0.575 if n <= 2 else (0.545 if n == 3 else 0.515))))
    step = 126 if n <= 1 else max(88, min(126, (y_bot - ch - y0) // (n - 1)))
    for _i, (_t, _s, _ac) in enumerate(all_cards):
        card(y0 + _i * step, _t, _s, _ac, ch=ch, fs1=fs1, fs2=fs2, pad=pad)

    # ---- 7) 底部信息条 ----
    f4 = font(28, bold=False)
    foot = cov["foot"]
    d.text((LX, H - 62), foot, font=f4, fill=(168, 178, 192))

    # 命名统一「<日期>_封面_横版_<尺寸>」，与竖版封面同一口径
    name = "%s_封面_横版_%dx%d%s.png" % (R.REPORT_DATE, W, H, ("_" + SUFFIX) if SUFFIX else "")
    out = os.path.join(OUTDIR, name)
    img.save(out)
    print("输出", out, img.size, "%.0f KB" % (os.path.getsize(out) / 1024))
    print("裁切区 源(%d,%d)+%dx%d  放大 %.2f×  帧 t=%.3f" % (x0, y0, crop_w, crop_h, sx, rec["t"]))
    return out


if __name__ == "__main__":
    build()
