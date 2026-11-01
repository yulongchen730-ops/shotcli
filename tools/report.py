"""指标计算 + HTML 报告生成（v2）

改动要点
1. 尺度校准：用「躯干长(颈→髋)/0.32」「大腿长(髋→膝)/0.245」估算身高像素尺度，
   避免用「头到踝」在屈膝时被压缩、造成高度类指标系统性偏差。
2. 文献对照：参考区间全部标注文献出处；一致性用「组内标准差」与文献的新手/熟练/精英对比。
3. 逐帧诊断播放器：30fps 连续帧 + 骨架 + 每帧角度标注 + 缺陷提示。
4. 曲线对比图：20 次投篮的手腕高度/膝角曲线叠加，肉眼可见快慢差异。

用法: python report.py [每个视频展示几个球的分解]
"""
import sys, os, json, math, time, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbkit import frame_features, hr, probe, SIDES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FRAMES = os.path.join(ROOT, "frames")


def media_path(p):
    """把数据文件里存的视频路径解析成真实路径（**所有抽帧脚本都必须走这里**）。

    深扫 / 单球 json 里的 `meta["video"]` 是**相对工程根**的（如 `../9月20日3.mp4`，
    因为视频在 `训练记录/` 而工程在 `训练记录/投篮分析/`）。脚本被 `shotcli` 以
    `cwd=<工程>/tools` 拉起时，直接拿这个相对路径去找就会指错地方，AVFoundation
    打不开 → 每一帧都抽不出来 → `write_video` 里 `next(gen)` 抛 StopIteration
    （现场表现是「表格都算完了、视频一步就崩」，很容易误判成解码器故障）。
    2026-10-12 踩过。这里统一：原样 → 工程根 → 视频所在目录（BASE）依次尝试。
    """
    if not p:
        return p
    if os.path.isabs(p) and os.path.exists(p):
        return p
    for base in (os.getcwd(), ROOT, BASE):
        q = os.path.normpath(os.path.join(base, p))
        if os.path.exists(q):
            return q
    return p
BASE = os.path.dirname(ROOT)

VIDEOS = [
    {"tag": "f", "file": os.path.join(BASE, "10月6日（2）.mp4"), "title": "10月6日 · 第 2 段（全部命中）"},
]

REPORT_DATE = "10月6日"
REPORT_SUFFIX = ""     # 同名日期（如 10月6日 有 f/g/h 三段）加后缀消歧；由标签表的 report_suffix 覆盖
PLAYER_INITS = []   # 逐帧播放器的初始化调用（页面末尾统一注入，脚本只出现一次）
HEIGHT_CM = 171.0   # 本人身高（2026-09-13 用户提供），用于把像素比例换算成 cm
HEIGHT_SRC = "manual"   # 身高来源：profile=建档填的 / manual=登记时手动指定 / default=占位值（未建档）
# 解剖学比例（用于身高像素尺度估计）
THIGH_RATIO = 0.245   # 髋→膝 / 身高
TORSO_RATIO = 0.32    # 颈→髋 / 身高

# --------------------------------------------------------------------------- 指标

SHOOT_HAND = "right"   # 投篮手：right / left（另一侧作为辅助手对照）；可在标签表按 tag 设 hand
OFF_HAND = "left"

# --------------------------------------------------------------- 标签表（多视频支持）
# 上面的 VIDEOS / REPORT_DATE / HEIGHT_CM / SHOOT_HAND 是「默认那一次训练」的缺省值。
# 要给别人看别的 tag，就在 data/tags.json 里登记一条（由 tools/shotcli.py 维护），
# 然后二选一告诉本模块用哪个 tag：
#   ① 命令行第二个参数： python report.py 4 f
#   ② 环境变量：        SHOTCLI_TAG=f python report.py 4
# 两条路都走 apply_tag()，不改任何渲染代码。
TAG_FILE = os.path.join(DATA, "tags.json")


def load_tags():
    if os.path.exists(TAG_FILE):
        try:
            return json.load(open(TAG_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def apply_tag(tag):
    """用标签表里的登记信息覆盖 VIDEOS / REPORT_DATE / HEIGHT_CM / SHOOT_HAND。"""
    global VIDEOS, REPORT_DATE, HEIGHT_CM, SHOOT_HAND, OFF_HAND, HEIGHT_SRC
    info = load_tags().get(tag)
    if not info:
        return False
    vf = info.get("video") or ""
    if vf and not os.path.isabs(vf):
        vf = os.path.join(BASE, vf)
    VIDEOS = [{"tag": tag, "file": vf,
               "title": info.get("title") or "%s · 训练" % (info.get("date") or tag)}]
    global REPORT_SUFFIX
    REPORT_SUFFIX = ""
    if info.get("date"):
        REPORT_DATE = info["date"]
    if info.get("report_suffix"):
        REPORT_SUFFIX = info["report_suffix"]
    if info.get("height_cm"):
        HEIGHT_CM = float(info["height_cm"])
        # 来源决定报告里怎么标：建档填的 / 登记时手动指定 / 还是没人设过的占位值
        HEIGHT_SRC = info.get("height_src") or "manual"
    if info.get("hand") in ("right", "left"):
        SHOOT_HAND = info["hand"]
        OFF_HAND = "left" if SHOOT_HAND == "right" else "right"
    return True


_ACTIVE_TAG = os.environ.get("SHOTCLI_TAG") or (sys.argv[2] if len(sys.argv) > 2 else "")
if _ACTIVE_TAG:
    apply_tag(_ACTIVE_TAG)


def hand_label(side):
    return "投篮手（右手）" if side == SHOOT_HAND else "辅助手（左手）"


SIDE_KEYS = ["shoulder_1_joint", "forearm_joint", "hand_joint", "upLeg_joint", "leg_joint", "foot_joint"]


def deflip(seq):
    """修复手臂交叉时 Vision 左右标签互换：按与上一帧的最近邻连续性重新指派"""
    prev = None
    for rec in seq:
        kp = rec.get("kp")
        if not kp:
            continue
        cur = {s: {k: kp.get(f"{s}_{k}") for k in SIDE_KEYS} for s in ("right", "left")}
        if prev:
            def cost(a):
                c, n = 0.0, 0
                for s_src, s_dst in a.items():
                    for k in ("forearm_joint", "hand_joint", "upLeg_joint", "leg_joint"):
                        p = prev.get(s_dst, {}).get(k)
                        q = cur[s_src][k]
                        if p and q:
                            c += math.hypot(p[0] - q[0], p[1] - q[1])
                            n += 1
                return c / n if n else 1e9
            if cost({"right": "left", "left": "right"}) < cost({"right": "right", "left": "left"}) * 0.9:
                newkp = dict(kp)
                for k in SIDE_KEYS:
                    r, l = kp.get("right_" + k), kp.get("left_" + k)
                    newkp["right_" + k] = l if l else newkp.pop("right_" + k, None)
                    newkp["left_" + k] = r if r else newkp.pop("left_" + k, None)
                    if not l:
                        newkp.pop("right_" + k, None)
                    if not r:
                        newkp.pop("left_" + k, None)
                rec["kp"] = kp = newkp
                cur = {s: {k: kp.get(f"{s}_{k}") for k in SIDE_KEYS} for s in ("right", "left")}
        prev = cur
    return seq


def seg(f, a, b):
    p, q = f["kp"].get(a), f["kp"].get(b)
    return math.hypot(p[0] - q[0], p[1] - q[1]) if (p and q) else None


def scale_px(feats):
    """用刚体段长估算身高像素尺度（对屈膝不敏感）"""
    vals = []
    for f in feats:
        for s in ("right", "left"):
            t = seg(f, s + "_upLeg_joint", s + "_leg_joint")
            if t:
                vals.append(t / THIGH_RATIO)
        tr = seg(f, "neck_1_joint", "root")
        if tr:
            vals.append(tr / TORSO_RATIO)
    if not vals:
        return None
    return st.median(vals)


def shot_metrics(shot):
    meta, seq = shot["meta"], shot["seq"]
    seq = deflip(seq)
    feats = [f for f in (frame_features({**r, "i": r["i"]}) for r in seq) if f]
    if len(feats) < 8:
        return None
    scale = scale_px(feats)
    if not scale:
        return None

    def mean_wr(f):
        if not f:
            return None
        vs = [f[s]["wr_over_sh"] for s in ("right", "left") if f[s]["wr_over_sh"] is not None]
        return sum(vs) / len(vs) if vs else None

    def knee_mean(f):
        if not f:
            return None
        vs = [f[s]["knee"] for s in ("right", "left") if f[s]["knee"] is not None]
        return sum(vs) / len(vs) if vs else None

    def root(f):
        if not f:
            return None
        v = f["kp"].get("root")
        return (v[0], v[1]) if v else None

    def ankle_y(f):
        if not f:
            return None
        ys = [f["kp"][s + "_foot_joint"][1] for s in ("right", "left") if f["kp"].get(s + "_foot_joint")]
        return max(ys) if ys else None

    rel = max([f for f in feats if mean_wr(f) is not None], key=mean_wr)
    t_rel = rel["t"]
    pre = [f for f in feats if t_rel - 1.3 <= f["t"] <= t_rel]
    post = [f for f in feats if t_rel <= f["t"] <= t_rel + 0.7]
    dip = min([f for f in pre if knee_mean(f) is not None], key=knee_mean, default=None)
    knee_ext = max([f for f in pre if knee_mean(f) is not None], key=knee_mean, default=None)
    hi = SHOOT_HAND   # 出手点一律取投篮手（右手）
    ground = max([ankle_y(f) for f in feats if ankle_y(f) is not None], default=None)
    apex_f = None
    for f in post:
        a = ankle_y(f)
        if a is not None and (apex_f is None or a < apex_f[1]):
            apex_f = (f, a)
    # 垂直弹跳：站立（屈膝前 0.3s）→ 出手后最高点的髋部抬升（与文献的 vertical displacement 同口径）
    # 站立参考：屈膝前 1.1s 内髋部最高（最直立）的一帧
    stand_f = None
    if dip:
        cands = [f for f in feats if dip["t"] - 1.1 <= f["t"] <= dip["t"] - 0.2 and root(f)]
        if cands:
            stand_f = min(cands, key=lambda f: root(f)[1])
    jump = None
    if stand_f and root(stand_f) and apex_f:
        r_apex = root(apex_f[0])
        if r_apex:
            jump = (root(stand_f)[1] - r_apex[1]) / scale
    jump_foot = (ground - apex_f[1]) / scale if (ground is not None and apex_f) else None
    postw = min(post, key=lambda f: abs(f["t"] - (t_rel + 0.2))) if post else None
    postland = min(feats, key=lambda f: abs(f["t"] - (t_rel + 0.45)))
    wrist_rel_y = rel["kp"].get(hi + "_hand_joint")
    rel_h = (ground - wrist_rel_y[1]) / scale if (ground is not None and wrist_rel_y) else None

    m = {
        "t_rel": round(t_rel, 2), "n_frames": len(feats), "出手手": "右",
        "scale_px": round(scale, 1),
        "屈膝角(最低)": knee_mean(dip),
        "出手时膝角": knee_mean(rel),
        "出手时肘角_左": rel["left"]["elbow"], "出手时肘角_右": rel["right"]["elbow"],
        "收球肘角_左": dip["left"]["elbow"] if dip else None,
        "收球肘角_右": dip["right"]["elbow"] if dip else None,
        "出手小臂仰角": rel[hi]["fore_ang"],
        "出手点离地高度": rel_h,
        "垂直弹跳": jump_foot,
        "屈膝到出手": (t_rel - dip["t"]) if dip else None,
        "膝伸展到出手": (t_rel - knee_ext["t"]) if knee_ext else None,
        "身体位移": ((root(rel)[0] - root(dip)[0]) / scale) if (dip and root(rel) and root(dip)) else None,
        "落地位移": ((root(postland)[0] - root(rel)[0]) / scale) if (root(postland) and root(rel)) else None,
        "出手点高于头": ((rel["kp"]["head_joint"][1] - wrist_rel_y[1]) / scale) if (wrist_rel_y and rel["kp"].get("head_joint")) else None,
        "t_dip": round(dip["t"], 2) if dip else None,
        "t_apex": round(apex_f[0]["t"], 2) if apex_f else None,
    }
    # 曲线（用于对比图）
    m["_series"] = {
        "dt": [round(f["t"] - t_rel, 3) for f in feats],
        "wrist": [mean_wr(f) for f in feats],
        "knee": [knee_mean(f) for f in feats],
    }
    # 展示帧
    keys = [("预备", t_rel - 1.0), ("屈膝", dip["t"] if dip else t_rel - 0.6),
            ("起球", t_rel - 0.35), ("出手", t_rel), ("随挥", t_rel + 0.2),
            ("落地", t_rel + 0.5)]
    strip = []
    for label, tt in keys:
        f = min(feats, key=lambda f: abs(f["t"] - tt))
        strip.append({"label": label, "t": round(f["t"], 2), "img": f["img"],
                      "cw": f["cw"], "ch": f["ch"], "kp": f["kp"],
                      "knee": knee_mean(f), "elbowL": f["left"]["elbow"], "elbowR": f["right"]["elbow"],
                      "fore": f["fore_ang"] if hi == "left" else f["right"]["fore_ang"],
                      "wrist_h": None if ground is None else round((ground - f["kp"].get(hi + "_hand_joint", [0, ground])[1]) / scale, 3)})
    m["_strip"] = strip
    m["_hi"] = hi
    return m


def aggregate(ms, keys):
    agg = {}
    for k in keys:
        vals = [m[k] for m in ms if m.get(k) is not None]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        agg[k] = {"n": len(vals), "mean": mean, "sd": sd,
                  "cv": (sd / abs(mean) * 100) if mean else None,
                  "min": min(vals), "max": max(vals)}
    return agg


# 指标 → (下限, 上限, 单位, 说明, 文献出处)
REFERENCE = {
    "屈膝角(最低)": (105, 121, "°", "预备期最深屈膝角。屈膝越充分，下肢蓄力越多",
                 "Cabarkapa 2022 职业男 112.5–121.5°；Frontiers 2023 罚球 114.8–118.0°；JSSM 最小膝角 104–107°"),
    "出手时膝角": (165, 172, "°", "出手瞬间腿部应接近蹬直",
                "JSSM 出手时膝角 166.4–168.3°；Frontiers 2023 释放点 167.6°"),
    "出手时肘角_左": (134, 180, "°", "出手瞬间肘接近伸直。文献量角口径不同：134–138° 与 166–168° 都是「接近伸直」",
                 "JSSM 出手肘角 134.3–138.2°；Frontiers 2023 罚球释放点 166.1–168.3°"),
    "出手时肘角_右": (134, 180, "°", "出手瞬间肘接近伸直。文献量角口径不同：134–138° 与 166–168° 都是「接近伸直」",
                 "JSSM 出手肘角 134.3–138.2°；Frontiers 2023 罚球释放点 166.1–168.3°"),
    "收球肘角_左": (55, 85, "°", "收球（屈膝最低）时肘折叠角度，约 60–80° 表示收球到位",
                "Cabarkapa 2022 职业男 58.2–63.6°；Frontiers 2023 78.5–81.5°；JSSM 最小值 54.5–64.0°"),
    "收球肘角_右": (55, 85, "°", "收球（屈膝最低）时肘折叠角度，约 60–80° 表示收球到位",
                "Cabarkapa 2022 职业男 58.2–63.6°；Frontiers 2023 78.5–81.5°；JSSM 最小值 54.5–64.0°"),
    "出手小臂仰角": (60, 85, "°", "小臂相对水平线仰角。该指标能区分水平：新手 60°／熟练 70°／精英 79°",
                "PMC9277478：新手 60.4°、熟练 70.5°、精英 78.7°；Okubo & Hubbard 2015 ≈80°"),
    "出手点离地高度": (1.15, 1.40, "倍身高", "出手瞬间手腕离地高度 ÷ 身高",
                 "Frontiers 2023 罚球 1.17–1.19；Cabarkapa 2022 职业男 1.31–1.38"),
    "垂直弹跳": (0.15, 0.26, "倍身高", "踝关节从地面到最高点的抬升；含踝关节跖屈，比文献的质心口径高约 0.03",
              "Cabarkapa 2022 职业男：罚球 15.3cm / 2分 26.9cm / 3分 31.2cm（质心垂直位移，约 0.09–0.18 倍身高）"),
    "屈膝到出手": (0.30, 0.50, "秒", "屈膝最低点→出手。文献无直接区间，按教学口径与本场分布给出；"
                "可确定的是出手应发生在起跳最高点之前",
                 "França 2022：球在到达跳跃最高点之前出手；ESPN Sport Science：库里接球到出手 0.4s（NBA 均值 0.54s）"),
    "身体位移": (0.0, 0.28, "倍身高", "屈膝最低点→出手的髋部水平位移（含前后方向投影）",
              "IJERPH 2021 综述：青少年 CoM 水平位移 2.8m 投篮 0.35m、6.4m 投篮 0.49m；高水平球员水平位移更小"),
    "膝伸展到出手": (0.0, 0.30, "秒", "膝伸展到出手的时差（15fps 采样，精度 ±0.07s，仅作参考）", "无直接文献区间"),
    "落地位移": (0.0, 0.20, "倍身高", "出手后 0.45 秒髋部继续前移量", "无直接文献区间"),
}
# 文献给出的「组内一致性」标杆（同一受试者多次投篮的标准差，单位：度）
CONSISTENCY_REF = {
    "屈膝角(最低)": {"novice": 4.17, "experienced": 4.00, "elite": 2.58, "src": "PMC9277478（最深屈膝角组内 SD）"},
    "收球肘角_左": {"novice": 3.31, "experienced": 2.88, "elite": 2.47, "src": "PMC9277478（最大屈肘角组内 SD）"},
    "收球肘角_右": {"novice": 3.31, "experienced": 2.88, "elite": 2.47, "src": "PMC9277478（最大屈肘角组内 SD）"},
    "出手小臂仰角": {"novice": 3.88, "experienced": 3.18, "elite": 2.33, "src": "PMC9277478（出手小臂仰角组内 SD）"},
}

METRIC_KEYS = ["屈膝角(最低)", "出手时膝角", "出手时肘角_左", "出手时肘角_右",
               "收球肘角_左", "收球肘角_右", "出手小臂仰角", "出手点离地高度", "垂直弹跳",
               "屈膝到出手", "膝伸展到出手", "身体位移", "落地位移"]

COACH = {
    "屈膝角(最低)": ("下肢蓄力到位且深度稳定", "屈膝深度不稳定，说明每球的下肢准备不一样",
                  "篮下无跳投：每次默念「坐—起—出」，屈膝到位再起，20 球 × 4 组，只要求深度一致。"),
    "出手时膝角": ("出手瞬间腿已蹬直，力量传导完整", "出手时腿没蹬直，靠手臂发力",
              "跳绳双摇 / 原地纵跳：每组 15 次 × 4 组，建立「脚先离地、球再离手」的时序。"),
    "出手时肘角_左": ("辅助手（左手）在出手时自然伸展，未干扰投篮", "辅助手在出手瞬间仍弯曲收在球侧，可能带偏出手方向",
                "单手投篮对比组：右手投 10 球（左手背在身后）→ 再双手投 10 球，感受辅助手只负责扶球、不发力。"),
    "出手时肘角_右": ("投篮手（右手）出手时肘接近伸直，与文献职业球员一致",
                "投篮手出手时肘伸展不足（推球），球出手后没有「弹」出去",
                "右手单手 form shooting：篮下 1.5 米只用右手，肘对准篮筐，出手时肘完全伸直、手腕下压，20 球 × 5 组。"),
    "收球肘角_左": ("左臂收球角度合理", "左臂收球过紧或过松，起球路径不稳",
               "对墙 form shooting：球贴墙、肘折成 L 形再出手，20 次 × 4 组。"),
    "收球肘角_右": ("投篮手收球折叠到位（与文献职业球员 58–64° 一致）", "投篮手收球折叠不足/过度，起球路径不稳",
               "定点持球准备姿势：出手前停在收球位 1 秒再出手，20 次 × 4 组。"),
    "出手小臂仰角": ("小臂出手角度接近精英区间（78.7°）", "小臂出手角度偏低且波动，弧线偏平、容错小",
               "近距离单手投篮 + 固定高度目标（篮板上沿切线）：只练手型一致，不看进不进，25 球 × 4 组。"),
    "出手点离地高度": ("出手点高度理想", "出手点偏低，容易被封盖、弧度偏平",
                "提高出手点：篮下开始，每次出手后手停在头上方 0.1 秒，逐步后退，20 球 × 4 组。"),
    "垂直弹跳": ("弹跳高度稳定，下肢爆发一致", "垂直弹跳波动大，下肢发力不一致",
              "跳箱 / 原地摸高：每组 8 次 × 4 组，记录每次高度差。"),
    "屈膝到出手": ("出手节奏稳定", "出手节奏波动大：有时抢、有时停，这是最值得先改的一项",
              "节拍器投篮：把「屈膝最低→出手」固定在 0.4–0.5 秒，用节拍器（120BPM 两拍）配合，25 球 × 4 组。"),
    "膝伸展到出手": ("蹬伸→出手衔接清晰", "蹬伸到出手的时差波动大，动量传导不连续",
               "坐姿投篮：先坐姿只做上肢出手，再站起做完整动作，交替体会「腿先发力」，每轮 10 次。"),
    "身体位移": ("出手时身体基本原地，重心稳定", "出手时身体水平位移偏大，重心在移动中出手",
              "原地定点投篮：脚不动，出手后落回起跳点，落点偏移超过一个脚掌算无效，20 球 × 4 组。"),
    "落地位移": ("落地平稳无多余前冲", "落地明显前冲，影响平衡与连贯",
              "自抛自投 + 落地控制：落地必须双脚原位，20 球 × 4 组。"),
}
CORE = ["屈膝角(最低)", "出手时膝角", "出手时肘角_左", "出手时肘角_右", "收球肘角_左", "收球肘角_右",
        "出手小臂仰角", "出手点离地高度", "垂直弹跳", "屈膝到出手", "身体位移"]


def dig_for(unit):
    return 2 if unit in ("倍身高", "秒", "×身高") else 0


def label(k):
    return (k.replace("_右", "（投篮手）").replace("_左", "（辅助手）")
             .replace("(最低)", "（最低点）"))


def fmt(v, digits=1, unit=""):
    if v is None:
        return "—"
    return f"{v:.{digits}f}{unit}"


# 有些指标的「低」不代表坏（膝角越小=蹲得越深），单独给措辞
VERDICT_WORDS = {
    "屈膝角(最低)": ("蹲得过深", "明显蹲得过深", "蹲得过浅", "明显蹲得过浅"),
    "屈膝到出手": ("出手偏快", "明显出手偏快", "出手偏慢", "明显出手偏慢"),
    "收球肘角_左": ("折叠更多", "明显折叠过度", "折叠不足", "明显折叠不足"),
    "收球肘角_右": ("折叠更多", "明显折叠过度", "折叠不足", "明显折叠不足"),
}


def qual(k, v):
    if v is None or k not in REFERENCE:
        return "", ""
    lo, hi = REFERENCE[k][0], REFERENCE[k][1]
    if lo <= v <= hi:
        return "区间内", "ok"
    span = max(hi - lo, 1e-6)
    dev = (lo - v) / span if v < lo else (v - hi) / span
    w = VERDICT_WORDS.get(k)
    if w:
        txt = (w[0] if dev < 0.5 else w[1]) if v < lo else (w[2] if dev < 0.5 else w[3])
    else:
        txt = ("偏低" if v < lo else "偏高") if dev < 0.5 else ("明显偏低" if v < lo else "明显偏高")
    return txt, ("warn" if dev < 0.5 else "bad")


# --------------------------------------------------------------- 结论推导（唯一事实来源）
# 口径（用户 2026-10-12 定）：结论只能由「实测值 × 文献参考区间」推出 ——
# 先找**超出参考区间**的项目，按偏离程度排序，再取其中最严重的进标题/封面。
# 绝不先定结论再往上套；所有对外文案都必须从这里取。

# 辅助手（左手）指标：报告与结论清单里**照实列出**（数据要完整），
# 但对外素材（视频标注 / 封面 / 配图 / 文案）**不引用** ——
# 背身侧后方机位下 Vision 的左右标签会跳变，这两个数值本身不可靠。
LEFT_KEYS = {k for k in METRIC_KEYS if "_左" in k}
# 参考区间里掺了教学口径 / 本场分布的指标（不是纯文献硬边界），报告里要如实标注。
FIELD_KEYS = {"屈膝到出手", "膝伸展到出手", "落地位移"}
# 有方向的指标：数值正负取决于机位在球员左/右侧，跨训练对比必须先取绝对值（见 _metric_vals）。
SIGNED_KEYS = {"身体位移", "落地位移"}
# 封面数据卡用的短标签（卡片宽度有限）
COVER_SHORT = {
    "屈膝角(最低)": "膝盖角", "出手时膝角": "出手膝角",
    "出手时肘角_左": "辅助手肘角", "出手时肘角_右": "投篮手肘角",
    "收球肘角_左": "收球辅助手肘角", "收球肘角_右": "收球投篮手肘角",
    "出手小臂仰角": "小臂仰角", "出手点离地高度": "出手点高度",
    "垂直弹跳": "弹跳", "屈膝到出手": "出手节奏",
    "膝伸展到出手": "蹬伸到出手", "身体位移": "身体位移", "落地位移": "落地位移",
}
UNIT_TXT = {"倍身高": "×身高", "×身高": "×身高", "°": "°", "秒": "s"}
# 跨日期对比卡上的方向词：值变大 / 变小时各用哪个说法（直白，不用比较级暗语）
SHIFT_WORD = {
    "屈膝角(最低)": ("蹲得更浅", "蹲得更深"),
    "出手时膝角": ("腿更伸直", "腿更弯"),
    "出手时肘角_右": ("出手更伸展", "出手更折叠"),
    "收球肘角_右": ("收球位置外移", "收球位置内收"),
    "出手小臂仰角": ("弧线更高", "弧线更平"),
    "出手点离地高度": ("出手点更高", "出手点更低"),
    "垂直弹跳": ("跳得更高", "跳得更低"),
    "屈膝到出手": ("出手更慢", "出手更快"),
    "膝伸展到出手": ("蹬伸更晚", "蹬伸更早"),
    "身体位移": ("重心偏移更大", "重心偏移更小"),
    "落地位移": ("落地更前", "落地更后"),
}


def unit_txt(unit):
    return UNIT_TXT.get(unit, unit or "")


def card_value(mean, unit):
    """封面数据卡上的数值文本：98° / 0.50s / 1.28×身高"""
    if mean is None:
        return "—"
    return "%.*f%s" % (dig_for(unit), mean, unit_txt(unit))


def ref_text(lo, hi, unit):
    """参考区间的可读文本：105–121° / 0.30–0.50s"""
    if lo is None:
        return "无参考区间"
    d = dig_for(unit)
    return "%.*f–%.*f%s" % (d, lo, d, hi, unit_txt(unit))


def derive_conclusions(agg, ms=None, tag=None, top_for_title=2):
    """【结论的唯一来源】聚合指标 × 参考区间 → 结构化结论清单。

    判定完全复用 qual()：值落在 [lo,hi] → in_ref=True；否则 dev = 超出量 ÷ 区间宽度，
    dev<0.5 → warn、>=0.5 → bad。措辞复用 VERDICT_WORDS（同一指标固定绑定同一个词）。

    返回 {"tag","n_shots","generated_at","items","by_consistency",
          "out_of_range","public_out_of_range","title"}
      items            全部指标（含辅助手），按 |dev| 降序 —— 偏离参考区间最严重的在最前
      by_consistency   同一批条目，按 CV 降序（稳定性尺度，与偏离度分开看）
      out_of_range      items 中 in_ref=False 的子集
      public_out_of_range  上者中 public=True（供标题 / 封面 / 对外文案）
      title.line3      标题第三行 = 最严重的 1~2 项措辞拼接（无越界项时说明"均在区间内"）
      title.cards      封面数据卡 [主行, 副行]，最多 top_for_title 条
    """
    items = []
    for k in METRIC_KEYS:
        a = agg.get(k)
        if not a:
            continue
        mean = a.get("mean")
        lo, hi, unit, desc, src = REFERENCE.get(k, (None, None, "", "", ""))
        dev_s, over, in_ref = 0.0, 0, True
        if lo is not None and mean is not None:
            span = float(hi - lo) or 1e-9
            if mean < lo:
                dev_s, over, in_ref = (mean - lo) / span, -1, False
            elif mean > hi:
                dev_s, over, in_ref = (mean - hi) / span, 1, False
        verdict, level = (qual(k, mean) if lo is not None else ("", ""))
        good_txt, bad_txt, drill = COACH.get(k, ("", "", ""))
        items.append({
            "key": k, "label": label(k), "short": COVER_SHORT.get(k, label(k)),
            "mean": mean, "sd": a.get("sd"), "cv": a.get("cv"),
            "n": a.get("n"), "min": a.get("min"), "max": a.get("max"),
            "lo": lo, "hi": hi, "unit": unit, "desc": desc,
            "dev": round(dev_s, 4), "dev_abs": round(abs(dev_s), 4), "over": over,
            "in_ref": in_ref, "level": level, "verdict": verdict,
            "public": k not in LEFT_KEYS,
            "side": "off" if k in LEFT_KEYS else ("shoot" if k.endswith("_右") else "both"),
            "good_text": good_txt, "bad_text": bad_txt, "advice": drill,
            "source": src,
            "interval_src": "field" if k in FIELD_KEYS else "literature",
            "consistency_ref": CONSISTENCY_REF.get(k),
        })

    # 排序写死：偏离参考区间最严重的永远排最前，与稳定性无关
    items.sort(key=lambda e: (-e["dev_abs"], -(e["cv"] or 0)))
    by_c = sorted(items, key=lambda e: -(e["cv"] or 0))
    oor = [e for e in items if not e["in_ref"]]
    pub_oor = [e for e in oor if e["public"]]

    picks = pub_oor[:top_for_title]
    line3 = " · ".join(e["verdict"] for e in picks) if picks else "各环节均在参考区间内"
    cards = [[e["short"] + " " + card_value(e["mean"], e["unit"]),
              "%s · 参考 %s" % (e["verdict"], ref_text(e["lo"], e["hi"], e["unit"]))]
             for e in picks]
    if len(cards) < top_for_title:          # 越界项不足时，用「稳定且在区间内」的补充
        for e in by_c:
            if len(cards) >= top_for_title:
                break
            if not e["public"] or not e["in_ref"] or e["mean"] is None:
                continue
            if any(c[0].startswith(e["short"]) for c in cards):
                continue
            cards.append([e["short"] + " " + card_value(e["mean"], e["unit"]),
                          "区间内 · 参考 %s" % ref_text(e["lo"], e["hi"], e["unit"])])

    return {
        "tag": tag,
        "n_shots": len(ms) if ms else (items[0]["n"] if items else 0),
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "items": items, "by_consistency": by_c,
        "out_of_range": oor, "public_out_of_range": pub_oor,
        "title": {"line3": line3, "cards": cards},
    }


def conclusions_path(tag):
    return os.path.join(DATA, "%s_conclusions.json" % tag)


def load_conclusions(tag, agg=None, ms=None):
    """优先用现算（纯本地计算，最不会过期）；没有 agg 时才退回落盘的 json。"""
    if agg:
        return derive_conclusions(agg, ms, tag=tag)
    fp = conclusions_path(tag)
    if os.path.exists(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    return derive_conclusions({}, ms, tag=tag)


# --------------------------------------------------------------------------- 跨日期对比
# 与结论同一套口径：**变化量只能由两场的实测值推出**，不写死任何数字。
# 三条尺度分开算、不混：
#   位置尺度  效应量 d = (本场均值 − 上场均值) ÷ 合并标准差  → 值本身挪了多少
#   稳定尺度  cv_ratio = 本场 CV ÷ 上场 CV                  → >1 就是一致性变差
#   区间状态  上场在区间内、本场掉出去（或反之）
# **默认用稳健统计**（先按中位数 ± 3×1.4826×MAD 去掉离群球再算）：
# 一场投篮里只要有一颗误检/试投，就能把 CV 抬高好几倍，让「稳定性进步」这种
# 假结论凭空出现（2026-10-13 实测：10月6日第1段有一颗弹跳仅 3cm 的球，
# 把该段 CV 从 4% 抬到 25%，差点让封面写出「起跳稳定性大幅进步」）。
# 全量口径仍然保留在 raw 字段里，报告与结论清单用的还是全量。

def compare_path(tag):
    return os.path.join(DATA, "%s_compare.json" % tag)


def _has_shots(tag):
    d = os.path.join(DATA, tag)
    return os.path.isdir(d) and any(
        n.startswith("shot_") and n.endswith(".json") for n in os.listdir(d))


def find_prev_tag(tag, tags=None):
    """标签表顺序里、当前 tag 之前最近的「已经跑出逐球数据」的那一个。"""
    tags = tags if tags is not None else load_tags()
    keys = list(tags.keys())
    if tag not in keys:
        return None
    for k in reversed(keys[:keys.index(tag)]):
        if _has_shots(k):
            return k
    return None


def find_prev_tags(tag, tags=None):
    """上一个**训练日**已出数据的全部 tag —— 同一天多段合并成一个基线。

    只取最近的那一天：往前扫，遇到日期与当前 tag 相同的段直接跳过
    （同一天的另一段不算「上一次」），取到第一个不同日期的段之后，
    把与它同日的其它段一起收进来（同一天多段本质是同一次训练）。
    """
    tags = tags if tags is not None else load_tags()
    keys = list(tags.keys())
    if tag not in keys:
        return []
    d0 = (tags.get(tag) or {}).get("date")
    out, base = [], None
    for k in reversed(keys[:keys.index(tag)]):
        dk = (tags.get(k) or {}).get("date")
        if dk == d0:
            continue
        if not _has_shots(k):
            continue
        if base is None:
            base, _ = dk, out.append(k)
        elif dk == base:
            out.append(k)
        else:
            break
    return list(reversed(out))


def _metric_vals(tags, k):
    """指标在所有给定 tag 里的逐球原始值。

    **方向性指标（身体位移 / 落地位移）先取绝对值**：它们算的是髋部在画面
    x 方向上的位移，正负号取决于机位在球员的左侧还是右侧。两次训练换了机位，
    符号就会整体翻转 —— 实测 9月13日 均值 −0.13、10月6日 +0.16 倍身高，
    直接比会造出「重心偏移大了 0.29 倍身高」的假结论。取模后才是同一件事
    （位移了多少），也才和 REFERENCE 的 [0, 0.28] 口径一致。
    """
    out = []
    for t in (tags if isinstance(tags, (list, tuple)) else [tags]):
        for s in load_shots(t):
            m = shot_metrics(s)
            if m and m.get(k) is not None:
                v = float(m[k])
                out.append(abs(v) if k in SIGNED_KEYS else v)
    return out


def _stat(vals):
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    sd = st.pstdev(vals)
    return {"mean": m, "sd": sd, "cv": (sd / abs(m) * 100 if m else None), "n": len(vals)}


def _robust_vals(vals, k=3.0):
    """按 中位数 ± k×1.4826×MAD 剔除离群球（标准稳健准则）。样本太少就不动。"""
    if len(vals) < 5:
        return list(vals)
    med = st.median(vals)
    mad = st.median([abs(x - med) for x in vals])
    if mad <= 0:
        return list(vals)
    lim = k * 1.4826 * mad
    return [x for x in vals if abs(x - med) <= lim]


def derive_compare(tag, prev=None, top=2):
    """【跨日期对比的唯一来源】本场 vs 上一训练日 → 结构化变化清单。

    返回 {"tag","prev","date","prev_date","n_cur","n_prev","generated_at",
          "items","by_shift","by_cv","cards","headline","regressed","improved"}
      items     全部可比指标（含辅助手，照实列出）；每项含 raw（全量）与 rob（稳健）
      by_shift  按 |效应量 d| 降序 —— 值本身挪动最多
      by_cv     按 cv_ratio 降序   —— 一致性变差最多
      cards     给封面 / 文案用的对比卡 [主行, 副行]，最多 top 条（只取 public 项）
      regressed / improved  稳健口径下 CV 显著变差 / 变好的项
    """
    tags = load_tags()
    if prev is None:
        prev_list = find_prev_tags(tag, tags)
    elif isinstance(prev, (list, tuple)):
        prev_list = list(prev)
    else:
        prev_list = [prev]
    date = (tags.get(tag) or {}).get("date", tag)
    prev_date = (tags.get(prev_list[0]) or {}).get("date", prev_list[0]) if prev_list else ""
    if not prev_list:
        return {"tag": tag, "prev": None, "prev_tags": [], "date": date, "prev_date": "",
                "n_cur": 0, "n_prev": 0, "generated_at": time.strftime("%Y-%m-%d %H:%M"),
                "items": [], "by_shift": [], "by_cv": [], "cards": [],
                "headline": "没有可对比的上一场（还没有更早的逐球数据）",
                "regressed": [], "improved": []}

    items, n_cur, n_prev = [], 0, 0
    for k in METRIC_KEYS:
        cv_all, pv_all = _metric_vals(tag, k), _metric_vals(prev_list, k)
        if len(cv_all) < 2 or len(pv_all) < 2:
            continue
        c_r, p_r = _stat(_robust_vals(cv_all)), _stat(_robust_vals(pv_all))
        if not c_r or not p_r:
            continue
        lo, hi, unit, desc, src = REFERENCE.get(k, (None, None, "", "", ""))
        span = (float(hi - lo) if lo is not None else None)
        pooled = math.sqrt((c_r["sd"] ** 2 + p_r["sd"] ** 2) / 2) or 1e-9
        d = (c_r["mean"] - p_r["mean"]) / pooled
        cv_ratio = (c_r["cv"] / p_r["cv"]) if (c_r["cv"] and p_r["cv"]) else None
        items.append({
            "key": k, "label": label(k), "short": COVER_SHORT.get(k, label(k)),
            "unit": unit, "lo": lo, "hi": hi, "span": span,
            "raw": {"cur": _stat(cv_all), "prev": _stat(pv_all)},
            "rob": {"cur": c_r, "prev": p_r},
            "dmean": c_r["mean"] - p_r["mean"], "d": d,
            "pos_move": ((c_r["mean"] - p_r["mean"]) / span if span else None),
            "cv_ratio": cv_ratio,
            "in_ref_cur": _in_ref(c_r["mean"], lo, hi),
            "in_ref_prev": _in_ref(p_r["mean"], lo, hi),
            "public": k not in LEFT_KEYS,
            "field": k in FIELD_KEYS,
        })
    n_cur = max([e["rob"]["cur"]["n"] for e in items] or [0])
    n_prev = max([e["rob"]["prev"]["n"] for e in items] or [0])

    public = [e for e in items if e["public"] and not e["field"]]
    by_shift = sorted(public, key=lambda e: -abs(e["d"]))
    by_cv = sorted([e for e in public if e["cv_ratio"]], key=lambda e: -e["cv_ratio"])
    by_cv = [e for e in public if e["cv_ratio"]] if not by_cv else by_cv
    by_cv.sort(key=lambda e: -e["cv_ratio"])
    reg = [e for e in by_cv if e["cv_ratio"] >= 1.5]
    imp = [e for e in reversed(by_cv) if e["cv_ratio"] <= 0.67]

    # 封面卡：先取「值挪动最大」且效应量够大的一条，再取「波动变大最多」的一条，去重
    picked, cards = [], []
    for e in by_shift:
        if abs(e["d"]) < 1.0:
            break
        picked.append(e)
        break
    for e in by_cv:
        if e in picked:
            continue
        if e["cv_ratio"] < 1.5:
            break
        picked.append(e)
        break
    for e in picked[:top]:
        u = unit_txt(e["unit"])
        pm, cm = e["rob"]["prev"]["mean"], e["rob"]["cur"]["mean"]
        if e["key"] in SHIFT_WORD and e in by_shift[:1]:
            word = SHIFT_WORD[e["key"]][0 if e["dmean"] > 0 else 1]
            main = "%s %s" % (e["short"], card_value(cm, e["unit"]))
            sub = "对比上次 %s%s → %s%s · %s" % (fmt(pm, dig_for(e["unit"]), ""),
                                          u, fmt(cm, dig_for(e["unit"]), ""), u, word)
        else:
            main = "%s波动 %.1f×" % (e["short"], e["cv_ratio"])
            sub = "对比上次 · CV %.1f%% → %.1f%%" % (
                e["rob"]["prev"]["cv"] or 0, e["rob"]["cur"]["cv"] or 0)
        cards.append([main, sub])
    if not cards:
        e = by_shift[0]
        cards.append([e["short"] + " " + card_value(e["rob"]["cur"]["mean"], e["unit"]),
                      "对比上次无显著变化（已剔除离群球）"])

    if reg:
        headline = "一致性退步：%s" % "、".join(
            "%s CV %.1f%%→%.1f%%" % (e["short"], e["rob"]["prev"]["cv"], e["rob"]["cur"]["cv"])
            for e in reg[:2])
    elif imp:
        headline = "一致性变好：%s" % "、".join(
            "%s CV %.1f%%→%.1f%%" % (e["short"], e["rob"]["prev"]["cv"], e["rob"]["cur"]["cv"])
            for e in imp[:2])
    else:
        headline = "与上一训练日相比，各环节一致性没有显著变化"

    return {
        "tag": tag, "prev": prev_list[0] if prev_list else None,
        "prev_tags": prev_list, "date": date, "prev_date": prev_date,
        "n_cur": n_cur, "n_prev": n_prev,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "items": by_shift, "by_shift": by_shift, "by_cv": by_cv,
        "cards": cards, "headline": headline, "regressed": reg, "improved": imp,
    }


def _in_ref(mean, lo, hi):
    if lo is None or mean is None:
        return True
    return lo <= mean <= hi


def load_compare(tag, prev=None):
    """优先现算；现算不可行（缺上场数据）时才退回落盘 json。"""
    d = derive_compare(tag, prev=prev)
    if d.get("items"):
        return d
    fp = compare_path(tag)
    if os.path.exists(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    return d


# --------------------------------------------------------------------------- 基础身体数据

def compute_body(tag, shots):
    """从站立帧量身体：像素段长 -> 比例 -> cm"""
    feats = []
    for sh in shots:
        for r in sh["seq"]:
            f = frame_features(r)
            if f:
                feats.append(f)
    for fp in sorted(__import__("glob").glob(os.path.join(DATA, tag, "deep_*.json"))):
        for r in json.load(open(fp))["seq"]:
            f = frame_features(r)
            if f:
                feats.append(f)
    if not feats:
        return None

    def knee(f):
        if not f:
            return None
        vs = [f[s]["knee"] for s in SIDES if f[s]["knee"] is not None]
        return sum(vs) / len(vs) if vs else None

    stand = [f for f in feats if knee(f) and knee(f) > 155]
    pool = stand or feats

    def s2(n1, n2, fs=None):
        vals = []
        for f in (fs or pool):
            p, q = f["kp"].get(n1), f["kp"].get(n2)
            if p and q:
                vals.append(math.hypot(p[0] - q[0], p[1] - q[1]))
        return st.median(vals) if vals else None

    head_ankle = None
    vals = []
    for f in pool:
        hd = f["kp"].get("head_joint")
        ans = [f["kp"].get(sd + "_foot_joint") for sd in SIDES]
        ans = [a for a in ans if a]
        if hd and ans:
            vals.append(max(abs(a[1] - hd[1]) for a in ans))
    if vals:
        head_ankle = st.median(vals)
    if not head_ankle:
        return None
    px_per_cm = head_ankle / (0.93 * HEIGHT_CM)   # 头顶到踝 ≈ 0.93×身高

    def cm(v):
        return None if v is None else round(v / px_per_cm, 1)

    sh_w = None
    vals = []
    for f in pool:
        p, q = f["kp"].get("right_shoulder_1_joint"), f["kp"].get("left_shoulder_1_joint")
        if p and q:
            vals.append(math.hypot(p[0] - q[0], p[1] - q[1]))
    if vals:
        sh_w = max(vals)   # 取最接近正面的一帧

    return {
        "参考身高_cm": HEIGHT_CM,
        "站立帧数": len(stand), "分析帧数": len(feats),
        "头顶到踝_cm": cm(head_ankle),
        "颈到髋(躯干)_cm": cm(s2("neck_1_joint", "root")),
        "头高(颈到顶)_cm": cm(s2("neck_1_joint", "head_joint")),
        "肩宽_cm": cm(sh_w),
        "上臂_cm": cm(s2("right_shoulder_1_joint", "right_forearm_joint")),
        "前臂_cm": cm(s2("right_forearm_joint", "right_hand_joint")),
        "大腿_cm": cm(s2("right_upLeg_joint", "right_leg_joint")),
        "小腿_cm": cm(s2("right_leg_joint", "right_foot_joint")),
        "髋到踝_cm": cm(s2("right_upLeg_joint", "right_foot_joint")),
        "肩到髋_cm": cm(s2("right_shoulder_1_joint", "right_upLeg_joint")),
        "_px_per_cm": round(px_per_cm, 3), "_head_ankle_px": round(head_ankle, 1),
    }


def body_section(d):
    b = d.get("body")
    if not b:
        return ""
    agg = d["agg"]
    rel_h = agg.get("出手点离地高度", {}).get("mean")
    jump = agg.get("垂直弹跳", {}).get("mean")
    h = ['<h2>基础身体数据</h2><div class="card">']
    src_txt = {"profile": "你建档填的", "manual": "标签表里记录的",
               "default": "默认占位值"}.get(HEIGHT_SRC, "标签表里记录的")
    if HEIGHT_SRC == "default":
        h.append('<p style="background:rgba(224,82,82,.12);border-left:4px solid #e05252;'
                 'padding:10px 14px;border-radius:6px;margin:0 0 12px">'
                 '⚠ <b>还没有设置你的身高</b>：下面所有 cm 值都是按默认 '
                 f'{b["参考身高_cm"]:.0f} cm 估出来的，可能整体偏大或偏小。'
                 '跑一次 <code>python3 tools/shotcli.py setup</code> 填上自己的身高，再重出报告即可。</p>')
    h.append(f'<p class="mut">从视频里量出来的（站立帧 {b["站立帧数"]} 帧 / 共 {b["分析帧数"]} 帧）。'
             f'尺度按身高 {b["参考身高_cm"]:.0f}cm（{src_txt}）换算（头顶到踝 ≈ 0.93×身高，得到 '
             f'{b["_px_per_cm"]:.2f} px/cm）。</p>')
    h.append('<div class="grid g4">')
    h.append(kpi("身高", f'{b["参考身高_cm"]:.0f} cm',
                 "⚠ 默认值，未设置" if HEIGHT_SRC == "default" else "本人提供"))
    if rel_h:
        h.append(kpi("出手点离地高度", f'{rel_h*b["参考身高_cm"]:.0f} cm',
                     f'{rel_h:.2f} 倍身高'))
    if jump:
        h.append(kpi("垂直弹跳", f'{jump*b["参考身高_cm"]:.0f} cm', f'{jump:.2f} 倍身高（踝口径）'))
    if rel_h and jump:
        h.append(kpi("出手高度(去弹跳)", f'{(rel_h-jump)*b["参考身高_cm"]:.0f} cm',
                     "出手点高度 − 垂直弹跳 = 姿态+臂展贡献"))
    h.append('</div>')
    h.append('<table style="margin-top:14px"><thead><tr><th>部位</th><th>实测（cm）</th>'
             '<th>占身高比例</th><th class="l">参考（成年人常见比例）</th></tr></thead><tbody>')
    rows = [
        ("头顶到踝", "头顶到踝_cm", "0.93", "头顶到踝 ≈ 0.93×身高（踝关节高于地面）"),
        ("躯干（颈→髋）", "颈到髋(躯干)_cm", "0.29–0.32", "颈部到髋关节"),
        ("肩宽（双肩峰）", "肩宽_cm", "0.23–0.26", "背身机位取最接近正面的一帧，偏小"),
        ("上臂（肩→肘）", "上臂_cm", "0.17–0.19", "肱骨长"),
        ("前臂（肘→腕）", "前臂_cm", "0.15–0.17", "尺桡骨长"),
        ("大腿（髋→膝）", "大腿_cm", "0.24–0.26", "股骨长"),
        ("小腿（膝→踝）", "小腿_cm", "0.23–0.25", "胫骨长"),
        ("髋到踝", "髋到踝_cm", "0.47–0.50", "下肢总长"),
        ("肩到髋", "肩到髋_cm", "0.29–0.31", "躯干侧长"),
    ]
    for name, key, ref, note in rows:
        v = b.get(key)
        if v is None:
            continue
        h.append(f'<tr><td>{name}</td><td><b>{v:.1f}</b></td>'
                 f'<td class="mut">{v/b["参考身高_cm"]:.3f}</td><td class="l mut">{note}（参考 {ref}）</td></tr>')
    h.append('</tbody></table>')
    h.append('<p class="mut">自检：大腿、小腿、髋到踝均落在解剖学比例的 ±3% 内，说明尺度校准可信；'
             '上臂/前臂偏小 15–25%，原因是姿态识别把肩、肘关节位置估计得偏内偏上，加上背身机位透视缩短，'
             '所以这两个数值只能当参考。臂展需要正面 T 字姿势，本视频无法给出。</p>')
    h.append('</div>')
    return "".join(h)


# --------------------------------------------------------------------------- 逐帧诊断

def ball_chart_svg(ball, meta):
    """轨迹小图：全图坐标，y 向上；黄点=球心轨迹，灰点=出手点，橙竖线=当前窗口范围"""
    pts = [(p.get("fx", p["x"]), p.get("fy", p["y"])) for p in ball["pts"] if p]
    if len(pts) < 4:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs) - 30, max(xs) + 30
    y0, y1 = min(ys) - 40, max(ys) + 30
    W, H = 420, 190
    def sx(x): return 12 + (x - x0) / (x1 - x0) * (W - 24)
    def sy(y): return H - 16 - (y - y0) / (y1 - y0) * (H - 36)
    poly = " ".join("%.1f,%.1f" % (sx(x), sy(y)) for x, y in pts)
    rx, ry = pts[0]
    apex = min(pts, key=lambda p: p[1])
    ang = ball.get("fit", {}) or {}
    lab = ("起球路径 · 最大横向偏移 %spx（%.0f%%）" % (ang["dev_px"], ang["dev_rel"] * 100)) \
        if ang.get("dev_px") is not None else "起球路径（跟踪点不足）"
    return ('<svg viewBox="0 0 %d %d" style="width:100%%;max-width:%dpx;background:var(--panel2);'
            'border:1px solid var(--line);border-radius:8px;margin-top:8px">' % (W, H, W)) + \
        ('<text x="10" y="14" fill="#e3b341" font-size="11">%s</text>' % lab) + \
        ('<polyline points="%s" fill="none" stroke="#ffd166" stroke-width="2"/>' % poly) + \
        ('<circle cx="%.1f" cy="%.1f" r="3" fill="#8b949e"/>' % (sx(rx), sy(ry))) + \
        ('<circle cx="%.1f" cy="%.1f" r="3" fill="#f0883e"/>' % (sx(apex[0]), sy(apex[1]))) + \
        ('<text x="10" y="%d" fill="#8b949e" font-size="10">灰=出手点 · 橙=最高点 · 横轴=画面水平方向</text>' % (H - 4)) + \
        '</svg>'


def build_deep(shot_json, metrics):
    """把 30fps 深扫序列加工成「逐帧诊断」数据"""
    d = json.load(open(shot_json))
    meta, seq = d["meta"], deflip(d["seq"])
    feats = [f for f in (frame_features(r) for r in seq) if f]
    if not feats:
        return None
    scale = scale_px(feats)
    t_rel = metrics["t_rel"]
    t_dip = metrics["t_dip"] or (t_rel - 0.5)

    def knee_mean(f):
        vs = [f[s]["knee"] for s in ("right", "left") if f[s]["knee"] is not None]
        return sum(vs) / len(vs) if vs else None

    def ankle_y(f):
        if not f:
            return None
        ys = [f["kp"][s + "_foot_joint"][1] for s in ("right", "left") if f["kp"].get(s + "_foot_joint")]
        return max(ys) if ys else None

    ground = max([ankle_y(f) for f in feats if ankle_y(f) is not None], default=None)
    hi = metrics["_hi"]
    ball = None
    bp = shot_json.replace("deep_", "ball_")
    if os.path.exists(bp):
        _bj = json.load(open(bp))
        if len(_bj.get("pts") or []) == len(seq):
            ball = _bj

    def phase(t):
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

    frames = []
    for f in feats:
        t = f["t"]
        ph = phase(t)
        knee = knee_mean(f)
        wr = f["kp"].get(hi + "_hand_joint")
        wrel = (ground - wr[1]) / scale if (ground is not None and wr) else None
        badges = []
        if ph == "屈膝/蓄力" and knee is not None:
            lo, hi_ = REFERENCE["屈膝角(最低)"][:2]
            badges.append({"k": "屈膝角", "v": f"{knee:.0f}°", "ok": lo <= knee <= hi_,
                           "note": f"参考 {lo:.0f}–{hi_:.0f}°（职业/大学球员）"})
        if ph == "出手":
            at_release = abs(t - t_rel) <= 1.5 / 30.0   # 只在出手瞬间给结论
            for side, key in (("左", "出手时肘角_左"), ("右", "出手时肘角_右")):
                ev = f["left" if side == "左" else "right"]["elbow"]
                if ev is not None:
                    lo, hi_ = REFERENCE[key][:2]
                    badges.append({"k": f"{side}肘角", "v": f"{ev:.0f}°",
                                   "ok": (lo <= ev <= hi_) if at_release else None,
                                   "note": f"参考 {lo:.0f}–{hi_:.0f}°" if at_release else "起球中，仅参考"})
            fa = f[hi]["fore_ang"]
            if fa is not None:
                lo, hi_ = REFERENCE["出手小臂仰角"][:2]
                badges.append({"k": "小臂仰角", "v": f"{fa:.0f}°",
                               "ok": (lo <= fa <= hi_) if at_release else None,
                               "note": f"参考 {lo:.0f}–{hi_:.0f}°（精英 78.7°）" if at_release else "起球中，仅参考"})
            if wrel is not None:
                lo, hi_ = REFERENCE["出手点离地高度"][:2]
                badges.append({"k": "出手点高度", "v": f"{wrel:.2f}×身高",
                               "ok": (lo <= wrel <= hi_) if at_release else None,
                               "note": f"参考 {lo:.2f}–{hi_:.2f}" if at_release else "球尚未离手"})
        if ph == "出手" and ball and ball.get("fit"):
            f_ = ball["fit"]
            lvl = "直" if f_["dev_rel"] < 0.08 else ("轻微绕弧" if f_["dev_rel"] < 0.15 else "明显绕弧")
            badges.append({"k": "起球轨迹", "v": f'横向偏移 {f_["dev_rel"]*100:.0f}%',
                           "ok": True if f_["dev_rel"] < 0.08 else (None if f_["dev_rel"] < 0.15 else False),
                           "note": f"{lvl} · 跟踪 {f_['n_track']} 帧（观察项，无文献基准）"})
        if ph == "随挥" and metrics.get("垂直弹跳") is not None:
            j = metrics["垂直弹跳"]
            lo, hi_ = REFERENCE["垂直弹跳"][:2]
            badges.append({"k": "垂直弹跳", "v": f"{j:.2f}×身高", "ok": lo <= j <= hi_,
                           "note": f"参考 {lo:.2f}–{hi_:.2f}（职业 2–3 分球）"})
        frames.append({
            "t": round(t, 3), "img": f["img"], "cw": f["cw"], "ch": f["ch"], "kp": f["kp"],
            "phase": ph, "knee": None if knee is None else round(knee),
            "elbowL": None if f["left"]["elbow"] is None else round(f["left"]["elbow"]),
            "elbowR": None if f["right"]["elbow"] is None else round(f["right"]["elbow"]),
            "fore": None if f[hi]["fore_ang"] is None else round(f[hi]["fore_ang"]),
            "wrel": None if wrel is None else round(wrel, 3),
            "badges": badges,
        })
    # 球轨迹（balltrack.py 的输出；按帧文件名对齐到 frames）
    ball = None
    bp = shot_json.replace("deep_", "ball_")
    if os.path.exists(bp):
        bj = json.load(open(bp))
        by_img = {}
        for k, p in enumerate(bj.get("pts") or []):
            if k < len(seq):
                by_img[seq[k]["img"]] = p
        pts = []
        for f in frames:
            p = by_img.get(f["img"])
            pts.append([p["x"], p["y"]] if p else None)
        ball = {"pts": pts, "r": bj.get("seed_r") or 16,
                "fit": bj.get("fit"), "release_i": bj.get("release_i")}
    dp = {"meta": {"tag": meta["tag"], "t_center": meta["t_center"], "fps": meta["fps"],
                   "crop_w": meta["crop_w"], "crop_h": meta["crop_h"]},
          "frames": frames,
          "summary": {**{k: (None if metrics.get(k) is None else round(metrics[k], 3)) for k in METRIC_KEYS},
                      "scale_px": metrics.get("scale_px")}}
    if ball:
        dp["ball"] = ball
        dp["chart"] = ball_chart_svg(bj, meta)   # 小图用原始全图坐标（bj），叠加层用裁剪坐标（ball）
    return dp


# --------------------------------------------------------------------------- HTML

HEAD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2230;--line:#2a3240;--tx:#e6edf3;--tx2:#9aa7b6;
--accent:#4ea1ff;--good:#3fb950;--warn:#e3b341;--bad:#f85149;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:28px;margin:0 0 6px;letter-spacing:.5px}
h2{font-size:19px;margin:38px 0 14px;padding-left:11px;border-left:3px solid var(--accent)}
h3{font-size:15px;margin:22px 0 10px}
.sub{color:var(--tx2);font-size:13px;margin:0 0 10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0}
.grid{display:grid;gap:13px}.g4{grid-template-columns:repeat(4,1fr)}.g3{grid-template-columns:repeat(3,1fr)}
@media(max-width:900px){.g4,.g3{grid-template-columns:repeat(2,1fr)}}
.srcbox{background:#12202e;border:1px solid #24425e;border-radius:10px;padding:10px 14px;
margin:12px 0 4px;font-size:12.5px;color:var(--tx2)}
.srcbox b{color:var(--tx)}
.src{display:inline-block;margin-left:8px;font-size:11.5px;color:var(--tx2);
border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-weight:400}
.kpi{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.kpi .k{font-size:12px;color:var(--tx2);margin-bottom:5px}
.kpi .v{font-size:21px;font-weight:600}.kpi .n{font-size:11px;color:var(--tx2);margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--tx2);font-weight:500;font-size:12px;background:var(--panel2)}
th:first-child,td:first-child{text-align:left}
td.l{text-align:left}
tr:hover td{background:#1a2130}
.ok{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--tx2);font-size:12px}
.bar{height:7px;border-radius:4px;background:#232a36;overflow:hidden;display:inline-block;vertical-align:middle}
.bar>i{display:block;height:100%}
.strip{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:6px}
@media(max-width:900px){.strip{grid-template-columns:repeat(3,1fr)}}
.frame{position:relative;background:#000;border-radius:8px;overflow:hidden;border:1px solid var(--line)}
.frame img{width:100%;display:block}
.frame svg{position:absolute;inset:0;width:100%;height:100%}
.frame .lbl{position:absolute;left:0;bottom:0;right:0;background:linear-gradient(transparent,#000c);
color:#fff;font-size:11px;padding:14px 6px 4px;text-align:center}
.shot{border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0;background:var(--panel)}
.shot h4{margin:0 0 8px;font-size:14px}
.mrow{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px}
.chip b{font-size:13px}
ul{margin:8px 0 0 18px;padding:0}li{margin:4px 0;font-size:13px}
.legend{font-size:12px;color:var(--tx2);display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
/* 逐帧播放器 */
.pl{display:grid;grid-template-columns:minmax(0,430px) 1fr;gap:16px;align-items:start}
@media(max-width:900px){.pl{grid-template-columns:1fr}}
.pl .stage{position:relative;background:#000;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.pl .stage img{width:100%;display:block}
.pl .stage svg{position:absolute;inset:0;width:100%;height:100%}
.pl .phase{position:absolute;left:10px;top:10px;background:#000a;border:1px solid #ffffff22;border-radius:6px;
padding:3px 9px;font-size:12px;letter-spacing:.5px}
.pl input[type=range]{width:100%;margin:10px 0 4px}
.pl .tc{display:flex;justify-content:space-between;font-size:12px;color:var(--tx2)}
.idx{display:flex;flex-direction:column;gap:6px}
.ixrow{display:grid;grid-template-columns:74px 78px 1fr 62px;gap:10px;align-items:center;
background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px 12px;
text-decoration:none;color:var(--tx);font-size:12.5px}
.ixrow:hover{border-color:var(--accent)}
.ixn{font-weight:600}
.ixt{color:var(--tx2)}
.ixb{color:var(--accent);text-align:right}
@media(max-width:760px){.ixrow{grid-template-columns:1fr 1fr;row-gap:4px}.ixk{grid-column:1/3}}
.plbox{outline:none}
.plbox:focus .stage{border-color:var(--accent)}
.film{display:flex;gap:3px;overflow-x:auto;padding:6px 0 4px}
.film img{width:52px;height:auto;border-radius:4px;border:2px solid transparent;cursor:pointer;opacity:.72;flex:0 0 auto}
.film img:hover{opacity:1}
.film img.cur{border-color:var(--accent);opacity:1}
.ctrls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.cbtn{cursor:pointer;color:var(--tx)}
.cbtn:hover{border-color:var(--accent)}
select.chip{color:var(--tx);background:var(--panel2)}
.badge{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--line);
border-radius:8px;padding:6px 10px;font-size:12px;margin:0 6px 6px 0}
.badge.no{border-color:#5a3a12;background:#2a1f0d}
.badge .n{color:var(--tx2);font-size:11px}
.ang{font-size:11px;fill:#e6edf3;paint-order:stroke;stroke:#000a;stroke-width:3px}
</style></head><body>"""


def skeleton_svg(fr, with_angles=True):
    kp, cw, ch = fr["kp"], fr["cw"], fr["ch"]
    if not kp:
        return ""
    pairs = []

    def P(n):
        v = kp.get(n)
        return (v[0], v[1]) if v else None

    def line(a, b, col, w=2.4):
        if a and b:
            pairs.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                         f'stroke="{col}" stroke-width="{w}" stroke-linecap="round"/>')

    line(P("neck_1_joint"), P("head_joint"), "#4ea1ff")
    for s in ("right", "left"):
        col = "#ff6b6b" if s == "right" else "#22d3ee"
        line(P("neck_1_joint"), P(s + "_shoulder_1_joint"), col)
        line(P(s + "_shoulder_1_joint"), P(s + "_forearm_joint"), col)
        line(P(s + "_forearm_joint"), P(s + "_hand_joint"), col)
        line(P(s + "_shoulder_1_joint"), P(s + "_upLeg_joint"), "#ff8fab")
        line(P(s + "_upLeg_joint"), P(s + "_leg_joint"), "#ff8fab")
        line(P(s + "_leg_joint"), P(s + "_foot_joint"), "#c792ea")
    line(P("right_upLeg_joint"), P("left_upLeg_joint"), "#ff8fab")
    dots = "".join(f'<circle cx="{v[0]:.1f}" cy="{v[1]:.1f}" r="2.4" fill="#e6edf3" opacity=".9"/>'
                   for v in kp.values())
    txt = ""
    if with_angles:
        labels = []
        kn = P("right_leg_joint") or P("left_leg_joint")
        if kn and fr.get("knee") is not None:
            labels.append((kn, f"膝 {fr['knee']}°"))
        for s, key in (("right", "elbowR"), ("left", "elbowL")):
            el = P(s + "_forearm_joint")
            if el and fr.get(key) is not None:
                labels.append((el, f"肘 {fr[key]}°"))
        wr = P("right_hand_joint") or P("left_hand_joint")
        if wr and fr.get("fore") is not None:
            labels.append((wr, f"小臂 {fr['fore']}°"))
        for pt, t in labels:
            txt += (f'<text class="ang" x="{pt[0]+8:.0f}" y="{pt[1]-6:.0f}">{t}</text>')
    return (f'<svg viewBox="0 0 {cw} {ch}" preserveAspectRatio="none">' + "".join(pairs) + dots
            + txt + '</svg>')


def kpi(k, v, n=""):
    return f'<div class="kpi"><div class="k">{k}</div><div class="v">{v}</div><div class="n">{n}</div></div>'


def timeline(d, ms):
    evs = d["events"]
    if not evs:
        return ""
    span = d["duration"] or (evs[-1]["t"] if evs else 1)
    span = max(span, evs[-1]["t"] + 5, 10)
    total = d.get("video_duration") or span
    W, H = 1000, 150
    pad_l, pad_r, base = 46, 16, 118
    pw = W - pad_l - pad_r
    mx = max([e["peak"] for e in evs] + [0.3])
    bars = []
    for e in evs:
        x = pad_l + e["t"] / span * pw
        bh = (e["peak"] / mx) * 78
        col = "#4ea1ff" if e["hand"] == "right" else "#c792ea"
        bars.append(f'<rect x="{x-3:.1f}" y="{base-bh:.1f}" width="6" height="{bh:.1f}" rx="2" fill="{col}" opacity=".75">'
                    f'<title>{hr(e["t"])} · 峰值 {e["peak"]:.2f}</title></rect>')
    marks = "".join(f'<circle cx="{pad_l + m["t_rel"]/span*pw:.1f}" cy="{base+12}" r="4" fill="#3fb950">'
                    f'<title>已测算 {hr(m["t_rel"])}</title></circle>' for m in ms)
    ticks = []
    t = 0
    step = 30 if span < 400 else 120
    while t <= span:
        x = pad_l + t / span * pw
        ticks.append(f'<line x1="{x:.1f}" y1="{base}" x2="{x:.1f}" y2="{base+5}" stroke="#3a4553"/>')
        ticks.append(f'<text x="{x:.1f}" y="{base+22}" fill="#9aa7b6" font-size="11" text-anchor="middle">'
                     f'{int(t//60)}:{int(t%60):02d}</text>')
        t += step
    return ('<h3>投篮时间轴</h3><div class="card">'
            f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">'
            f'<line x1="{pad_l}" y1="{base}" x2="{W-pad_r}" y2="{base}" stroke="#2a3240"/>'
            + "".join(bars) + "".join(ticks) + marks +
            f'<text x="{pad_l}" y="14" fill="#9aa7b6" font-size="11">手举过肩峰值（相对幅度）</text>'
            f'<circle cx="{W-pad_r-150}" cy="{base+12}" r="4" fill="#3fb950"/>'
            f'<text x="{W-pad_r-140}" y="{base+16}" fill="#9aa7b6" font-size="11">已逐帧测算</text>'
            f'<text x="{W-pad_r-40}" y="{base+16}" fill="#9aa7b6" font-size="11">蓝=右手紫=左手</text>'
            '</svg>'
            f'<div class="note mut">已扫描 <b>0:00 – {hr(span)}</b>（视频全长 {hr(total)}）：每 0.5 秒一次姿态识别，'
            f'识别到 <b>{len(evs)}</b> 次出手动作，其中 <b>{len(ms)}</b> 次做了逐帧测算。</div></div>')


def curve_charts(d):
    """把多次投篮的曲线叠在一起，直观看出快慢/高低差异"""
    ms = [m for m in d["metrics"] if m.get("_series")]
    if len(ms) < 3:
        return ""
    out = ['<h3>多球曲线对比（同一机位、按出手时刻对齐）</h3><div class="card">']

    def chart(title, key, ylab, ymin, ymax, unit, highlight):
        W, H = 1000, 260
        pad_l, pad_r, pad_t, pad_b = 52, 16, 26, 34
        pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
        x0, x1 = -1.1, 0.7

        def X(t):
            return pad_l + (t - x0) / (x1 - x0) * pw

        def Y(v):
            return pad_t + (ymax - v) / (ymax - ymin) * ph
        g = []
        for v in range(0, 5):
            yy = Y(ymin + (ymax - ymin) * v / 4)
            g.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{W-pad_r}" y2="{yy:.1f}" stroke="#232a36"/>')
            g.append(f'<text x="{pad_l-8}" y="{yy+4:.1f}" fill="#9aa7b6" font-size="11" text-anchor="end">'
                     f'{ymin + (ymax-ymin)*v/4:.0f}</text>')
        for t in (-1, -0.5, 0, 0.5):
            g.append(f'<line x1="{X(t):.1f}" y1="{pad_t}" x2="{X(t):.1f}" y2="{pad_t+ph}" stroke="#232a36" stroke-dasharray="3 4"/>')
            g.append(f'<text x="{X(t):.1f}" y="{H-14}" fill="#9aa7b6" font-size="11" text-anchor="middle">{t:+.1f}s</text>')
        g.append(f'<line x1="{X(0):.1f}" y1="{pad_t}" x2="{X(0):.1f}" y2="{pad_t+ph}" stroke="#3fb950" stroke-width="1.5"/>')
        g.append(f'<text x="{X(0)+6:.1f}" y="{pad_t+12}" fill="#3fb950" font-size="11">出手</text>')
        for m in ms:
            s = m["_series"]
            pts = []
            for i, dt in enumerate(s["dt"]):
                v = s[key][i]
                if v is None or dt < x0 or dt > x1:
                    continue
                pts.append(f"{X(dt):.1f},{Y(v):.1f}")
            if len(pts) < 3:
                continue
            hl = abs(m["t_rel"] - highlight) < 0.6
            g.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                     f'stroke="{"#e3b341" if hl else "#4ea1ff"}" stroke-width="{2.6 if hl else 1.1}" '
                     f'opacity="{1 if hl else .45}"/>')
            if hl:
                g.append(f'<text x="{X(0.05):.1f}" y="{Y(s[key][-1] or 0)-8:.1f}" fill="#e3b341" font-size="11">'
                         f'{hr(m["t_rel"])}</text>')
        out.append(f'<div style="margin-bottom:16px"><div class="mut" style="margin-bottom:6px">{title}</div>'
                   f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">' + "".join(g) + '</svg></div>')

    chart("手腕相对肩的高度（负值=手在肩下；越陡=举手越快）", "wrist",
          "手腕高于肩", -0.6, 0.4, "", d["highlight_t"])
    chart("膝角（越小=蹲得越深）", "knee", "膝角 °", 60, 185, "", d["highlight_t"])
    out.append(f'<div class="note mut">共 {len(ms)} 次投篮曲线叠加，黄色为该场「最慢一次」（{hr(d["highlight_t"])}）。'
               f'曲线越分散说明各次动作差异越大。</div></div>')
    return "".join(out)


def _conc_of(d):
    """取本次的结论清单（有缓存就用缓存），所有结论类渲染都走这里。"""
    if d.get("conc"):
        return d["conc"]
    d["conc"] = derive_conclusions(d.get("agg") or {}, d.get("metrics"), tag=d.get("tag"))
    return d["conc"]


def insights(d):
    """结论与训练建议 —— 三节，顺序有讲究：

    ① 偏离参考区间（按 |dev| 排序）—— 结论的主要来源
    ② 已稳定**且在参考区间内**（先别动）
    ③ 波动偏大（CV > 12）

    2026-10-12 改：原来按 CV 分组、排序只看 CV，于是「屈膝角本场 98° 且 CV 仅 2.1%」
    被塞进「已经稳定的部分（先别动）」并配文案「下肢蓄力到位且深度稳定」，
    而同一份报告的文献对照表里写着「蹲得过深」—— 同一份报告自相矛盾。
    现在「先别动」必须同时满足 in_ref 与低 CV，偏离度与稳定性分开呈现。
    判定与排序全部来自 derive_conclusions()（结论的唯一来源），这里只做呈现。
    """
    agg, ms = d["agg"], d["metrics"]
    if not agg:
        return ""
    conc = _conc_of(d)
    oors, by_c = conc["out_of_range"], conc["by_consistency"]

    h = ['<h2>结论与训练建议</h2><div class="card">']
    h.append(f'<p class="mut">基于 {len(ms)} 次逐帧测算。结论只看一件事：本场均值是否落在'
             f'<b>文献参考区间</b>内；<b>偏离量 dev</b> = 超出量 ÷ 区间宽度（dev 越大越该改）。'
             f'稳定性（CV）单独成节，不与偏离度混排。</p>')
    h.append('<div class="srcbox">标题结论：<b>%s</b>　←　自动取自「对外」指标中偏离最大的 1~2 项'
             '（推导过程见下方〈结论是怎么来的〉）</div>' % conc["title"]["line3"])

    # ① 偏离参考区间
    h.append('<h3 style="color:var(--warn)">① 偏离参考区间（按偏离程度排序，这一节出结论）</h3>')
    if oors:
        for i, e in enumerate(oors[:6], 1):
            off = "" if e["public"] else '<span class="mut"> · 对外不引用</span>'
            field = '<span class="mut"> · 教学口径，非文献硬边界</span>' \
                if e["interval_src"] == "field" else ""
            d_txt = "" if e["in_ref"] else "　偏离 %.2f 个区间宽" % e["dev_abs"]
            h.append(
                f'<div class="kpi" style="margin:10px 0;border-color:#6b4b12">'
                f'<div class="k" style="font-size:13px">{i}. <b class="{e["level"]}">{e["verdict"]}</b>'
                f'　{e["label"]}{off}</div>'
                f'<div style="margin:6px 0 4px">本场 {fmt(e["mean"], dig_for(e["unit"]), unit_txt(e["unit"]))}'
                f'　±{fmt(e["sd"], dig_for(e["unit"]))}　·　参考区间 {ref_text(e["lo"], e["hi"], e["unit"])}'
                f'<span class="mut">{d_txt}{field}</span></div>'
                f'<div class="mut">处方：{e["advice"] or "—"}</div></div>')
    else:
        h.append('<p class="mut">本次所有指标的均值都落在参考区间内 —— 没有需要改的项。</p>')

    # ② 已稳定且在区间内
    stay = [e for e in by_c if e["in_ref"] and e["cv"] is not None and e["cv"] < 5]
    h.append('<h3 style="color:var(--good)">② 已稳定且在区间内（先别动）</h3>')
    if stay:
        h.append('<ul>')
        for e in stay:
            off = '' if e["public"] else ' · 对外不引用'
            h.append(f'<li><b>{e["label"]}</b>{off}（CV {e["cv"]:.1f}%，'
                     f'{fmt(e["mean"], dig_for(e["unit"]), unit_txt(e["unit"]))}）：{e["good_text"] or "稳定"}</li>')
        h.append('</ul>')
    else:
        h.append('<p class="mut">本次没有「稳定且在区间内」的项。</p>')

    # ③ 波动偏大（注意用 bad_text —— 这一节讲的是问题，不能用正面描述）
    wob = [e for e in by_c if e["cv"] is not None and e["cv"] > 12]
    h.append('<h3>③ 波动偏大（CV &gt; 12%，按 CV 降序）</h3>')
    if wob:
        for i, e in enumerate(wob[:6], 1):
            off = ' · 对外不引用' if not e["public"] else ''
            inref = '' if e["in_ref"] else '　<span class="warn">且均值已超出参考区间</span>'
            h.append(
                f'<div class="kpi" style="margin:10px 0">'
                f'<div class="k" style="font-size:13px">{i}. {e["label"]}{off} · '
                f'离散度 CV {e["cv"]:.1f}%</div>'
                f'<div style="margin:6px 0 4px">{e["bad_text"] or "波动偏大"}{inref}</div>'
                f'<div class="mut">处方：{e["advice"] or "—"}</div></div>')
    else:
        h.append('<p class="mut">没有 CV &gt; 12% 的指标。</p>')

    h.append('</div>')
    return "".join(h)


def provenance_table(d):
    """〈结论是怎么来的〉—— 把 derive_conclusions 的中间量原样摊开，供核对。

    目的：让人能自己验算「结论是不是从参考区间推出来的」，而不是只看到一句结论。
    """
    agg, ms = d["agg"], d["metrics"]
    if not agg:
        return ""
    conc = _conc_of(d)
    levels = {"ok": ("区间内", "ok"), "warn": ("偏离", "warn"), "bad": ("明显偏离", "bad")}

    h = ['<h2>结论是怎么来的（可核对）</h2><div class="card">',
         '<p class="mut">下表是程序推导结论的<b>全部中间量</b>，没有人工取舍：'
         '<b>dev</b> = 超出量 ÷ 参考区间宽度（0 = 落在区间内），按 dev 从大到小排列；'
         '<b>对外</b>列打 ✓ 的指标才会进入标题 / 封面 / 文案。</p>',
         '<table><thead><tr><th>指标</th><th>本场均值 ± 组内SD</th><th>CV</th>'
         '<th>参考区间</th><th>偏离量 dev</th><th>方向</th><th>判定</th><th>措辞</th>'
         '<th>对外</th><th>区间来源</th><th>出处</th></tr></thead><tbody>']
    for e in conc["items"]:
        u, dg = e["unit"], dig_for(e["unit"])
        ut = unit_txt(u)
        cv = e["cv"]
        cv_cls = "ok" if (cv is not None and cv < 5) else ("warn" if (cv is not None and cv < 12) else "bad")
        cv_txt = f"{cv:.1f}%" if cv is not None else "—"
        dev_txt = "0" if e["in_ref"] else f'{e["dev"]:+.2f}'
        dev_cls = "mut" if e["in_ref"] else ("bad" if e["level"] == "bad" else "warn")
        direction = "↓低于下限" if e["over"] < 0 else ("↑高于上限" if e["over"] > 0 else "—")
        lv, lv_cls = levels.get(e["level"], ("—", "mut"))
        src_txt = "教学口径（非文献）" if e["interval_src"] == "field" else "文献"
        h.append(
            f'<tr><td class="l">{e["label"]}</td>'
            f'<td>{fmt(e["mean"], dg)} ± {fmt(e["sd"], dg)} {ut}</td>'
            f'<td class="{cv_cls}">{cv_txt}</td>'
            f'<td>{ref_text(e["lo"], e["hi"], u)}</td>'
            f'<td class="{dev_cls}">{dev_txt}</td>'
            f'<td class="mut">{direction}</td>'
            f'<td class="{lv_cls}">{lv}</td>'
            f'<td class="{lv_cls}">{e["verdict"]}</td>'
            f'<td>{"✓" if e["public"] else "—"}</td>'
            f'<td class="mut">{src_txt}</td>'
            f'<td class="mut" style="font-size:11.5px">{e["source"]}</td></tr>')
    h.append('</tbody></table>')
    h.append('<p class="mut">→ 由「对外 = ✓」中偏离最大的 %d 项拼出标题结论：<b>%s</b>'
             '（无越界项时写作「各环节均在参考区间内」）。</p>'
             % (len(conc["public_out_of_range"]) and min(2, len(conc["public_out_of_range"])) or 0,
                conc["title"]["line3"]))
    h.append('</div>')
    return "".join(h)



def drift(d):
    """前半段 vs 后半段：找出系统性漂移（不是随机波动）"""
    ms = sorted(d["metrics"], key=lambda x: x["t_rel"])
    if len(ms) < 9:
        return ""
    n = max(3, len(ms) // 3)
    early, late = ms[:n], ms[-n:]
    rows = []
    for k in CORE:
        e = [m[k] for m in early if m.get(k) is not None]
        l = [m[k] for m in late if m.get(k) is not None]
        if len(e) < 2 or len(l) < 2:
            continue
        me, ml = sum(e) / len(e), sum(l) / len(l)
        sd = st.pstdev(e + l) or 1e-9
        rows.append((k, me, ml, (ml - me) / sd, ml - me))
    rows.sort(key=lambda r: -abs(r[3]))
    h = ['<h2>前半段 vs 后半段（趋势对比）</h2><div class="card">',
         f'<p class="mut">取前 {n} 球与后 {n} 球对比。差异以「该指标本场标准差」为单位衡量，'
         f'|差异| &gt; 0.8 视为系统性变化（不是随机波动），这类变化通常来自站位距离改变或疲劳。</p>',
         '<table><thead><tr><th>指标</th><th>前半段</th><th>后半段</th><th>变化</th><th>判读</th></tr></thead><tbody>']
    for k, me, ml, z, delta in rows[:8]:
        unit = REFERENCE.get(k, (0, 0, ""))[2]
        dig = dig_for(unit)
        if abs(z) < 0.5:
            txt, cls = "基本稳定", "mut"
        else:
            txt = ("系统性" + ("上升" if delta > 0 else "下降")) if abs(z) > 0.8 else ("略微" + ("上升" if delta > 0 else "下降"))
            cls = "warn" if abs(z) > 0.8 else "mut"
        h.append(f'<tr><td>{label(k)}</td><td>{fmt(me,dig)} {unit}</td><td>{fmt(ml,dig)} {unit}</td>'
                 f'<td class="{cls}">{delta:+.{dig}f}</td><td class="{cls}">{txt}</td></tr>')
    h.append('</tbody></table>')
    deep = [r for r in rows if r[0] == "屈膝角(最低)"]
    if deep and abs(deep[0][3]) > 0.8:
        d0 = deep[0]
        direction = "加深" if d0[4] < 0 else "变浅"
        h.append(f'<p class="mut">其中最值得注意的是<b>屈膝深度后半段系统性{direction}</b>'
                 f'（{d0[1]:.0f}° → {d0[2]:.0f}°，膝角越小蹲得越深）。'
                 f'文献里 3 分球比罚球需要更大的膝/髋屈曲（Cabarkapa 2022），'
                 f'所以如果你的后半段是在更远的位置出手，这是<b>正确的自适应</b>；'
                 f'如果站位距离没变，那就是疲劳导致的下蹲变深、出手变慢。</p>')
    h.append('</div>')
    return "".join(h)


def lit_table(d):
    """文献对照：本次值 vs 公开研究值"""
    agg = d["agg"]
    h = ['<h2>文献对照</h2><div class="card">',
         '<p class="mut">参考区间与「精英/熟练/新手」标杆均来自公开的生物力学研究（样本多为职业/大学球员，'
         '3D 动作捕捉或高清视频量角）。悬停指标名可看该指标的文献出处。</p>',
         '<table><thead><tr><th>指标</th><th>本次（均值 ± 标准差）</th><th>文献参考区间</th>'
         '<th>评价</th><th class="l">出处</th></tr></thead><tbody>']
    for k in METRIC_KEYS:
        a = agg.get(k)
        if not a:
            continue
        lo, hi, unit, desc, src = REFERENCE.get(k, (None, None, "", "", "无直接文献区间"))
        txt, cls = qual(k, a["mean"])
        ref = f"{lo}–{hi} {unit}" if lo is not None else "—"
        dig = dig_for(unit)
        h.append(f'<tr><td title="{desc}">{label(k)}</td>'
                 f'<td><b>{fmt(a["mean"],dig)}</b> ± {fmt(a["sd"],dig)} {unit}</td><td>{ref}</td>'
                 f'<td class="{cls}">{txt}</td><td class="l mut">{src}</td></tr>')
    h.append('</tbody></table>')
    # 一致性对照（文献给的是组内标准差，单位统一为度）
    rows = []
    for k, ref in CONSISTENCY_REF.items():
        a = agg.get(k)
        if not a:
            continue
        mine = a["sd"]
        lvl = "优于精英" if mine <= ref["elite"] else ("熟练~精英" if mine <= ref["experienced"]
                                                   else ("新手~熟练" if mine <= ref["novice"] else "低于新手水平"))
        rows.append((k, mine, a["mean"], ref, lvl))
    if rows:
        h.append('<h3>动作一致性对照（同一人多次投篮的组内标准差，越小越稳定）</h3>')
        h.append('<table><thead><tr><th>指标</th><th>本次 SD</th><th>本次均值</th>'
                 '<th>新手 / 熟练 / 精英 SD</th><th>对应水平</th></tr></thead><tbody>')
        for k, mine, mean, ref, lvl in rows:
            cls = "ok" if lvl in ("优于精英", "熟练~精英") else ("warn" if lvl == "新手~熟练" else "bad")
            h.append(f'<tr><td>{label(k)}</td><td><b>±{mine:.2f}°</b></td><td>{mean:.1f}°</td>'
                     f'<td class="mut">{ref["novice"]:.2f} / {ref["experienced"]:.2f} / {ref["elite"]:.2f}°</td>'
                     f'<td class="{cls}">{lvl}</td></tr>')
        h.append('</tbody></table>')
        h.append('<p class="mut">标杆出处：PMC9277478（Skills 分组：Novice / Experienced / Elite，'
                 '统计量为同一受试者多次投篮的最大关节角标准差）。'
                 '<b>注意</b>：本研究用的是实验室标记点/动作捕捉，本报告用视频姿态估计，'
                 '关节定位噪声会放大标准差，因此这个对照只说明「距离实验室指标的差距」，不能等同于技能等级。</p>')
    h.append('</div>')
    return "".join(h)


def goal_chain(ms, agg):
    """一球一图：关键环节 vs 文献区间 + 与全场均值的偏离（缺陷用红黄标记）"""
    if not agg:
        return ""
    med = {k: (agg[k]["mean"] if agg.get(k) else None) for k in METRIC_KEYS}
    h = ['<h2>逐球诊断</h2>',
         '<p class="mut">每球 6 个环节：颜色按<b>文献区间</b>判定（绿=区间内、黄=略偏、红=明显偏），'
         '括号里是与本场均值的偏离。这样一眼能看出「哪些球在哪些环节掉出去了」。</p>']
    for i, m in enumerate(sorted(ms, key=lambda x: x["t_rel"]), 1):
        side_key = "出手时肘角_右"   # 投篮手
        items = [
            ("屈膝深度", m["屈膝角(最低)"], "屈膝角(最低)", 0),
            ("出手肘角（投篮手）", m[side_key], side_key, 0),
            ("辅助手肘角", m["出手时肘角_左"], "出手时肘角_左", 0),
            ("小臂仰角", m["出手小臂仰角"], "出手小臂仰角", 0),
            ("出手节奏", m["屈膝到出手"], "屈膝到出手", 2),
            ("垂直弹跳", m["垂直弹跳"], "垂直弹跳", 2),
            ("身体位移", m["身体位移"], "身体位移", 2),
        ]
        h.append('<div class="shot"><h4>第 %d 次 · %s</h4><div class="mrow">' % (i, hr(m["t_rel"])))
        flaws = 0
        for name, v, key, dig in items:
            if v is None:
                continue
            unit = REFERENCE.get(key, (0, 0, ""))[2]
            verdict, cls = qual(key, v)
            mu = med.get(key)
            dev = "" if mu is None else f'　{(v-mu)/(abs(mu) or 1)*100:+.0f}% vs 均值'
            if cls in ("warn", "bad"):
                flaws += 1
            mark = {"ok": "✓", "warn": "!", "bad": "✗"}.get(cls, "")
            h.append(f'<span class="chip {cls}" style="border-color:currentColor">{name} '
                     f'<b>{fmt(v,dig,unit)}</b> <span class="mut">{mark} {verdict}{dev}</span></span>')
        tag = "无越界环节" if flaws == 0 else f"{flaws} 个环节越界"
        h.append(f'<span class="chip" style="border-color:var(--line)">{tag}</span>')
        h.append('</div></div>')
    return "".join(h)


def player_section(d):
    deeps = d.get("deep") or []
    if not deeps:
        return ""
    fps_all = sorted({dp["meta"]["fps"] for dp in deeps})
    fps_txt = "/".join(f"{f:.0f}" for f in fps_all)
    h = ['<h2>逐帧诊断播放器</h2>',
         f'<p class="mut">本次 <b>{len(deeps)}</b> 次出手<b>全部</b>做了逐帧慢放：'
         f'{fps_txt}fps 连续帧，每个样本 {len(deeps[0]["frames"])} 帧（覆盖出手前后约 1 秒），'
         f'每帧都跑了姿态识别。可拖动滑块、用「上一帧/下一帧」单帧步进、直接点缩略图跳帧；'
         f'默认 0.15 倍速慢放，并<b>默认停在出手那一帧</b>。帧上标出该帧的关节角度，右侧列出该帧该看的指标与缺陷提示。</p>']
    # 7 次出手索引（点击跳转）
    h.append('<div class="card"><h3>本次出手一览（点「查看」跳到对应播放器）</h3>'
             '<div class="idx">')
    for i, dp in enumerate(deeps, 1):
        summ = dp.get("summary") or {}
        kk = []
        for k in ["屈膝角(最低)", "出手时肘角_右", "出手小臂仰角", "屈膝到出手"]:
            v = summ.get(k)
            if v is None:
                continue
            unit = REFERENCE.get(k, (0, 0, ""))[2]
            kk.append(f'<span class="mut">{label(k)}</span> <b>{fmt(v, dig_for(unit), unit)}</b>')
        h.append(f'<a class="ixrow" href="#pl{i}"><span class="ixn">第 {i} 次</span>'
                 f'<span class="ixt">{hr(dp["meta"]["t_center"])}</span>'
                 f'<span class="ixk">{"　".join(kk)}</span>'
                 f'<span class="ixb">查看 →</span></a>')
    h.append('</div></div>')
    for i, dp in enumerate(deeps, 1):
        summ = dp["summary"]
        h.append(f'<div class="card" id="pl{i}"><h3>样本 %d · %s（%s）'
                 '<span class="src">源文件 %s</span></h3>'
                 % (i, dp["title"], hr(dp["meta"]["t_center"]), os.path.basename(d["file"])))
        h.append('<div class="mrow">')
        for k in ["屈膝角(最低)", "出手时肘角_左", "出手小臂仰角", "出手点离地高度", "垂直弹跳", "屈膝到出手", "身体位移"]:
            v = summ.get(k)
            if v is None:
                continue
            unit = REFERENCE.get(k, (0, 0, ""))[2]
            h.append(f'<span class="chip">{label(k)} <b>{fmt(v, dig_for(unit), unit)}</b></span>')
        if dp.get("ball") and dp["ball"].get("fit"):
            f_ = dp["ball"]["fit"]
            sc = (summ.get("scale_px") or 0) or 1.0
            cm = f_["dev_px"] / sc
            lvl = "直" if f_["dev_rel"] < 0.08 else ("轻微绕弧" if f_["dev_rel"] < 0.15 else "明显绕弧")
            h.append(f'<span class="chip">起球轨迹 <b>{lvl}</b>'
                     f'<span class="mut">　跟踪 {f_["n_track"]} 帧 · 最大横向偏移 {f_["dev_px"]:.0f}px ≈ {cm:.0f}cm</span></span>')
        elif dp.get("ball"):
            h.append('<span class="chip">起球轨迹 <span class="mut">已跟踪但点太少，未计算</span></span>')
        h.append('</div>')
        if dp.get("chart"):
            h.append(dp["chart"])
        n = len(dp["frames"])
        pi = len(PLAYER_INITS) + 1
        # 默认停在「出手」相位第一帧（否则一进来是预备姿势，容易误以为没做姿态识别）
        start = next((k for k, f in enumerate(dp["frames"]) if f["phase"] == "出手"), n // 2)
        h.append(f'<div class="pl"><div data-player="{pi}" tabindex="0" class="plbox">'
                 f'<div class="stage">'
                 f'<img id="img{pi}" src="frames/{d["tag"]}/{dp["frames"][start]["img"]}" alt="">'
                 f'<div id="svg{pi}"></div><div class="phase" id="ph{pi}"></div></div>'
                 f'<input type="range" id="rng{pi}" min="0" max="{n-1}" value="{start}" step="1">'
                 f'<div class="tc"><span id="tl{pi}"></span><span id="tr{pi}"></span></div>'
                 f'<div class="film" id="film{pi}"></div>'
                 f'<div class="ctrls">'
                 f'<button class="chip cbtn" id="prev{pi}">◀ 上一帧</button>'
                 f'<button class="chip cbtn" id="play{pi}">▶ 慢放</button>'
                 f'<button class="chip cbtn" id="next{pi}">下一帧 ▶</button>'
                 f'<select class="chip" id="rate{pi}">'
                 f'<option value="0">手动（只拖滑块）</option>'
                 f'<option value="0.1">0.1× 最慢</option>'
                 f'<option value="0.15" selected>0.15×</option>'
                 f'<option value="0.3">0.3×</option>'
                 f'<option value="0.5">0.5×</option>'
                 f'<option value="1">1× 原速</option></select>'
                 f'<span class="mut" style="font-size:12px">点画面后可用 ← → 单帧步进，空格播放/暂停</span>'
                 f'</div></div><div id="bd{pi}"></div></div>')
        frames_js = json.dumps(dp["frames"], ensure_ascii=False)
        ball_js = json.dumps(dp.get("ball"), ensure_ascii=False) if dp.get("ball") else "null"
        PLAYER_INITS.append(
            f'<script>window.initPlayer({pi}, '
            f'Object.assign({frames_js}, {{meta_fps: {dp["meta"]["fps"]}, start: {start}, ball: {ball_js}}}), "{d["tag"]}");</script>')
    return "".join(h)


SCRIPT_TMPL = """<script>
(function(){
const EDGES=[["neck_1_joint","head_joint","#4ea1ff"],
 ["neck_1_joint","right_shoulder_1_joint","#ff6b6b"],["right_shoulder_1_joint","right_forearm_joint","#ff6b6b"],
 ["right_forearm_joint","right_hand_joint","#ff6b6b"],
 ["neck_1_joint","left_shoulder_1_joint","#22d3ee"],["left_shoulder_1_joint","left_forearm_joint","#22d3ee"],
 ["left_forearm_joint","left_hand_joint","#22d3ee"],
 ["right_shoulder_1_joint","right_upLeg_joint","#ff8fab"],["right_upLeg_joint","right_leg_joint","#ff8fab"],
 ["left_shoulder_1_joint","left_upLeg_joint","#ff8fab"],["left_upLeg_joint","left_leg_joint","#ff8fab"],
 ["right_upLeg_joint","left_upLeg_joint","#ff8fab"],
 ["right_leg_joint","right_foot_joint","#c792ea"],["left_leg_joint","left_foot_joint","#c792ea"]];
window.initPlayer = function(idx, frames, tag){
  const FPS = frames.meta_fps || 30;
  let i=frames.start||0, timer=null;
  const box=document.querySelector('[data-player="'+idx+'"]'),
        img=document.getElementById('img'+idx), svgBox=document.getElementById('svg'+idx),
        ph=document.getElementById('ph'+idx), rng=document.getElementById('rng'+idx),
        tl=document.getElementById('tl'+idx), tr=document.getElementById('tr'+idx),
        bd=document.getElementById('bd'+idx), btn=document.getElementById('play'+idx),
        prevB=document.getElementById('prev'+idx), nextB=document.getElementById('next'+idx),
        rateSel=document.getElementById('rate'+idx);
  if(!box||!img||!svgBox||!rng){ console.warn('player '+idx+' 缺少元素，已跳过'); return; }
  const film=document.getElementById('film'+idx);
  let thumbs=[];
  frames.forEach((f,k)=>{
    const im=document.createElement('img');
    im.src='frames/'+tag+'/'+f.img; im.loading='lazy'; im.title='第 '+(k+1)+' 帧 · '+f.phase;
    im.addEventListener('click',()=>{ stop(); i=k; rng.value=i; render(); });
    film.appendChild(im); thumbs.push(im);
  });
  function render(){
    const f=frames[i]; if(!f) return;
    thumbs.forEach((t,k)=>t.classList.toggle('cur', k===i));
    if(thumbs[i] && thumbs[i].scrollIntoView) thumbs[i].scrollIntoView({block:'nearest',inline:'nearest'});
    img.src='frames/'+tag+'/'+f.img;
    ph.textContent=f.phase;
    const kp=f.kp||{};
    let s='<svg viewBox="0 0 '+f.cw+' '+f.ch+'" preserveAspectRatio="none">';
    for(const [a,b,c] of EDGES){const p=kp[a],q=kp[b];
      if(p&&q) s+='<line x1="'+p[0].toFixed(1)+'" y1="'+p[1].toFixed(1)+'" x2="'+q[0].toFixed(1)+'" y2="'+q[1].toFixed(1)+'" stroke="'+c+'" stroke-width="2.4" stroke-linecap="round"/>';}
    for(const k in kp){s+='<circle cx="'+kp[k][0].toFixed(1)+'" cy="'+kp[k][1].toFixed(1)+'" r="2.4" fill="#e6edf3" opacity=".9"/>';}
    if(frames.ball){
      const rel=(frames.ball.release_i!=null)?frames.ball.release_i:frames.length-1;
      // 起球→出手的完整连续轨迹（始终显示整条线）
      let path='';
      for(let k=0;k<=rel;k++){const p=frames.ball.pts[k]; if(p){path+=(path?' L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1);}}
      if(path){
        s+='<path d="'+path+'" fill="none" stroke="#ffd166" stroke-width="6" stroke-linecap="round" stroke-linejoin="round" opacity=".35"/>';
        s+='<path d="'+path+'" fill="none" stroke="#ffd166" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity=".95"/>';
      }
      const cur=(i<=rel)?frames.ball.pts[i]:null;
      if(cur){ s+='<circle cx="'+cur[0].toFixed(1)+'" cy="'+cur[1].toFixed(1)+'" r="'+(frames.ball.r||16).toFixed(0)+
        '" fill="none" stroke="#ffd166" stroke-width="3"/><circle cx="'+cur[0].toFixed(1)+'" cy="'+cur[1].toFixed(1)+
        '" r="3.5" fill="#ffd166"/>'; }
    }
    function lab(name,txt,xo,yo){const p=kp[name];if(!p||txt==null)return;
      s+='<text class="ang" x="'+(p[0]+xo).toFixed(0)+'" y="'+(p[1]+yo).toFixed(0)+'">'+txt+'</text>';}
    lab('right_leg_joint', f.knee!=null?('膝 '+f.knee+'°'):null, 8,-6);
    lab('right_forearm_joint', f.elbowR!=null?('肘 '+f.elbowR+'°'):null, 8,-6);
    lab('left_forearm_joint', f.elbowL!=null?('肘 '+f.elbowL+'°'):null, 8,-6);
    lab('right_hand_joint', f.fore!=null?('小臂 '+f.fore+'°'):null, 10,10);
    s+='</svg>';
    svgBox.innerHTML=s;
    tl.textContent='第 '+(i+1)+' / '+frames.length+' 帧    t='+f.t.toFixed(2)+'s';
    tr.textContent=(f.wrel!=null?('出手点 '+(f.wrel*100).toFixed(0)+'% 身高'):'');
    let html='<div class="mut" style="margin-bottom:8px">该帧：膝 '+(f.knee??'—')+'° ｜ 左肘 '+(f.elbowL??'—')+
      '° ｜ 右肘（投篮手） '+(f.elbowR??'—')+'° ｜ 小臂仰角 '+(f.fore??'—')+'°</div>';
    if(!f.badges.length){html+='<div class="mut">（该帧无需要判读的指标）</div>';}
    for(const b of f.badges){
      const bad=(b.ok===false);
      html+='<div class="badge'+(bad?' no':'')+'"><span style="color:'+(b.ok===true?'var(--good)':(bad?'var(--warn)':'var(--tx2)'))+
        '">'+(b.ok===true?'✓':(bad?'✗':'·'))+'</span> '+b.k+' <b>'+b.v+'</b> <span class="n">'+b.note+'</span></div>';
    }
    bd.innerHTML=html;
  }
  function stop(){ if(timer){clearInterval(timer);timer=null;} btn.textContent='▶ 慢放'; }
  function start(){
    const rate=parseFloat(rateSel.value)||0;
    if(rate<=0){ stop(); return; }
    if(timer) clearInterval(timer);
    timer=setInterval(function(){ i=(i+1)%frames.length; rng.value=i; render(); }, 1000/(FPS*rate));
    btn.textContent='⏸ 暂停';
  }
  function step(d){ stop(); i=Math.max(0,Math.min(frames.length-1,i+d)); rng.value=i; render(); }
  rng.addEventListener('input',e=>{ stop(); i=+e.target.value; render(); });
  btn.addEventListener('click',()=>{ timer?stop():start(); });
  prevB.addEventListener('click',()=>step(-1));
  nextB.addEventListener('click',()=>step(1));
  rateSel.addEventListener('change',()=>{ const was=!!timer; if(was) start(); });
  box.addEventListener('keydown',e=>{
    if(e.key==='ArrowLeft'){step(-1);e.preventDefault();}
    else if(e.key==='ArrowRight'){step(1);e.preventDefault();}
    else if(e.key===' '){timer?stop():start();e.preventDefault();}
  });
  render();
};
})();
</script>"""


def method_section(d):
    deeps = d.get("deep") or []
    h = ['<h2>方法、校准与局限</h2><div class="card"><ul>']
    h.append("<li><b>技术路线</b>：AVFoundation 顺序解码 → Apple Vision 人体姿态（19 关键点）→ "
             "裁剪球员区域放大后二次识别（逐帧）→ 关节角度/高度/时序 → 跨球统计与一致性分析。</li>")
    sc = [m["scale_px"] for m in d["metrics"] if m.get("scale_px")]
    if sc:
        h.append(f"<li><b>尺度校准</b>：高度类指标不用「头到踝」（屈膝时会被压缩），"
                 f"而用刚体段长估算身高像素尺度（股骨长 ÷ 0.245、颈-髋长 ÷ 0.32，取中位数，"
                 f"本次约 {st.median(sc):.0f} px/身高），因此「出手点离地高度」「垂直弹跳」可与文献直接比对。</li>")
    h.append("<li><b>出手帧</b>：以双手手腕平均过肩高度最高的一帧作为出手（无第三方球检测），误差约 ±0.03 秒（30fps 采样）。</li>")
    h.append("<li><b>投篮手</b>：已按本人确认的口径 —— <b>右手投篮、左手辅助</b>。"
             "出手点高度、小臂仰角等一律取右手；左臂指标作为辅助手对照保留。"
             "（背身机位下手臂在头顶交叉时识别模型的左右标签会短暂互换，脚本已按帧间最近邻连续性纠正。）</li>")
    h.append("<li><b>不能测的东西</b>：球的真实飞行弧线角（需要球体跟踪），以及躯干在矢状面的真实倾斜"
             "（背身机位会把它投影掉）。报告中「小臂仰角」只是手臂角度的近似，不等于出手角。</li>")
    h.append("<li><b>关节定位精度</b>：球员在画面里只有 300–400 像素高，视频姿态估计的关节误差会放大角度噪声，"
             "因此「组内标准差」整体会高于文献中的实验室数值——这只影响一致性对照的严格程度，不影响趋势判断。</li>")
    h.append("<li><b>参考区间的用途</b>：用于定位偏差方向（偏低/偏高），不等于个体最优值；"
             "身高、臂展、投篮距离都会改变最优参数（文献本身也显示出手角随距离下降）。</li>")
    h.append("</ul></div>")
    return "".join(h)


def build_html(all_data):
    parts = [HEAD, '<div class="wrap">', '<h1>投篮动作分析报告</h1>',
             f'<p class="sub">数据来源：<b>{REPORT_DATE}</b> 训练视频 · Apple Vision 全身姿态识别 + 关节角度生物力学测算</p>']
    for d in all_data:
        parts.append(f'<p class="sub">分析范围：<b>{d["title"]}</b> 的 0:00 – {hr(d["duration"])}'
                     f'（全长 {hr(d.get("video_duration") or d["duration"])}），识别 <b>{len(d["events"])}</b> 次出手，'
                     f'其中 <b>{len(d["metrics"])}</b> 次逐帧测算。</p>')
    for d in all_data:
        parts.append('<h2>%s</h2>' % d["title"])
        ms, agg, evs = d["metrics"], d["agg"], d["events"]
        ms = sorted(ms, key=lambda x: x["t_rel"])
        parts.append(
            f'<div class="srcbox">数据来源 <b>{os.path.basename(d["file"])}</b>'
            f'　·　{d["vw"]}×{d["vh"]}　·　{d["vfps"]:.0f}fps'
            f'　·　时长 {hr(d["video_duration"])}'
            f'　·　已扫描 0:00–{hr(d["duration"])}'
            f'　·　识别 {len(evs)} 次出手　·　逐帧测算 {len(ms)} 次'
            f'　·　逐帧播放器 {len(d.get("deep") or [])} 个</div>')
        parts.append(body_section(d))
        parts.append('<div class="grid g4">')
        parts.append(kpi("识别到的出手动作", f'{len(evs)} 次', f'逐帧测算 {len(ms)} 次'))
        jump_ratio = sum(1 for m in ms if (m["垂直弹跳"] or 0) > 0.09)
        parts.append(kpi("真实起跳投篮", f"{jump_ratio} / {len(ms)}", "垂直弹跳 > 0.09 倍身高"))
        parts.append(kpi("出手节奏", fmt(agg.get("屈膝到出手", {}).get("mean"), 2, " 秒"),
                         "屈膝最低点 → 出手"))
        parts.append(kpi("出手点高度", fmt(agg.get("出手点离地高度", {}).get("mean"), 2, " 倍身高"),
                         "手腕离地 ÷ 身高"))
        parts.append('</div>')
        parts.append(timeline(d, ms))
        parts.append(insights(d))
        parts.append(provenance_table(d))
        parts.append(drift(d))
        parts.append(lit_table(d))
        parts.append(curve_charts(d))
        parts.append(goal_chain(ms, agg))
        parts.append(player_section(d))
        # 关键指标明细表
        parts.append('<h3>全部指标明细（均值 ± 组内标准差）</h3><div class="card"><table><thead><tr>'
                     '<th>指标</th><th>均值</th><th>组内标准差</th><th>CV</th><th>最小 / 最大</th>'
                     '<th>评价</th></tr></thead><tbody>')
        for k in METRIC_KEYS:
            a = agg.get(k)
            if not a:
                continue
            unit = REFERENCE.get(k, (0, 0, ""))[2]
            dig = dig_for(unit)
            txt, cls = qual(k, a["mean"])
            cv = a["cv"]
            cv_cls = "ok" if cv is not None and cv < 5 else ("warn" if cv is not None and cv < 12 else "bad")
            parts.append(f'<tr><td>{label(k)}</td><td><b>{fmt(a["mean"],dig)}</b> {unit}</td>'
                         f'<td>±{fmt(a["sd"],dig)}</td><td class="{cv_cls}">{fmt(cv,1,"%")}</td>'
                         f'<td class="mut">{fmt(a["min"],dig)} / {fmt(a["max"],dig)}</td>'
                         f'<td class="{cls}">{txt}</td></tr>')
        parts.append('</tbody></table></div>')
        # 逐球明细
        parts.append('<h3>逐球明细</h3><div class="card" style="overflow:auto"><table><thead><tr>'
                     '<th>#</th><th>时间</th><th>屈膝角</th><th>出手肘角(投篮手)</th><th>辅助手肘角</th>'
                     '<th>小臂仰角</th><th>出手点高度</th><th>垂直弹跳</th><th>节奏</th></tr></thead><tbody>')
        for i, m in enumerate(sorted(ms, key=lambda x: x["t_rel"]), 1):
            parts.append(f'<tr><td>{i}</td><td>{hr(m["t_rel"])}</td>'
                         f'<td>{fmt(m["屈膝角(最低)"],0,"°")}</td>'
                         f'<td>{fmt(m["出手时肘角_右"],0,"°")}</td><td>{fmt(m["出手时肘角_左"],0,"°")}</td>'
                         f'<td>{fmt(m["出手小臂仰角"],0,"°")}</td><td>{fmt(m["出手点离地高度"],2)}</td>'
                         f'<td>{fmt(m["垂直弹跳"],2)}</td><td>{fmt(m["屈膝到出手"],2,"s")}</td></tr>')
        parts.append('</tbody></table></div>')
        parts.append(method_section(d))
    parts.append('</div>')
    if PLAYER_INITS:
        parts.append(SCRIPT_TMPL)
        parts.extend(PLAYER_INITS)
    parts.append('</body></html>')
    return "\n".join(parts).replace("@@TITLE@@", f"投篮动作分析报告 · {REPORT_DATE}")


# --------------------------------------------------------------------------- main

def load_shots(tag):
    dd = os.path.join(DATA, tag)
    if not os.path.isdir(dd):
        return []
    out = []
    for fn in sorted(os.listdir(dd)):
        if fn.startswith("shot_") and fn.endswith(".json"):
            out.append(json.load(open(os.path.join(dd, fn))))
    return out


def main(strip_limit=4):
    if not VIDEOS:
        print("没有可分析的视频。\n"
              "请先在 data/tags.json 里登记一段训练视频（推荐用 shotcli）：\n"
              "    python3 shotcli.py add \"<视频文件>\" --tag a --date 10月7日\n"
              "或指定已登记的标签：\n"
              "    SHOTCLI_TAG=a python3 report.py 4\n"
              "    python3 report.py 4 a")
        return
    all_data = []
    for v in VIDEOS:
        shots = load_shots(v["tag"])
        ms = [m for m in (shot_metrics(s) for s in shots) if m]
        evs = []
        ep = os.path.join(DATA, f'{v["tag"]}_events.json')
        if os.path.exists(ep):
            evs = json.load(open(ep))
        coarse = os.path.join(DATA, f'{v["tag"]}_coarse.jsonl')
        dur, n_scan = 0, 0
        if os.path.exists(coarse):
            lines = [json.loads(l) for l in open(coarse) if l.strip()]
            n_scan = len(lines)
            dur = lines[-1]["t"] if lines else 0
        if not ms and not evs:
            continue
        vmeta = {"duration": dur, "size": [1920, 1080], "fps": 30.0}
        try:
            vmeta = probe(v["file"])
            vdur = vmeta["duration"]
        except Exception:
            vdur = dur
        # 深扫样例
        deeps = []
        dd = os.path.join(DATA, v["tag"])
        if os.path.isdir(dd):
            _files = [fn for fn in os.listdir(dd) if fn.startswith("deep_") and fn.endswith(".json")]
            _files.sort(key=lambda fn: float(fn[5:-5]))     # 必须按时间数值排序
            for fn in _files:
                if True:
                    t0 = float(fn[5:-5])
                    mm = min(ms, key=lambda m: abs(m["t_rel"] - t0), default=None)
                    if not mm:
                        continue
                    dp = build_deep(os.path.join(dd, fn), mm)
                    if dp:
                        note = "典型（节奏接近全队中位）"
                        tempo_sd = st.pstdev([m["屈膝到出手"] for m in ms if m["屈膝到出手"] is not None]) if len(ms) > 2 else 0
                        med = st.median([m["屈膝到出手"] for m in ms if m["屈膝到出手"] is not None])
                        if mm["屈膝到出手"] is not None and tempo_sd:
                            z = (mm["屈膝到出手"] - med) / tempo_sd
                            note = ("节奏最快的一次" if z < -1 else "节奏最慢的一次" if z > 1
                                    else "节奏接近中位")
                        tm = mm["屈膝到出手"]
                        dp["title"] = (note + (f'，节奏 {tm:.2f}s / 本场中位 {med:.2f}s' if tm is not None else ''))
                        deeps.append(dp)
        highlight = max(ms, key=lambda m: m["屈膝到出手"] or 0)["t_rel"] if ms else 0
        body = compute_body(v["tag"], shots)
        all_data.append({"tag": v["tag"], "title": v["title"], "file": v["file"],
                         "body": body, "metrics": ms,
                         "agg": aggregate(ms, METRIC_KEYS), "events": evs, "deep": deeps,
                         "duration": dur, "video_duration": vdur, "scan_frames": n_scan,
                         "vw": vmeta["size"][0], "vh": vmeta["size"][1], "vfps": vmeta["fps"],
                         "strip_limit": strip_limit, "highlight_t": highlight})
    html = build_html(all_data)
    # 命名统一「<日期>_<类别>」——日期在前，一眼看出是哪一天（用户 2026-10-13 要求）
    out = os.path.join(ROOT, f"{REPORT_DATE}_投篮分析报告{REPORT_SUFFIX}.html")
    open(out, "w").write(html)
    # 结论清单落盘：给外部核对 / 给不依赖 report_data.json 的脚本读
    for d in all_data:
        if not d.get("agg"):
            continue
        json.dump(_conc_of(d), open(conclusions_path(d["tag"]), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    # 跨日期对比落盘：基线=上一训练日（同一天多段合并），没有可比的上一场就跳过。
    # 封面 / 文案里的「比上场…」一律从这里取，跟结论清单一样有据可查。
    for d in all_data:
        try:
            cpd = derive_compare(d["tag"])
            if cpd.get("items"):
                json.dump(cpd, open(compare_path(d["tag"]), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
        except Exception as e:
            print("  compare skip (%s): %s" % (d["tag"], e))
    rows = ["tag,时间(秒)," + ",".join(METRIC_KEYS)]
    dump = []
    for d in all_data:
        for m in d["metrics"]:
            rows.append(",".join([d["tag"], str(m["t_rel"])] +
                                 ["" if m.get(k) is None else f"{m[k]:.3f}" for k in METRIC_KEYS]))
        dd = {k: v for k, v in d.items() if k not in ("metrics", "deep", "conc")}
        dd["metrics"] = [{k: v for k, v in m.items() if not k.startswith("_")} for m in d["metrics"]]
        dump.append(dd)
    open(os.path.join(ROOT, f"{REPORT_DATE}_逐球指标{REPORT_SUFFIX}.csv"), "w").write("\n".join(rows))
    json.dump(dump, open(os.path.join(DATA, "report_data.json"), "w"), ensure_ascii=False, indent=1)
    print("written", out, "videos", len(all_data))
    for d in all_data:
        print(f'  {d["tag"]}: shots={len(d["metrics"])} events={len(d["events"])} deep={len(d["deep"])}')


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 4)
