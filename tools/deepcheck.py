# -*- coding: utf-8 -*-
"""用报告里同一份深扫数据 + 同一套骨架连接关系，离线渲染画面，用于人工核验"""
import json, os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
from Foundation import NSURL
from bbkit import save_jpeg
from montage import montage

EDGES = [("neck_1_joint","head_joint",(0.31,0.63,1.0)),
 ("neck_1_joint","right_shoulder_1_joint",(1.0,0.42,0.42)),
 ("right_shoulder_1_joint","right_forearm_joint",(1.0,0.42,0.42)),
 ("right_forearm_joint","right_hand_joint",(1.0,0.42,0.42)),
 ("neck_1_joint","left_shoulder_1_joint",(0.13,0.83,0.93)),
 ("left_shoulder_1_joint","left_forearm_joint",(0.13,0.83,0.93)),
 ("left_forearm_joint","left_hand_joint",(0.13,0.83,0.93)),
 ("right_shoulder_1_joint","right_upLeg_joint",(1.0,0.56,0.67)),
 ("right_upLeg_joint","right_leg_joint",(1.0,0.56,0.67)),
 ("left_shoulder_1_joint","left_upLeg_joint",(1.0,0.56,0.67)),
 ("left_upLeg_joint","left_leg_joint",(1.0,0.56,0.67)),
 ("right_upLeg_joint","left_upLeg_joint",(1.0,0.56,0.67)),
 ("right_leg_joint","right_foot_joint",(0.78,0.57,0.92)),
 ("left_leg_joint","left_foot_joint",(0.78,0.57,0.92))]

def load_jpg(p):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(os.path.abspath(p)), None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)

def draw(path, kp, W, H, out):
    cg = load_jpg(path)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W*4, cs,
          Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0,0,W,H), cg)
    Quartz.CGContextSetLineWidth(ctx, 3.0)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for a, b, c in EDGES:
        p, q = kp.get(a), kp.get(b)
        if not (p and q):
            continue
        Quartz.CGContextSetRGBStrokeColor(ctx, c[0], c[1], c[2], 0.95)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextMoveToPoint(ctx, p[0], H-p[1])
        Quartz.CGContextAddLineToPoint(ctx, q[0], H-q[1])
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 0.95)
    for k, v in kp.items():
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(v[0]-3.2, H-v[1]-3.2, 6.4, 6.4))
    img = Quartz.CGBitmapContextCreateImage(ctx)
    save_jpeg(img, out, 0.9)
    return out

def run(tag="d"):
    picked = []
    for f in sorted(glob.glob("data/%s/deep_*.json" % tag)):
        d = json.load(open(f)); seq = d["seq"]
        name = os.path.basename(f)
        for idx in (0, 45, 70):          # 预备 / 出手附近 / 随挥
            if idx >= len(seq):
                continue
            r = seq[idx]
            out = "/tmp/dc_%s_%02d.jpg" % (name[5:-5], idx)
            draw("frames/%s/%s" % (tag, r["img"]), r["kp"], r["cw"], r["ch"], out)
            picked.append(out)
        print(name, "fps", d["meta"]["fps"], "帧数", len(seq), "→ 已渲染 0/45/70 帧")
    sheet = montage(picked, "/tmp/deepcheck_%s.jpg" % tag, cols=3, cell=(300, 366))
    print("拼图:", sheet)
    return sheet


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "d")
