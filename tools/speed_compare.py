# -*- coding: utf-8 -*-
"""球速对比页：LadderLab 库里投篮速度分析（参考视频） vs 我方最佳球
用法: python speed_compare.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R

ROOT = R.ROOT
TAG = "d"                                           # 对比基准取自 9月13日（tag d）的最佳球
R.apply_tag(TAG)                                    # 取 R.REPORT_DATE 拼产物名（日期前缀）
OUT = os.path.join(ROOT, "%s_球速对比_库里vs我.html" % R.REPORT_DATE)
REF_DIR = os.path.join(ROOT, "参考资料")

# 参考视频里读出的库里曲线（LadderLab 篮球实验室《斯蒂芬库里 常规距离三分投篮 球速度分析》）
CURRY_PTS = [
    ("收球起速", 0.48, "球在收球位几乎静止，随后开始加速"),
    ("第 1 次加速后", 2.28, "腿部蹬伸把球带起来（该阶段标注「加速↑」）"),
    ("第 2 次加速后", 4.41, "球已过胸/肩，进入举球段"),
    ("中途回落", 3.47, "**关键特征：速度回落**（标注「减速↓」）——起球过程在这里有一次「顿」"),
    ("出手瞬间", 7.78, "最后一段爆发（标注「加速↑」），峰值出现在离手瞬间"),
]

OURS = json.load(open(os.path.join(ROOT, "data", "d_speed.json")))
BEST_KEY = min(OURS, key=lambda k: abs(float(k) - 16.37))
BEST = OURS[BEST_KEY]


def curve_svg(curve, rel, pocket, peak, dip, start_v):
    pts = [(i, v) for i, v in enumerate(curve) if v is not None]
    if not pts:
        return ""
    lo = max(0, (pocket or 0) - 6)
    hi = min(len(curve), (rel or len(curve)) + 2)
    seg = [(i, curve[i]) for i in range(lo, hi) if curve[i] is not None]
    if not seg:
        return ""
    W, H = 560, 260
    x0, x1 = lo, hi - 1
    vmax = max(v for _, v in seg) * 1.25 or 1.0
    px = lambda i: 40 + (i - x0) / max(1, (x1 - x0)) * (W - 70)
    py = lambda v: H - 34 - v / vmax * (H - 64)
    poly = " ".join("%.1f,%.1f" % (px(i), py(v)) for i, v in seg)
    s = ['<svg viewBox="0 0 %d %d" style="width:100%%;max-width:%dpx">' % (W, H, W),
         '<rect x="0" y="0" width="%d" height="%d" rx="8" fill="#0f1620"/>' % (W, H)]
    # 网格
    for v in range(1, int(vmax) + 1):
        s.append('<line x1="40" y1="%.1f" x2="%d" y2="%.1f" stroke="#1e2b3a"/>' % (py(v), W - 30, py(v)))
        s.append('<text x="6" y="%.1f" fill="#5a6b7d" font-size="10">%d m/s</text>' % (py(v) + 3, v))
    # 库里参考点
    for name, v, _ in CURRY_PTS:
        if v <= vmax:
            s.append('<line x1="40" y1="%.1f" x2="%d" y2="%.1f" stroke="#e3b341" stroke-dasharray="4 4" opacity=".55"/>' % (py(v), W - 30, py(v)))
            s.append('<text x="%d" y="%.1f" fill="#e3b341" font-size="10">库里 %s</text>' % (W - 96, py(v) - 3, v))
    s.append('<polyline points="%s" fill="none" stroke="#4ea1ff" stroke-width="7" opacity=".3"/>' % poly)
    s.append('<polyline points="%s" fill="none" stroke="#4ea1ff" stroke-width="2.6"/>' % poly)
    if peak:
        s.append('<circle cx="%.1f" cy="%.1f" r="5" fill="#ff6b6b"/>' % (px(peak["i"]), py(peak["v"])))
        s.append('<text x="%.1f" y="%.1f" fill="#ff6b6b" font-size="11">峰值 %.1f m/s</text>' % (px(peak["i"]) - 10, py(peak["v"]) - 8, peak["v"]))
    if pocket is not None and x0 <= pocket <= x1:
        s.append('<line x1="%.1f" y1="34" x2="%.1f" y2="%d" stroke="#8b949e" stroke-dasharray="3 3"/>' % (px(pocket), px(pocket), H - 34))
        s.append('<text x="%.1f" y="30" fill="#8b949e" font-size="10">收球点</text>' % (px(pocket) + 4))
    if rel is not None and x0 <= rel <= x1:
        s.append('<line x1="%.1f" y1="34" x2="%.1f" y2="%d" stroke="#3fb950" stroke-dasharray="3 3"/>' % (px(rel), px(rel), H - 34))
        s.append('<text x="%.1f" y="30" fill="#3fb950" font-size="10">出手</text>' % (px(rel) + 4))
    s.append('<text x="40" y="%d" fill="#5a6b7d" font-size="10">← 收球点之前　　　→ 出手方向</text>' % (H - 8))
    s.append("</svg>")
    return "".join(s)


CHART_BLOCKS = []
CHART_BLOCKS.append('<h1>球速对比 · 库里（参考视频）vs 我方最佳球</h1>')
CHART_BLOCKS.append('<p class="sub">参考视频：B 站「LadderLab 篮球实验室」《斯蒂芬库里 常规距离三分投篮 '
                    '球速度分析》（BV1W3411n734，已下载到 <b>投篮分析/参考资料/curry_ref.mp4</b>，'
                    '1080×1920 竖屏 · 30fps · 19.5 秒）。</p>')
CHART_BLOCKS.append('<div class="srcbox">口径说明：参考视频注脚写明「展示球速在<b>该平面内</b>的分速度」——'
                    '也就是画面里量到的速度，不含纵深方向的移动。我方的速度用同一口径计算：'
                    '球心逐帧位移 ÷ 比例尺 × 帧率。所以两者是<b>同一量纲、可以对比</b>的。</div>')

CHART_BLOCKS.append('<h2>一、参考视频里的库里速度曲线</h2><div class="card">')
CHART_BLOCKS.append('<img src="参考资料/frames/ref_018.0.jpg" style="max-width:320px;border-radius:10px;'
                    'border:1px solid var(--line)" alt="库里球速分析">')
CHART_BLOCKS.append('<table style="margin-top:12px"><thead><tr><th>阶段</th><th>速度</th>'
                    '<th class="l">含义</th></tr></thead><tbody>')
for name, v, note in CURRY_PTS:
    CHART_BLOCKS.append('<tr><td>%s</td><td><b>%.2f m/s</b></td><td class="l mut">%s</td></tr>'
                        % (name, v, note.replace("**", "")))
CHART_BLOCKS.append('</tbody></table>')
CHART_BLOCKS.append('<p class="mut">库里这条曲线最重要的信息不是峰值 7.78 m/s，而是它<b>不单调</b>：'
                    '4.41 → 3.47 出现回落，再到 7.78 爆发。也就是说他的起球过程里有一段"减速"，'
                    '然后才是出手瞬间的爆发。这个形状本身就是他的节奏特征。</p>')
CHART_BLOCKS.append('<p class="mut">你还可以直接在下面播放参考视频看原片：</p>')
CHART_BLOCKS.append('<video controls preload="metadata" style="width:100%;max-width:340px;border-radius:10px;'
                    'border:1px solid var(--line)" src="参考资料/curry_ref.mp4"></video>')
CHART_BLOCKS.append('</div>')

CHART_BLOCKS.append('<h2>二、我方最佳球（0:16.4）的球速曲线</h2><div class="card">')
CHART_BLOCKS.append(curve_svg(BEST["curve"], BEST["rel"], BEST["pocket"], BEST["peak"],
                              BEST["dip"], BEST["start_v"]))
CHART_BLOCKS.append('<p class="mut">口径与参考视频相同（画面平面内分速度）。'
                    '起始 %.2f m/s（库里 0.48）、过肩段峰值 %.2f m/s、出手帧 %s。'
                    '比例尺 %.2f px/cm（全图口径）。</p>'
                    % (BEST["start_v"] or 0, BEST["peak"]["v"] if BEST["peak"] else 0,
                       BEST["rel"], BEST["px_per_cm"]))
CHART_BLOCKS.append('<p class="mut"><b>读这条曲线前必须知道的噪声水平</b>：这台机位下球员只有 1.97 px/cm，'
                    '球心检测每次有 ±2px 抖动，换算成速度是 <b>±0.6 m/s／帧对</b>；'
                    '做了 3 帧滑动平均后噪声降到约 ±0.3 m/s。所以曲线在 1.0–1.7 之间的上下抖动'
                    '<b>大概率是噪声，不是你的节奏</b>，不能像库里那条曲线一样逐点解读。</p>')
CHART_BLOCKS.append('</div>')

CHART_BLOCKS.append('<h2>三、能比的和不能比的</h2><div class="card"><table><thead><tr>'
                    '<th>项目</th><th>库里</th><th>我方最佳球</th><th class="l">结论</th></tr></thead><tbody>')
rows = [
    ("收球起速", "0.48 m/s", "%.2f m/s" % (BEST["start_v"] or 0),
     "<b>可比，而且接近</b>。两者都从近乎静止开始，说明收球→启动的衔接节奏相近。"),
    ("过肩段速度", "4.41 m/s（回落前）", "%.2f m/s（峰值，含噪声）" % (BEST["peak"]["v"] if BEST["peak"] else 0),
     "库里是三分（6.75m+），我方是近中距，<b>距离不同不能直接比大小</b>。可比的只有「有没有加速」这个趋势。"),
    ("出手瞬间峰值", "7.78 m/s", "测不到", 
     "<b>我方测不到</b>：球出手前与深色树背景融为一体（黑球+暗背景），出手后 1–2 帧即离开裁剪框。"
     "这是本次最遗憾的缺口，也是下次拍摄要解决的问题。"),
    ("速度是否单调", "不单调，中间回落（4.41 → 3.47）", "无法判断（噪声 ±0.3 m/s 大于特征幅度）",
     "「先减速再爆发」是他节奏的核心特征。要判断你有没有同样的特征，需要先把噪声压下来。"),
]
for k, a, b, c in rows:
    CHART_BLOCKS.append('<tr><td>%s</td><td><b>%s</b></td><td><b>%s</b></td><td class="l mut">%s</td></tr>'
                        % (k, a, b, c.replace('"', '「', 1).replace('"', '」', 1)))
CHART_BLOCKS.append('</tbody></table></div>')

CHART_BLOCKS.append('<h2>四、要把这一项做到和参考视频一样，缺什么</h2><div class="card"><ul>')
CHART_BLOCKS.append('<li><b>侧面 90° 机位</b>：这一条同时解决两个问题——出手角（50–55°）和出手瞬间速度。'
                    '库里那条曲线之所以能测出 7.78 m/s，是因为球出手后仍在画面内、且背景干净。</li>')
CHART_BLOCKS.append('<li><b>球要留在画面里直到下落到网</b>：我方跟踪是"只画出手段"，出口即断，'
                    '所以峰值天然测不到。若下次拍摄让球飞完，出手瞬间峰值就能补上。</li>')
CHART_BLOCKS.append('<li><b>球对着亮天空或单一背景</b>：黑球对亮背景是对比度最高的组合，'
                    '这也是参考视频能画出一条干净曲线的原因；我方球在深色围网/树前融合掉了。</li>')
CHART_BLOCKS.append('<li><b>人物放大到占画面 60–70%</b>：现在 1.97 px/cm，噪声 ±0.6 m/s；'
                    '放大一倍后（约 4 px/cm）噪声降到 ±0.3 m/s，才有可能分辨"有没有那次减速"。</li>')
CHART_BLOCKS.append('</ul></div>')

html = (R.HEAD.replace("@@TITLE@@", "球速对比 · 库里 vs 我")
        + '<div class="wrap">' + "".join(CHART_BLOCKS) + '</div></body></html>')
open(OUT, "w").write(html)
print("written", OUT)
print("最佳球 key:", BEST_KEY, "起始", BEST["start_v"], "峰值", BEST["peak"], "px/cm", BEST["px_per_cm"])
