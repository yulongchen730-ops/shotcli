"""QA：把精细分析得到的骨架画到裁剪帧上，拼成 contact sheet 供肉眼校验"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
from bbkit import load_cg, save_jpeg

EDGES = [
    ("neck_1_joint", "head_joint", "blue"),
    ("neck_1_joint", "right_shoulder_1_joint", "red"),
    ("neck_1_joint", "left_shoulder_1_joint", "cyan"),
    ("right_shoulder_1_joint", "right_forearm_joint", "red"),
    ("right_forearm_joint", "right_hand_joint", "red"),
    ("left_shoulder_1_joint", "left_forearm_joint", "cyan"),
    ("left_forearm_joint", "left_hand_joint", "cyan"),
    ("right_shoulder_1_joint", "right_upLeg_joint", "pink"),
    ("left_shoulder_1_joint", "left_upLeg_joint", "pink"),
    ("right_upLeg_joint", "left_upLeg_joint", "pink"),
    ("right_upLeg_joint", "right_leg_joint", "green"),
    ("right_leg_joint", "right_foot_joint", "green"),
    ("left_upLeg_joint", "left_leg_joint", "purple"),
    ("left_leg_joint", "left_foot_joint", "purple"),
]
COLORS = {"blue": (0.30, 0.63, 1.0, 1), "yellow": (1, 0.82, 0.4, 1), "green": (0.25, 0.73, 0.31, 1),
          "pink": (1, 0.56, 0.67, 1), "purple": (0.78, 0.57, 0.92, 1),
          "red": (1, 0.2, 0.2, 1), "cyan": (0.2, 1, 0.9, 1)}


def draw(cg, kp):
    W, H = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, W, H), cg)

    def P(n):
        v = kp.get(n)
        return (v[0], v[1]) if v else None

    Quartz.CGContextSetLineWidth(ctx, 5)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for a, b, col in EDGES:
        pa, pb = P(a), P(b)
        if not (pa and pb):
            continue
        r, g, bl, al = COLORS[col]
        Quartz.CGContextSetRGBStrokeColor(ctx, r, g, bl, al)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextMoveToPoint(ctx, pa[0], H - pa[1])
        Quartz.CGContextAddLineToPoint(ctx, pb[0], H - pb[1])
        Quartz.CGContextStrokePath(ctx)
    for k, v in kp.items():
        x, y = v[0], H - v[1]
        Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 0.95)
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(x - 5, y - 5, 10, 10))
    return Quartz.CGBitmapContextCreateImage(ctx)


if __name__ == "__main__":
    shot_json = sys.argv[1]
    out_dir = sys.argv[2]
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    d = json.load(open(shot_json))
    frame_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(shot_json))), "..", "frames",
                             d["meta"]["tag"][:1])
    frame_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(shot_json))),
                                              "..", "frames", d["meta"]["tag"][:1]))
    os.makedirs(out_dir, exist_ok=True)
    outs = []
    for r in d["seq"][::step]:
        if not r.get("kp"):
            continue
        cg = load_cg(os.path.join(frame_dir, r["img"]))
        if cg is None:
            continue
        img = draw(cg, r["kp"])
        p = os.path.join(out_dir, "qa_" + r["img"])
        save_jpeg(img, p, 0.85)
        outs.append(p)
    print(" ".join(outs))
