# -*- coding: utf-8 -*-
"""生成「最佳球 vs 库里基准」对比页
用法: python compare_curry.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R

ROOT, DATA, BASE = R.ROOT, R.DATA, R.BASE
TAG = "d"
R.apply_tag(TAG)                                    # 取 R.REPORT_DATE 拼产物名（日期前缀）
OUT = os.path.join(ROOT, "%s_库里对比_最佳球.html" % R.REPORT_DATE)

# ---------------------------------------------------------------- 库里公开基准
CURRY = [
    ("出手时间（接球→离手）", "0.3–0.4 秒", "NBA 平均 0.54 秒",
     "Stats LLC 数据，见 Quick and Deadly: Analyzing Stephen Curry's Shot（Miraitalk）；"
     "basketballgem《Stephen Curry Shooting Form》同为 0.4s / 0.54s"),
    ("出手角度", "50–55°（近年 49–51°）", "普通球员约 45°",
     "Miraitalk 引 Stats LLC：50–55°；143 Basketball Haven：2020 年代后抬到 49–51°；"
     "另有「接近 45°」的另一种口径，见运动视界《量化解析 Curry》"),
    ("弧线最高点", "4.94–4.95 m", "NBA 平均 4.81 m",
     "Stats LLC：Curry 16.23 ft / 联盟 15.77 ft"),
    ("出手点高度", "≈2.7 m（约 9 ft）", "—",
     "camcatbooks 引用的二手数据，可信度低于上两条 Stats LLC 数据，仅作参考"),
    ("起跳高度", "「几乎不跳」，离地数英寸（约 5–8 cm）", "—",
     "Miraitalk：hardly jumps, just a few inches；basketballgem：deliberately low jump"),
    ("投篮节奏机制", "one-motion，上升途中出手（不到最高点）", "多数球员在最高点出手",
     "Miraitalk；Metro League《The Science of His Shot》"),
    ("收球（dip）", "投篮袋提到胸高、取消深蹲（只留约 10° 微沉）", "传统教学：沉到腰、深蹲",
     "143 Basketball Haven，引勇士投篮教练 Bruce Fraser"),
    ("出手瞬间", "肘完全伸展与手腕下压同步发生；跟随动作肘停在额头上方", "—",
     "basketballgem 第 8–9 步"),
]

# ---------------------------------------------------------------- 我方最佳球
shots = [s for s in R.load_shots(TAG) if abs(R.shot_metrics(s)["t_rel"] - 16.4) < 0.5]
best = R.shot_metrics(shots[0])
d = json.load(open(os.path.join(DATA, TAG, "deep_16.5.json")))
ball = json.load(open(os.path.join(DATA, TAG, "ball_16.5.json")))
meta, seq = d["meta"], d["seq"]
nf = len(seq)

EDGES = [("neck_1_joint", "head_joint", "#4ea1ff"),
         ("neck_1_joint", "right_shoulder_1_joint", "#ff6b6b"),
         ("right_shoulder_1_joint", "right_forearm_joint", "#ff6b6b"),
         ("right_forearm_joint", "right_hand_joint", "#ff6b6b"),
         ("neck_1_joint", "left_shoulder_1_joint", "#22d3ee"),
         ("left_shoulder_1_joint", "left_forearm_joint", "#22d3ee"),
         ("left_forearm_joint", "left_hand_joint", "#22d3ee"),
         ("right_shoulder_1_joint", "right_upLeg_joint", "#ff8fab"),
         ("right_upLeg_joint", "right_leg_joint", "#ff8fab"),
         ("left_shoulder_1_joint", "left_upLeg_joint", "#ff8fab"),
         ("left_upLeg_joint", "left_leg_joint", "#ff8fab"),
         ("right_upLeg_joint", "left_upLeg_joint", "#ff8fab"),
         ("right_leg_joint", "right_foot_joint", "#c792ea"),
         ("left_leg_joint", "left_foot_joint", "#c792ea")]


def skeleton_svg(rec, traj_pts=None, up_to=None):
    kp = rec.get("kp") or {}
    w, h = rec["cw"], rec["ch"]
    out = ['<svg viewBox="0 0 %d %d" preserveAspectRatio="none">' % (w, h)]
    if traj_pts:
        pts = [p for k, p in enumerate(traj_pts) if p and (up_to is None or k <= up_to)]
        if len(pts) > 1:
            dd = " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % (p["x"], p["y"])
                          for i, p in enumerate(pts))
            out.append('<path d="%s" fill="none" stroke="#ffd166" stroke-width="6" opacity=".32"/>' % dd)
            out.append('<path d="%s" fill="none" stroke="#ffd166" stroke-width="2.6"/>' % dd)
    for a, b, c in EDGES:
        p, q = kp.get(a), kp.get(b)
        if p and q:
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                       'stroke-width="2" stroke-linecap="round"/>' % (p[0], p[1], q[0], q[1], c))
    for v in kp.values():
        out.append('<circle cx="%.1f" cy="%.1f" r="2" fill="#e6edf3"/>' % (v[0], v[1]))
    out.append("</svg>")
    return "".join(out)


def strip_html():
    fit = ball["fit"]
    rel = ball["release_i"]
    ks = [fit["pocket_i"], fit["pocket_i"] + 8, fit["pocket_i"] + 16,
          fit["pocket_i"] + 24, rel - 6, rel]
    ks = [k for k in ks if 0 <= k < nf]
    labels = ["收球点", "起球", "举球", "过肩", "接近出手", "出手"]
    out = ['<div class="strip">']
    for k, lab in zip(ks, labels):
        rec = seq[k]
        out.append('<div class="frame"><img src="frames/%s/%s" alt="">%s'
                   '<div class="lbl">%s · %+.2fs</div></div>'
                   % (TAG, rec["img"], skeleton_svg(rec, ball["pts"], k),
                      lab, rec["t"] - meta["t_center"]))
    out.append("</div>")
    return "".join(out)


def traj_chart():
    pts = [(p.get("fx", p["x"]), p.get("fy", p["y"])) for p in ball["pts"] if p]
    if len(pts) < 4:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1 = min(xs) - 40, max(xs) + 40
    y0, y1 = min(ys) - 40, max(ys) + 40
    W, H = 460, 300

    def sx(x):
        return 30 + (x - x0) / (x1 - x0) * (W - 60)

    def sy(y):
        return H - 30 - (y - y0) / (y1 - y0) * (H - 60)

    poly = " ".join("%.1f,%.1f" % (sx(x), sy(y)) for x, y in pts)
    fit = ball["fit"]
    sc = best.get("scale_px") or 1.0
    return ('<svg viewBox="0 0 %d %d" style="width:100%%;max-width:%dpx">' % (W, H, W)
            + '<rect x="0" y="0" width="%d" height="%d" rx="8" fill="#0f1620"/>' % (W, H)
            + '<polyline points="%s" fill="none" stroke="#ffd166" stroke-width="8" opacity=".28"/>' % poly
            + '<polyline points="%s" fill="none" stroke="#ffd166" stroke-width="3"/>' % poly
            + '<circle cx="%.1f" cy="%.1f" r="5" fill="#8b949e"/>' % (sx(pts[0][0]), sy(pts[0][1]))
            + '<circle cx="%.1f" cy="%.1f" r="5" fill="#ff6b6b"/>' % (sx(pts[-1][0]), sy(pts[-1][1]))
            + ('<text x="30" y="24" fill="#e3b341" font-size="12">灰点=收球点　红点=出手点　'
               '最大横向偏移 %dpx（≈%.0fcm）</text>' % (fit["dev_px"], fit["dev_px"] / sc))
            + '</svg>')


MINE = [
    ("出手点离地高度", "216 cm（1.29 倍身高）", "参考 ≈2.7 m（1.44 倍身高）",
     "绝对值不可比（身高不同），看倍数：你 1.29，库里约 1.44。你的倍数已落在文献职业区间 1.31–1.38 附近。"),
    ("起跳高度", "31 cm（踝口径，去踝跖屈约 25 cm）", "约 5–8 cm（「几乎不跳」）",
     "<b>最大差异</b>。库里用腿做「节奏」不做「动力」，让上肢独立完成投篮；你靠 30cm 起跳把出手点抬上去。"
     "好处是省上肢力，代价是弹跳本身的波动会直接传导到出手点。"),
    ("出手节奏", "0.40 秒（屈膝最低点→出手）", "0.3–0.4 秒（收球袋→离手）",
     "数值接近但<b>口径不同</b>（我的窗口跨了收球+举球+出手），不能说已达标。真正可比的一条："
     "他 0.4s 是每次都一样，你 7 球在 0.40–0.53s 之间。"),
    ("出手小臂仰角", "76.6°", "出手角 50–55°（弧线角，与手臂角不同口径）",
     "不能直接比。但有意义的是 76.6° 已贴近文献「精英组 78.7°」（PMC9277478），说明手臂伸展方向是对的。"),
    ("出手肘角（投篮手）", "150°", "肘完全伸展与手腕下压同步发生",
     "库里强调肘伸展与压腕同时发生。你 150° 接近伸直但未到 180°，属于「推出去」多、「甩出去」少的区间。"),
    ("收球肘角", "65.7°", "投篮袋提到胸高、取消深蹲",
     "65.7° 落在文献职业预备期 58–64° 上沿，收球折叠到位，这一点你和库里的路子一致。"),
    ("屈膝深度", "102°（膝角最低点）", "深蹲取消，只留微沉",
     "你的 102° 比文献职业预备期 112–121° 更深。深蹲本身不坏，但叠加高弹跳会拖慢节奏，"
     "且每次下沉深度成为新变量。"),
    ("身体水平位移", "0.13 倍身高（≈22 cm）", "—",
     "库里没有公开的同口径数据。你这一球是 7 球里最小的之一，属好的一侧。"),
]

CONCLUSION = [
    ("你是「跳投」，库里是「推投」",
     "他的腿只打节奏（弹跳 5–8cm），你靠 30cm 弹跳把出手点抬上去。你的出手点里有一大半来自弹跳，"
     "所以 <b>弹跳的波动（本场 CV 7–31%）会直接传导到出手点</b>。不是要你改成一跳不跳，"
     "而是要把起跳高度当作一个要练稳定的指标，而不是随意发挥。"),
    ("你最该练的不是出手点高度，是「节奏的绝对稳定」",
     "库里最狠的一条不是 0.4s 这个数字，而是<b>每次都一样</b>。你的 7 球里节奏 0.40–0.53s，跨度 26%。"
     "出手点已经稳（CV 0.7%），节奏是你现在最大的变量。"),
    ("出手角（50–55°）是唯一测不了、但最该对标的一项",
     "这正是库里弧线高的来源。要测它必须<u>侧面 90° 机位</u>——这也是下次拍摄最值钱的一个改动。"),
]

PRESCRIPTION = [
    "**节拍器投篮**：把「收球点 → 出手」锁在 0.4 秒（用手机节拍器 150bpm 或口令），20 球 × 4 组，"
    "目标不是投进而是每次节奏一致。",
    "**同口径重算节奏**：我可以把节奏口径改成「收球点→离手」（和库里一致），重算你这 7 球，"
    "看真实差距是 0.1s 还是 0.02s。",
    "**侧向 90° 机位拍一组**：只有这个角度能量出真实出手角，才能和库里的 50–55° 直接对比。",
    "**起跳一致性**：跳箱或单脚跳定点，练「同一个起跳高度」而不是「跳得更高」。",
]

BODY = []
BODY.append('<h1>最佳球 vs 库里投篮基准 · 对比</h1>')
BODY.append('<p class="sub">最佳球选取：9 月 13 日 7 次出手按 6 项指标加权打分'
            '（起球轨迹直度 25% / 小臂仰角 20% / 出手肘角 20% / 屈膝深度 15% / 出手点高度 10% / 节奏 10%），'
            '<b>第 2 次出手（0:16）得分最高（67.4 分）</b>。</p>')
BODY.append('<div class="srcbox">对比对象：小红书「灯火篮球」《库里投篮姿势解析》'
            '（页面本身是视频、<b>没有给出量化数据</b>）。所以库里一侧改用<b>公开生物力学数据</b>，'
            '逐条标注出处；我方数据来自本机视频分析（Apple Vision 19 点姿态 + 33 项关节指标 + 黑球轨迹跟踪）。</div>')

BODY.append('<h2>一、我方最佳球（0:16，第 2 次出手）</h2><div class="card">')
BODY.append(strip_html())
BODY.append('<p class="mut">黄线是这一球的<b>黑球球心轨迹</b>（收球点 → 出手），随帧推进逐段显示；'
            '照片上的骨架是同一帧的姿态识别结果。</p></div>')

BODY.append('<h2>二、起球轨迹（全画幅）</h2><div class="card">')
BODY.append(traj_chart())
BODY.append('<p class="mut">竖轴向上为升高方向。灰点是收球点（最低处），红点是出手点。'
            '这条黄色轨迹线偏离首尾连线越多，说明球从收球位到出手位越不"直"。</p></div>')

BODY.append('<h2>三、逐项对比</h2>')
BODY.append('<div class="card"><table><thead><tr><th>项目</th><th>我方最佳球</th>'
            '<th>库里（公开数据）</th><th class="l">解读</th></tr></thead><tbody>')
for k, a, b, c in MINE:
    BODY.append('<tr><td>%s</td><td><b>%s</b></td><td>%s</td><td class="l mut">%s</td></tr>' % (k, a, b, c))
BODY.append('</tbody></table></div>')

BODY.append('<h2>四、库里基准（逐条标注出处）</h2>')
BODY.append('<div class="card"><table><thead><tr><th>项目</th><th>库里</th><th>参照值</th>'
            '<th class="l">出处</th></tr></thead><tbody>')
for k, v, ref, src in CURRY:
    BODY.append('<tr><td>%s</td><td><b>%s</b></td><td>%s</td><td class="l mut">%s</td></tr>' % (k, v, ref, src))
BODY.append('</tbody></table></div>')

BODY.append('<h2>五、结论</h2><div class="card"><ul>')
for t, txt in CONCLUSION:
    BODY.append('<li><b>%s</b>：%s</li>' % (t, txt))
BODY.append('</ul></div>')

BODY.append('<h2>六、训练处方</h2><div class="card"><ul>')
for t in PRESCRIPTION:
    BODY.append('<li>%s</li>' % t.replace("**", ""))
BODY.append('</ul></div>')

BODY.append('<h2>七、这次对比测不了什么</h2><div class="card"><ul>')
BODY.append('<li><b>出手角（50–55°）测不了</b>：库里最核心的可对标指标，但需要侧面 90° 机位把球出手后的'
            '飞行弧线完整拍进画面；侧后方机位看到的是球的飞行方向，弧线被压成一条线。</li>')
BODY.append('<li><b>弧线最高点（4.94 m）测不了</b>：同上，需要完整的出手后弹道。'
            '本次轨迹只覆盖到球离手为止（按你的要求）。</li>')
BODY.append('<li><b>出手节奏口径不同</b>：我的口是「屈膝最低点→出手」，库里的是「收球袋→离手」，'
            '两者窗口长度不同，不能直接判定谁快谁慢。</li>')
BODY.append('<li>库里「出手点高度 2.7 m」来自二手来源，可信度低于 Stats LLC 那组数据。</li>')
BODY.append('</ul></div>')

HTML = (R.HEAD.replace("@@TITLE@@", "最佳球 vs 库里基准对比")
        + '<div class="wrap">' + "".join(BODY) + '</div></body></html>')

if __name__ == "__main__":
    open(OUT, "w").write(HTML)
    print("written", OUT)
    print("最佳球:", best["t_rel"], "轨迹偏移", ball["fit"]["dev_rel"])
