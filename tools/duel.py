# -*- coding: utf-8 -*-
"""逐帧并排对比页：两球按出手帧对齐，同步播放，每帧显示指标差 + 库里基准刻度

用法: python duel.py <tag> <A秒> <B秒> [输出文件名]
例:   python duel.py d 16.5 85.5
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R

ROOT, DATA, FRAMES = R.ROOT, R.DATA, R.FRAMES
HEAD = R.HEAD.replace("@@TITLE@@", "逐帧并排对比")

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


def load(tag, t_sec):
    dp = os.path.join(DATA, tag, "deep_%.1f.json" % t_sec)
    bp = os.path.join(DATA, tag, "ball_%.1f.json" % t_sec)
    d = json.load(open(dp))
    b = json.load(open(bp)) if os.path.exists(bp) else {"pts": [], "release_i": len(d["seq"]) // 2}
    shots = [s for s in R.load_shots(tag) if abs(R.shot_metrics(s)["t_rel"] - t_sec) < 0.6]
    m = R.shot_metrics(shots[0]) if shots else {}
    return d, b, m


def build_side(tag, d, b, tag_letter, metric):
    """逐帧数据：与报告同口径（先做左右臂标签纠正，锚点用报告的出手帧）"""
    meta = d["meta"]
    seq = R.deflip(d["seq"])                     # 关键：与报告一致的左右臂纠正
    t_rel = metric.get("t_rel")
    rel_i = None
    if t_rel is not None:
        rel_i = min(range(len(seq)), key=lambda k: abs(seq[k]["t"] - t_rel))
    ball_by_i = {}
    for i, p in enumerate(b.get("pts") or []):
        if p:
            ball_by_i[i] = p
    out = []
    for i, r in enumerate(seq):
        f = R.frame_features(r) or {}
        kp = r.get("kp") or {}
        vs = [(f.get(s) or {}).get("knee") for s in ("right", "left")]
        vs = [v for v in vs if v is not None]
        out.append({
            "img": r["img"], "cw": r["cw"], "ch": r["ch"], "kp": kp,
            "t": round(r["t"] - meta["t_center"], 3),
            "knee": None if not vs else round(sum(vs) / len(vs)),
            "elbowR": (f.get("right") or {}).get("elbow"),
            "fore": (f.get("right") or {}).get("fore_ang"),
            "ball": ball_by_i.get(i),
        })
    return {"tag": tag_letter, "frames": out, "release_i": rel_i,
            "ball_release_i": b.get("release_i"),
            "fit": b.get("fit"), "t_center": meta["t_center"], "fps": meta["fps"]}


def main(tag, ta, tb, out_name=None):
    R.apply_tag(tag)          # 取 R.REPORT_DATE 拼产物名（日期前缀）
    dA, bA, mA = load(tag, ta)
    dB, bB, mB = load(tag, tb)
    A = build_side(tag, dA, bA, "A", mA)
    B = build_side(tag, dB, bB, "B", mB)

    # 两侧球轨迹（crop 坐标）
    def trail(side):
        return [[p["x"], p["y"]] if p else None for p in (side["frames"][i]["ball"] for i in range(len(side["frames"])))]

    js = {
        "EDGES": EDGES,
        "fps": A["fps"],
        "n": len(A["frames"]),
        "relA": A["release_i"], "relB": B["release_i"],
        "A": {"tag": tag, "frames": A["frames"], "trail": trail(A), "t": A["t_center"]},
        "B": {"tag": tag, "frames": B["frames"], "trail": trail(B), "t": B["t_center"]},
    }

    cards = []
    cards.append('<h1>逐帧并排对比</h1>')
    cards.append('<p class="sub">两球按<b>报告的出手帧（手腕过肩最高点）对齐</b>同步播放（拖动滑块或点播放）。'
                 '帧上叠加骨架与起球轨迹，右侧逐帧列出两侧指标与差值，并给出库里公开基准作为对照刻度。</p>')
    cards.append('<div class="srcbox">说明：库里那段视频拿不到（小红书页面是登录/JS 墙，'
                 '返回的是空壳，无视频地址），所以库里一侧以<b>公开数据基准</b>的形式参与对比，逐条标注出处；'
                 '两份视频画面均来自你的 9月13日.mp4。</div>')

    # 基准刻度
    cards.append('<h2>对照刻度（库里公开数据 + 文献）</h2><div class="card"><table><thead><tr>'
                 '<th>指标</th><th>库里 / 文献基准</th><th class="l">出处</th></tr></thead><tbody>')
    for k, v, s in [
        ("出手节奏", "0.4 秒（收球袋→离手）", "Stats LLC 转引；注意与「屈膝最低→出手」口径不同"),
        ("起跳高度", "5–8 cm（「几乎不跳」）", "Miraitalk；basketballgem"),
        ("出手角", "50–55°（需侧面机位才能测）", "Miraitalk 引 Stats LLC"),
        ("出手肘角", "接近完全伸直（134–180° 视口径）", "JSSM 134.3–138.2°；Frontiers 2023 166–168°"),
        ("出手小臂仰角", "精英 78.7±7.8°", "PMC9277478"),
        ("预备期屈膝角", "112–121°", "Cabarkapa 2022, J Funct Morphol Kinesiol"),
        ("收球肘角", "58–64°", "Cabarkapa 2022"),
    ]:
        cards.append('<tr><td>%s</td><td><b>%s</b></td><td class="l mut">%s</td></tr>' % (k, v, s))
    cards.append('</tbody></table></div>')

    cards.append('<h2>逐帧对比</h2>')
    cards.append('''
<div class="card">
  <div class="duelbar">
    <button class="chip cbtn" id="dprev">◀ 上一帧</button>
    <button class="chip cbtn" id="dplay">▶ 慢放</button>
    <button class="chip cbtn" id="dnext">下一帧 ▶</button>
    <select class="chip" id="drate">
      <option value="0">手动</option>
      <option value="0.15">0.15×</option>
      <option value="0.25" selected>0.25×</option>
      <option value="0.5">0.5×</option>
      <option value="1">1×</option>
    </select>
    <input type="range" id="drng" min="0" max="95" value="0" step="1" style="flex:1">
    <span class="tick" id="dtick"></span>
  </div>
  <div class="duel">
    <div class="dcol">
      <div class="dhead" id="hA"></div>
      <div class="dstage"><img id="imA" alt=""><div id="svA"></div><div class="dphase" id="phA"></div></div>
      <div class="dmet" id="metA"></div>
    </div>
    <div class="dcol">
      <div class="dhead" id="hB"></div>
      <div class="dstage"><img id="imB" alt=""><div id="svB"></div><div class="dphase" id="phB"></div></div>
      <div class="dmet" id="metB"></div>
    </div>
  </div>
  <div class="dnote" id="dnote"></div>
  <div class="dfilm"><div id="filmA" class="dfrow"></div><div id="filmB" class="dfrow"></div></div>
</div>''')

    extra_css = '''
.duelbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.tick{font-size:12px;color:var(--tx2);min-width:120px;text-align:right}
.duel{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.duel{grid-template-columns:1fr}}
.dhead{font-size:13px;font-weight:600;margin-bottom:6px}
.dstage{position:relative;background:#000;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.dstage img{width:100%;display:block}
.dstage svg{position:absolute;inset:0;width:100%;height:100%}
.dphase{position:absolute;left:10px;top:10px;background:#000a;border:1px solid #ffffff22;border-radius:6px;
padding:3px 9px;font-size:12px}
.dmet{font-size:12.5px;color:var(--tx2);margin-top:8px;line-height:1.9}
.dmet b{color:var(--tx);font-size:13.5px}
.dmet .delta{margin-left:6px;font-size:11.5px}
.dnote{margin-top:12px;font-size:12.5px;color:var(--tx2);background:var(--panel2);
border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.dfilm{margin-top:10px;display:grid;gap:6px}
.dfrow{display:flex;gap:3px;overflow-x:auto;padding:2px 0}
.dfrow img{width:44px;border-radius:4px;border:2px solid transparent;opacity:.7;cursor:pointer;flex:0 0 auto}
.dfrow img.cur{border-color:var(--accent);opacity:1}
'''
    script = '''
<script>
(function(){
const D = __DATA__;
let i = 0, timer = null;
const $ = id => document.getElementById(id);
const imA=$('imA'), imB=$('imB'), svA=$('svA'), svB=$('svB'), phA=$('phA'), phB=$('phB');
const metA=$('metA'), metB=$('metB'), note=$('dnote'), rng=$('drng'), tick=$('dtick');
const btn=$('dplay');
$('hA').innerHTML = 'A · 最佳球 · '+D.A.t.toFixed(1)+'s 出手';
$('hB').innerHTML = 'B · 待改进球 · '+D.B.t.toFixed(1)+'s 出手';
const off = D.relA - D.relB;          // B 侧偏移，使两侧出手帧对齐
function svgFor(side, k, upto){
  const f = side.frames[k]; if(!f) return '';
  const kp = f.kp || {};
  let s = '<svg viewBox="0 0 '+f.cw+' '+f.ch+'" preserveAspectRatio="none">';
  // 整条轨迹虚显（浅），已走过的部分实显
  let dAll='', dPast='';
  for(let j=0;j<side.trail.length;j++){ const p = side.trail[j]; if(!p) continue;
    dAll += (dAll?' L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1);
    if(j<=upto) dPast += (dPast?' L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1); }
  if(dAll) s += '<path d="'+dAll+'" fill="none" stroke="#ffd166" stroke-width="3" opacity=".22" stroke-dasharray="6 6"/>';
  if(dPast){ s += '<path d="'+dPast+'" fill="none" stroke="#ffd166" stroke-width="6" opacity=".3"/>';
             s += '<path d="'+dPast+'" fill="none" stroke="#ffd166" stroke-width="2.6"/>'; }
  for(const [a,b,c] of D.EDGES){ const p=kp[a], q=kp[b];
    if(p&&q) s += '<line x1="'+p[0].toFixed(1)+'" y1="'+p[1].toFixed(1)+'" x2="'+q[0].toFixed(1)+'" y2="'+q[1].toFixed(1)+
      '" stroke="'+c+'" stroke-width="2" stroke-linecap="round"/>'; }
  for(const k2 in kp){ s += '<circle cx="'+kp[k2][0].toFixed(1)+'" cy="'+kp[k2][1].toFixed(1)+'" r="2" fill="#e6edf3"/>'; }
  const cur = side.trail[k];
  if(cur) s += '<circle cx="'+cur[0].toFixed(1)+'" cy="'+cur[1].toFixed(1)+'" r="15" fill="none" stroke="#ffd166" stroke-width="3"/>';
  return s + '</svg>';
}
function phaseOf(side, k){
  const rel = side === D.A ? D.relA : D.relB;
  const d = k - rel;
  if(d < -0.35*D.fps) return '接球/持球';
  if(d < -0.18*D.fps) return '屈膝蓄力';
  if(d < 0) return '举球上升';
  if(d <= 0.08*D.fps) return '出手';
  return '随挥';
}
function fmt(v,u,dg){ return v==null ? '—' : (v.toFixed(dg||0)+(u||'')); }
function render(){
  const kA = Math.max(0, Math.min(D.n-1, i));
  const kB = Math.max(0, Math.min(D.n-1, i - off));
  const fa = D.A.frames[kA], fb = D.B.frames[kB];
  if(!fa || !fb) return;
  imA.src = 'frames/'+D.A.tag+'/'+fa.img;
  imB.src = 'frames/'+D.B.tag+'/'+fb.img;
  svA.innerHTML = svgFor(D.A, kA, kA);
  svB.innerHTML = svgFor(D.B, kB, kB);
  phA.textContent = phaseOf(D.A, kA) + '　' + (fa.t>=0?'+':'') + fa.t.toFixed(2) + 's';
  phB.textContent = phaseOf(D.B, kB) + '　' + (fb.t>=0?'+':'') + fb.t.toFixed(2) + 's';
  function block(f, other){
    const rows = [];
    const dl = (v,o,u,dg)=>{
      if(v==null) return '';
      const dv = (o==null) ? '' : ((v-o)>=0?'+':'') + (v-o).toFixed(dg||0) + (u||'');
      const cls = (o==null) ? '' : (Math.abs(v-o) > (dg===2?0.06:8) ? 'warn' : 'ok');
      return dv ? '<span class="delta '+cls+'">('+dv+')</span>' : '';
    };
    rows.push('膝角 <b>'+fmt(f.knee,'°')+'</b>'+dl(f.knee, other.knee, '°', 0));
    rows.push('投篮手肘角 <b>'+fmt(f.elbowR,'°')+'</b>'+dl(f.elbowR, other.elbowR, '°', 0));
    rows.push('小臂仰角 <b>'+fmt(f.fore,'°')+'</b>'+dl(f.fore, other.fore, '°', 0));
    const bh = (f.ball && f.kp && f.kp.right_hand_joint)
        ? (f.kp.right_hand_joint[1] - f.ball.y) : null;
    const ohand = (other.ball && other.kp && other.kp.right_hand_joint)
        ? (other.kp.right_hand_joint[1] - other.ball.y) : null;
    rows.push('球高于投篮手 <b>'+fmt(bh,' px',0)+'</b>'+dl(bh, ohand, 'px', 0));
    return rows.join('<br>');
  }
  metA.innerHTML = block(fa, fb);
  metB.innerHTML = block(fb, fa);
  const rel = D.relA, dRel = kA - rel;
  let verdict = '';
  if(Math.abs(dRel) <= 2){
    const dE = (fa.elbowR!=null && fb.elbowR!=null) ? fa.elbowR - fb.elbowR : null;
    const dK = (fa.knee!=null && fb.knee!=null) ? fa.knee - fb.knee : null;
    verdict = '出手帧对比：' + (dE!=null ? ('投篮手肘角 ' + (dE>=0?'A 更直 ':'B 更直 ') + Math.abs(dE).toFixed(0) + '°；') : '')
            + (dK!=null ? ('膝角 ' + (dK>=0?'A 更直 ':'B 更直 ') + Math.abs(dK).toFixed(0) + '°') : '');
    verdict += '。库里基准：出手瞬间肘接近完全伸直（134–180° 视口径）。';
  } else if(dRel < -0.35*D.fps) {
    verdict = '持球/接球阶段。库里在此阶段把投篮袋放在胸高、不做腰以下深蹲（143 Basketball Haven 引 Bruce Fraser），这一段是"取消多余下沉、压缩时间"的地方。';
  } else if(dRel < 0) {
    verdict = '举球上升阶段。库里是 one-motion：球从投篮袋到离手一路不停顿，且在上升到最高点之前就出手（Miraitalk）。这一段的停顿越少越好。';
  } else {
    verdict = '随挥阶段。库里要求肘完全伸展与手腕下压同时发生、跟随动作肘停在额头上方（basketballgem）。';
  }
  note.innerHTML = '<b>该帧解读：</b>' + verdict;
  tick.textContent = '第 '+(kA+1)+' / '+D.n+' 帧　　t='+fa.t.toFixed(2)+'s　(两侧按出手帧对齐)';
  document.querySelectorAll('#filmA img').forEach((im,k)=>im.classList.toggle('cur', k===kA));
  document.querySelectorAll('#filmB img').forEach((im,k)=>im.classList.toggle('cur', k===kB));
}
function stop(){ if(timer){clearInterval(timer); timer=null;} btn.textContent='▶ 慢放'; }
function start(){
  const rate = parseFloat($('drate').value) || 0;
  if(rate <= 0){ stop(); return; }
  if(timer) clearInterval(timer);
  timer = setInterval(()=>{ i = (i+1) % D.n; rng.value = i; render(); }, 1000/(D.fps*rate));
  btn.textContent = '⏸ 暂停';
}
function step(d){ stop(); i = Math.max(0, Math.min(D.n-1, i+d)); rng.value = i; render(); }
rng.addEventListener('input', e=>{ stop(); i = +e.target.value; render(); });
btn.addEventListener('click', ()=> timer ? stop() : start());
$('dprev').addEventListener('click', ()=>step(-1));
$('dnext').addEventListener('click', ()=>step(1));
$('drate').addEventListener('change', ()=>{ if(timer) start(); });
document.addEventListener('keydown', e=>{
  if(e.key==='ArrowLeft'){step(-1); e.preventDefault();}
  if(e.key==='ArrowRight'){step(1); e.preventDefault();}
});
// 缩略图胶片
[D.A, D.B].forEach((side, si)=>{
  const box = $(si===0?'filmA':'filmB');
  side.frames.forEach((f,k)=>{
    const im = document.createElement('img');
    im.src = 'frames/'+side.tag+'/'+f.img; im.loading='lazy';
    im.addEventListener('click', ()=>{ stop();
      i = Math.max(0, Math.min(D.n-1, k + (si===1?off:0))); rng.value = i; render(); });
    box.appendChild(im);
  });
});
render();
})();
</script>'''.replace("__DATA__", json.dumps(js, ensure_ascii=False))

    html = (HEAD + "<style>" + extra_css + "</style><div class='wrap'>"
            + "".join(cards) + "</div>" + script + "</body></html>")
    out = os.path.join(ROOT, out_name or ("%s_逐帧并排对比.html" % R.REPORT_DATE))
    open(out, "w").write(html)
    print("written", out)
    print("A 出手帧", A["release_i"], "B 出手帧", B["release_i"], "偏移", A["release_i"] - B["release_i"])
    return out


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "d"
    ta = float(sys.argv[2]) if len(sys.argv) > 2 else 16.5
    tb = float(sys.argv[3]) if len(sys.argv) > 3 else 85.5
    main(tag, ta, tb, sys.argv[4] if len(sys.argv) > 4 else None)
