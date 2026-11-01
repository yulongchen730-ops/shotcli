# -*- coding: utf-8 -*-
"""出手瞬间七连图：每个样本各取「出手那一帧」，叠骨架，拼成一张对比图
用法: python seven.py <tag>
"""
import os
import sys
import glob
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
from bbkit import save_jpeg
from deepcheck import EDGES, load_jpg
from montage import montage
import report as R


def draw(path, kp, W, H, out, lw=6.0):
    cg = load_jpg(path)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, W, H), cg)
    Quartz.CGContextSetLineWidth(ctx, lw)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for a, b, c in EDGES:
        p, q = kp.get(a), kp.get(b)
        if not (p and q):
            continue
        Quartz.CGContextSetRGBStrokeColor(ctx, c[0], c[1], c[2], 1.0)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextMoveToPoint(ctx, p[0], H - p[1])
        Quartz.CGContextAddLineToPoint(ctx, q[0], H - q[1])
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1.0)
    for k, v in kp.items():
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(v[0] - 5, H - v[1] - 5, 10, 10))
    save_jpeg(Quartz.CGBitmapContextCreateImage(ctx), out, 0.92)
    return out


def main(tag="d"):
    R.apply_tag(tag)          # 取 R.REPORT_DATE 拼产物名（日期前缀）
    # 用 report 里的出手时刻来决定每个样本取哪一帧（deep 原始数据里没有 phase 字段）
    shots = R.load_shots(tag)
    ms = [m for m in (R.shot_metrics(s) for s in shots) if m]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tiles = []
    files = sorted(glob.glob(os.path.join(root, "data", tag, "deep_*.json")),
                   key=lambda f: float(os.path.basename(f)[5:-5]))
    for n, f in enumerate(files, 1):
        d = json.load(open(f))
        seq, tc = d["seq"], d["meta"]["t_center"]
        m = min(ms, key=lambda x: abs(x["t_rel"] - tc), default=None)
        t_rel = m["t_rel"] if m else tc
        idx = next((k for k, r in enumerate(seq) if r["t"] >= t_rel), len(seq) // 2)
        r = seq[idx]
        out = os.path.join(root, "frames", tag, "_release_%02d.jpg" % n)
        draw(os.path.join(root, "frames", tag, r["img"]), r["kp"], r["cw"], r["ch"], out)
        tiles.append(out)
        print("第%d球 t=%.1fs 出手帧=第%d帧" % (n, tc, idx + 1), flush=True)
    out_path = os.path.join(root, "%s_出手瞬间七连图.jpg" % R.REPORT_DATE)
    montage(tiles, out_path, cols=4, cell=(470, 574))
    print("拼图:", out_path)
    return out_path


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "d")
