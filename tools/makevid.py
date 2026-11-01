# -*- coding: utf-8 -*-
"""把第一段最好的 3 个动作做成带实时数据面板的视频

- 数据全部来自已有分析产物（deep_*.json 的关节数据 + ball_*.json 的球轨迹 + report_data.json 的逐球指标），
  不重新分析视频。
- 画面：左侧 deep 裁剪帧 + 骨架（红=投篮手 青=辅助手 粉=躯干腿）+ 起球轨迹（黄）+ 当前球圈；
  右侧数据面板：相位、6 项实时指标（带参考区间与判定）、本球汇总。
- 输出 30fps、每源帧写 1 次 → 源是 60fps、深扫抽 30fps，故播放为 2 倍慢放（连续不重复）；最后定格 1 秒。
- 用 AVAssetWriter 原生写 H.264（本机无 ffmpeg）。

用法: python makevid.py [tag]
"""
import glob
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
import numpy as np
import Quartz
import AVFoundation as AVF
import CoreMedia as CM
import Quartz.CoreVideo as CV
from Foundation import NSURL

import report as R
from makecharts import (font, rrect, text, qual, fmt, wrap,
                        BG, CARD, CARD2, LINE, TX, TX2, ACC, GOOD, WARN, BAD, YEL, CYAN)
from bbkit import load_cg, probe, make_generator, frame_at
from ball import cg_to_np

TAG = sys.argv[1] if len(sys.argv) > 1 else "d"
LAYOUT = sys.argv[2] if len(sys.argv) > 2 else "v"     # v=抖音竖版(默认)  h=横版
TOPSUF = sys.argv[3] if len(sys.argv) > 3 else "top3"  # 清单文件后缀：top3=按评分，first3=按时间取前三
LABEL = "名" if TOPSUF.startswith("top") else "球"      # 文件名用「第N名」还是「第N球」
DEEP_SUF = sys.argv[4] if len(sys.argv) > 4 else ""     # 深扫数据后缀："" = deep_*.json(30fps)
                                                        # "60" = deep60_*.json(60fps，出 2 倍慢放用)
ONLY = sys.argv[5] if len(sys.argv) > 5 else ""         # 只出第 N 个，如 "2,3,4,5"（空 = 全出）
                                                        # 注意 N 是「清单里的序号」，不是文件名里的球号
R.apply_tag(TAG)      # 别忘了：report 导入时只按 sys.argv[2] 猜 tag，这里用真的 tag 覆盖一次
OUTDIR = os.path.join(R.ROOT, "视频") if LAYOUT == "v" else os.path.join(R.ROOT, "视频", "横版")
os.makedirs(OUTDIR, exist_ok=True)
# 画面用【全画幅】原帧：竖版填满画布宽度，上方留给标题覆盖；数据区在下方。
# 显示比例 DISP_AR 决定画面在画布上的大小（见 _apply_layout）：
#   16/9 = 源比例，不裁切；调小（如 1.5 / 4÷3）= 左右裁切、主体更大。
if LAYOUT == "v":
    CANVAS_W, CANVAS_H = 1080, 1920   # 抖音竖版 9:16
    FRAME_W = 1080                    # 填满宽度（原来 1032，两侧各留 24px）
    FRAME_Y = 76
else:
    CANVAS_W, CANVAS_H = 1240, 1160   # 横版（全画幅 + 下方面板）
    FRAME_W = 1200
    FRAME_Y = 22
FRAME_H = int(round(FRAME_W * 9 / 16))
FRAME_X = (CANVAS_W - FRAME_W) // 2
PANEL_Y = FRAME_Y + FRAME_H + 26
SRC_SIZE = [1920, 1080]               # 源视频尺寸（首次抽帧时校正）
_CROP = [None]                        # 源帧坐标系的裁切框 (x0,y0,x1,y1)；None = 整幅
DEFAULT_DISP_AR = 16 / 9              # 竖版默认仍用源比例（不裁切）

# 慢放倍数 = REPEAT × 采样fps ÷ 播放fps（采样fps 由 deep 序列实际帧间隔算出，见 build_shot）
# 切记：**降采样不等于慢放** —— 深扫抽 30fps 只是丢了帧，播放速度由 write_video 的 fps 和 REPEAT 决定。
SPEED_TXT = [""]      # 运行时填充，供面板文案使用
FPS_OUT = 30
REPEAT = 1            # 每源帧只写 1 次 → 输出 30fps 连续帧（原来写 2 次会让每个画面重复一遍，
                      # 视觉上「一顿一顿」，用户 2026-10-06 要求定格之外的画面必须连贯）
HOLD_FRAMES = 0        # 结尾不再额外定格 —— 关键帧已经在定格了；
                      # 收尾定格会把「球落地后」的静止画面再重复一遍（用户 2026-10-06 反馈「最后重复了第三球」）

PHASES = ["预备", "屈膝/蓄力", "起球/蹬伸", "出手", "随挥"]
PHASE_COLOR = {"预备": TX2, "屈膝/蓄力": ACC, "起球/蹬伸": YEL, "出手": GOOD, "随挥": CYAN, "落地": TX2}


def load_all():
    # 注意：report_data.json 会被最近一次生成的报告覆盖，不能依赖它；
    # 直接从已保存的 shot json 现算指标（纯本地计算，不重新分析视频）。
    shots = R.load_shots(TAG)
    ms = {}
    for m in (R.shot_metrics(s) for s in shots):
        if m:
            ms[round(m["t_rel"], 1)] = m
    agg = R.aggregate(list(ms.values()), R.METRIC_KEYS)
    top3 = json.load(open(os.path.join(R.DATA, "%s_%s.json" % (TAG, TOPSUF))))
    _pat = os.path.join(R.DATA, TAG, "deep%s_*.json" % DEEP_SUF)
    _st = len("deep%s_" % DEEP_SUF)
    files = sorted(glob.glob(_pat), key=lambda f: float(os.path.basename(f)[_st:-5]))
    return None, ms, agg, top3, files


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


_GEN = [None]


def frame_to_pil(dj, idx):
    """【全画幅】原帧 + 骨架 → PIL RGB。

    画面直接取原视频的完整帧（不裁剪），骨架的 kp 存在 crop 帧坐标系里，
    要按 `全图 = box + kp × box尺寸/crop尺寸` 换算回来，再一起缩到 FRAME_W×FRAME_H。
    """
    meta = dj["meta"]
    r = dj["seq"][idx]
    if _GEN[0] is None:
        # meta["video"] 是相对工程根的路径；shotcli 以 cwd=tools 拉起时必须走 media_path，
        # 否则打不开视频 → 一帧都产不出来（见 report.media_path 说明）
        _GEN[0], _ = make_generator(R.media_path(meta["video"]), (1920, 1080))
    cg = frame_at(_GEN[0], r["t"])
    if cg is None:
        return None
    full = Image.fromarray(cg_to_np(cg))
    crop = init_frame(meta)                      # 裁切框（每球一次，但同一视频只算一次）
    img = full.crop(tuple(int(v) for v in crop)).resize((FRAME_W, FRAME_H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    sc = FRAME_W / float(crop[2] - crop[0])
    kp = {}
    for k, v in (r.get("kp") or {}).items():
        kp[k] = kp_to_frame(meta, v, crop)
    lw = max(2, int(round(3.2 * sc * 1.6)))
    for a, b, c in EDGES:
        p, q = kp.get(a), kp.get(b)
        if not (p and q):
            continue
        d.line([p, q], fill=tuple(int(x * 255) for x in c), width=lw)
    for p in kp.values():
        d.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=(255, 255, 255))
    return img


_JCOL = {"ok": GOOD, "warn": WARN, "bad": BAD}


def jq(k, v):
    """实时判定：措辞用 report.VERDICT_WORDS（蹲得过深 / 出手偏慢…），颜色按级别映射。

    注意别和 makecharts.qual 混用 —— 那个返回 (文本, 颜色, 类别)，措辞是「偏低/偏高」。
    """
    txt, cls = R.qual(k, v)
    return txt, _JCOL.get(cls, TX2)


def phase_of(t, t_dip, t_rel):
    if t < t_dip - 0.08:
        return "预备"
    if t < t_dip + 0.10:
        return "屈膝/蓄力"
    if t < t_rel - 0.13:
        return "起球/蹬伸"
    if t <= t_rel + 0.10:
        return "出手"
    if t <= t_rel + 0.35:
        return "随挥"
    return "落地"


# 相位 → 帧级指标该对照哪条文献区间（None = 该相位没有对应文献区间，只显示数值、不给判定）
#
# 为什么必须分相位：面板显示的是「这一帧」的数值，而文献区间是按特定时刻测出来的。
# 用错相位会捏造假偏差 —— 屈膝最低点那一帧，手臂本来就折叠着（本球实测投篮手肘角 67°），
# 拿「出手瞬间」的 134–180° 去比就是「明显偏低」；出手那一帧，腿本来就蹬直了（168°），
# 拿「屈膝最低点」的 105–121° 去比就是「明显蹲得过浅」。两个定格各有一组假偏差。
# 正确的配对是：屈膝帧的手臂 ← 收球肘角区间、屈膝帧的膝 ← 屈膝角(最低) 区间；
#              出手帧的手臂 ← 出手时肘角区间、出手帧的膝 ← 出手时膝角区间。
# （2026-10-12 修，用户确认「按相位给区间」）
PHASE_REF = {
    "投篮手肘角": {"预备": "收球肘角_右", "屈膝/蓄力": "收球肘角_右", "起球/蹬伸": None,
                   "出手": "出手时肘角_右", "随挥": "出手时肘角_右", "落地": None},
    "膝角": {"预备": "屈膝角(最低)", "屈膝/蓄力": "屈膝角(最低)", "起球/蹬伸": None,
             "出手": "出手时膝角", "随挥": None, "落地": None},
    # 小臂仰角的文献区间只按「出手瞬间」定义；其余相位没有可比区间
    "小臂仰角": {"出手": "出手小臂仰角"},
}
NO_REF_TXT = "该相位无文献区间"


def phase_ref(name, ph):
    """某指标在某相位下该对照的文献区间键；返回 None = 该相位不判定。"""
    return PHASE_REF.get(name, {}).get(ph)


FREEZE_FRAMES = 78    # 问题帧定格时长（30fps × 2.6s）
POST_CUT = 1.4        # 出手后画面保留时长（秒）。人约 1.1s 落地，球 0.6s 出画；
                      # 超过这个时间的画面是「落地后站着不动」，纯拖时长，直接截掉


ADVERSE = {          # True = 该指标「偏大」为不利方向；False = 偏小为不利；None = 两个方向都算
    "出手时肘角_右": False, "出手小臂仰角": False, "出手点离地高度": False,
    "垂直弹跳": False, "收球肘角_右": False, "屈膝角(最低)": False,
    "身体位移": True, "落地位移": True, "屈膝到出手": None,
}
REL_MIN = 0.08       # 相对本场均值的偏离阈值（8%）

# 均值尺度的「结论词」：直接说结论，别只抛数字。格式 = （偏大时的说法, 偏小时的说法）
REL_WORDS = {
    "身体位移":      ("身体位移太多", "身体位移太少"),
    "落地位移":      ("落地位移太大", "落地位移太小"),
    "屈膝角(最低)":  ("蹲得过深", "蹲得过浅"),
    "屈膝到出手":    ("出手偏慢", "出手偏快"),
    "出手点离地高度": ("出手点偏高", "出手点偏低"),
    "垂直弹跳":      ("起跳偏高", "起跳偏低"),
    "出手时肘角_右": ("投篮手肘角偏大", "投篮手肘角偏小"),
    "出手小臂仰角":  ("弧线偏陡", "弧线偏平"),
    "收球肘角_右":   ("收球折叠过度", "收球折叠不足"),
}
CIRCLE_R = 20         # 定格标注：关节圈半径（用户 2026-10-12 要求从 30 缩小）
ANNOT_COLOR = {"ok": GOOD, "warn": (255, 196, 0), "bad": (255, 92, 92), "rel": (126, 178, 235)}


def find_issues(m, seq, t_rel, t_dip, agg):
    """挑出这一球要定格讲解的问题 —— 两个尺度都标。

    ① 文献区间：dev = 超出量 / 区间宽度，只标 dev > 0 的（黄牌 ⚠ / 红牌 ✗）。
    ② 相对本场均值：文献达标、但朝不利方向偏离均值 ≥ 8% 的（灰牌 ·），用来指出「这一球比平时差在哪」。
    **左臂（辅助手）的肘角一律不参与** —— 背身机位下 Vision 的左右标签会跳变，
    这两个数值实测不可靠（报告里也标了这条局限），拿它当问题讲会误导。
    """
    i_rel = min(range(len(seq)), key=lambda k: abs(seq[k]["t"] - t_rel))
    i_dip = min(range(len(seq)), key=lambda k: abs(seq[k]["t"] - t_dip))
    # (指标key, 定格帧, 圈哪个关节, 标题, 单位, 小数位)
    checks = [
        ("出手时肘角_右", i_rel, "right_forearm_joint", "投篮手肘角", "°", 0),
        ("出手小臂仰角", i_rel, "right_hand_joint", "出手小臂仰角", "°", 0),
        ("出手点离地高度", i_rel, "right_hand_joint", "出手点高度", "×身高", 2),
        ("垂直弹跳", i_rel, "right_foot_joint", "垂直弹跳", "×身高", 2),
        ("落地位移", i_rel, "root", "落地位移", "×身高", 2),
        ("收球肘角_右", i_dip, "right_forearm_joint", "收球右肘角", "°", 0),
        ("屈膝角(最低)", i_dip, "right_leg_joint", "屈膝角", "°", 0),
        ("屈膝到出手", i_dip, "root", "出手节奏", "s", 2),
        ("身体位移", i_dip, "root", "身体位移", "×身高", 2),
    ]
    ref, rel = [], []
    for key, idx, joint, title, unit, nd in checks:
        if key in R.LEFT_KEYS:      # 防御：辅助手指标永不进对外标注（见 report.LEFT_KEYS）
            continue
        v = m.get(key)
        if v is None or key not in R.REFERENCE:
            continue
        lo, hi = R.REFERENCE[key][0], R.REFERENCE[key][1]
        span = max(hi - lo, 1e-6)
        dev = (lo - v) / span if v < lo else (v - hi) / span
        if dev > 0:
            ref.append((dev, idx, joint, title, v, lo, hi, key, unit, nd))
        a = agg.get(key)
        mu = a.get("mean") if isinstance(a, dict) else a
        if not mu:
            continue
        r = (v - mu) / abs(mu)
        ad = ADVERSE.get(key)
        bad = (r > 0) if ad is True else ((r < 0) if ad is False else abs(r) > 0)
        if bad and abs(r) >= REL_MIN:
            rel.append((abs(r), idx, joint, title, v, r, unit, nd, key))
    ref.sort(key=lambda x: -x[0])
    rel.sort(key=lambda x: -x[0])
    out, used = [], set()
    for dev, idx, joint, title, v, lo, hi, key, unit, nd in ref[:2]:
        w, cls = R.qual(key, v)
        out.append({"idx": idx, "joint": joint, "level": "bad" if cls == "bad" else "warn",
                    "text": "%s%s：%s %.*f%s（参考 %.*f–%.*f%s）" % (
                        "⚠ " if dev < 0.5 else "✗ ", w or "偏离", title, nd, v, unit, nd, lo, nd, hi, unit)})
        used.add(key)
    for a, idx, joint, title, v, r, unit, nd, key in rel:
        if key in used or len(out) >= 3:
            continue
        used.add(key)
        wl = REL_WORDS.get(key)
        head = (wl[0] if r > 0 else wl[1]) if wl else ("%s %.*f%s" % (title, nd, v, unit) + "偏" + ("大" if r > 0 else "小"))
        out.append({"idx": idx, "joint": joint, "level": "rel",
                    "text": "%s · 比本场均值%s %d%%" % (
                        head, "大" if r > 0 else "小", round(abs(r) * 100))})
    return out


def panel_v(im_draw, rank, score, t_rel, t_dip, m, agg, cur, ph, nrows):
    """数据面板：左＝我的数据，右＝专业范围＋出处。

    竖版＝大卡片（行高 148）；横版＝紧凑单行（行高 44）—— 横版画布只有 1160 高，
    面板区实际可用仅 413px，套竖版那套会溢出画布。
    """
    cp = (LAYOUT == "h")
    px0, py0 = 24, PANEL_Y
    px1, py1 = CANVAS_W - 24, CANVAS_H - 24
    rrect(im_draw, (px0, py0, px1, py1), 16, CARD, LINE)
    x = px0 + 30
    text(im_draw, (x, py0 + 16), "第 %d %s · 评分 %.0f" % (rank, LABEL, score), 22 if cp else 25, TX, True)
    text(im_draw, (px1 - 30, py0 + 18), "t = %+.2f s（0 = 出手）" % (cur["t"] - t_rel), 18, TX2, anchor="ra")
    text(im_draw, (px1 - 30, py0 + 40), "右手投篮 · 60fps 源 · %s" % SPEED_TXT[0], 14, TX2, anchor="ra")

    # 相位大字 + 进度条
    text(im_draw, (x, py0 + 46), ph, 32 if cp else 46, PHASE_COLOR.get(ph, TX), True)
    bx, bw = x, px1 - 30 - x
    pi = PHASES.index(ph) if ph in PHASES else (4 if ph == "落地" else 0)
    seg = bw / len(PHASES)
    by = py0 + (90 if cp else 112)
    for i, name in enumerate(PHASES):
        c = PHASE_COLOR[name] if i == pi else CARD2
        rrect(im_draw, (bx + i * seg + 3, by, bx + (i + 1) * seg - 3, by + (12 if cp else 16)), 5, c)
        if not cp:
            text(im_draw, (bx + i * seg + seg / 2, by + 22), name, 14,
                 PHASE_COLOR[name] if i == pi else TX2, anchor="ma")

    # ---------- 左右分栏：左＝我的数据，右＝专业范围 + 出处 ----------
    lx = px0 + 30
    colw = (px1 - px0 - 60) // 2
    rx = lx + colw + 24
    ty = by + (30 if cp else 52)
    text(im_draw, (lx, ty), "我的数据", 17 if cp else 20, TX, True)
    text(im_draw, (rx, ty), "专业范围 / 出处", 17 if cp else 20, TX, True)
    ty += (26 if cp else 36)

    def src_short(k):
        """出处取第一段（第一个分号前），截断防溢出"""
        s5 = R.REFERENCE.get(k, ("", "", "", "", ""))[4]
        return s5.split("；")[0].split(";")[0][:30]

    # (名称, 本球值, 单位, 判定key(实时项才有), 默认参考区间key)
    # 前 3 行是「帧级」指标：区间键按当前相位重新解析（PHASE_REF），拿到的可能是
    # 收球肘角/屈膝角(最低)/出手时肘角/出手时膝角，也可能是 None（该相位不判定）。
    rows = [
        ("投篮手肘角", cur["elbowR"], "°", "elbow", "出手时肘角_右"),
        ("膝角", cur["knee"], "°", "knee", "屈膝角(最低)"),
        ("小臂仰角", cur["fore"], "°", "fore", "出手小臂仰角"),
        ("出手点高度", m.get("出手点离地高度"), "×身高", None, "出手点离地高度"),
        ("垂直弹跳", m.get("垂直弹跳"), "×身高", None, "垂直弹跳"),
        ("出手节奏", m.get("屈膝到出手"), "s", None, "屈膝到出手"),
    ]
    ROWH = 44 if cp else 142      # 竖版行高（画面加高后压到 142，6 行正好填满面板）
    jd = cur.get("judge") or {}
    for i, (name, val, unit, jkey, rkey0) in enumerate(rows):
        rkey = phase_ref(name, ph) if name in PHASE_REF else rkey0
        ry = ty + i * ROWH
        nd = 2 if unit in ("×身高", "s") else 0
        u2 = "倍身高" if unit == "×身高" else unit
        valstr = "—" if val is None else ("%.*f%s" % (nd, val, unit))
        a = agg.get(rkey) if rkey else None
        mu = a.get("mean") if isinstance(a, dict) else a

        # 判定走两条路，别混：
        #   ① 帧级指标（在 PHASE_REF 里：投篮手肘角 / 膝角 / 小臂仰角）——
        #      面板给的是「这一帧」的值，判定必须按当前相位取，事先算在 cur["judge"] 里；
        #      该相位没有对应文献区间时取到 None → 不判定（宁可不给，也不拿别的相位凑）。
        #   ② 球级指标（出手点高度 / 垂直弹跳 / 出手节奏）—— 整球只有一个值、与相位无关，
        #      直接对文献区间判。
        # **2026-10-13 修**：原来球级指标的 jkey=None，这里被整段跳过 —— 于是
        # 「出手节奏 0.60s」明明超出 0.30–0.50s，面板上却只有白色数字和一句「全场均值」，
        # 和报告 HTML 里那行「! 出手偏慢」对不上（用户指出）。现在两条路都出判定。
        if jkey is not None:
            jt, jc = jd.get(jkey, (None, None))
        elif rkey and val is not None:
            jt, jc = jq(rkey, val)
        else:
            jt, jc = None, None
        col = jc or TX
        # 区间来源不是纯文献硬边界的指标（R.FIELD_KEYS：教学口径 / 本场分布），打个星号
        star = "*" if rkey in R.FIELD_KEYS else ""

        if rkey:
            lo, hi = R.REFERENCE[rkey][0], R.REFERENCE[rkey][1]
            rng = "%.*f–%.*f%s" % (nd, lo, nd, hi, u2)
        else:
            rng = NO_REF_TXT       # 该相位没有对应区间：只给数值，不给区间也不给判定
        if cp:
            # ---- 横版：紧凑单行（名称 · 值 · 判定 || 区间 · 出处）----
            text(im_draw, (lx, ry + 22), name, 18, TX2, anchor="lm")
            text(im_draw, (lx + 200, ry + 22), valstr, 28, col, True, anchor="lm")
            if jt:
                text(im_draw, (lx + 320, ry + 22), jt, 17, jc, True, anchor="lm")
            text(im_draw, (rx, ry + 22), rng, 22, TX, anchor="lm")
            if rkey:
                text(im_draw, (rx + 230, ry + 22), src_short(rkey), 13, TX2, anchor="lm")
            if i < len(rows) - 1:
                im_draw.line([(lx, ry + ROWH - 4), (px1 - 30, ry + ROWH - 4)], fill=LINE, width=1)
        else:
            # ---- 竖版：大卡片 ----
            rrect(im_draw, (lx - 10, ry, lx + colw, ry + ROWH - 26), 12, CARD2)
            text(im_draw, (lx + 4, ry + 12), name, 21, TX2)
            text(im_draw, (lx + 6, ry + 42), valstr, 42, col, True)
            if jt:
                # 判定词（带色）＋ 本场均值（灰），判定词是主角
                text(im_draw, (lx + 6, ry + 92), jt, 18, jc, True)
                if mu is not None:
                    wj = im_draw.textlength(jt, font=font(18, True))
                    text(im_draw, (lx + 16 + wj, ry + 94),
                         "均值 %.*f%s" % (nd, mu, unit), 15, TX2)
            elif mu is not None:
                text(im_draw, (lx + 6, ry + 92), "全场均值 %.*f%s" % (nd, mu, unit), 17, TX2)
            else:
                text(im_draw, (lx + 6, ry + 92),
                     "该相位不判定" if name in PHASE_REF else "—", 17, TX2)
            rrect(im_draw, (rx - 10, ry, rx + colw, ry + ROWH - 26), 12, CARD2)
            text(im_draw, (rx + 4, ry + 12), "职业 / 精英区间" + star, 21, TX2)
            text(im_draw, (rx + 6, ry + 42), rng, 32, TX)
            if rkey:
                text(im_draw, (rx + 6, ry + 92), src_short(rkey), 15, TX2)
    if not cp:
        text(im_draw, (px0 + 30, py1 - 26),
             "参考区间源自公开文献，逐项出处见上；区间按当前相位取用，不等于个体最优值；"
             "* 该项区间为教学口径+本场分布，非纯文献硬边界",
             13, TX2)


def panel_h(im_draw, rank, score, t_rel, t_dip, m, agg, cur, ph, nrows):
    """横版数据面板（画面右侧 520px，原版布局）"""
    px0, py0 = 660, 20
    px1, py1 = CANVAS_W - 20, CANVAS_H - 20
    rrect(im_draw, (px0, py0, px1, py1), 14, CARD, LINE)
    x = px0 + 26
    text(im_draw, (x, py0 + 22), "第 %d %s · 评分 %.0f" % (rank, LABEL, score), 27, TX, True)
    text(im_draw, (x, py0 + 58), f"{R.REPORT_DATE} · 60fps 源 · {SPEED_TXT[0]} · 右手投篮", 17, TX2)
    text(im_draw, (x, py0 + 92), ph, 40, PHASE_COLOR.get(ph, TX), True)
    bx, bw = x, px1 - 26 - x
    pi = PHASES.index(ph) if ph in PHASES else (4 if ph == "落地" else 0)
    seg = bw / len(PHASES)
    for i, name in enumerate(PHASES):
        c = PHASE_COLOR[name] if i == pi else CARD2
        rrect(im_draw, (bx + i * seg + 2, py0 + 146, bx + (i + 1) * seg - 2, py0 + 162), 4, c)
    text(im_draw, (x, py0 + 170), "t = %+.2f s（0 = 出手）" % (cur["t"] - t_rel), 18, TX2)
    y = py0 + 210
    rows = [
        ("投篮手肘角", fmt(cur["elbowR"], 0, "°"), "出手时伸直 134–180°", "elbow"),
        ("膝角", fmt(cur["knee"], 0, "°"), "蓄力 105–121°", "knee"),
        ("小臂仰角", fmt(cur["fore"], 0, "°"), "精英 78.7°（出手时）", "fore"),
        ("手腕高于肩", ("—" if cur["wr_sh"] is None else "%+.2f" % cur["wr_sh"]), "出手点在肩上方", None),
        ("出手点高于头", ("—" if cur["head_dy"] is None else "%+.2f" % cur["head_dy"]), "倍身高 · 出手在头上方", None),
    ]
    for name, val, ref, jkey in rows:
        text(im_draw, (x, y), name, 21, TX2)
        text(im_draw, (x + 175, y - 8), val, 30, TX, True)
        col = TX2
        if jkey and cur.get("judge") and jkey in cur["judge"]:
            txt_, col_ = cur["judge"][jkey]
            text(im_draw, (x + 330, y - 2), txt_, 18, col_)
        text(im_draw, (x + 330, y + 26), ref, 15, TX2)
        y += 58
    im_draw.line([(x, y + 4), (px1 - 26, y + 4)], fill=LINE, width=2)
    y += 18
    text(im_draw, (x, y), "本球汇总（对照全场均值）", 19, TX, True)
    y += 34
    items = [
        ("出手点高度", "%s ×身高" % fmt(m.get("出手点离地高度"), 2), agg["出手点离地高度"]["mean"]),
        ("垂直弹跳", "%s ×身高" % fmt(m.get("垂直弹跳"), 2), agg["垂直弹跳"]["mean"]),
        ("出手节奏", "%s s" % fmt(m.get("屈膝到出手"), 2), agg["屈膝到出手"]["mean"]),
        ("出手肘角(投篮手)", "%s°" % fmt(m.get("出手时肘角_右"), 0), agg["出手时肘角_右"]["mean"]),
    ]
    for i, (k, v, mu) in enumerate(items):
        cx = x + (i % 2) * 270
        cy = y + (i // 2) * 66
        text(im_draw, (cx, cy), k, 17, TX2)
        text(im_draw, (cx, cy + 20), v, 23, TX, True)
        if mu is not None:
            text(im_draw, (cx + 120, cy + 24), "均值 %.2f" % mu if abs(mu) < 5 else "均值 %.0f" % mu, 14, TX2)
    text(im_draw, (x, py1 - 30), "参考区间源自公开文献（Cabarkapa 2022 / Okazaki & Rodacki JSSM 等），详见 HTML 报告", 14, TX2)


PANEL = panel_v    # 全画幅后横竖版都用下方面板（横版画布加高到 1160）


def draw_annot(d, base_img, annots, ox, oy, iw, ih):
    """定格帧：圈出问题关节 + 画面上方的说明条。

    说明条放**顶部**（人物在画面中央，底部压腿脚、顶部一般是天空/背景），圈只圈关节本身不做大。
    """
    k = iw / float(base_img.width)
    fs = 24 if LAYOUT == "v" else 24
    for n, annot in enumerate(annots):
        col = ANNOT_COLOR.get(annot.get("level"), (255, 196, 0))
        kp = annot.get("kp")
        if kp:
            px, py = ox + kp[0] * k, oy + kp[1] * k
            R0 = CIRCLE_R + n * 5                 # 圈只圈住关节本身（用户 2026-10-12：原来 30 偏大）
            d.ellipse([px - R0, py - R0, px + R0, py + R0], outline=col + (255,), width=4)
            d.ellipse([px - R0 - 5, py - R0 - 5, px + R0 + 5, py + R0 + 5], outline=col + (105,), width=2)
        txt = annot["text"]
        tw = d.textlength(txt, font=font(fs, True))
        # 右对齐放在画面【右侧】：人物在画面左侧（约占 x 137–407），右侧整块是背景，不挡人
        bx1 = ox + iw - 14
        bx0 = max(ox + 14, bx1 - (tw + 36))
        by0 = oy + 158 + n * (fs + 24)
        by1 = by0 + fs + 20
        rrect(d, (bx0, by0, bx1, by1), 10, (10, 14, 22, 236), col + (255,), 3)
        text(d, (bx1 - 18, (by0 + by1) / 2), txt, fs, (255, 255, 255), True, anchor="rm")
    # 「关键帧」小标挪到左下角，避开顶部的说明条
    tag = "关键帧"
    tfs = 22 if LAYOUT == "v" else 18
    col = ANNOT_COLOR.get(annots[0].get("level"), (255, 196, 0))
    ttw = d.textlength(tag, font=font(tfs, True))
    tx0 = ox + iw - 14 - (ttw + 26)            # 小标挪到右下角，避开左侧的说明条
    ty0 = oy + ih - (tfs + 18) - 14
    rrect(d, (tx0, ty0, tx0 + ttw + 26, ty0 + tfs + 18), 8, (10, 14, 22, 220), col + (255,), 2)
    text(d, (tx0 + (ttw + 26) / 2, ty0 + (tfs + 18) / 2), tag, tfs, col, True, anchor="mm")


# ---- 标题（压在画面顶部，与抖音封面同一套文案）----
# 2026-10-13 品牌口径：账号/作品署名统一「投篮精进 · 日拱一卒」（原「我的投篮 / AI逐帧拆解」）。
# 注意 data/tags.json 里每个 tag 的 video_title 优先级更高，改这里不会影响已登记过标题的 tag。
TITLE_BIG = "投篮精进"
TITLE_SUB = "日拱一卒"
TITLE_CONCL = ""      # 空 = 还没定；由 _apply_title() 按下面三级优先填上

# 标题里那句结论**必须来自这一次分析**（用户 2026-10-06 定的口径），绝不能写死。
# 三级优先：环境变量 SHOTVID_TITLE（用 | 分隔三行） > data/tags.json 的 video_title > 结论推导
# 2026-10-12 改：原来第三级是硬编码的 "蹲得过深 · 弧线偏平"（10月6日 的结论），
# 新 tag 只要没手工登记就会印错别人那天的结论 —— 现在改为由 R.derive_conclusions() 现算，
# 取「对外指标里偏离参考区间最大的 1~2 项」自动拼。
def _apply_title(agg=None):
    """按 环境变量 > 标签表 > 数据推导 三级优先，定下三行标题。"""
    global TITLE_BIG, TITLE_SUB, TITLE_CONCL
    try:
        info = json.load(open(os.path.join(R.DATA, "tags.json"), encoding="utf-8")).get(TAG, {})
        vt = info.get("video_title")
        if isinstance(vt, (list, tuple)) and len(vt) >= 2:
            TITLE_BIG, TITLE_SUB = vt[0], vt[1]
            TITLE_CONCL = vt[2] if len(vt) > 2 else ""
    except Exception:
        pass
    env = os.environ.get("SHOTVID_TITLE")
    if env:
        parts = env.split("|")
        TITLE_BIG = parts[0]
        TITLE_SUB = parts[1] if len(parts) > 1 else TITLE_SUB
        TITLE_CONCL = parts[2] if len(parts) > 2 else ""
        return
    if TITLE_CONCL:
        return                       # 标签表登记过标题，尊重手工口径
    if agg:
        try:
            TITLE_CONCL = R.derive_conclusions(agg, tag=TAG)["title"]["line3"]
        except Exception:
            pass


def set_titles(agg):
    """外部调用（如 preview_frame）：拿到 agg 之后再定一次标题。返回三行。"""
    _apply_title(agg)
    return TITLE_BIG, TITLE_SUB, TITLE_CONCL


_apply_title(None)


def _apply_layout():
    """按 DISP_AR 重算画面高度与面板位置。

    优先级：环境变量 SHOTVID_AR > data/tags.json 的 disp_ar > DEFAULT_DISP_AR。
    只对竖版生效 —— 横版画布只有 1160 高，画面一高面板就没地方放了。
    """
    global DISP_AR, FRAME_H, PANEL_Y
    ar = DEFAULT_DISP_AR
    if LAYOUT == "v":
        try:
            info = json.load(open(os.path.join(R.DATA, "tags.json"), encoding="utf-8")).get(TAG, {})
            if info.get("disp_ar"):
                ar = float(info["disp_ar"])
        except Exception:
            pass
        env = os.environ.get("SHOTVID_AR")
        if env:
            ar = float(env)
    DISP_AR = ar
    FRAME_H = int(round(FRAME_W / ar))
    PANEL_Y = FRAME_Y + FRAME_H + 26


def _crop_box(sw, sh):
    """按 DISP_AR 算**中心裁切框**（源像素）。比例不比源更宽时返回整幅。

    只横向裁、不裁上下 —— 篮球动作的垂直信息（出手点/落地）比左右更难补。
    """
    if DISP_AR >= sw / float(sh) - 1e-6:
        return (0.0, 0.0, float(sw), float(sh))
    cw = min(float(sw), sh * DISP_AR)
    x0 = (sw - cw) / 2.0
    return (x0, 0.0, x0 + cw, float(sh))


def init_frame(meta):
    """定下这一球的裁切框（按源视频真实尺寸）。返回裁切框。"""
    if _CROP[0] is None:
        try:
            sz = tuple(probe(R.media_path(meta["video"]))["size"])
        except Exception:
            sz = (1920, 1080)
        SRC_SIZE[0], SRC_SIZE[1] = sz
        _CROP[0] = _crop_box(*sz)
    return _CROP[0]


def kp_to_frame(meta, p, crop=None):
    """kp（分析用的 crop 帧坐标）→ 画面坐标（FRAME_W × FRAME_H）。

    **坐标换算只留这一个入口**：骨架、定格圈都走它。
    踩过两次：① 改全画幅只改了骨架没改圈 → 圈偏到画面右下角；
    ② 加了左右裁切后，圈和骨架必须用同一个裁切框，否则会错位。
    """
    box = meta["box"]
    x = box[0] + p[0] * (box[2] / float(meta["crop_w"]))
    y = box[1] + p[1] * (box[3] / float(meta["crop_h"]))
    c = crop if crop is not None else (_CROP[0] or (0.0, 0.0, float(SRC_SIZE[0]), float(SRC_SIZE[1])))
    sc = FRAME_W / float(c[2] - c[0])
    return ((x - c[0]) * sc, (y - c[1]) * sc)


_apply_layout()
_GRAD = [None]


def _grad():
    """画面顶部的半透明渐变遮罩（缓存，只生成一次）"""
    if _GRAD[0] is None:
        h = FRAME_H
        a = (255 * 0.82 * (1 - np.arange(h, dtype=np.float32) / h) ** 1.5).astype(np.uint8)
        g = np.zeros((h, FRAME_W, 4), np.uint8)
        g[..., 0], g[..., 1], g[..., 2] = 6, 9, 14
        g[..., 3] = a[:, None]
        _GRAD[0] = Image.fromarray(g, "RGBA")
    return _GRAD[0]


def draw_title(d, rank, score):
    """标题压在画面顶部（渐变垫底保证可读）。竖版从画面上方的留白起，横版贴画面顶边。"""
    x = FRAME_X + 34
    y0 = 24 if LAYOUT == "v" else FRAME_Y + 10
    text(d, (x, y0), TITLE_BIG, 58, TX, True)
    text(d, (x + 3, y0 + 70), TITLE_SUB, 24, TX2)
    text(d, (x + 3, y0 + 104), TITLE_CONCL, 32, YEL, True)
    text(d, (FRAME_X + FRAME_W - 30, y0 + 6), "第 %d %s" % (rank, LABEL), 22, TX, True, anchor="ra")
    text(d, (FRAME_X + FRAME_W - 30, y0 + 38), "评分 %.0f" % score, 18, TX2, anchor="ra")


def compose(base_img, im, rank, score, t_rel, t_dip, m, agg, cur, ph, annot=None):
    """画面 + 面板合成到画布（画面已是全画幅 16:9，直接贴到 FRAME_X/FRAME_Y）"""
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), BG + (255,))
    d = ImageDraw.Draw(canvas)
    ox, oy = FRAME_X, FRAME_Y
    iw, ih = base_img.size
    canvas.paste(base_img.convert("RGBA"), (ox, oy))
    rrect(d, (ox - 2, oy - 2, ox + iw + 2, oy + ih + 2), 8, None, LINE, 2)
    if annot:
        draw_annot(d, base_img, annot, ox, oy, iw, ih)
    canvas.alpha_composite(_grad(), (FRAME_X, FRAME_Y))
    d = ImageDraw.Draw(canvas)
    draw_title(d, rank, score)
    PANEL(d, rank, score, t_rel, t_dip, m, agg, cur, ph, 6)
    return canvas


def write_video(out_path, gen, fps=FPS_OUT):
    """gen: 生成器，逐帧产出 PIL RGB Image"""
    try:
        first = next(gen)
    except StopIteration:
        # 生成器一帧都没产出。以前这里直接抛 StopIteration，看不出原因 —— 实际上
        # 99% 是源视频没打开（路径错 / 解码器被占），导致 frame_to_pil 每帧都返回 None。
        raise RuntimeError(
            "一帧都没渲染出来 —— 先查源视频能不能打开（路径是否解析对、"
            "是否另有粗扫/深扫在占用硬件解码器）") from None
    W, H = first.size
    if os.path.exists(out_path):
        os.remove(out_path)
    url = NSURL.fileURLWithPath_(out_path)
    writer, err = AVF.AVAssetWriter.alloc().initWithURL_fileType_error_(url, AVF.AVFileTypeMPEG4, None)
    settings = {AVF.AVVideoCodecKey: AVF.AVVideoCodecTypeH264,
                AVF.AVVideoWidthKey: W, AVF.AVVideoHeightKey: H}
    inp = AVF.AVAssetWriterInput.alloc().initWithMediaType_outputSettings_(AVF.AVMediaTypeVideo, settings)
    inp.setExpectsMediaDataInRealTime_(False)
    ad = AVF.AVAssetWriterInputPixelBufferAdaptor.alloc().initWithAssetWriterInput_sourcePixelBufferAttributes_(
        inp, {CV.kCVPixelBufferPixelFormatTypeKey: CV.kCVPixelFormatType_32BGRA,
              CV.kCVPixelBufferWidthKey: W, CV.kCVPixelBufferHeightKey: H})
    writer.addInput_(inp)
    writer.startWriting()
    writer.startSessionAtSourceTime_(CM.CMTimeMake(0, 1))

    def pb_from(im):
        res = CV.CVPixelBufferCreate(None, W, H, CV.kCVPixelFormatType_32BGRA, None, None)
        pb = res[1] if isinstance(res, tuple) else res
        CV.CVPixelBufferLockBaseAddress(pb, 0)
        bpr = CV.CVPixelBufferGetBytesPerRow(pb)
        base = CV.CVPixelBufferGetBaseAddress(pb)
        buf = base.as_buffer(bpr * H)
        raw = im.convert("RGBA").tobytes("raw", "BGRA")
        for y in range(H):
            buf[y * bpr:y * bpr + W * 4] = raw[y * W * 4:(y + 1) * W * 4]
        CV.CVPixelBufferUnlockBaseAddress(pb, 0)
        return pb

    i = 0
    im = first
    while True:
        pb = pb_from(im)
        t1 = time.time()
        while not inp.isReadyForMoreMediaData() and time.time() - t1 < 10:
            time.sleep(0.002)
        if not ad.appendPixelBuffer_withPresentationTime_(pb, CM.CMTimeMake(i, fps)):
            print("append fail at", i, writer.status(), writer.error(), flush=True)
            break
        i += 1
        try:
            im = next(gen)
        except StopIteration:
            break
    inp.markAsFinished()
    writer.finishWritingWithCompletionHandler_(lambda: None)
    t0 = time.time()
    while writer.status() == 1 and time.time() - t0 < 60:
        time.sleep(0.05)
    ok = writer.status() == 2
    if not ok:
        print("writer error:", writer.error())
    return ok


def build_shot(rank, score, t_target, ms, agg, files):
    fp = min(files, key=lambda f: abs(float(os.path.basename(f)[5:-5]) - t_target))
    dj = json.load(open(fp))
    seq = R.deflip(dj["seq"])
    t_rel = min(ms.keys(), key=lambda k: abs(k - t_target))
    m = ms[t_rel]
    t_dip = t_rel - (m.get("屈膝到出手") or 0.45)
    feats = [R.frame_features(r) for r in seq]
    n = len(seq)
    # 采样帧率（按序列实际帧间隔算）→ 推导慢放倍数
    _dt = sorted(seq[k + 1]["t"] - seq[k]["t"] for k in range(len(seq) - 1))
    _samp = 1.0 / _dt[len(_dt) // 2] if _dt else 30.0
    _spd = REPEAT * _samp / float(FPS_OUT)
    _half = round(_spd * 2) / 2.0                  # 就近取 0.5 的整数倍
    if abs(_spd - 1.0) < 0.05:
        SPEED_TXT[0] = "原速"
    elif abs(_spd - _half) < 0.06:                 # 1.961 → 2；显示干净值，避免「1.961 倍慢放」
        SPEED_TXT[0] = ("%g" % _half) + " 倍慢放"
    else:
        SPEED_TXT[0] = ("%.2f" % _spd) + " 倍慢放"
    scale = m.get("scale_px") or 1.0
    # 文件名统一「<日期>_第N球_出手X.Xs_评分NN[_慢60].mp4」——
    # 日期前缀是硬约定：视频、封面、配图、报告全部一个口径，一眼看出属于哪一天。
    # 注意 join_shots.py 的 find() 是按这个名字**精确拼接路径**去取片的，改这里必须同步改它。
    out = os.path.join(OUTDIR, "%s_第%d%s_出手%.1fs_评分%.0f%s.mp4" % (R.REPORT_DATE, rank, LABEL, t_rel, score, ("_慢" + DEEP_SUF) if DEEP_SUF else ""))
    print("  采样 %.1f fps / 输出 %d fps / REPEAT=%d → %s" % (_samp, FPS_OUT, REPEAT, SPEED_TXT[0]), flush=True)

    # 问题帧：算出来再把关节坐标挂上，按帧索引索引
    by_idx = {}
    init_frame(dj["meta"])
    for it in find_issues(m, seq, t_rel, t_dip, agg):
        p = (seq[it["idx"]].get("kp") or {}).get(it["joint"])
        if p:
            it["kp"] = kp_to_frame(dj["meta"], p)     # 走同一个换算入口，别再手写
            by_idx.setdefault(it["idx"], []).append(it)
    print("  定格帧:", " | ".join("i=%d %s" % (i, a["text"]) for i, v in sorted(by_idx.items()) for a in v), flush=True)

    def gen():
        last_base = None
        for idx in range(n):
            if seq[idx]["t"] > t_rel + POST_CUT:      # 出手后保留到落地，再往后就是静止，截掉
                break
            f = feats[idx] or {}
            base = frame_to_pil(dj, idx)
            if base is None:
                base = last_base           # 抽帧失败时沿用上一帧，宁可重复也不要缺帧跳顿
                if base is None:
                    continue
            last_base = base
            kp = seq[idx].get("kp") or {}
            cur = {
                "t": seq[idx]["t"],
                "elbowR": (f.get("right") or {}).get("elbow"),
                "elbowL": (f.get("left") or {}).get("elbow"),
                "knee": None,
                "fore": (f.get("right") or {}).get("fore_ang"),
                "wr_sh": (f.get("right") or {}).get("wr_over_sh"),
                "head_dy": None, "judge": {},
            }
            kn = [(f.get(s) or {}).get("knee") for s in ("right", "left")]
            kn = [v for v in kn if v is not None]
            cur["knee"] = sum(kn) / len(kn) if kn else None
            hd, wr = kp.get("head_joint"), kp.get("right_hand_joint")
            if hd and wr:
                cur["head_dy"] = (hd[1] - wr[1]) / scale
            ph = phase_of(seq[idx]["t"], t_dip, t_rel)
            # 判定严格按「当前相位」取对应文献区间（见 PHASE_REF 说明）——
            # 该相位没有对应区间时就不判定，宁可不给，也不用别的相位的区间凑一个假结论。
            for jkey, ckey, mname in (("elbow", "elbowR", "投篮手肘角"),
                                      ("fore", "fore", "小臂仰角"),
                                      ("knee", "knee", "膝角")):
                rk = phase_ref(mname, ph)
                if rk and cur.get(ckey) is not None:
                    cur["judge"][jkey] = jq(rk, cur[ckey])
            canvas = compose(base, None, rank, score, t_rel, t_dip, m, agg, cur, ph)
            for _ in range(REPEAT):
                yield canvas.convert("RGB")
            ann = by_idx.get(idx)
            if ann:
                c2 = compose(base, None, rank, score, t_rel, t_dip, m, agg, cur, ph, annot=ann)
                for _ in range(FREEZE_FRAMES):
                    yield c2.convert("RGB")

    # 定格帧：单独生成一次
    def gen2():
        last = None
        for im in gen():
            last = im
            yield im
        for _ in range(HOLD_FRAMES):
            yield last

    ok = write_video(out, gen2())
    meta = probe(out) if ok else None
    print("OK" if ok else "FAIL", out, meta, flush=True)
    return out


def main():
    d, ms, agg, top3, files = load_all()
    _apply_title(agg)          # 拿到 agg 才能定标题（数据推导那一路）；标签表/环境变量仍优先
    print("标题：%s / %s / %s" % (TITLE_BIG, TITLE_SUB, TITLE_CONCL), flush=True)
    only = None
    if ONLY:
        only = {int(x) for x in ONLY.replace("，", ",").split(",") if x.strip().isdigit()}
        print("只出清单里的第 %s 个（共 %d 个）" % (sorted(only), len(top3)), flush=True)
    outs = []
    for rank, it in enumerate(top3, 1):
        if only and rank not in only:
            print("  跳过 第%d个 t=%.2f" % (rank, it["t"]), flush=True)
            continue
        outs.append(build_shot(rank, it["score"], it["t"], ms, agg, files))
    for p in outs:
        if p and os.path.exists(p):
            print("%.1f MB  %s" % (os.path.getsize(p) / 1048576, p))


if __name__ == "__main__":
    main()
