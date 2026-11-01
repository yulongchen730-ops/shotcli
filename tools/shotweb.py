#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shotweb —— 投篮分析 Web 服务（B 路线 · Web 层）

把 shotcli 的流水线包成一个网页：上传视频 → 选参数 → 后台跑 → 看进度 → 打开报告。

设计要点
--------
1. **只用标准库**（http.server / json / threading），任何 python3 都能起。
   真正干活的解释器仍然由 `shotcli.json` 的 `python` 字段决定 —— 这一层不碰环境的事。
2. **单工作线程**：Vision 姿态估计是 CPU 密集的（Intel 双核），并发跑只会互相拖慢。
   任务排队，一次一个。
3. **上传走原始字节流**：Python 3.13 已移除 `cgi`，没有现成的 multipart 解析；
   而且视频动辄几百 MB，不该整个读进内存。所以前端用 XHR 把 File 当 body 直接 POST，
   参数放 query，服务端分块落盘。
4. **静态路由以项目根为界**：报告里用相对路径引 `frames/<tag>/xxx.jpg`，
   所以报告必须挂在 `/files/<项目内相对路径>` 下，相对引用才能解析。
   路径做 realpath 校验，防目录穿越。

用法
----
    python3 shotweb.py                # 起在 127.0.0.1:8765，浏览器打开
    python3 shotweb.py --port 9000
    python3 shotweb.py --host 0.0.0.0 # 想让手机/别的机器访问（注意安全）
"""
import argparse
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(TOOLS)                     # 投篮分析/
TRN = os.path.dirname(PROJ)                       # 训练记录/
INCOMING = os.path.join(PROJ, "incoming")
WEBDIR = os.path.join(PROJ, "data", "web")
JOBS_DB = os.path.join(WEBDIR, "jobs.json")
PIDFILE = os.path.join(WEBDIR, "shotweb.pid")
LOGFILE = os.path.join(WEBDIR, "shotweb.log")

sys.path.insert(0, TOOLS)
import shotcli as SC                              # noqa: E402  （只用它的读取类 helper，不碰重活）

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")

STAGE_LABEL = {"scan": "粗扫", "shots": "逐球精算", "deep": "逐帧深扫", "top3": "评分排名",
               "report": "报告", "assets": "配图", "video": "视频", "cover": "封面"}
# 上传界面可选的“跑到哪一步”
TO_CHOICES = [("scan", "只粗扫（先探路，最省时间）"), ("report", "只出报告（推荐）"),
              ("assets", "+ 配图"), ("video", "+ 慢放视频"), ("full", "全部（含封面）")]

_LOCK = threading.Lock()
_Q = queue.Queue()
JOBS = {}


# ------------------------------------------------------------------ 任务存储
def load_jobs():
    global JOBS
    if os.path.exists(JOBS_DB):
        try:
            JOBS = json.load(open(JOBS_DB, encoding="utf-8"))
        except Exception:
            JOBS = {}
    # 上次退出时还在跑的任务，重启后标记为中断（工作线程已不在）
    for j in JOBS.values():
        if j.get("status") in ("queued", "running"):
            j["status"] = "interrupted"
            j["log"] = (j.get("log") or "") + "\n[服务重启，任务中断]\n"
    return JOBS


def save_jobs():
    os.makedirs(WEBDIR, exist_ok=True)
    tmp = JOBS_DB + ".tmp"
    json.dump(JOBS, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, JOBS_DB)


def new_job(**kw):
    jid = time.strftime("%m%d-%H%M%S")
    n = 2
    while jid in JOBS:
        jid = time.strftime("%m%d-%H%M%S") + "-%d" % n
        n += 1
    j = {"id": jid, "created": time.strftime("%F %T"), "status": "queued",
         "log": "", "pct": 0, "stage": "", "report": None, "error": None}
    j.update(kw)
    with _LOCK:
        JOBS[jid] = j
        save_jobs()
    return jid


def upd(jid, **kw):
    with _LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        j.update(kw)
        save_jobs()


def app_log(jid, line):
    with _LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        j["log"] = (j.get("log", "") + line)[-20000:]   # 只留最近 20KB


# ------------------------------------------------------------------ 工作线程
STAGE_RE = re.compile(r"^▶\s*(\S+)")
# 各阶段在总进度里的权重（按实测耗时占比大致给）
STAGE_W = {"scan": 0.45, "shots": 0.10, "deep": 0.35, "top3": 0.01,
           "report": 0.02, "assets": 0.03, "video": 0.03, "cover": 0.01}


def run_job(j):
    jid = j["id"]
    tag = j["tag"]
    upd(jid, status="running", started=time.strftime("%F %T"))
    interp = SC.interp(SC.load_cfg())
    args = [interp, os.path.join(TOOLS, "shotcli.py"), "run", tag, "--to", j.get("to") or "report"]
    if j.get("suffix"):
        args += ["--suffix", j["suffix"]]
    app_log(jid, "▶ 开始：%s\n" % " ".join(args[1:]))
    t0 = time.time()
    try:
        # start_new_session：让整个子进程组独立，取消时能连带孙进程（scan.py/deep.py）一起杀
        p = subprocess.Popen(args, cwd=TOOLS, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             start_new_session=True,
                             # _child_env：剔掉 __PYVENV_LAUNCHER__，否则子进程里 venv 的包全找不到
                             env=SC._child_env({"SHOTCLI_TAG": tag}))
        upd(jid, pid=p.pid)
        done_w = 0.0
        for line in p.stdout:
            app_log(jid, line)
            m = STAGE_RE.match(line.strip())
            if m:
                s = m.group(1)
                if s in STAGE_W:
                    upd(jid, stage=s, stage_label=STAGE_LABEL.get(s, s),
                        pct=int(min(99, (done_w + 0.02) * 100)))
                    done_w += STAGE_W[s]
            upd(jid, elapsed=int(time.time() - t0))
        p.wait()
        rc = p.returncode
    except Exception as e:
        app_log(jid, "\n[启动失败] %s\n" % e)
        rc = -1
    # 结果判定：看 report.py 到底写出了什么
    info = SC.load_tags().get(tag, {})
    rep = SC.report_of(tag, info)[0]
    ok_rep = os.path.exists(rep) and "report" in (SC.load_stamp(tag) or {})
    if rc == 0:
        upd(jid, status="done", pct=100, stage="", report=rep if ok_rep else None,
            elapsed=int(time.time() - t0), finished=time.strftime("%F %T"))
        app_log(jid, "\n✓ 完成\n")
    else:
        stopped = "■ 在阶段" in j.get("log", "")
        upd(jid, status="stopped" if stopped else "failed",
            report=rep if ok_rep else None, rc=rc, pid=None,
            elapsed=int(time.time() - t0), finished=time.strftime("%F %T"))
        app_log(jid, "\n■ 结束（退出码 %s）\n" % rc)


def worker():
    while True:
        jid = _Q.get()
        j = JOBS.get(jid)
        if j and j.get("status") == "queued":
            try:
                run_job(j)
            except Exception as e:
                upd(jid, status="failed", error=str(e))
                app_log(jid, "[工作线程异常] %s\n" % e)
        _Q.task_done()


# ------------------------------------------------------------------ 上传
def next_tag():
    """下一个可用标签：先 a–z，用完再 aa–zz。

    踩过：只给 a–z 时，第 27 次上传直接抛异常、返回 500 —— 而且是在「已经收下文件、
    也写进 tags.json 之前」才炸，用户只看到一句 500，不知道发生了什么。
    """
    t = SC.load_tags()
    for c in "abcdefghijklmnopqrstuvwxyz":
        if c not in t:
            return c
    for a in "abcdefghijklmnopqrstuvwxyz":
        for b in "abcdefghijklmnopqrstuvwxyz":
            tag = a + b
            if tag not in t:
                return tag
    raise RuntimeError("标签用完了（a–z / aa–zz 共 702 个）—— 该清理一下 data/tags.json 了")


def guess_date(fn):
    m = re.match(r"^(\d{1,2}月\d{1,2}日)", fn)
    return m.group(1) if m else time.strftime("%-m月%-d日")


def save_stream(rfile, length, dest):
    """分块落盘，别把整个视频读进内存。"""
    left = length
    with open(dest, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    return length - left


def duration_of(path):
    """用配置里的解释器探一下时长（失败不影响主流程）。"""
    try:
        interp = SC.interp(SC.load_cfg())
        code = ("import sys;sys.path.insert(0,%r);from bbkit import probe;import json;"
                "print(json.dumps(probe(sys.argv[1])))" % TOOLS)
        r = subprocess.run([interp, "-c", code, path], capture_output=True, text=True,
                           timeout=60, env=SC._child_env())
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ 页面
CSS = """
:root{--bg:#0e1622;--card:#16212f;--card2:#1d2a3a;--line:#28374a;--tx:#e8eef7;--tx2:#93a4ba;
--acc:#ffc400;--ok:#3ecf8e;--bad:#ff6b6b;--warn:#ffb020}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,"PingFang SC",sans-serif}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
header{padding:22px 26px 16px;border-bottom:1px solid var(--line);display:flex;
 align-items:baseline;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:20px;letter-spacing:.3px}
header .sub{color:var(--tx2);font-size:12.5px}
main{max-width:1080px;margin:0 auto;padding:20px 20px 70px;display:grid;gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.card h2{margin:0 0 12px;font-size:15px;color:var(--acc);font-weight:600}
.drop{border:2px dashed var(--line);border-radius:12px;padding:26px;text-align:center;
 color:var(--tx2);cursor:pointer;transition:.15s}
.drop.hot{border-color:var(--acc);background:#1a2434;color:var(--tx)}
.drop b{color:var(--tx)}
.row{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0 0}
label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--tx2)}
input,select{background:var(--card2);border:1px solid var(--line);color:var(--tx);
 border-radius:8px;padding:8px 10px;font-size:13.5px;min-width:118px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--acc)}
button{background:var(--acc);color:#20180a;border:0;border-radius:9px;padding:10px 22px;
 font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:transparent;color:var(--tx2);border:1px solid var(--line);font-weight:400}
.bar{height:7px;background:var(--card2);border-radius:4px;overflow:hidden;margin:10px 0 4px}
.bar>i{display:block;height:100%;background:var(--acc);width:0;transition:.3s}
.job{border-top:1px solid var(--line);padding:13px 0}
.job:first-of-type{border-top:0}
.job .hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:11.5px;padding:2px 9px;border-radius:20px;background:var(--card2);color:var(--tx2)}
.pill.run{background:#3a2f10;color:var(--acc)}
.pill.done{background:#12321f;color:var(--ok)}
.pill.bad{background:#3a1c1c;color:var(--bad)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{font-size:11.5px;padding:2px 9px;border-radius:6px;background:var(--card2);color:var(--tx2)}
.chip.on{background:#12321f;color:var(--ok)}
.chip.cur{background:#3a2f10;color:var(--acc)}
pre{background:#0b1420;border:1px solid var(--line);border-radius:9px;padding:11px 13px;
 max-height:260px;overflow:auto;font-size:12px;line-height:1.55;color:#b9c8dc;
 white-space:pre-wrap;word-break:break-all;margin:9px 0 0}
.hint{color:var(--tx2);font-size:12.5px;margin:6px 0 0}
.tag{display:flex;align-items:center;gap:9px;padding:8px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.tag:first-of-type{border-top:0}
.tag .n{font-weight:600;color:var(--acc);width:20px}
.tag .d{color:var(--tx2);width:76px;font-size:12.5px}
.tag .t{flex:1;min-width:180px;font-size:13px}
"""

JS_COMMON = """
function el(id){return document.getElementById(id)}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function human(n){return n<60?n+'s':(n<3600?Math.floor(n/60)+'m'+(n%60)+'s'):Math.floor(n/3600)+'h'+Math.floor(n%3600/60)+'m'}
"""


def hesc(x):
    """服务端渲染用的最小 HTML 转义（别和 JS 里那个 esc 混了）。"""
    return ("" if x is None else str(x)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page_index(tags, jobs):
    opts = "".join('<option value="%s">%s</option>' % (v, t) for v, t in TO_CHOICES)
    # 身高 / 投篮手 默认值来自用户档案（`shotcli setup` 写的 data/profile.json）。
    # 没建档 → 留空 + 红条提示：绝不预填一个"看起来像真值"的 175。
    cfg = SC.load_cfg()
    hp, hsrc = SC.profile_height(cfg)
    hd = SC.profile_hand(cfg)
    if hsrc == "profile":
        prof_html = ('<p class="hint">档案：身高 <b>%.0f cm</b> · 投篮手 %s —— '
                     '要改就在命令行跑 <code>python3 tools/shotcli.py setup</code>。</p>'
                     % (hp, "右手" if hd == "right" else "左手"))
        hval, hph = "%.0f" % hp, ""
    else:
        prof_html = ('<p class="hint" style="background:rgba(224,82,82,.12);'
                     'border-left:4px solid #e05252;padding:8px 12px;border-radius:6px">'
                     '⚠ <b>你还没设置身高</b>：这里留空的话会按默认 %.0f cm 估算，'
                     '报告里所有 cm 值（出手点离地高度、垂直弹跳、各段长度）都会跟着错。'
                     '先在命令行跑一次 <code>python3 tools/shotcli.py setup</code>，'
                     '或直接在下面填。</p>' % hp)
        hval, hph = "", ' placeholder="不填按 %.0f 估算"' % hp
    ident = ('<label>身高 cm<input id="height" value="%s" size="6"%s></label>'
             '<label>投篮手<select id="hand">%s</select></label>'
             % (hval, hph,
                "".join('<option value="%s"%s>%s</option>'
                        % (v, " selected" if v == hd else "", t)
                        for v, t in (("right", "右手"), ("left", "左手")))))
    rows = []
    for k in sorted(tags):
        info = tags[k]
        st = SC.stage_status(k)
        rep = SC.report_of(k, info)[0]
        # 报告名只带日期，同一天多个 tag 会指向同一个文件 —— 收紧一下：
        # 必须有球、文件存在，且要么自己构建过、要么这一天没有别的 tag 构建过。
        links = []
        same_date = [o for o, oi in tags.items()
                     if o != k and (oi.get("date") or o) == (info.get("date") or k)]
        claimed = any("report" in SC.load_stamp(o) for o in same_date)
        if (len(SC.shot_files(k)) > 0 and os.path.exists(rep)
                and ("report" in SC.load_stamp(k) or not claimed)):
            links.append('<a href="/files/%s" target="_blank">报告</a>' % urllib.parse.quote(os.path.basename(rep)))
        done = [s for s in SC.STAGES if st.get(s)]
        rerun = ('<select id="rto_%s" style="min-width:96px;padding:5px 8px">'
                 '<option value="report">重出报告</option>'
                 '<option value="assets">+配图</option>'
                 '<option value="video">+视频</option>'
                 '<option value="full">全部</option></select>'
                 '<button class="ghost" style="padding:5px 12px" '
                 'onclick="rerun(\'%s\')">重跑</button>' % (k, k))
        rows.append(
            '<div class="tag"><span class="n">%s</span><span class="d">%s</span>'
            '<span class="t">%s</span><span class="pill">球 %d</span>'
            '<span class="pill">%s</span>%s %s</div>'
            % (k, info.get("date", "?"), hesc(info.get("title") or info.get("video") or ""),
               len(SC.shot_files(k)), "/".join(done) if done else "未扫",
               " ".join(links), rerun))
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>投篮分析 · 上传</title><style>%s</style></head><body>
<header><h1>投篮分析</h1><span class="sub">上传视频 → 后台逐帧分析 → 出报告</span></header>
<main>
<div class="card">
  <h2>1 · 选视频</h2>
%s
  <div class="drop" id="drop"><b>把视频拖进来</b>，或点击选择文件<br>
     <span style="font-size:12px">支持 mp4 / mov / m4v</span></div>
  <input type="file" id="file" accept="video/*,.mp4,.mov,.m4v" style="display:none">
  <div class="row">
    <label>日期<input id="date" placeholder="10月7日"></label>
%s
    <label>本段距离<input id="distance" placeholder="罚球线 / 三分线"></label>
    <label>标题<input id="title" placeholder="留空自动生成" size="20"></label>
  </div>
  <div class="row">
    <label>报告后缀<input id="suffix" placeholder="第1段（同一天多段时必填）" size="16"></label>
    <label>跑到哪一步<select id="to">%s</select></label>
    <label style="justify-content:flex-end"><button id="go" disabled>开始分析</button></label>
  </div>
  <div class="bar"><i id="ubar"></i></div>
  <p class="hint" id="uhint">还没选文件。</p>
</div>
<div class="card">
  <h2>2 · 任务</h2>
  <div id="jobs"></div>
  <p class="hint">分析是 CPU 密集的（Vision 姿态估计），同时只跑一个任务，其余的排队。
     粗扫大约 1.7 秒/秒视频；每个球深扫约 30 秒。</p>
</div>
<div class="card">
  <h2>3 · 已有的分析</h2>
  %s
</div>
</main>
<script>%s
let chosen=null, up={running:false};
const drop=el('drop'), file=el('file');
drop.onclick=()=>file.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('hot')};
drop.ondragleave=()=>drop.classList.remove('hot');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('hot');
  if(e.dataTransfer.files.length) pick(e.dataTransfer.files[0])};
file.onchange=()=>{if(file.files.length) pick(file.files[0])};
function pick(f){chosen=f;
  el('uhint').textContent='已选：'+f.name+'（'+(f.size/1048576).toFixed(1)+' MB）';
  if(!el('date').value){const m=f.name.match(/^(\\d{1,2}月\\d{1,2}日)/); if(m) el('date').value=m[1];}
  el('go').disabled=false;}
el('go').onclick=()=>{
  if(!chosen||up.running) return;
  up.running=true; el('go').disabled=true;
  const q=new URLSearchParams({name:chosen.name,date:el('date').value,height:el('height').value,
    hand:el('hand').value,distance:el('distance').value,title:el('title').value,
    suffix:el('suffix').value,to:el('to').value});
  const x=new XMLHttpRequest();
  x.open('POST','/api/upload?'+q.toString());
  x.upload.onprogress=e=>{if(e.lengthComputable){
    const p=e.loaded/e.total*100; el('ubar').style.width=p+'%%';
    el('uhint').textContent='上传中 '+p.toFixed(0)+'%%';}};
  x.onload=()=>{up.running=false; el('go').disabled=false;
    el('ubar').style.width='0%%';
    try{const r=JSON.parse(x.responseText);
      el('uhint').textContent = r.ok ? ('已加入队列：'+r.tag+' → '+(r.queued||'已开始')) : ('失败：'+r.error);
    }catch(e){el('uhint').textContent='服务端返回异常';}
    refresh();};
  x.onerror=()=>{up.running=false; el('go').disabled=false; el('uhint').textContent='上传失败（连接中断）'};
  el('uhint').textContent='上传中…';
  x.send(chosen);
};
const STAGES=%s;
function jobHtml(j){
  const cls=j.status==='done'?'done':(j.status==='failed'?'bad':(j.status==='running'?'run':''));
  const label={queued:'排队中',running:'进行中',done:'完成',failed:'失败',stopped:'主动停下',interrupted:'已中断'}[j.status]||j.status;
  let chips=STAGES.map(s=>{
    const on=(j.doneStages||[]).includes(s);
    const cur=(j.stage===s&&j.status==='running');
    return '<span class="chip'+(on?' on':(cur?' cur':''))+'">'+(LABELS[s]||s)+'</span>';}).join('');
  let link=j.report?'<a href="/files/'+encodeURIComponent(j.reportName)+'" target="_blank">打开报告</a>':'';
  let acts=(j.status==='running'||j.status==='queued')
    ?'<button class="ghost" onclick="cancel(\\''+j.id+'\\')">取消</button>':'';
  return '<div class="job"><div class="hd"><b>'+esc(j.tag)+'</b>'
    +'<span class="pill '+cls+'">'+label+'</span>'
    +'<span class="pill">'+esc(j.title||'')+'</span>'
    +(j.elapsed?'<span class="pill">'+human(j.elapsed)+'</span>':'')
    +'<span class="pill">'+esc(j.created)+'</span>'+link+acts+'</div>'
    +'<div class="chips">'+chips+'</div>'
    +(j.status==='running'?'<div class="bar"><i style="width:'+(j.pct||0)+'%%"></i></div>':'')
    +(j.log?'<pre>'+esc(j.log.slice(-4000))+'</pre>':'')+'</div>';
}
const LABELS={scan:'粗扫',shots:'逐球',deep:'深扫',top3:'评分',report:'报告',assets:'配图',video:'视频',cover:'封面'};
function cancel(id){fetch('/api/cancel?id='+id,{method:'POST'}).then(refresh)}
function rerun(tag){
  const to=el('rto_'+tag).value;
  if(to==='full'&&!confirm('「全部」会重出配图/视频/封面 —— 那三个目录的产物名都带日期前缀，\n只会覆盖「同一天」已生成的同类素材（不同日期互不影响）。确定继续？')) return;
  fetch('/api/run?tag='+tag+'&to='+to,{method:'POST'})
    .then(r=>r.json()).then(d=>{ if(!d.ok) alert('失败：'+d.error); refresh(); });
}
function refresh(){
  fetch('/api/jobs').then(r=>r.json()).then(d=>{
    el('jobs').innerHTML = d.jobs.length? d.jobs.map(jobHtml).join('') : '<p class="hint">还没有任务。</p>';
  }).catch(()=>{});
}
refresh(); setInterval(refresh, 1500);
</script></body></html>""" % (CSS, prof_html, ident, opts,
                              "".join(rows) or '<p class="hint">还没有。</p>', JS_COMMON,
                              json.dumps(SC.STAGES))


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version = "shotweb/1.0"

    def log_message(self, fmt, *a):        # 安静点，只留错误
        if not str(a[1] if len(a) > 1 else "").startswith("2"):
            sys.stderr.write("  %s\n" % (fmt % a))

    # ---------- 工具
    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def jsend(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def qs(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def serve_static(self, rel):
        """以项目根为界做静态路由（报告里的 frames/... 相对引用靠它解析）。"""
        target = os.path.realpath(os.path.join(PROJ, rel))
        if not target.startswith(os.path.realpath(PROJ) + os.sep) and target != os.path.realpath(PROJ):
            return self.send(403, "forbidden")
        if os.path.isdir(target):
            return self.send(403, "is a directory")
        if not os.path.exists(target):
            return self.send(404, "not found: %s" % rel)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        # 大视频要支持 Range，否则浏览器拖不动进度条
        size = os.path.getsize(target)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-")
                a = int(a) if a else 0
                b = int(b) if b else size - 1
            except Exception:
                a, b = 0, size - 1
            a, b = max(0, a), min(size - 1, b)
            length = b - a + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(target, "rb") as f:
                f.seek(a)
                left = length
                while left > 0:
                    c = f.read(min(1 << 20, left))
                    if not c:
                        break
                    try:
                        self.wfile.write(c)
                    except BrokenPipeError:
                        return
                    left -= len(c)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(target, "rb") as f:
            while True:
                c = f.read(1 << 20)
                if not c:
                    break
                try:
                    self.wfile.write(c)
                except BrokenPipeError:
                    return

    # ---------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self.send(200, page_index(SC.load_tags(), JOBS), "text/html; charset=utf-8")
        if p == "/api/jobs":
            with _LOCK:
                recent = sorted(JOBS.values(), key=lambda x: x["created"], reverse=True)[:12]
                recent_ids = {j["id"] for j in recent}
                out = []
                for j in sorted(JOBS.values(), key=lambda x: x["created"], reverse=True)[:40]:
                    d = dict(j)
                    # 只在最近 12 个任务上做磁盘扫描（轮询很频繁，别对全部任务 listdir）
                    if j["id"] in recent_ids:
                        try:
                            st = SC.stage_status(j["tag"], "")
                            d["doneStages"] = [s for s in SC.STAGES if st.get(s)]
                        except Exception:
                            d["doneStages"] = []
                    else:
                        d["doneStages"] = []
                    d["reportName"] = os.path.basename(d["report"]) if d.get("report") else None
                    d.pop("report", None)
                    d.pop("pid", None)
                    out.append(d)
            return self.jsend({"jobs": out})
        if p == "/api/tags":
            return self.jsend(SC.load_tags())
        if p.startswith("/files/"):
            return self.serve_static(urllib.parse.unquote(p[len("/files/"):]))
        return self.send(404, "not found")

    # ---------- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/upload":
            return self.do_upload()
        if u.path == "/api/run":
            q = self.qs()
            tag = q.get("tag")
            info = SC.load_tags().get(tag)
            if not info:
                return self.jsend({"ok": False, "error": "没有这个标签"}, 404)
            other = SC.report_conflict(tag, info) if q.get("to") != "scan" else None
            jid = new_job(tag=tag, title=info.get("title"), to=q.get("to") or "report",
                          suffix=info.get("report_suffix", ""), src=info.get("video"),
                          warn=("报告名会覆盖 tag %s，建议先加后缀" % other) if other else None)
            _Q.put(jid)
            return self.jsend({"ok": True, "job": jid, "warn": JOBS[jid].get("warn")})
        if u.path == "/api/cancel":
            jid = self.qs().get("id")
            j = JOBS.get(jid)
            if not j:
                return self.jsend({"ok": False, "error": "没有这个任务"}, 404)
            # 杀整个进程组：shotcli 自己还会拉起 scan.py / deep.py 这些孙进程
            pid = j.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception as e:
                    sys.stderr.write("  取消时杀进程失败：%s\n" % e)
            upd(jid, status="failed", error="用户取消", pid=None)
            app_log(jid, "\n[用户取消]\n")
            return self.jsend({"ok": True})
        return self.send(404, "not found")

    def do_upload(self):
        q = self.qs()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        name = os.path.basename(q.get("name") or "upload.mp4")
        if not length:
            return self.jsend({"ok": False, "error": "空文件"}, 400)
        if not re.search(r"\.(mp4|mov|m4v)$", name, re.I):
            return self.jsend({"ok": False, "error": "只支持 mp4 / mov / m4v"}, 400)

        os.makedirs(INCOMING, exist_ok=True)
        stem = re.sub(r"[^\w\u4e00-\u9fff.]+", "_", os.path.splitext(name)[0])
        dest = os.path.join(INCOMING, "%s_%s" % (time.strftime("%m%d-%H%M%S"), stem + os.path.splitext(name)[1]))
        got = save_stream(self.rfile, length, dest)
        if got < length:
            return self.jsend({"ok": False, "error": "上传中断（%d/%d 字节）" % (got, length)}, 400)

        # 登记标签（这一步失败要把已经落盘的文件删掉，别在 incoming/ 里留垃圾）
        try:
            tag = next_tag()
            interp = SC.interp(SC.load_cfg())
            args = [interp, os.path.join(TOOLS, "shotcli.py"), "add", dest,
                    "--tag", tag, "--date", q.get("date") or guess_date(name)]
            for f, k in (("title", "--title"), ("distance", "--distance"), ("suffix", "--suffix")):
                if q.get(f):
                    args += [k, q[f]]
            if q.get("height"):
                args += ["--height", q["height"]]
            if q.get("hand"):
                args += ["--hand", q["hand"]]
            r = subprocess.run(args, cwd=TOOLS, capture_output=True, text=True,
                               env=SC._child_env())
            if r.returncode != 0:
                os.remove(dest)
                return self.jsend({"ok": False,
                                   "error": "登记标签失败：" + (r.stderr or r.stdout)[-200:]}, 500)
        except Exception as e:
            try:
                os.remove(dest)
            except OSError:
                pass
            return self.jsend({"ok": False, "error": "登记标签异常：%s" % e}, 500)

        meta = duration_of(dest) or {}
        info = SC.load_tags().get(tag, {})
        # 撞车预警：同一天已有别的 tag 出过报告，而后缀又跟人家一样
        other = SC.report_conflict(tag, info)
        jid = new_job(tag=tag, title=info.get("title"), to=q.get("to") or "report",
                      suffix=info.get("report_suffix", ""), src=os.path.basename(dest),
                      duration=meta.get("duration"), size=meta.get("size"),
                      fps=meta.get("fps"), warn=("报告名会覆盖 tag %s，建议改后缀" % other) if other else None)
        _Q.put(jid)
        return self.jsend({"ok": True, "tag": tag, "job": jid, "duration": meta.get("duration"),
                           "eta_scan": int((meta.get("duration") or 0) * 1.7),
                           "warn": JOBS[jid].get("warn")})


def daemonize(logpath):
    """双 fork + setsid → 脱离原会话/进程组。

    为什么必须这么做：只靠 `nohup ... & disown` 挡不住「整个进程组被杀」，
    服务会在启动它的那次命令结束时一起消失（踩过：GET 全 200，下一条命令再访问就 502）。
    双 fork 之后进程在新会话里，父进程组怎么死都牵连不到它。

    返回 True 表示当前是守护进程本体，False 表示是父进程（父进程应直接退出）。
    """
    if os.fork() > 0:
        for _ in range(60):                       # 父：等子进程写好 pidfile 再退出
            try:
                pid = int(open(PIDFILE).read().strip())
                os.kill(pid, 0)
                print("已后台启动  pid=%d" % pid)
                print("  日志 %s" % logpath)
                print("  停止 python3 %s --stop" % os.path.basename(__file__))
                return False
            except Exception:
                pass
            time.sleep(0.1)
        print("后台启动失败：pidfile 没出现，看 %s" % logpath)
        return False
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(PROJ)
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    fd = os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(fd)
    os.close(devnull)
    open(PIDFILE, "w").write(str(os.getpid()))
    return True


def stop_server():
    if not os.path.exists(PIDFILE):
        print("没有 pidfile，服务可能没在跑")
        return 1
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, signal.SIGTERM)
        print("已停止 pid=%d" % pid)
    except ProcessLookupError:
        print("进程 %s 已不在，清掉 pidfile" % open(PIDFILE).read().strip())
    except Exception as e:
        print("停止失败：%s" % e)
        return 1
    os.remove(PIDFILE)
    return 0


def status_server(port):
    if os.path.exists(PIDFILE):
        pid = open(PIDFILE).read().strip()
        try:
            os.kill(int(pid), 0)
            print("运行中  pid=%s  http://127.0.0.1:%d" % (pid, port))
            return 0
        except Exception:
            print("pidfile 有但进程 %s 已不在（残留文件）" % pid)
            return 1
    print("没在跑（无 pidfile）")
    return 1


def main():
    ap = argparse.ArgumentParser(description="投篮分析 Web 服务（B 路线 · Web 层）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--daemon", action="store_true",
                    help="后台运行（双 fork + setsid，进程组被杀也不受影响）")
    ap.add_argument("--log", default=LOGFILE, help="后台日志路径")
    ap.add_argument("--stop", action="store_true", help="按 pidfile 停掉后台服务")
    ap.add_argument("--status", action="store_true", help="看后台服务在不在跑")
    a = ap.parse_args()

    os.makedirs(WEBDIR, exist_ok=True)
    os.makedirs(INCOMING, exist_ok=True)
    if a.stop:
        return stop_server()
    if a.status:
        return status_server(a.port)
    if a.daemon:
        if not daemonize(a.log):
            return 0
        a.host = a.host
    load_jobs()

    cfg = SC.load_cfg()
    print("=" * 62)
    print(" 投篮分析 Web 服务")
    print("=" * 62)
    print(" 地址        http://%s:%d" % (a.host, a.port))
    print(" 工程目录    %s" % PROJ)
    print(" 上传落地    %s" % INCOMING)
    print(" 干活解释器  %s" % SC.expand(cfg["python"]))
    print(" 任务记录    %s" % JOBS_DB)
    if not os.path.exists(SC.expand(cfg["python"])):
        print(" ⚠ 解释器不存在！改 tools/shotcli.json 的 python 字段，或先跑 `shotcli doctor`")
    print("=" * 62)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        if os.path.exists(PIDFILE):
            try:
                os.remove(PIDFILE)
            except OSError:
                pass


if __name__ == "__main__":
    main()
