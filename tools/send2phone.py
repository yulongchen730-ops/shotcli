#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send2phone.py —— 弹出系统 AirDrop 面板，把文件投到 iPhone。

为什么不能全自动：AirDrop 选设备这一步必须人来点（系统安全设计），
任何 App 都一样。另外这个脚本必须在你自己的桌面会话里跑 ——
从后台/远程进程调用是弹不出面板的（macOS 会把 GUI 呈现挡掉）。

用法：
    python3 tools/send2phone.py                     # 交互菜单，选要发什么
    python3 tools/send2phone.py 文件1 [文件2 ...]    # 直接发这些文件
    python3 tools/send2phone.py --tag v --covers     # 9月20日 两版封面
    python3 tools/send2phone.py --tag v --video      # 9月20日 合集视频

    --mail          改用「邮件」面板（需 Mail.app 已配账号）
    --timeout N     面板最长挂多久（秒），默认 300
"""
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def pick_video(tag):
    d = date_of(tag)
    return sorted(glob.glob(os.path.join(ROOT, "视频", "%s_合集_*.mp4" % d)))


def newest_covers():
    out = []
    for suf in ("竖版_1080x1440", "横版_1440x1080"):
        fs = sorted(glob.glob(os.path.join(ROOT, "抖音封面", "*_封面_%s.png" % suf)))
        if fs:
            out.append(fs[-1])
    return out


def newest_videos():
    fs = sorted(glob.glob(os.path.join(ROOT, "视频", "*_合集_*.mp4")),
                key=os.path.getmtime)
    return fs[-1:] if fs else []


def menu():
    """无参数时的交互菜单。返回 (文件列表, 是否用邮件)。"""
    items = [
        ("9月20日 两版封面", lambda: pick_covers("v")),
        ("9月20日 合集视频", lambda: pick_video("v")),
        ("9月20日 封面 + 视频（全套）", lambda: pick_covers("v") + pick_video("v")),
        ("最新一期封面", newest_covers),
        ("最新一期合集视频", newest_videos),
    ]
    print("\n要发什么到手机？\n")
    for i, (name, _) in enumerate(items, 1):
        print("  %d) %s" % (i, name))
    print("  0) 退出\n")
    try:
        s = input("输入序号（默认 1）：").strip()
    except EOFError:
        s = ""
    if s == "0":
        return [], False
    idx = int(s) if s.isdigit() and 1 <= int(s) <= len(items) else 1
    files = items[idx - 1][1]()
    if not files:
        print("✗ 该项没有找到文件。")
        return [], False
    return files, False


def run(files, use_mail=False, timeout=300):
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                        NSSharingService)
    from Foundation import NSObject, NSTimer, NSURL

    svc_name = ("com.apple.share.Mail.compose" if use_mail
                else "com.apple.share.AirDrop.send")
    svc = NSSharingService.sharingServiceNamed_(svc_name)
    if svc is None:
        print("✗ 系统没提供分享服务 %s" % svc_name)
        return 1

    print("\n通道：%s" % ("邮件" if use_mail else "隔空投送 (AirDrop)"))
    total = 0.0
    for f in files:
        mb = os.path.getsize(f) / 1048576.0
        total += mb
        print("  · %-44s %6.2f MB" % (os.path.basename(f), mb))
    print("  合计 %.2f MB" % total)
    print("\n面板马上弹出 → 在屏幕上点一下你的 iPhone。\n")

    t0 = time.time()

    class Delegate(NSObject):
        def sharingService_didShareItems_(self, s, items):
            print("✓ 已投送：手机上的「文件」App 里接收")
            NSApplication.sharedApplication().stop_(None)

        def sharingService_didFailToShareItems_error_(self, s, items, err):
            print("✗ 投送失败：%s" % err)
            NSApplication.sharedApplication().stop_(None)

        def tick_(self, _):
            if time.time() - t0 > timeout:
                print("（等了 %ds 没动作，面板关闭。想重发就再跑一次）" % timeout)
                NSApplication.sharedApplication().stop_(None)

    d = Delegate.alloc().init()
    svc.setDelegate_(d)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, d, "tick:", None, True)
    svc.performWithItems_([NSURL.fileURLWithPath_(f) for f in files])
    app.run()
    return 0


def main():
    args = sys.argv[1:]
    timeout, use_mail, tag, files = 300, False, None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--timeout":
            i += 1
            timeout = int(args[i])
        elif a == "--mail":
            use_mail = True
        elif a == "--tag":
            i += 1
            tag = args[i]
        elif a == "--covers":
            files += pick_covers(tag or "v")
        elif a == "--video":
            files += pick_video(tag or "v")
        else:
            files.append(a)
        i += 1

    if not files:
        files, use_mail = menu()
    files = [os.path.abspath(f) for f in dict.fromkeys(files)]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("没有可发送的文件。")
        return 1
    return run(files, use_mail, timeout)


if __name__ == "__main__":
    sys.exit(main())
