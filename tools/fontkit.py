# -*- coding: utf-8 -*-
"""中文字体加载的唯一入口（配图 / 封面都用它）。

为什么单独抽一个模块 —— 2026-10-12 踩过的坑：
    macOS 的 /System/Library/Fonts/PingFang.ttc 是字体集合，里面同时装了
        0/3/6/9   = PingFang HK
        1/4/7/10  = PingFang TC
        2/5/8/11  = PingFang SC
    繁体字库（HK/TC）**不含简体专用字**。之前图里写
        index=4 if bold else 2
    加粗时选中的是 PingFang TC Medium，于是标题「已经稳定且在区间内」里的
    「稳」整个字渲染成空白 —— 不报错、不警告，只是没字形，肉眼看着像排版缺字。
    排查时先怀疑字符串编码，其实是字体选错了。

这里的策略：
    1. 候选表全部优先 PingFang SC（简体字齐全）；
    2. 加载后用 getmask() 真正校验「稳」的字形存在，缺字形的候选直接跳过；
    3. 全都不行才退到 load_default()。
这样以后换字体、换 macOS 版本都不会再出「静默缺字」。
"""
import os

from PIL import ImageFont

# 校验样本：在 HK/TC 里缺、在 SC 里有 —— 用它区分「简体字库」和「繁体字库」
_PROBE = "稳"

# (路径, 加粗时的索引, 常规时的索引)
_CANDS = [
    ("/System/Library/Fonts/PingFang.ttc", 5, 2),                 # PingFang SC Medium / SC Regular
    ("/System/Library/Fonts/PingFang.ttc", 8, 2),                 # PingFang SC Semibold / SC Regular
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0, 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0, 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, 0),
]

_CACHE = {}


def _has_glyph(f, probe=_PROBE):
    """字体里是否真有这个字的字形（缺字形时 mask 是空的）。"""
    try:
        return f.getmask(probe).getbbox() is not None
    except Exception:
        return False


def load(size, bold=False):
    """按字号取字体，带缓存；保证返回的字体能渲染简体字。"""
    size = int(round(size))
    key = (size, bool(bold))
    if key in _CACHE:
        return _CACHE[key]
    f = None
    for path, idx_b, idx_r in _CANDS:
        if not os.path.exists(path):
            continue
        try:
            cand = ImageFont.truetype(path, size, index=(idx_b if bold else idx_r))
        except Exception:
            continue
        if _has_glyph(cand):
            f = cand
            break
    if f is None:
        f = ImageFont.load_default()
    _CACHE[key] = f
    return f


def name(size=20, bold=False):
    """给 doctor / 自检用：报出实际选中的字体名。"""
    f = load(size, bold)
    try:
        return "%s %s" % tuple(f.getname())
    except Exception:
        return type(f).__name__
