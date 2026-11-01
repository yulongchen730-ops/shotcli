#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send2icloud.py —— 把产物放进 iCloud Drive，手机上「文件」App 直接取。

为什么用它而不用 AirDrop：这一步**纯文件操作，不需要任何 GUI 交互**，
agent 能独立完成（AirDrop 选设备必须人点，而且后台进程弹不出面板）。

用法：
    python3 tools/send2icloud.py --tag v --covers      # 9月20日 两版封面
    python3 tools/send2icloud.py --tag v --video       # 9月20日 合集视频
    python3 tools/send2icloud.py --tag v               # 全套（封面 + 视频）
    python3 tools/send2icloud.py 任意文件 [文件2 ...]   # 指定文件

    --dest 名称    云盘下的子目录名（默认「投篮精进」）
    --flat         不按日期建子目录，直接丢在 --dest 下
"""
import glob
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICLOUD = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
TAG_DATE = {}


def _load_tags():
    if TAG_DATE:
        return
    try:
        with open(os.path.join(ROOT, "data", "tags.json"), encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                if isinstance(v, dict) and v.get("date"):
                    TAG_DATE[k] = v["date"]
    except Exception:
        pass


def date_of(tag):
    _load_tags()
    return TAG_DATE.get(tag, tag)


def pick_covers(tag):
    d = date_of(tag)
    out = []
    for suf in ("竖版_1080x1440", "横版_1440x1080"):
        p = os.path.join(ROOT, "抖音封面", "%s_封面_%s.png" % (d, suf))
        if os.path.exists(p):
            out.append(p)
    return out


def pick_video(tag, kind="合集"):
    d = date_of(tag)
    fs = sorted(glob.glob(os.path.join(ROOT, "视频", "%s_%s_*.mp4" % (d, kind))))
    if not fs:                        # 没有合集就退而取单球
        fs = sorted(glob.glob(os.path.join(ROOT, "视频", "%s_第*球_*.mp4" % d)))
    return fs


def main():
    args = sys.argv[1:]
    tag = None
    dest_name = "投篮精进"
    flat = False
    want_covers = want_video = False
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tag":
            i += 1
            tag = args[i]
        elif a == "--dest":
            i += 1
            dest_name = args[i]
        elif a == "--flat":
            flat = True
        elif a == "--covers":
            want_covers = True
        elif a == "--video":
            want_video = True
        else:
            files.append(a)
        i += 1

    if tag and not (want_covers or want_video or files):
        want_covers = want_video = True          # 只给 --tag 默认发全套
    if want_covers:
        files += pick_covers(tag or "v")
    if want_video:
        files += pick_video(tag or "v")

    files = [os.path.abspath(f) for f in dict.fromkeys(files)]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("没有可发送的文件。用法见脚本头部注释。")
        return 1

    if not os.path.isdir(ICLOUD):
        print("✗ 找不到 iCloud Drive 目录：\n  %s" % ICLOUD)
        print("  → 系统设置 → Apple ID → iCloud → 打开「iCloud 云盘」后再试。")
        return 1

    dest = os.path.join(ICLOUD, dest_name)
    if not flat:
        dest = os.path.join(dest, date_of(tag)) if tag else dest
    os.makedirs(dest, exist_ok=True)

    print("目标：%s" % dest.replace(os.path.expanduser("~"), "~"))
    ok = 0
    for f in files:
        name = os.path.basename(f)
        tgt = os.path.join(dest, name)
        shutil.copy2(f, tgt)
        src_sz, dst_sz = os.path.getsize(f), os.path.getsize(tgt)
        flag = "✓" if src_sz == dst_sz else "✗"
        if src_sz == dst_sz:
            ok += 1
        print("  %s %-46s %6.2f MB" % (flag, name, dst_sz / 1048576.0))

    print("\n已放好 %d/%d 个（大小已逐一核对，不等 = 拷贝失败）。" % (ok, len(files)))
    print("\n手机上看：")
    print("  「文件」App → 浏览 → iCloud 云盘 → %s%s"
          % (dest_name, ("" if flat else "/" + date_of(tag)) if tag else ""))
    print("  视频长按可「存储到照片」，之后从相册发抖音。")
    print("\n上传由系统后台完成（同步守护 bird），手机端下拉刷新即可。")
    print("本机若开了「优化存储」，源文件稍后可能显示成 .icloud 占位符 —— 云端仍在，不影响手机取用。")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
