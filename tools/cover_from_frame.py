# -*- coding: utf-8 -*-
"""用训练视频的真实帧做抖音竖版封面（1080×1440，3:4）—— v2 构图修正版

v1 的问题：直接放大 deep 裁剪帧导致人物占满画面，出手动作被标题挡住、只看到腿。
v2 改法：从**全画幅源帧**重新裁一个 3:4 区域，让「出手的完整动作」正好落在标题下方，
两侧留出空间放数据标注。

流程：取全画幅帧 → 按姿态数据定位人物 → 裁 675×900（3:4）→ 放大 1.6× → 锐化
      → 背景虚化压暗 → 顶部/底部渐变 → 骨架 → 标题 → 两侧数据标注 → 底部信息条

用法: python cover_from_frame.py [出手帧的时间(秒)] [输出名后缀]
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

TAG = sys.argv[3] if len(sys.argv) > 3 else "d"
T_REL = float(sys.argv[1]) if len(sys.argv) > 1 else 16.383
SUFFIX = sys.argv[2] if len(sys.argv) > 2 else ""
R.apply_tag(TAG)          # 让 R.REPORT_DATE / HEIGHT_CM 等跟着这个 tag 走
OUTDIR = os.path.join(R.ROOT, "抖音封面")
os.makedirs(OUTDIR, exist_ok=True)
W, H = 1080, 1440
PERSON_FRAC = 0.56                 # 人物（含举手）占封面高度的比例（用户要求以人为主体）
TOP_FRAC = 0.295                   # 人物最高点放在裁切区的 29.5% 处，上方留给标题、下方留脚
# 注意：源视频里人物（含举手）只有约 244px 高（deep 的 crop 帧是放大过的，不能按它估），
# 所以裁切区要按人物真实像素高度反算，否则人物在封面里会很小。

C_TITLE = (255, 255, 255)
C_STROKE = (255, 196, 0)
C_SKEL = (255, 202, 71)
C_ACC = (255, 196, 0)
C_TX = (233, 241, 250)

# ---- 封面文案：**必须来自本次分析的结论**，不再写死（用户 2026-10-12 定）----
# 原来这里是硬编码的 10月6日 结论与数值（"蹲得过深 · 弧线偏平"/"膝盖角 96°"/"小臂仰角 73°"），
# 拿任何新 tag 跑封面都会印错。现在统一由 R.derive_conclusions() 推导：
#   三行标题 = 我的投篮 / AI逐帧拆解 / 对外指标里偏离参考区间最大的 1~2 项
#   数据卡   = 同样那两项的「本场均值 + 措辞 + 参考区间」
# 手工覆盖入口仍然保留：SHOTVID_COVER_TITLE（| 分三行） > tags.json 的 video_title。
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
        line3 = vt[2] if len(vt) > 2 else line3
        title = (vt[0], vt[1], line3)
    else:
        title = ("投篮精进", "日拱一卒", line3)
    env = os.environ.get("SHOTVID_COVER_TITLE")
    if env:
        p = env.split("|")
        title = (p[0], p[1] if len(p) > 1 else title[1], p[2] if len(p) > 2 else title[2])

    sides = ("left", "right")
    cards = [(main, sub) for main, sub in conc["title"]["cards"]]

    # ---- 跨日期对比卡（新增）：同样由数据推导，见 report.derive_compare ----
    # 取「值挪动最大（效应量最大）」+「波动变大最多」各一条，追加在本场结论卡之后。
    # 数量可用 SHOTVID_COVER_CMP 控制（0 = 不要对比卡）。
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


def fit_font(draw, text, target_w, size_max=300, size_min=12, bold=True):
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
    # ---- 0) 取全画幅帧 + 该时刻的姿态数据 ----
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

    # crop 坐标 → 全画幅坐标
    sxc, syc = box[2] / cw0, box[3] / ch0
    fpts = {k: (box[0] + v[0] * sxc, box[1] + v[1] * syc) for k, v in kp.items()}
    fxs = [p[0] for p in fpts.values()]
    fys = [p[1] for p in fpts.values()]
    cx_person = (min(fxs) + max(fxs)) / 2

    # ---- 1) 按人物真实高度反算裁切区，再裁 3:4 ----
    person_h = max(fys) - min(fys)
    crop_h = int(round(person_h / PERSON_FRAC))
    crop_w = int(round(crop_h * W / H))
    y0 = clamp(min(fys) - crop_h * TOP_FRAC, 0, full.shape[0] - crop_h)
    x0 = clamp(cx_person - crop_w / 2, 0, full.shape[1] - crop_w)
    y0, x0 = int(y0), int(x0)
    crop_h, crop_w = int(crop_h), int(crop_w)
    print('人物高 %.0fpx → 裁切 %dx%d 放大 %.2f×' % (person_h, crop_w, crop_h, W / crop_w))
    sub = full[y0:y0 + crop_h, x0:x0 + crop_w]
    img = Image.fromarray(sub)
    img = img.filter(ImageFilter.GaussianBlur(0.7))          # 先轻微降噪，避免放大后噪点被锐化
    img = img.resize((W, H), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2.6, percent=145, threshold=3))
    sx, sy = W / crop_w, H / crop_h

    def mp(p):
        return ((p[0] - x0) * sx, (p[1] - y0) * sy)

    pts = {k: mp(v) for k, v in fpts.items()}

    # ---- 2) 背景虚化 + 压暗 + 去饱和 ----
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
    b = (b * 0.40 + g * 0.60) * 0.60          # 去饱和 + 压暗
    blur = Image.fromarray(b.clip(0, 255).astype(np.uint8))
    img = Image.composite(img, blur, mask)

    # ---- 2b) 人物提亮 + 暖色分级（与暖黄骨架呼应）----
    a2 = np.asarray(img).astype(np.float32)
    m2 = np.asarray(mask).astype(np.float32) / 255.0
    a2 *= (1.0 + 0.15 * m2[:, :, None])          # 主体提亮
    a2[:, :, 0] *= 1.05                          # 暖调：抬红
    a2[:, :, 2] *= 0.96                          # 压蓝
    img = Image.fromarray(a2.clip(0, 255).astype(np.uint8))

    # ---- 3) 顶部/底部渐变 ----
    arr = np.asarray(img).astype(np.float32)
    yy = np.arange(H, dtype=np.float32).reshape(H, 1)
    top_a = np.clip((H * 0.36 - yy) / (H * 0.36), 0, 1) ** 1.2 * 0.86
    bot_a = np.clip((yy - H * 0.78) / (H * 0.22), 0, 1) ** 1.5 * 0.8
    alpha = np.clip(np.maximum(top_a, bot_a), 0, 1)
    dark = np.array([6.0, 10.0, 18.0], dtype=np.float32).reshape(1, 1, 3)
    arr = arr * (1 - alpha[:, :, None]) + dark * alpha[:, :, None]
    # 暗角：以人物为中心，四周压暗，视觉重心落到人身上
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    rr = np.sqrt(((gx - W * 0.5) / (W * 0.80)) ** 2 + ((gy - H * 0.60) / (H * 0.64)) ** 2)
    vig = np.clip(1.0 - 0.42 * np.clip(rr - 0.42, 0, 1) / 0.58, 0.55, 1.0)
    arr = arr * vig[:, :, None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)

    # ---- 4) 骨架（先画发光层，再画清晰线）----
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for a, b_ in EDGES:
        p, q = pts.get(a), pts.get(b_)
        if p and q:
            gd.line([p, q], fill=(255, 198, 70, 82), width=15)
    for p in pts.values():
        gd.ellipse([p[0] - 22, p[1] - 22, p[0] + 22, p[1] + 22], fill=(255, 198, 70, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(9))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)

    for a, b_ in EDGES:
        p, q = pts.get(a), pts.get(b_)
        if not (p and q):
            continue
        d.line([p, q], fill=(0, 0, 0), width=9)
        d.line([p, q], fill=C_SKEL, width=4)
    for p in pts.values():
        d.ellipse([p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7], fill=(0, 0, 0))
        d.ellipse([p[0] - 4.5, p[1] - 4.5, p[0] + 4.5, p[1] + 4.5], fill=(255, 255, 255))

    # ---- 5) 标题（占上部 ~26%）----
    l1, l2, l3 = cov["title"]
    tw = W * 0.63
    s1 = fit_font(d, l1, tw, 210)
    s2 = fit_font(d, l2, tw, 210)
    s1 = min(s1, int(s2 * 1.7))     # 短句别撑满，和第二行拉开层级
    f1, f2 = font(s1), font(s2)
    y1 = int(H * 0.036)
    w1 = d.textlength(l1, font=f1)
    w2 = d.textlength(l2, font=f2)
    for dx, dy, col, stro in ((5, 7, (0, 0, 0), (0, 0, 0)), (0, 0, C_TITLE, C_STROKE)):
        d.text(((W - w1) / 2 + dx, y1 + dy), l1, font=f1, fill=col, stroke_width=6, stroke_fill=stro)
        d.text(((W - w2) / 2 + dx, y1 + s1 * 1.22 + dy), l2, font=f2, fill=col, stroke_width=6, stroke_fill=stro)
    if l3:                                  # 第三行为空则整行不画
        f3 = font(46, bold=False)
        w3 = d.textlength(l3, font=f3)
        y3 = y1 + s1 * 1.22 + s2 * 1.40 + 4
        d.text(((W - w3) / 2 + 3, y3 + 3), l3, font=f3, fill=(0, 0, 0), stroke_width=7, stroke_fill=(0, 0, 0))
        d.text(((W - w3) / 2, y3), l3, font=f3, fill=(255, 233, 152))

    # ---- 6) 数据标注（放人物两侧，不压主体）----
    # 卡位固定 4 个槽位，左右交替、自上而下错开，最多画 4 张（本场结论 2 + 对比 2）。
    # 跨日期对比卡用冷色描边，与本场结论卡（暖黄）区分，避免被误读成同一场的数据。
    SLOTS = [("left", 0.500), ("right", 0.440), ("left", 0.635), ("right", 0.575)]
    C_CMP = (110, 205, 255)

    def callout(text, sub, side, yf, accent=C_ACC):
        bw, bh = 420, 100
        m = 34
        bx = m if side == "left" else W - m - bw
        by = int(H * yf)
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=13, fill=(9, 15, 25),
                            outline=accent, width=3)
        d.rounded_rectangle((bx, by, bx + 9, by + bh), radius=5, fill=accent)
        inner = bw - 44
        fs1 = fit_font(d, text, inner, size_max=33, size_min=20)
        d.text((bx + 22, by + 34), text, font=font(fs1), fill=C_TX, anchor="lm")
        if sub:
            fs2 = fit_font(d, sub, inner, size_max=21, size_min=13, bold=False)
            d.text((bx + 22, by + 74), sub, font=font(fs2, bold=False),
                   fill=(150, 162, 178), anchor="lm")

    all_cards = [(t, s, C_ACC) for t, s in cov["cards"]] + \
                [(t, s, C_CMP) for t, s in cov.get("cmp", [])]
    for _i, (_t, _s, _ac) in enumerate(all_cards):
        side, yf = SLOTS[_i % len(SLOTS)]
        callout(_t, _s, side, yf, _ac)

    # ---- 7) 底部信息条 ----
    f4 = font(26, bold=False)
    foot = cov["foot"]
    wf = d.textlength(foot, font=f4)
    d.text(((W - wf) / 2, H - 76), foot, font=f4, fill=(168, 178, 192))

    # 命名统一「<日期>_封面_<版式>_<尺寸>」：日期前缀 + 版式，一眼分清是哪天、哪个版式
    name = "%s_封面_竖版_1080x1440%s.png" % (R.REPORT_DATE, ("_" + SUFFIX) if SUFFIX else "")
    out = os.path.join(OUTDIR, name)
    img.save(out)
    print("输出", out, img.size, "%.0f KB" % (os.path.getsize(out) / 1024))
    print("裁切区 源(%d,%d)+%dx%d  放大 %.2f×  帧 t=%.3f" % (x0, y0, crop_w, crop_h, sx, rec["t"]))
    return out


if __name__ == "__main__":
    build()
