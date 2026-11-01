#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shotcli —— 投篮分析本地 CLI（A 路线：把散落的脚本收成一条命令）

设计原则
--------
1. **本文件只用标准库**，任何 python3 都能跑（连 `--help` 都不需要 numpy）。
   所有需要 numpy/PIL/pyobjc 的重活，都通过配置里的解释器起子进程执行 ——
   这样「用错解释器」这个反复踩的坑从根上消失。
2. **标签表**：`data/tags.json` 登记「视频文件 ↔ 标签 ↔ 日期 ↔ 身高/投篮手/距离」。
   各脚本不再需要改代码换视频（report.py 已支持从标签表读）。
3. **可复用、可中断**：每个阶段先看产物在不在，在就跳过；`--force` 才重算。

用法
----
    shotcli setup                      建档：填身高 / 投篮手（**第一次用先跑这个**）
    shotcli doctor                     环境 + 数据自检
    shotcli init                       扫视频目录，生成/更新标签表
    shotcli add <视频> [--tag x]        登记一条
    shotcli list                       所有标签 + 各阶段完成度
    shotcli probe <视频>                视频参数
    shotcli run <tag> [--to S]          跑流水线（默认跑到报告为止）
    shotcli report|assets|video|cover <tag>
    shotcli shots <tag>                 打印逐球指标表

    阶段顺序： scan → shots → deep → top3 → report → assets → video → cover
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)                      # 投篮分析/
BASE = os.path.dirname(ROOT)                       # 训练记录/
DATA = os.path.join(ROOT, "data")
FRAMES = os.path.join(ROOT, "frames")
CONFIG = os.path.join(TOOLS, "shotcli.json")
TAGS = os.path.join(DATA, "tags.json")
PROFILE = os.path.join(DATA, "profile.json")   # 用户档案：身高 / 投篮手（由 `shotcli setup` 写）
DEFAULT_HEIGHT = 175.0   # 占位值：没建档时用它，并在 doctor / 报告里明确标注「未设置」

STAGES = ["scan", "shots", "deep", "top3", "report", "assets", "video", "cover"]

DEFAULT_CFG = {
    "python": "~/.workbuddy/binaries/python/envs/default/bin/python3",  # 本机兜底；tools/shotcli.json 优先
    "height_cm": 171.0,     # 本机身高；正常由 `shotcli setup` 写进 data/profile.json（那里优先）
    "hand": "right",        # 投篮手：right / left
    "scan_fps": 2.5,        # 粗扫采样率（事件定位用，别调太高）
    "shots_fps": 20,        # 逐球精算采样率
    "deep_fps": 30,         # 深扫采样率（报告播放器 / 评分口径）
    "deep_pre": 0.95,       # 深扫窗口：出手前多少秒
    "deep_post": 2.6,       # 深扫窗口：出手后多少秒（≥1.4 才覆盖到落地）
    "video_dir": "..",      # 训练视频所在目录，相对 BASE 为空
    "log_dir": "data",
}


# --------------------------------------------------------------------- 基础
def die(msg, code=1):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(code)


def expand(p):
    return os.path.expanduser(p) if p else p


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG):
        try:
            cfg.update(json.load(open(CONFIG, encoding="utf-8")))
        except Exception as e:
            die("配置文件坏了：%s（%s）" % (CONFIG, e))
    return cfg


def save_cfg(cfg):
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------- 用户档案（建档）
# 为什么单独存一份：**身高是唯一的绝对尺度来源**。报告里的「出手点离地高度 /
# 垂直弹跳 / 各段长度(cm)」都是 `身高cm × 像素比例` 算出来的 —— 身高填错，
# 这一整片数字会同比例全错，而且从报告里看不出来。所以要求第一次先 `setup` 建档，
# 并且每个标签都记下「这个身高是谁给的」（height_src），报告据此标注来源。
HEIGHT_SRC_TXT = {
    "profile": "你建档填的",
    "manual": "标签表里记录的",
    "default": "⚠ 默认占位值（还没建档）",
}


def load_profile():
    if os.path.exists(PROFILE):
        try:
            return json.load(open(PROFILE, encoding="utf-8"))
        except Exception as e:
            die("档案文件坏了：%s（%s）。删掉它重跑 `shotcli setup` 即可。" % (PROFILE, e))
    return {}


def save_profile(p):
    os.makedirs(DATA, exist_ok=True)
    json.dump(p, open(PROFILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)


def profile_height(cfg):
    """→ (身高 cm, 来源)。来源 ∈ profile / default。"""
    try:
        h = float(load_profile().get("height_cm"))
    except (TypeError, ValueError):
        h = 0.0
    if 100.0 < h < 260.0:
        return h, "profile"
    return float((cfg or {}).get("height_cm") or DEFAULT_HEIGHT), "default"


def profile_hand(cfg):
    hand = load_profile().get("hand")
    if hand in ("right", "left"):
        return hand
    return (cfg or {}).get("hand") or "right"


def height_warn(cfg):
    """没建档时，跑流水线前明确提醒一次（只警告，不阻断）。"""
    h, src = profile_height(cfg)
    if src != "default":
        return
    print("\n" + "!" * 66)
    print("⚠  你还没设置身高，本次按默认 %.0f cm 估算。" % h)
    print("   报告里的「出手点离地高度 / 垂直弹跳 / 各段长度」都会同比例错。")
    print("   先跑： python3 tools/shotcli.py setup      （只问一次，之后一直用）")
    print("!" * 66 + "\n")


def load_tags():
    if os.path.exists(TAGS):
        try:
            return json.load(open(TAGS, encoding="utf-8"))
        except Exception as e:
            die("标签表坏了：%s（%s）" % (TAGS, e))
    return {}


def save_tags(t):
    os.makedirs(DATA, exist_ok=True)
    json.dump(t, open(TAGS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)


def resolve_video(info):
    vf = info.get("video") or ""
    if vf and not os.path.isabs(vf):
        vf = os.path.join(BASE, vf)
    return vf


def get_tag(tag):
    t = load_tags()
    if tag not in t:
        die("标签表里没有 %r。先跑 `shotcli init`，或 `shotcli add <视频> --tag %s`。\n"
            "当前已有：%s" % (tag, tag, ", ".join(sorted(t)) or "（空）"))
    return t[tag]


# --------------------------------------------------------------------- 执行
def _child_env(env=None):
    """给子进程用的环境：清掉会劫持 venv 的变量。

    macOS 上 `__PYVENV_LAUNCHER__` 会覆盖 sys.executable，让 venv 的
    pyvenv.cfg 定位失败、site-packages 整体丢失（sys.prefix 退化成 base
    解释器，报「Could not find platform dependent libraries」）。它在 C 层
    生效，`-E` / `-I` / `-S` 都挡不住；被 launcher / shim 注入时必须显式
    剔除，否则子进程里 numpy / PIL / pyobjc 全部 import 不到。
    """
    e = dict(os.environ)
    e.pop("__PYVENV_LAUNCHER__", None)
    e.update(env or {})
    return e


def _has_deps(py):
    """这个解释器有没有干活要用的依赖（numpy / PIL / pyobjc）。"""
    if not py or not os.path.exists(py):
        return False
    code = ("import numpy, PIL, objc, Quartz, AVFoundation, Vision, CoreMedia"
            "\nprint('ok')")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           timeout=90, env=_child_env())
        return r.returncode == 0
    except Exception:
        return False


def find_python(cfg):
    """挑出真正能干活的解释器。

    顺序：配置里写死的（存在即用）> 常见托管环境 > PATH 上的 python3。
    写死的路径视为用户明确指定，不再探测依赖 —— 探测要起子进程、慢，而且容易
    被运行环境的噪声左右；真有问题 `shotcli doctor` 会报出来。
    返回 (路径, 来源说明)；找不到返回 (None, 尝试过的列表)。
    """
    if cfg.get("python"):
        p = expand(cfg["python"])
        if os.path.exists(p):
            return p, "配置 shotcli.json"
    tried = []
    cands = []
    for c in (sys.executable,
              "~/.workbuddy/binaries/python/envs/default/bin/python3",
              "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
              "/usr/bin/python3"):
        cands.append(("自动探测", expand(c) if c.startswith("~") else c))
    for c in ("python3", "python"):
        w = shutil.which(c)
        if w:
            cands.append(("PATH", w))
    seen = set()
    for src, p in cands:
        if not p or p in seen:
            continue
        seen.add(p)
        tried.append(p)
        if _has_deps(p):
            return p, src
    return None, tried


def interp(cfg):
    p, src = find_python(cfg)
    if not p:
        die("找不到带依赖的 Python 解释器（需要 numpy + Pillow + pyobjc）。\n"
            "试过这些：\n  " + "\n  ".join(src or ["（无）"]) + "\n"
            "装依赖：\n"
            "    <python3> -m pip install numpy pillow "
            "pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia\n"
            "装好后可把它写进 tools/shotcli.json 的 python 字段钉死。")
    return p


def run_stage(cfg, label, argv, env=None, log=None, dry=False, tail_on_fail=True,
              allow_fail=False):
    """起子进程跑一个阶段；输出同时打到屏幕和日志文件。返回 (ok, 秒数)。"""
    py = interp(cfg)
    full = [py] + argv
    print("\n▶ %s" % label)
    print("  $ " + " ".join(full))
    if dry:
        return True, 0.0
    e = _child_env(env)          # 剔掉 __PYVENV_LAUNCHER__，否则 venv 的包全找不到
    t0 = time.time()
    p = subprocess.Popen(full, cwd=TOOLS, env=e, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        sys.stdout.write("    " + line)
        if log:
            log.write(line)
        tail.append(line.rstrip())
        if len(tail) > 400:
            tail.pop(0)
    p.wait()
    dt = time.time() - t0
    if p.returncode != 0 and allow_fail:    # 诊断类：非 0 退出是「正常结论」，不算失败
        print("  · 已输出诊断（退出码 %d）" % p.returncode)
        return True, dt
    if p.returncode != 0:
        print("  ✗ 失败（退出码 %d，耗时 %.1fs）" % (p.returncode, dt))
        if tail_on_fail:                      # 诊断类脚本的完整输出已经在上面打过了，别重打
            print("    最后几行：")
            for l in tail[-6:]:
                print("      " + l)
        return False, dt
    print("  ✓ 完成（%.1fs）" % dt)
    return True, dt


def tag_env(tag, info, cfg):
    """子进程环境：让 report.py / makecharts.py 等知道该用哪个标签。"""
    e = {"SHOTCLI_TAG": tag}
    if info.get("height_cm"):
        e["SHOTCLI_HEIGHT"] = str(info["height_cm"])
    return e


# --------------------------------------------------------------------- 产物检查
def deep_files(tag, suf=""):
    d = os.path.join(DATA, tag)
    if not os.path.isdir(d):
        return []
    pre = "deep%s_" % suf
    fs = [f for f in os.listdir(d) if f.startswith(pre) and f.endswith(".json")]
    fs.sort(key=lambda f: float(f[len(pre):-5]))
    return fs


def shot_files(tag):
    d = os.path.join(DATA, tag)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.startswith("shot_") and f.endswith(".json"))


def stamp_path(tag):
    return os.path.join(DATA, "%s.build.json" % tag)


def load_stamp(tag):
    p = stamp_path(tag)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def mark_stage(tag, stage, extra=None):
    """记一笔：这个 tag 的这个阶段什么时候跑过。共用产物目录的判定靠它。"""
    st = load_stamp(tag)
    rec = st.get(stage)
    rec = {"at": time.strftime("%F %T"), "runs": (rec or {}).get("runs", 0) + 1}
    if extra:
        rec.update(extra)
    st[stage] = rec
    json.dump(st, open(stamp_path(tag), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)


def stage_status(tag, suf=""):
    """每个阶段是否已有产物。

    scan/shots/deep/top3 的产物文件名带 tag，可以直接看文件；
    report/assets/video/cover 的产物目录是**所有 tag 共用**的（文件名里只有日期，
    而 9月12日 就有 a/b/c 三个 tag），看文件必然误判 —— 所以那几项以
    `data/<tag>.build.json` 的构建记录为准。
    """
    info = load_tags().get(tag, {})
    src = resolve_video(info)
    date = info.get("date") or tag
    stamp = load_stamp(tag)
    return {
        "scan":   os.path.exists(os.path.join(DATA, "%s_coarse.jsonl" % tag)),
        "shots":  len(shot_files(tag)) > 0,
        "deep":   len(deep_files(tag, suf)) > 0,
        "top3":   os.path.exists(os.path.join(DATA, "%s_top3.json" % tag)),
        "report": "report" in stamp or os.path.exists(report_of(tag, info)[0]) and bool(shot_files(tag)),
        "assets": "assets" in stamp,
        "video":  "video" in stamp,
        "cover":  "cover" in stamp,
        "video_exists": os.path.exists(src),
    }


# --------------------------------------------------------------------- 命令
def cmd_setup(a):
    """建档：问身高 / 投篮手 → 写 data/profile.json，并同步到「还没单独设过」的标签。"""
    cfg = load_cfg()
    old_h, old_src = profile_height(cfg)
    old_hand = profile_hand(cfg)
    interactive = sys.stdin.isatty()

    print("=" * 66)
    print("投篮分析 · 建档（只问一次，之后所有报告都用它）")
    print("=" * 66)
    print("身高是**唯一的绝对尺度来源**：报告里的「出手点离地高度 / 垂直弹跳 / 各段")
    print("长度(cm)」都是用它把像素换算成 cm。填错 → 这一片数字会同比例全错，而且")
    print("从报告里看不出来（所以这份档案要单独存、并且标明是谁给的）。\n")

    h = getattr(a, "height", None)
    if h is None and not interactive:
        die("当前不是交互终端，请直接给值：\n"
            "    python3 tools/shotcli.py setup --height 171 --hand right")
    if h is None:
        cur = "%.0f" % old_h if old_src == "profile" else ""
        tip = "当前 %s，回车保持不变" % cur if cur else "例：171"
        s = input("① 身高 cm（%s）：" % tip).strip()
        if s:
            try:
                h = float(s)
            except ValueError:
                die("身高要填数字，收到的是 %r" % s)
        elif cur:
            h = old_h

    hand = getattr(a, "hand", None)
    if hand is None and interactive:
        d = "1" if old_hand == "right" else "2"
        s = input("② 投篮手：[1] 右手 / [2] 左手（回车 = %s）："
                  % ("右手" if d == "1" else "左手")).strip().lower()
        hand = {"1": "right", "2": "left", "r": "right", "l": "left"}.get(s)
        if hand is None and s:
            die("投篮手只认 1 / 2（或 right / left），收到 %r" % s)
    hand = hand or old_hand

    if h is None:
        h = old_h
    h = float(h)
    if not (100.0 < h < 260.0):
        die("身高 %.0f 不像人类身高（要求 100–260 cm）" % h)
    if hand not in ("right", "left"):
        die("投篮手只能是 right / left")

    prof = load_profile()
    prof.update({"height_cm": h, "hand": hand,
                 "updated": time.strftime("%Y-%m-%d %H:%M")})
    save_profile(prof)
    print("\n✓ 已写入 %s" % PROFILE)
    print("    身高 %.0f cm · 投篮手 %s" % (h, "右手" if hand == "right" else "左手"))

    # 同步到标签表：只覆盖「还没单独设过」的标签 ——
    #   ① 新代码写的占位值（height_src == "default"）
    #   ② 老标签（没有 height_src）且身高正好等于占位值 175
    # 用 `shotcli add --height` 手动指定过的（height_src == "manual"）一律不动。
    tags = load_tags()
    synced, skipped = [], []
    for k, v in tags.items():
        src = v.get("height_src")
        legacy_placeholder = (src is None
                              and abs(float(v.get("height_cm") or 0) - DEFAULT_HEIGHT) < 0.01)
        if src in ("default", "profile") or legacy_placeholder:
            v["height_cm"], v["height_src"], v["hand"] = h, "profile", hand
            synced.append(k)
        elif src == "manual":
            skipped.append(k)
    if synced:
        save_tags(tags)
        print("  已同步 %d 个标签：%s" % (len(synced), ", ".join(sorted(synced))))
    if skipped:
        print("  跳过 %d 个手动指定过身高的标签：%s（要改它们请用 `shotcli add`）"
              % (len(skipped), ", ".join(sorted(skipped))))

    print("\n改主意了随时重跑： python3 tools/shotcli.py setup")
    print("下一步：            python3 tools/shotcli.py doctor")
    return 0


def cmd_doctor(a):
    cfg = load_cfg()
    print("=" * 66)
    print("shotcli doctor")
    print("=" * 66)
    ok = True

    print("\n[1] 目录")
    for name, p in (("工程根", ROOT), ("数据", DATA), ("帧缓存", FRAMES), ("工具", TOOLS)):
        made = ""
        if not os.path.isdir(p):
            try:                      # 这些目录本来就是脚本按需创建的，doctor 顺手建掉，
                os.makedirs(p, exist_ok=True)   # 免得一装上就永远显示 ✗
                made = "（已创建）"
            except OSError:
                pass
        mark = "✓" if os.path.isdir(p) else "✗"
        if mark == "✗":
            ok = False
        print("  %s %-8s %s%s" % (mark, name, p, made))
    w = DATA if os.path.isdir(DATA) else ROOT
    print("  %s 可写     %s" % ("✓" if os.access(w, os.W_OK) else "✗", w))

    print("\n[2] 解释器（真正干活的那个）")
    py, src = find_python(cfg)
    if py:
        print("  选中：   ✓ %s   （%s）" % (py, src))
        code = ("import numpy, PIL, objc;"
                "print('numpy', numpy.__version__, '| PIL ok | pyobjc ok')")
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           env=_child_env())
        if r.returncode == 0:
            print("  依赖：   ✓ %s" % r.stdout.strip())
        else:
            ok = False
            print("  依赖：   ✗ 缺包（%s）" % (r.stderr.strip().splitlines() or ["?"])[-1][:90])
    else:
        ok = False
        print("  选中：   ✗ 没找到带依赖的解释器")
        print("  试过：   " + "、".join(src or ["（无）"]))
        print("  装依赖： <python3> -m pip install numpy pillow "
              "pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia")
    print("  本进程： %s（CLI 本身只用标准库，所以哪个 python 跑都行）" % sys.executable)

    print("\n[3] 字体（渲染骨架/面板要用）")
    fonts = [("/System/Library/Fonts/PingFang.ttc", "苹方"),
             ("/System/Library/Fonts/Helvetica.ttc", "Helvetica")]
    for f, n in fonts:
        print("  %s %s" % ("✓" if os.path.exists(f) else "·", n))
    # 关键：不只是文件在不在，还要确认「加粗时真的取到简体字库」——
    # 历史上加粗错取 PingFang TC，导致「稳」渲染成空白（见 fontkit.py 说明）
    if py and os.path.exists(py):
        code = ("import sys; sys.path.insert(0, %r); " % TOOLS +
                "import fontkit;"
                "print('加粗→%s | 常规→%s | 字形自检 稳=%s' % ("
                "fontkit.name(24, True), fontkit.name(24, False),"
                "fontkit.load(24, True).getmask('稳').getbbox() is not None))")
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           env=_child_env())
        if r.returncode == 0:
            print("  实选：   ✓ %s" % r.stdout.strip())
            if "稳=True" not in r.stdout:
                ok = False
                print("           ✗ 加粗字体缺简体字形，配图会静默缺字")
        else:
            print("  实选：   · 跳过（%s）" % (r.stderr.strip().splitlines() or ["?"])[-1][:90])

    print("\n[4] 用户档案（身高是唯一的绝对尺度来源）")
    prof = load_profile()
    h, src = profile_height(cfg)
    hand = profile_hand(cfg)
    if src == "profile":
        print("  ✓ 身高 %.0f cm · 投篮手 %s（%s）"
              % (h, "右手" if hand == "right" else "左手", HEIGHT_SRC_TXT["profile"]))
        if prof.get("updated"):
            print("    建档时间： %s" % prof["updated"])
    else:
        ok = False
        print("  ✗ 还没建档：当前按默认 %.0f cm 估算" % h)
        print("    报告里的「出手点离地高度 / 垂直弹跳 / 各段长度」都会同比例错。")
        print("    先跑： python3 tools/shotcli.py setup")
    # 标签表里的身高来源统计（哪个 tag 用的是占位值）
    t_src = load_tags()
    bad = sorted(k for k, v in t_src.items()
                 if (v.get("height_src") or ("manual" if v.get("height_cm") else "default")) == "default"
                 or abs(float(v.get("height_cm") or 0) - DEFAULT_HEIGHT) < 0.01)
    if t_src:
        if bad:
            print("  ⚠ %d/%d 个标签还挂着占位身高 %.0f cm：%s"
                  % (len(bad), len(t_src), DEFAULT_HEIGHT, ", ".join(bad)))
        else:
            print("  ✓ %d 个标签的身高都已确认（来源：%s）"
                  % (len(t_src), " / ".join(sorted({HEIGHT_SRC_TXT.get(
                      v.get("height_src") or "manual", "?") for v in t_src.values()}))))

    print("\n[5] 配置 / 标签表")
    print("  %s 配置 %s" % ("✓" if os.path.exists(CONFIG) else "·", CONFIG))
    t = load_tags()
    print("  %s 标签表 %s（%d 条）" % ("✓" if t else "·", TAGS, len(t)))
    for k in sorted(t):
        info = t[k]
        vf = resolve_video(info)
        st = stage_status(k)
        done = [s for s in STAGES if st.get(s)]
        print("    %-3s %-12s %-40s 视频%s  已完成: %s" % (
            k, info.get("date", "?"), os.path.basename(vf) or "（未填）",
            "✓" if os.path.exists(vf) else "✗",
            "/".join(done) if done else "（无）"))

    print("\n" + ("全部通过 ✓" if ok else "有问题 ✗（见上面标 ✗ 的行）"))
    return 0 if ok else 1


def _guess_date(fn):
    m = re.match(r"^(\d{1,2}月\d{1,2}日)", fn)
    return m.group(1) if m else ""


def _guess_title(fn, date):
    m = re.search(r"[（(](\d+)[)）]", fn)
    tail = ""
    if m:
        tail = " · 第 %s 段" % m.group(1)
    elif "分球" in fn:
        tail = " · " + re.search(r"(\d+分球)", fn).group(1)
    return "%s%s" % (date or fn, tail)


def cmd_init(a):
    cfg = load_cfg()
    tags = load_tags()
    vids = []
    for f in sorted(os.listdir(BASE)):
        if f.lower().endswith((".mp4", ".mov", ".m4v")):
            vids.append(f)
    if not vids:
        die("在 %s 里没找到视频" % BASE)
    added = 0
    known = {os.path.basename(resolve_video(v)) for v in tags.values()}
    letters = [c for c in "abcdefghijklmnopqrstuvwxyz"]
    ph, psrc = profile_height(cfg)
    for f in vids:
        if f in known:
            continue
        tag = next((c for c in letters if c not in tags), None)
        if tag is None:
            die("26 个标签字母用完了")
        date = _guess_date(f)
        tags[tag] = {"video": f, "date": date or f, "title": _guess_title(f, date),
                     "height_cm": ph, "height_src": psrc, "hand": profile_hand(cfg)}
        print("  + %s  %-28s → %s" % (tag, f, date or "（日期没认出来，用 --date 改）"))
        added += 1
    if added:
        save_tags(tags)
        print("\n新增 %d 条，已写入 %s" % (added, TAGS))
    else:
        print("标签表已是最新（%d 条）" % len(tags))
    if psrc == "default":
        print("\n⚠ 你还没建档，这些标签先用占位身高 %.0f cm。" % ph)
        print("  先跑 `python3 tools/shotcli.py setup` 填自己的身高 —— 否则报告的")
        print("  「出手点离地高度 / 垂直弹跳 / 各段长度」会整体算错。")
    print("提示：日期/标题/距离可以手工改 %s，或重新 `shotcli add --tag x ...`。" % TAGS)


def cmd_add(a):
    cfg = load_cfg()
    tags = load_tags()
    vf = a.video
    if not os.path.isabs(vf):
        cand = os.path.join(BASE, vf)
        vf = cand if os.path.exists(cand) else os.path.abspath(vf)
    if not os.path.exists(vf):
        die("找不到视频：%s" % vf)
    tag = a.tag
    if not tag:
        tag = next((c for c in "abcdefghijklmnopqrstuvwxyz" if c not in tags), None)
        if tag is None:
            die("标签字母用完了，用 --tag 指定一个")
    fn = os.path.basename(vf)
    date = a.date or _guess_date(fn) or fn
    ph, psrc = profile_height(cfg)
    rec = {
        "video": vf if os.path.dirname(vf) != BASE else fn,
        "date": date,
        "title": a.title or _guess_title(fn, date),
        "height_cm": a.height if a.height else ph,
        "height_src": "manual" if a.height else psrc,
        "hand": a.hand or profile_hand(cfg),
    }
    if a.distance:
        rec["distance"] = a.distance
    if a.suffix:
        rec["report_suffix"] = "_" + a.suffix.lstrip("_")
    if tag in tags:
        rec = dict(tags[tag], **{k: v for k, v in rec.items() if v})
        print("  ~ 覆盖已有标签 %s" % tag)
    else:
        print("  + 新标签 %s" % tag)
    tags[tag] = rec
    save_tags(tags)
    for k, v in rec.items():
        print("    %-11s %s" % (k, v))


def cmd_list(a):
    tags = load_tags()
    if not tags:
        die("标签表是空的，先跑 `shotcli init`")
    suf = a.deep_suf or ""
    print("%-4s %-12s %-11s %-7s %s" % ("tag", "日期", "身高/手", "视频", "阶段进度"))
    print("-" * 88)
    for k in sorted(tags):
        info = tags[k]
        st = stage_status(k, suf)
        cells = [("%-7s" % s) if st.get(s) else ("%-7s" % "·") for s in STAGES]
        print("%-4s %-12s %-11s %-7s %s  球%d 深扫%d" % (
            k, info.get("date", "?"),
            "%.0f/%s" % (info.get("height_cm", 0), (info.get("hand") or "?")[:1]),
            "✓" if st["video_exists"] else "✗",
            " ".join(cells), len(shot_files(k)), len(deep_files(k, suf))))
    print("\n·=未做　字=已做。scan/shots/deep/top3 看产物文件（文件名带 tag）；")
    print("report/assets/video/cover 看 data/<tag>.build.json 的构建记录（那些产物目录是所有 tag 共用的）。")
    print("扫视频很慢，先 `shotcli run <tag> --dry-run` 看计划，再 `--to scan` 试水。")


def cmd_probe(a):
    cfg = load_cfg()
    vf = a.video
    if not os.path.isabs(vf) and os.path.exists(os.path.join(BASE, vf)):
        vf = os.path.join(BASE, vf)
    code = ("import sys;sys.path.insert(0,%r);from bbkit import probe;import json;"
            "print(json.dumps(probe(sys.argv[1]),ensure_ascii=False,indent=2))" % TOOLS)
    r = subprocess.run([interp(cfg), "-c", code, vf], capture_output=True, text=True,
                       env=_child_env())
    if r.returncode != 0:
        die(r.stderr.strip()[-300:])
    print(r.stdout)


def report_of(tag, info):
    """这个 tag 的报告 / CSV 输出路径（与 report.py 的命名规则保持一致）。"""
    date = info.get("date") or tag
    sfx = info.get("report_suffix", "") or ""
    return (os.path.join(ROOT, "%s_投篮分析报告%s.html" % (date, sfx)),
            os.path.join(ROOT, "%s_逐球指标%s.csv" % (date, sfx)))


def report_conflict(tag, info):
    """同名日期 + 同后缀，已经被别的 tag 出过报告 —— 再跑就会覆盖它。"""
    date = info.get("date") or tag
    sfx = info.get("report_suffix", "") or ""
    for other, oinfo in load_tags().items():
        if other == tag:
            continue
        if (oinfo.get("date") or other) != date:
            continue
        if (oinfo.get("report_suffix", "") or "") != sfx:
            continue
        if "report" in load_stamp(other):
            return other
    return None


def suggest_suffix(info, tag):
    """从标题里猜一个消歧后缀，如「10月6日 · 第 1 段」→ 第1段。"""
    ti = info.get("title", "") or ""
    m = re.search(r"第\s*(\d+)\s*段", ti)
    if m:
        return "第%s段" % m.group(1)
    m = re.search(r"(\d+分球|慢速|全场)", ti)
    if m:
        return m.group(1)
    return "tag%s" % tag


def _need(info, tag, keys=("video",)):
    vf = resolve_video(info)
    if not os.path.exists(vf):
        die("视频不存在：%s（标签 %s；改 data/tags.json 或用 `shotcli add` 更新）" % (vf, tag))
    return vf


def _deep_times(tag):
    """深扫要覆盖哪些出手时刻 —— 直接读 run_shots 产出的 shot_*.json。"""
    ts = []
    for fn in shot_files(tag):
        try:
            d = json.load(open(os.path.join(DATA, tag, fn), encoding="utf-8"))
            ts.append(float(d["meta"]["t_center"]))
        except Exception:
            pass
    return sorted(ts)


def cmd_run(a):
    cfg = load_cfg()
    info = get_tag(a.tag)
    tag = a.tag
    vf = _need(info, tag)
    fs, ts_ = cfg["scan_fps"], cfg["shots_fps"]
    to = a.to or "report"
    if to == "full":
        to = STAGES[-1]
    frm = a.frm or STAGES[0]
    if to not in STAGES or frm not in STAGES:
        die("阶段名不对。可选：%s（或 full）" % " / ".join(STAGES))
    i0, i1 = STAGES.index(frm), STAGES.index(to)

    height_warn(cfg)      # 没建档就在这里吼一声（只警告，不阻断）

    if getattr(a, "suffix", None):
        _t = load_tags()
        _t[tag]["report_suffix"] = "_" + a.suffix.lstrip("_")
        save_tags(_t)
        info = _t[tag]
        print("已把报告后缀写进标签表：%s" % info["report_suffix"])

    logp = os.path.join(ROOT, cfg["log_dir"], "%s_run.log" % tag)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    log = open(logp, "a", encoding="utf-8")
    log.write("\n\n===== %s  run %s → %s =====\n" % (time.strftime("%F %T"), frm, to))

    plan = []
    for s in STAGES[i0:i1 + 1]:
        plan.append(s)
    if a.dry_run:
        print("计划（dry-run）：%s" % " → ".join(plan))
        print("视频：%s\n标签：%s（%s）" % (vf, tag, info.get("title")))
        return

    # 报告名只带日期，同一天有多段视频会互相覆盖 —— 先拦住，给出一条能直接抄的命令
    if "report" in plan:
        other = report_conflict(tag, info)
        if other and not a.force:
            sug = suggest_suffix(info, tag)
            die("报告文件名会和 tag %s 撞车（同一天多段视频，报告名只带日期）。\n"
                "  受影响：%s\n\n"
                "  推荐：给 %s 加个后缀，写进标签表后就不再冲突 ——\n"
                "      shotcli add \"%s\" --tag %s --suffix %s\n"
                "  然后照常跑：\n"
                "      shotcli run %s --to report\n\n"
                "  确实要覆盖 tag %s 的报告（不推荐）：加 --force"
                % (other, os.path.basename(report_of(tag, info)[0]),
                   tag, os.path.basename(resolve_video(info)), tag, sug, tag, other))

    print("=" * 66)
    print("shotcli run  tag=%s  %s → %s" % (tag, frm, to))
    print("  视频 %s" % vf)
    print("  日志 %s" % logp)
    print("=" * 66)

    env = tag_env(tag, info, cfg)
    t_all = time.time()
    failed = None
    stopped = None

    for s in plan:
        if s == "scan":
            out = os.path.join(DATA, "%s_coarse.jsonl" % tag)
            if os.path.exists(out) and not a.force:
                print("\n▶ scan  —— 已有 %s，跳过（--force 重算）" % os.path.basename(out))
                continue
            if a.force and os.path.exists(out):
                os.remove(out)
            ok, _ = run_stage(cfg, "scan 粗扫（定位出手事件）",
                              ["scan.py", vf, tag, str(fs)], env, log)
            if not ok:
                failed = s
                break

        elif s == "shots":
            done = len(shot_files(tag))
            print("\n▶ shots 逐球精算（已有 %d 球，run_shots 会跳过已完成的）" % done)
            ok, _ = run_stage(cfg, "shots 逐球精算",
                              ["run_shots.py", vf, tag, str(ts_)], env, log)
            if not ok:
                failed = s
                break
            # 0 球是最坑的情况：后面 deep/report 全是空跑。当场诊断，别让它静默"成功"。
            if len(shot_files(tag)) == 0:
                run_stage(cfg, "诊断：为什么 0 球（粗扫信号拆解）",
                          ["diag_scan.py", tag], env, log,
                          tail_on_fail=False, allow_fail=True)
                print("\n  这段视频没识别到出手 —— 后面 deep/report 跑了也是空跑，先停在这里。")
                stopped = s
                break

        elif s == "deep":
            suf = a.deep_suf or ""
            times = _deep_times(tag)
            if not times:
                print("\n▶ deep —— 没有 shot_*.json，先跑 shots 阶段")
                failed = s
                break
            have = set()
            for fn in deep_files(tag, suf):
                have.add(fn)
            need = [t for t in times if "deep%s_%.1f.json" % (suf, t) not in have]
            if a.force:
                for fn in deep_files(tag, suf):
                    os.remove(os.path.join(DATA, tag, fn))
                need = list(times)
            fps = a.deep_fps or cfg["deep_fps"]
            if not need:
                print("\n▶ deep —— %d 球都已有 %dfps 深扫，跳过" % (len(times), fps))
            else:
                print("\n▶ deep %dfps 深扫：%d/%d 球需要算" % (fps, len(need), len(times)))
                args = ["deep.py", vf, tag, str(fps),
                        ",".join("%.1f" % t for t in need),
                        str(cfg["deep_pre"]), str(cfg["deep_post"])]
                if suf:
                    args.append(suf)
                ok, _ = run_stage(cfg, "deep 逐帧深扫", args, env, log)
                if not ok:
                    failed = s
                    break

        elif s == "top3":
            which = a.which or ("picked" if getattr(a, "pick", None) else "top3")
            args = ["score_top3.py", tag]
            if which.startswith("first"):
                n = which[5:] or "3"
                args += ["--first", n]
            elif getattr(a, "pick", None):
                args += ["--pick", a.pick]
            ok, _ = run_stage(cfg, "top3 评分排名（%s）" % which, args, env, log)
            if not ok:
                failed = s
                break

        elif s == "report":
            ok, _ = run_stage(cfg, "report 生成报告（HTML + CSV）",
                              ["report.py", str(a.strip), tag], env, log)
            if not ok:
                failed = s
                break

        elif s == "assets":
            lay = a.layout or "both"
            layouts = ["v", "h"] if lay == "both" else [lay]
            for L in layouts:
                args = ["makecharts.py", tag] + (["v"] if L == "v" else [])
                ok, _ = run_stage(cfg, "assets 配图（%s）" % ("竖版" if L == "v" else "横版"),
                                  args, env, log)
                if not ok:
                    failed = s
                    break
            if failed:
                break

        elif s == "video":
            layouts = ["v", "h"] if (a.layout or "both") == "both" else [a.layout]
            which = a.which or ("picked" if getattr(a, "pick", None) else "top3")
            suf = a.deep_suf or ""
            only = getattr(a, "only", None) or ""
            if which == "picked" and not os.path.exists(
                    os.path.join(DATA, "%s_picked.json" % tag)):
                print("\n▶ video —— 还没有 %s_picked.json（需要先跑 top3 阶段挑球）" % tag)
                print("  先跑：  shotcli run %s --to top3 --pick 16.4,65.4,78.0" % tag)
                failed = s
                break
            if only:
                print("\n▶ video —— 清单 %s 里只出第 %s 个（makevid 的 ONLY 口径）" % (which, only))
            for L in layouts:
                # makevid argv: <tag> <layout> <清单后缀> <深扫后缀> <ONLY>
                # ONLY 是「清单里的第 N 个」而不是文件名里的球号：
                # first5（按时间取前 5）+ ONLY=2,3,4,5 → 得到视频里第 2~5 个投篮，文件命名 第2球~第5球
                args = ["makevid.py", tag, L, which, suf, only] if (suf or only) \
                    else ["makevid.py", tag, L, which]
                ok, _ = run_stage(cfg, "video 慢放视频（%s）" % ("竖版" if L == "v" else "横版"),
                                  args, env, log)
                if not ok:
                    failed = s
                    break
            if failed:
                break
            # 合集默认 3→2→1（第 1 名压轴）—— join_shots.py 自己的默认就是这个，
            # 以前这里写死 "1,2,3" 把它盖掉了，出片顺序是反的。
            # 版式只出 h 时合集也出横版；both 时仍只拼竖版（横版合集另跑）。
            jl = "v" if (a.layout or "both") == "both" else a.layout
            ok, _ = run_stage(cfg, "video 拼接合集",
                              ["join_shots.py", a.order or "3,2,1", jl, tag, which]
                              + ([suf] if suf else []), env, log)
            if not ok:
                failed = s
                break

        elif s == "cover":
            times = _deep_times(tag)
            t = a.t if a.t is not None else (times[0] if times else 0.0)
            ok, _ = run_stage(cfg, "cover 竖版封面 3:4",
                              ["cover_from_frame.py", "%.1f" % t, "", tag], env, log)
            if not ok:
                failed = s
                break
            ok, _ = run_stage(cfg, "cover 横版封面 4:3",
                              ["cover_wide.py", "%.1f" % t, "", "4x3", tag], env, log)
            if not ok:
                failed = s
                break

    log.close()
    # 记一笔本次成功跑完的阶段（list / doctor 的进度显示靠它；共用产物目录那几项尤其需要）
    suf_now = a.deep_suf or ""
    for s in plan:
        if failed and STAGES.index(s) >= STAGES.index(failed):
            break
        extra = None
        if s == "deep":
            extra = {"suf": suf_now or "-", "fps": a.deep_fps or cfg["deep_fps"]}
        elif s in ("video", "assets"):
            extra = {"layout": a.layout or "both", "which": a.which or "top3"}
        elif s == "report":
            extra = {"date": info.get("date"), "strip": a.strip}
        mark_stage(tag, s, extra)
    dt = time.time() - t_all
    print("\n" + "=" * 66)
    if failed or stopped:
        w = failed or stopped
        print("%s 在阶段 %s 停下，已跑 %.1f 分钟。" % ("✗" if failed else "■", w, dt / 60))
        if stopped and not failed:
            print("  这不是报错 —— 是拿到的结果不足以继续（原因见上面的诊断）。")
        print("  处理完接着跑：  shotcli run %s --from %s --to %s" % (tag, w, to))
        sys.exit(1)
    print("✓ 全部完成（%.1f 分钟）" % (dt / 60))
    done = stage_status(tag, a.deep_suf or "")
    print("  产物：报告 %s ｜ 配图 %s ｜ 视频 %s ｜ 封面 %s" % (
        "✓" if done["report"] else "—", "✓" if done["assets"] else "—",
        "✓" if done["video"] else "—", "✓" if done["cover"] else "—"))


def cmd_stage(a):
    """report / assets / video / cover 的单一阶段入口。"""
    stage = a.cmd
    if stage == "diag":
        cfg = load_cfg()
        info = get_tag(a.tag)
        run_stage(cfg, "diag 粗扫诊断", ["diag_scan.py", a.tag], tag_env(a.tag, info, cfg))
        return
    ns = argparse.Namespace(tag=a.tag, to=stage, frm=stage, force=a.force,
                            strip=getattr(a, "strip", 4), deep_suf=getattr(a, "deep_suf", None),
                            deep_fps=getattr(a, "deep_fps", None), which=getattr(a, "which", None),
                            pick=getattr(a, "pick", None),
                            layout=getattr(a, "layout", None), order=getattr(a, "order", None),
                            only=getattr(a, "only", None),
                            t=getattr(a, "t", None), dry_run=False)
    cmd_run(ns)


def cmd_shots(a):
    info = get_tag(a.tag)
    d = ROOT
    date = info.get("date", a.tag)
    csvp = os.path.join(d, "%s_逐球指标.csv" % date)
    if not os.path.exists(csvp):
        die("还没有逐球指标表。先跑：shotcli run %s --to report" % a.tag)
    rows = [l.rstrip("\n").split(",") for l in open(csvp, encoding="utf-8") if l.strip()]
    if not rows:
        die("CSV 是空的")
    head, body = rows[0], rows[1:]
    keep = [0] + [i for i, h in enumerate(head) if i and i < len(head)][:7]
    w = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in keep]
    def line(r):
        return "  ".join((("%-*s" if i == 0 else "%*s") % (w[j] + 1, (r[i] if i < len(r) else "")))
                         for j, i in enumerate(keep))
    print("逐球指标 · %s（%s）· 共 %d 球\n" % (a.tag, date, len(body)))
    print(line(head))
    print("  ".join("-" * (x + 1) for x in w))
    for r in body:
        print(line(r))
    print("\n完整表：%s" % csvp)


# --------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(
        prog="shotcli", description="投篮分析本地 CLI（A 路线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="阶段顺序： " + " → ".join(STAGES))
    sub = p.add_subparsers(dest="cmd")

    psu = sub.add_parser("setup", help="建档：填身高 / 投篮手（第一次用先跑这个）")
    psu.add_argument("--height", type=float, help="身高 cm；给了就不再交互提问")
    psu.add_argument("--hand", choices=["right", "left"], help="投篮手；给了就不再交互提问")
    psu.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="环境 + 数据自检 + 档案检查").set_defaults(func=cmd_doctor)

    sub.add_parser("init", help="扫视频目录，生成/更新标签表").set_defaults(func=cmd_init)

    pa = sub.add_parser("add", help="登记一个标签")
    pa.add_argument("video")
    pa.add_argument("--tag")
    pa.add_argument("--date")
    pa.add_argument("--title")
    pa.add_argument("--height", type=float, help="身高 cm（默认取配置）")
    pa.add_argument("--hand", choices=["right", "left"])
    pa.add_argument("--distance", help="本段投篮距离，如 罚球线 / 三分线")
    pa.add_argument("--suffix", help="报告文件名后缀，消歧用（同一天多段视频）：如 第2段 → 10月6日_投篮分析报告_第2段.html")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="所有标签 + 阶段完成度")
    pl.add_argument("--deep-suf", help="深扫后缀，如 60（看 deep60_*.json 的完成度）")
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("probe", help="视频参数")
    pp.add_argument("video")
    pp.set_defaults(func=cmd_probe)

    pr = sub.add_parser("run", help="跑流水线")
    pr.add_argument("tag")
    pr.add_argument("--to", help="跑到哪个阶段为止（默认 report；full = 全部）")
    pr.add_argument("--from", dest="frm", help="从哪个阶段开始（默认 scan）")
    pr.add_argument("--force", action="store_true", help="已存在的阶段也重算")
    pr.add_argument("--strip", type=int, default=4, help="报告里每个视频展示几个球的分解")
    pr.add_argument("--deep-suf", help="深扫后缀：60 = 用 deep60_*.json 出 2 倍慢放")
    pr.add_argument("--deep-fps", type=int, help="覆盖深扫采样率")
    pr.add_argument("--which", help="top3（按评分）或 first3（按时间取前 3）")
    pr.add_argument("--pick", help="先看全量排名再指定出哪几个，如 16.4,65.4,78.0（自动用 <tag>_picked.json）")
    pr.add_argument("--layout", choices=["v", "h", "both"], help="配图/视频的版式")
    pr.add_argument("--order", help="合集拼接顺序，默认 1,2,3")
    pr.add_argument("--only", help="清单内只出第 N 个，如 2,3,4,5"
                                   "（配 --which first5 可做「第2~第5球」；N 是清单序号，不是评分名次）")
    pr.add_argument("--t", type=float, help="封面取哪一球的出手秒数（默认最早那球）")
    pr.add_argument("--suffix", help="报告文件名后缀（会写进标签表，消歧用）")
    pr.add_argument("--dry-run", action="store_true", help="只打印计划")
    pr.set_defaults(func=cmd_run)

    for name, helptext in (("report", "只出报告"), ("assets", "只出配图"),
                           ("video", "只出慢放视频"), ("cover", "只出封面")):
        ps = sub.add_parser(name, help=helptext)
        ps.add_argument("tag")
        ps.add_argument("--force", action="store_true")
        if name == "report":
            ps.add_argument("--strip", type=int, default=4)
        if name in ("video", "assets"):
            ps.add_argument("--layout", choices=["v", "h", "both"])
        if name == "video":
            ps.add_argument("--which")
            ps.add_argument("--pick", help="指定出哪几个，如 16.4,65.4,78.0")
            ps.add_argument("--deep-suf")
            ps.add_argument("--order")
            ps.add_argument("--only", help="清单内只出第 N 个，如 2,3,4,5"
                                           "（配 --which first5 可做「第2~第5球」；N 是清单序号，不是评分名次）")
        if name == "cover":
            ps.add_argument("--t", type=float)
        ps.set_defaults(func=cmd_stage)

    pd = sub.add_parser("diag", help="粗扫诊断：为什么没识别到出手事件")
    pd.add_argument("tag")
    pd.add_argument("--force", action="store_true", help="占位，保持接口一致")
    pd.set_defaults(func=cmd_stage)

    ps = sub.add_parser("shots", help="打印逐球指标表")
    ps.add_argument("tag")
    ps.set_defaults(func=cmd_shots)
    return p


def main():
    ap = build_parser()
    a = ap.parse_args()
    if not getattr(a, "cmd", None):
        ap.print_help()
        # 没配置就先建一份，省得用户手动写
        if not os.path.exists(CONFIG):
            save_cfg(DEFAULT_CFG)
            print("\n已生成默认配置 %s" % CONFIG)
        return 0
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
